"""Merchant events (submit / update / upload) and the intake pass:
document extraction, validation, doc-vs-form checks, duplicate and
document-reuse detection, AI category check, and readiness of each check."""
import base64
import hashlib
import secrets
import uuid
from typing import Optional

from .. import extraction
from ..privacy import fingerprint
from ..providers import HIGH_RISK_CATEGORIES
from ..states import S
from .base import (CHECK_INPUTS, CHECK_TYPES, FORM_FIELDS, UPDATABLE_STATES, ActionError, j, normalize_form,
                   transactional, uj)

ALLOWED_MIME = {"image/png": b"\x89PNG", "image/jpeg": b"\xff\xd8\xff", "image/webp": b"RIFF",
                "application/pdf": b"%PDF"}


class IntakeMixin:
    # ------------------------------------------------------------ merchant events
    @transactional
    def submit(self, form: dict, documents: list, actor="merchant", channel="api"):
        """Returns (app_id, access_token). The token is shown once; only its hash is stored."""
        self._check_documents(documents)
        f = normalize_form(form)
        app_id = "APP-" + uuid.uuid4().hex[:8].upper()
        token = secrets.token_urlsafe(18)
        now = self._now()
        self._x("""INSERT INTO applications(id,created_at,updated_at,state,state_entered_at,access_token_hash,channel)
                   VALUES (?,?,?,?,?,?,?)""",
                (app_id, now, now, S.SUBMITTED, now, hashlib.sha256(token.encode()).hexdigest(), channel))
        self._store_form(app_id, f)
        self._x("INSERT INTO state_history(app_id,from_state,to_state,at,actor,reason) VALUES (?,?,?,?,?,?)",
                (app_id, None, S.SUBMITTED, now, actor, "application submitted"))
        self._audit(app_id, actor, "APPLICATION_SUBMITTED", channel=channel,
                    documents=[d.get("doc_type") for d in documents])
        for t in CHECK_TYPES:
            self._x("INSERT INTO checks(app_id,check_type,status) VALUES (?,?,?)", (app_id, t, "BLOCKED"))
        for d in documents:
            self._add_document(app_id, d, actor)
        self._enqueue_categorize(app_id)
        self._after_intake(app_id)
        return app_id, token

    @transactional
    def update(self, app_id, fields: Optional[dict] = None, documents: Optional[list] = None, actor="merchant"):
        app = self._app(app_id)
        if app["state"] == S.ACTIVE:
            raise ActionError("Merchant is already live. Changes such as a new bank account go through a separate "
                              "change-request flow with settlements on hold (out of scope for this prototype).")
        if app["state"] not in UPDATABLE_STATES:
            raise ActionError(f"Application cannot be changed while {app['state']}")
        if fields is not None and not isinstance(fields, dict):
            raise ActionError("fields must be an object")
        if documents is not None:
            self._check_documents(documents)
        fields = {k: v for k, v in (fields or {}).items() if k in FORM_FIELDS}
        if fields:
            old = self._form(app)
            new = normalize_form(fields, base=old)
            changed = [k for k in FORM_FIELDS if new.get(k) != old.get(k)]
            if changed:
                bank_before = self._check(app_id, "BANK")["status"]
                self._store_form(app_id, new)
                self._audit(app_id, actor, "FIELDS_UPDATED", fields=changed)
                bank = [k for k in changed if k in ("bank_account", "ifsc", "account_holder")]
                if bank and bank_before in ("PASSED", "IN_FLIGHT", "RETRY_WAIT", "DISPATCHING", "ADJUDICATING"):
                    self._audit(app_id, actor, "BANK_DETAILS_CHANGED_AFTER_VERIFICATION", fields=bank,
                                previous_bank_check=bank_before)
                if set(changed) & {"legal_name", "trade_name", "category", "business_description"}:
                    self._enqueue_categorize(app_id)
        for d in documents or []:
            self._x("UPDATE documents SET superseded=1 WHERE app_id=? AND doc_type=? AND superseded=0",
                    (app_id, d["doc_type"]))
            self._add_document(app_id, d, actor)
        if self._open_missing(app_id, "reviewer"):
            self._resolve_missing(app_id, "reviewer", "merchant responded")
            self._audit(app_id, actor, "INFO_RECEIVED", key="reviewer request")
        self._invalidate_pending_approval(app_id, "merchant changed the application")
        self._after_intake(app_id)

    @staticmethod
    def _check_documents(documents):
        if not isinstance(documents, list):
            raise ActionError("documents must be a list")
        if len(documents) > 10:
            raise ActionError("At most 10 documents per request")
        for d in documents:
            if not isinstance(d, dict) or not isinstance(d.get("doc_type"), str) or d["doc_type"] not in extraction.DOC_LABELS:
                raise ActionError("Each document needs a doc_type of PAN_CARD, GST_CERTIFICATE or CANCELLED_CHEQUE")

    def _store_form(self, app_id, f):
        sec = self.cfg.fingerprint_secret
        fp = lambda v: fingerprint(sec, v) if v else None  # noqa: E731
        bank_key = f"{f.get('bank_account')}|{f.get('ifsc')}" if f.get("bank_account") else None
        self._x(f"""UPDATE applications SET {', '.join(k + '=?' for k in FORM_FIELDS)},
                    pan_fp=?, gstin_fp=?, bank_fp=?, contact_fp=?, updated_at=? WHERE id=?""",
                [f.get(k) if k != "gst_registered" else int(bool(f.get(k))) for k in FORM_FIELDS]
                + [fp(f.get("pan")), fp(f.get("gstin")), fp(bank_key),
                   fp(f"{f.get('email')}|{f.get('phone')}") if f.get("email") or f.get("phone") else None,
                   self._now(), app_id])

    # ------------------------------------------------------------ documents
    def _add_document(self, app_id, d, actor="merchant"):
        if not isinstance(d, dict):
            raise ActionError("Each document must be an object")
        dt = d.get("doc_type")
        if not isinstance(dt, str) or dt not in extraction.DOC_LABELS:
            raise ActionError(f"Unsupported document type {dt}")
        now = self._now()
        if d.get("file_b64"):
            if not isinstance(d["file_b64"], str) or not isinstance(d.get("mime") or "", str):
                raise ActionError("file_b64 and mime must be strings")
            mime = (d.get("mime") or "").lower()
            if mime not in ALLOWED_MIME:
                raise ActionError("Upload a PNG, JPEG, WebP image or a PDF")
            try:
                data = base64.b64decode(d["file_b64"].split(",")[-1], validate=True)
            except (ValueError, TypeError):
                raise ActionError("File could not be decoded") from None
            if len(data) > self.cfg.max_upload_bytes:
                raise ActionError(f"File is larger than {self.cfg.max_upload_bytes // (1024 * 1024)} MB")
            if not data.startswith(ALLOWED_MIME[mime]):
                raise ActionError("File content does not match its type")
            sha = hashlib.sha256(data).hexdigest()
            status = "EXTRACTING" if self.gw.enabled else "MANUAL_REQUIRED"
            cur = self._x("""INSERT INTO documents(app_id,doc_type,filename,mime,size,sha256,blob,sidecar_json,status,
                             uploaded_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                          (app_id, dt, d.get("filename") or f"{dt.lower()}", mime, len(data), sha, data,
                           j(d.get("sidecar")) if isinstance(d.get("sidecar"), dict) else None, status, now))
            doc_id = cur.lastrowid
            if status == "EXTRACTING":
                self._enqueue("extract_document", app_id, str(doc_id), {"doc_id": doc_id})
            self._audit(app_id, actor, "DOCUMENT_UPLOADED", doc_type=dt, mime=mime, size=len(data),
                        extraction="queued for AI" if status == "EXTRACTING" else "manual (AI disabled)")
        else:
            text = d.get("text") or d.get("content") or ""
            if not isinstance(text, str):
                raise ActionError("Document text must be a string")
            text = text[:20000]
            q = d.get("quality")
            if q is not None:
                try:
                    q = max(0.0, min(1.0, float(q)))
                except (TypeError, ValueError):
                    raise ActionError("quality must be a number between 0 and 1") from None
            ex = extraction.extract_document({"doc_type": dt, "text": text, "quality": q})
            ex["method"] = "ocr-text"
            sha = hashlib.sha256(text.encode()).hexdigest()
            self._x("""INSERT INTO documents(app_id,doc_type,filename,mime,size,sha256,content,quality,status,
                       extraction_json,uploaded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (app_id, dt, d.get("filename") or dt.lower() + ".txt", "text/plain", len(text), sha, text,
                     ex["quality"], "EXTRACTED", j(ex), now))
            self._audit(app_id, actor, "DOCUMENT_UPLOADED", doc_type=dt, mime="text/plain (OCR payload)",
                        quality=ex["quality"])
            return

    def _after_intake(self, app_id):
        self._intake(app_id)
        self._dispatch_due(app_id)
        self._evaluate(app_id, "system")

    # ------------------------------------------------------------ intake
    def _intake(self, app_id):
        app = self._app(app_id)
        form = self._form(app)
        docs = []
        for d in self._q("SELECT id, doc_type, filename, status, extraction_json FROM documents WHERE app_id=? "
                         "AND superseded=0", (app_id,)):
            d["extraction"] = uj(d.pop("extraction_json"))
            docs.append(d)
        res = extraction.run_intake(form, docs, self.cfg)
        self._x("UPDATE applications SET extracted_json=? WHERE id=?",
                (j({"fields": res["fields"], "documents": res["doc_quality"]}), app_id))
        self._audit(app_id, "system", "INTAKE_EVALUATED",
                    documents={d["doc_type"]: d["status"] for d in res["doc_quality"]},
                    missing=len(res["missing"]), conflicts=len(res["conflicts"]), verify=len(res["verify"]))

        self._sync_missing(app_id, "intake", {f"{m['code']}:{m['field']}": m for m in res["missing"]})

        exc = {}
        for c in res["conflicts"]:
            exc[f"DOC_FORM_CONFLICT:{c['doc_type']}.{c['field']}"] = {
                "code": "DOC_FORM_CONFLICT", "severity": "HIGH", "message": c["message"],
                "detail": {"field": c["field"], "doc_type": c["doc_type"], "doc_id": c["doc_id"]}}
        for v in res["verify"]:
            exc[f"FIELD_VERIFICATION_REQUIRED:{v['doc_id']}"] = {
                "code": "FIELD_VERIFICATION_REQUIRED", "severity": "MEDIUM",
                "message": f"Low-confidence read of {', '.join(extraction.FIELD_LABELS[f] for f in v['fields'])} on the "
                           f"{extraction.DOC_LABELS[v['doc_type']]}. Check the image and confirm the values.",
                "detail": v}
        for m in res["manual"]:
            exc[f"MANUAL_EXTRACTION_REQUIRED:{m['doc_id']}"] = {
                "code": "MANUAL_EXTRACTION_REQUIRED", "severity": "MEDIUM",
                "message": f"The {extraction.DOC_LABELS[m['doc_type']]} could not be read automatically. "
                           f"Key in its fields from the image to continue the {m['check']} check.",
                "detail": m}
        for t in res["tamper"]:
            exc[f"DOCUMENT_TAMPER_SUSPECTED:{t['doc_id']}"] = {
                "code": "DOCUMENT_TAMPER_SUSPECTED", "severity": "HIGH",
                "message": f"{extraction.DOC_LABELS[t['doc_type']]}: " + "; ".join(t["signals"] or ["embedded instructions"])
                           + (" (document contains text addressed to the reviewer/AI)" if t["injection"] else ""),
                "detail": t}
        for d in self._find_duplicates(app_id):
            exc[d["key"]] = d["exception"]
        cat = uj(app["ai_category_json"])
        if cat and cat.get("restricted") and form.get("category") not in HIGH_RISK_CATEGORIES:
            exc["CATEGORY_MISMATCH"] = {
                "code": "CATEGORY_MISMATCH", "severity": "HIGH",
                "message": f"Business looks like '{cat['category']}' (MCC {cat.get('mcc')}, restricted) but the "
                           f"applicant selected '{form.get('category')}'. {cat.get('rationale', '')}".strip(),
                "detail": {k: cat.get(k) for k in ("category", "mcc", "confidence", "method")}}
        self._sync_exceptions(app_id, "intake", exc)

        blocked = set(res["pending"])
        for m in self._open_missing(app_id, "intake"):
            blocked.update(uj(m["blocks_json"], []))
        blocked.update(m["check"] for m in res["manual"])
        for t in CHECK_TYPES:
            c = self._check(app_id, t)
            if t == "GST" and not form["gst_registered"]:
                if c["status"] != "NOT_REQUIRED":
                    self._reset_check(app_id, t, "NOT_REQUIRED", None, "merchant not GST-registered")
                continue
            fp = fingerprint(self.cfg.fingerprint_secret, {k: form.get(k) for k in CHECK_INPUTS[t]})
            if c["fingerprint"] != fp:
                first = c["fingerprint"] is None and c["status"] == "BLOCKED"
                self._reset_check(app_id, t, "BLOCKED" if t in blocked else "PENDING", fp,
                                  "initial" if first else "verified inputs changed")
            elif c["status"] == "BLOCKED" and t not in blocked:
                self._x("UPDATE checks SET status='PENDING' WHERE app_id=? AND check_type=?", (app_id, t))
                self._audit(app_id, "system", "CHECK_UNBLOCKED", check=t)

    def _find_duplicates(self, app_id):
        app = self._app(app_id)
        out = []
        others = self._q("""SELECT id, legal_name, state, pan_fp, gstin_fp, bank_fp, contact_fp FROM applications
                            WHERE id<>? AND state<>?""", (app_id, S.REJECTED))
        for o in others:
            matched = [lbl for lbl, k in (("PAN", "pan_fp"), ("GSTIN", "gstin_fp"), ("bank account", "bank_fp"))
                       if app[k] and app[k] == o[k]]
            fuzzy = (app["contact_fp"] == o["contact_fp"] and
                     extraction.name_similarity(app["legal_name"], o["legal_name"]) >= 0.85)
            if matched or fuzzy:
                why = ", ".join(matched) if matched else "same contact details and similar name"
                out.append({"key": f"DUPLICATE:{o['id']}", "exception": {
                    "code": "DUPLICATE_APPLICATION" if matched else "POSSIBLE_DUPLICATE",
                    "severity": "HIGH" if matched else "MEDIUM",
                    "message": f"Matches existing application {o['id']} ({o['legal_name']}, {o['state']}) on: {why}.",
                    "detail": {"other_app": o["id"], "matched_on": matched or ["contact+name"]}}})
        # The same document file submitted by a different applicant is a classic fraud signal.
        for d in self._q("""SELECT d.doc_type, d.sha256, o.app_id other, a.legal_name, a.state FROM documents d
                            JOIN documents o ON o.sha256=d.sha256 AND o.app_id<>d.app_id
                            JOIN applications a ON a.id=o.app_id
                            WHERE d.app_id=? AND d.superseded=0 AND a.state<>? GROUP BY d.id, o.app_id""",
                         (app_id, S.REJECTED)):
            out.append({"key": f"DOCUMENT_REUSED:{d['doc_type']}:{d['other']}", "exception": {
                "code": "DOCUMENT_REUSED", "severity": "HIGH",
                "message": f"The {extraction.DOC_LABELS[d['doc_type']]} file is byte-identical to one submitted by "
                           f"{d['other']} ({d['legal_name']}, {d['state']}).",
                "detail": {"other_app": d["other"], "doc_type": d["doc_type"]}}})
        return out

    def _enqueue_categorize(self, app_id):
        self._x("UPDATE jobs SET status='CANCELLED' WHERE app_id=? AND kind='categorize' AND status='QUEUED'",
                (app_id,))
        self._enqueue("categorize", app_id, "category", {})
