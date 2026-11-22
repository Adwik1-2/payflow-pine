"""Durable background jobs for slow AI work (document extraction, business
categorisation, name adjudication, merchant message drafting).

The engine lock is NOT held while a model is being called:
    claim_jobs()  -> lock, mark RUNNING, copy inputs, unlock
    _execute()    -> call the LLM (seconds), no lock
    complete_job()-> lock, apply result or fallback, evaluate, unlock
"""
from .. import ai
from ..llm import LLMUnavailable
from ..states import EVALUABLE
from .base import j, transactional, uj

STALE_RUNNING_S = 300


class JobsMixin:
    def _enqueue(self, kind, app_id, ref, payload, gating=True, delay=0.0):
        self._x("""INSERT INTO jobs(kind,app_id,ref,payload_json,status,attempts,run_after,created_at,gating)
                   VALUES (?,?,?,?,'QUEUED',0,?,?,?)""",
                (kind, app_id, ref, j(payload), self._now() + delay, self._now(), int(gating)))

    @transactional
    def claim_jobs(self, limit=4):
        now = self._now()
        rows = self._q("""SELECT * FROM jobs WHERE (status='QUEUED' AND run_after<=?)
                          OR (status='RUNNING' AND started_at<=?) ORDER BY id LIMIT ?""",
                       (now, now - STALE_RUNNING_S, limit))
        out = []
        for jb in rows:
            self._x("UPDATE jobs SET status='RUNNING', started_at=?, attempts=attempts+1 WHERE id=?", (now, jb["id"]))
            jb["attempts"] += 1
            out.append((jb, self._job_inputs(jb)))
        return out

    def _job_inputs(self, jb):
        p = uj(jb["payload_json"], {})
        app = self._app(jb["app_id"])
        if jb["kind"] == "extract_document":
            d = self._one("SELECT * FROM documents WHERE id=?", (p["doc_id"],))
            return {"data": d["blob"], "mime": d["mime"], "doc_type": d["doc_type"], "filename": d["filename"],
                    "sidecar": uj(d["sidecar_json"]), "superseded": d["superseded"]}
        if jb["kind"] == "categorize":
            return {"name": app["legal_name"], "description": app["business_description"] or "",
                    "declared": app["category"]}
        if jb["kind"] == "adjudicate_bank_name":
            return p
        if jb["kind"] == "notify_merchant":
            return {"name": app["legal_name"], **p, "items": [m["message"] for m in self._open_missing(app["id"])]}
        return p

    def _execute(self, jb, inp):
        k, app_id = jb["kind"], jb["app_id"]
        if k == "extract_document":
            if inp["superseded"]:
                return {"skipped": True}
            return ai.extract_document(self.gw, inp["data"], inp["mime"], inp["doc_type"], inp["filename"],
                                       inp["sidecar"], app_id=app_id)
        if k == "categorize":
            return ai.categorize_business(self.gw, inp["name"], inp["description"], inp["declared"], app_id=app_id)
        if k == "adjudicate_bank_name":
            return ai.adjudicate_names(self.gw, inp["legal_name"], inp["beneficiary"],
                                       "bank penny-drop beneficiary vs registered business name", app_id=app_id)
        if k == "notify_merchant":
            if not inp["items"]:
                return {"skipped": True}
            return ai.draft_merchant_message(self.gw, inp["name"], inp["items"], inp["kind"], app_id=app_id)
        raise ValueError(f"unknown job kind {k}")

    def run_due_jobs(self, limit=4):
        done = 0
        for jb, inp in self.claim_jobs(limit):
            try:
                result, err = self._execute(jb, inp), None
            except Exception as e:  # noqa: BLE001 - every failure is handled by retry/fallback
                result, err = None, e
            self.complete_job(jb["id"], result, err)
            done += 1
        return done

    @transactional
    def complete_job(self, job_id, result, error):
        jb = self._one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if jb["status"] != "RUNNING":
            return
        now = self._now()
        if error is None:
            self._x("UPDATE jobs SET status='DONE', finished_at=?, error=NULL WHERE id=?", (now, job_id))
            self._on_job_result(jb, result)
        else:
            permanent = isinstance(error, LLMUnavailable)
            if not permanent and jb["attempts"] < self.cfg.job_max_attempts:
                self._x("UPDATE jobs SET status='QUEUED', run_after=?, error=? WHERE id=?",
                        (now + self._backoff(jb["attempts"]), str(error)[:300], job_id))
                return
            self._x("UPDATE jobs SET status='FAILED', finished_at=?, error=? WHERE id=?", (now, str(error)[:300], job_id))
            self._audit(jb["app_id"], "system", "AI_FALLBACK_USED", job=jb["kind"], error=str(error)[:200])
            self._on_job_fallback(jb, error)
        app = self._app(jb["app_id"])
        if jb["kind"] in ("extract_document", "categorize"):
            if app["state"] in ("SUBMITTED", "VERIFYING", "NEEDS_INFO", "MANUAL_REVIEW"):
                self._after_intake(jb["app_id"])
        else:
            self._evaluate(jb["app_id"], "system")

    # ------------------------------------------------------------ result handlers
    def _on_job_result(self, jb, r):
        app_id, k = jb["app_id"], jb["kind"]
        p = uj(jb["payload_json"], {})
        if k == "extract_document":
            if r.get("skipped"):
                return
            self._x("UPDATE documents SET status='EXTRACTED', extraction_json=?, quality=? WHERE id=? AND superseded=0",
                    (j(r), r["quality"], p["doc_id"]))
            self._audit(app_id, "system", "DOCUMENT_EXTRACTED_BY_AI", doc_id=p["doc_id"], model=r.get("model"),
                        detected_type=r.get("document_type_detected"), legibility=r["quality"],
                        fields={f: v["confidence"] for f, v in r["fields"].items()},
                        tamper_signals=len(r.get("tamper_signals") or []), injection=r.get("injection"))
        elif k == "categorize":
            self._x("UPDATE applications SET ai_category_json=? WHERE id=?", (j(r), app_id))
            self._audit(app_id, "system", "BUSINESS_CATEGORISED", category=r["category"], mcc=r["mcc"],
                        restricted=r["restricted"], confidence=r["confidence"], method=r.get("method"))
        elif k == "adjudicate_bank_name":
            c = self._check(app_id, "BANK")
            if self._app(app_id)["state"] not in EVALUABLE:
                return
            if c["status"] != "ADJUDICATING" or c["fingerprint"] != p["fingerprint"]:
                return  # inputs changed meanwhile; the new verification decides
            res = {**uj(c["result_json"], {}), "name_adjudication": {k2: r[k2] for k2 in ("same_entity", "confidence", "rationale", "model")}}
            if r["same_entity"] and r["confidence"] >= self.cfg.ai_name_min_confidence:
                self._audit(app_id, "system", "NAME_MATCH_CONFIRMED_BY_AI", similarity=p["similarity"],
                            confidence=r["confidence"], rationale=r["rationale"])
                self._complete_check(app_id, "BANK", "PASSED", None, None, None, None, res)
            else:
                verdict = "thinks it is the same business" if r["same_entity"] else "thinks it is a different business"
                self._bank_name_mismatch(app_id, p, res, f"AI {verdict} ({r['confidence']:.0%} sure; "
                                         f"{self.cfg.ai_name_min_confidence:.0%} is needed to accept it automatically): {r['rationale']}")
        elif k == "notify_merchant":
            if r.get("skipped") or self._app(app_id)["state"] != "NEEDS_INFO":
                return
            self._notify(app_id, p["kind"], r["subject"], r["body_en"], r["body_hi"], f"llm:{r.get('model')}")

    def _on_job_fallback(self, jb, error):
        app_id, k = jb["app_id"], jb["kind"]
        p = uj(jb["payload_json"], {})
        if k == "extract_document":
            self._x("UPDATE documents SET status='MANUAL_REQUIRED' WHERE id=? AND superseded=0", (p["doc_id"],))
        elif k == "categorize":
            app = self._app(app_id)
            r = ai.categorize_rules(app["legal_name"], app["business_description"] or "", app["category"])
            self._x("UPDATE applications SET ai_category_json=? WHERE id=?", (j(r), app_id))
        elif k == "adjudicate_bank_name":
            c = self._check(app_id, "BANK")
            if (self._app(app_id)["state"] in EVALUABLE and c["status"] == "ADJUDICATING"
                    and c["fingerprint"] == p["fingerprint"]):
                self._bank_name_mismatch(app_id, p, uj(c["result_json"], {}), "automatic name check unavailable")
        elif k == "notify_merchant":
            items = [m["message"] for m in self._open_missing(app_id)]
            if not items or self._app(app_id)["state"] != "NEEDS_INFO":
                return
            t = ai.message_template(self._app(app_id)["legal_name"], items, p["kind"])
            self._notify(app_id, p["kind"], t["subject"], t["body_en"], t["body_hi"], "template")

    def _bank_name_mismatch(self, app_id, p, res, note):
        self._complete_check(app_id, "BANK", "FAILED", "BANK_NAME_MISMATCH", "SUBSTANTIVE", "HIGH",
                             f"Bank beneficiary name '{p['beneficiary']}' does not match the business "
                             f"(similarity {p['similarity']:.2f}; {note}). Settlements cannot go to a third-party "
                             f"account.", res)
