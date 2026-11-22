"""Read models for the operator console and the merchant portal, plus
operational metrics. Everything returned here is masked."""
import hashlib
import hmac

from ..privacy import mask
from ..states import S
from .base import CHECK_TYPES, FORM_FIELDS, RUNNING, NotFound, transactional, uj

MERCHANT_STATUS = {
    S.SUBMITTED: ("We're checking your details", "This usually takes a minute."),
    S.VERIFYING: ("We're checking your details", "We are verifying your PAN, GST and bank account."),
    S.NEEDS_INFO: ("Action needed", "We need a few things from you before we can continue."),
    S.MANUAL_REVIEW: ("Under review", "Our team is reviewing your application. No action needed from you right now."),
    S.APPROVED: ("Approved", "Setting up your merchant account."),
    S.PROVISIONING: ("Approved", "Setting up your merchant account."),
    S.PROVISIONING_FAILED: ("Approved", "Setting up your merchant account is taking longer than usual."),
    S.ACTIVE: ("You're live", "Your merchant account is ready to accept payments."),
    S.REJECTED: ("Not approved", "We are unable to approve this application under our onboarding policy."),
}
MERCHANT_ACTION = {
    "MISSING_DOCUMENT": "upload", "DOCUMENT_UNREADABLE": "upload", "WRONG_DOCUMENT_TYPE": "upload",
    "FIELD_NOT_FOUND": "upload", "PAN_NOT_FOUND": "correct:pan", "GSTIN_NOT_FOUND": "correct:gstin",
    "BANK_ACCOUNT_INVALID": "correct:bank", "MISSING_FIELD": "correct", "INVALID_FORMAT": "correct",
    "INCONSISTENT": "correct", "REVIEWER_REQUEST": "reply",
}


class ViewsMixin:
    @transactional
    def list_applications(self):
        now = self._now()
        rows = self._q("""SELECT a.id, a.legal_name, a.state, a.state_entered_at, a.created_at, a.updated_at,
                          a.merchant_id, a.category, a.assigned_to, a.channel,
                          (SELECT COUNT(*) FROM exceptions e WHERE e.app_id=a.id AND e.resolved_at IS NULL) open_exceptions,
                          (SELECT COUNT(*) FROM missing_items m WHERE m.app_id=a.id AND m.resolved_at IS NULL) open_missing,
                          (SELECT COUNT(*) FROM approvals p WHERE p.app_id=a.id AND p.status='PENDING') pending_approval
                          FROM applications a ORDER BY a.created_at DESC""")
        gates = {}
        for c in self._q("SELECT app_id, check_type, status FROM checks"):
            gates.setdefault(c["app_id"], {})[c["check_type"]] = c["status"]
        for r in rows:
            r["checks"] = gates.get(r["id"], {})
            r["time_in_state_s"] = round(now - r["state_entered_at"], 1)
            r["sla_breached"] = r["state"] in (S.MANUAL_REVIEW, S.PROVISIONING_FAILED) and \
                r["time_in_state_s"] > self.cfg.review_sla_s
        return rows

    @transactional
    def get_application(self, app_id):
        app = self._app(app_id)
        now = self._now()
        form = {k: mask(k, app[k]) for k in FORM_FIELDS}
        form["gst_registered"] = bool(app["gst_registered"])
        checks = []
        for t in CHECK_TYPES:
            c = self._check(app_id, t)
            reqs = self._q("""SELECT request_id, attempt, sent_at, status, resolved_at FROM provider_requests
                              WHERE app_id=? AND check_type=? ORDER BY sent_at""", (app_id, t))
            latency = round(c["completed_at"] - c["started_at"], 1) if c["completed_at"] and c["started_at"] else None
            checks.append({"check_type": t, "status": c["status"], "attempts": c["attempts"],
                           "reason_code": c["reason_code"], "category": c["category"],
                           "result": uj(c["result_json"]), "latency_s": latency,
                           "deadline_in_s": round(c["deadline_at"] - now, 1) if c["status"] == "IN_FLIGHT" else None,
                           "retry_in_s": round(c["next_attempt_at"] - now, 1) if c["status"] == "RETRY_WAIT" else None,
                           "requests": reqs})
        hist = self._q("SELECT from_state, to_state, at, actor, reason FROM state_history WHERE app_id=? ORDER BY id",
                       (app_id,))
        for i, h in enumerate(hist):
            h["duration_s"] = round((hist[i + 1]["at"] if i + 1 < len(hist) else now) - h["at"], 1)
        docs = []
        for d in self._q("""SELECT id, doc_type, filename, mime, size, quality, status, extraction_json, uploaded_at,
                            superseded FROM documents WHERE app_id=? ORDER BY id""", (app_id,)):
            ex = uj(d.pop("extraction_json"), {}) or {}
            d.update(method=ex.get("method"), model=ex.get("model"), detected_type=ex.get("document_type_detected"),
                     tamper_signals=ex.get("tamper_signals") or [], injection=ex.get("injection", False),
                     verified_by=ex.get("verified_by"), notes=ex.get("notes"))
            docs.append(d)
        pa = self._one("""SELECT requested_by, reason, requested_at, status, decided_by, decision_reason FROM approvals
                          WHERE app_id=? ORDER BY id DESC LIMIT 1""", (app_id,))
        brief = self._one("SELECT * FROM copilot_briefs WHERE app_id=? ORDER BY id DESC LIMIT 1", (app_id,))
        return {
            "id": app_id, "state": app["state"], "time_in_state_s": round(now - app["state_entered_at"], 1),
            "created_at": app["created_at"], "merchant_id": app["merchant_id"], "channel": app["channel"],
            "capabilities": uj(app["capabilities_json"]), "form": form,
            "ai_category": uj(app["ai_category_json"]),
            "assigned_to": app["assigned_to"],
            "sla_due_in_s": round(app["state_entered_at"] + self.cfg.review_sla_s - now, 1)
            if app["state"] in (S.MANUAL_REVIEW, S.PROVISIONING_FAILED) else None,
            "approval": pa,
            "copilot": {**uj(brief["brief_json"]), "id": brief["id"], "at": brief["at"], "feedback": brief["feedback"],
                        "requested_by": brief["requested_by"]} if brief else None,
            "extracted": uj(app["extracted_json"], {}), "checks": checks,
            "missing": self._q("""SELECT id, code, field, message, source, created_at, resolved_at, resolution
                                  FROM missing_items WHERE app_id=? ORDER BY id""", (app_id,)),
            "exceptions": [{**e, "detail": uj(e.pop("detail_json"))} for e in self._q(
                """SELECT id, code, severity, message, source, detail_json, created_at, resolved_at, resolved_by,
                   resolution FROM exceptions WHERE app_id=? ORDER BY id""", (app_id,))],
            "documents": docs,
            "provisioning": self._one("SELECT status, attempts, last_error, next_attempt_at FROM provisioning WHERE app_id=?",
                                      (app_id,)),
            "jobs": self._q("SELECT kind, status, attempts, error FROM jobs WHERE app_id=? ORDER BY id", (app_id,)),
            "notifications": self._q("SELECT at, kind, channel, recipient, subject, body, body_hi, drafted_by "
                                     "FROM notifications WHERE app_id=? ORDER BY id", (app_id,)),
            "llm_calls": self._q("SELECT at, feature, model, status, latency_ms, input_tokens, output_tokens, cost_usd, "
                                 "error FROM llm_calls WHERE app_id=? ORDER BY id", (app_id,)),
            "history": hist,
            "callbacks": self._q("""SELECT event_id, provider, request_id, received_at, outcome, delivery_count
                                    FROM callback_inbox WHERE app_id=? ORDER BY received_at""", (app_id,)),
            "audit": [{**a, "detail": uj(a.pop("detail_json"))} for a in self._q(
                "SELECT at, actor, event, detail_json FROM audit WHERE app_id=? ORDER BY id", (app_id,))],
            "summary": self._summary(app_id),
        }

    def _summary(self, app_id):
        app = self._app(app_id)
        st = app["state"]
        if st == S.NEEDS_INFO:
            return "Waiting for merchant: " + " ".join(m["message"] for m in self._open_missing(app_id))
        if st in (S.VERIFYING, S.SUBMITTED):
            parts = [f"{t} {c['status'].lower().replace('_', ' ')}" for t, c in self._checks(app_id).items()
                     if c["status"] in RUNNING]
            jobs = [r["kind"].replace("_", " ") for r in self._q(
                "SELECT kind FROM jobs WHERE app_id=? AND status IN ('QUEUED','RUNNING') AND gating=1", (app_id,))]
            return "Working on: " + (", ".join(parts + jobs) or "-")
        if st in (S.MANUAL_REVIEW, S.PROVISIONING_FAILED):
            pa = self._one("SELECT * FROM approvals WHERE app_id=? AND status='PENDING'", (app_id,))
            if pa:
                return f"Approved by {pa['requested_by']}; waiting for a second reviewer to confirm."
            return "Needs a reviewer: " + " ".join(e["message"] for e in self._open_exceptions(app_id))
        if st == S.PROVISIONING:
            pr = self._one("SELECT * FROM provisioning WHERE app_id=?", (app_id,))
            return "Creating merchant ID" + (f" (last error: {pr['last_error']})" if pr and pr["last_error"] else "")
        if st == S.ACTIVE:
            return f"Live as {app['merchant_id']}"
        if st == S.REJECTED:
            h = self._one("SELECT reason FROM state_history WHERE app_id=? ORDER BY id DESC LIMIT 1", (app_id,))
            return "Rejected: " + (h["reason"] if h else "")
        return st

    @transactional
    def review_queue(self):
        sev = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        now = self._now()
        apps = self._q("""SELECT id, legal_name, state, state_entered_at, assigned_to FROM applications
                          WHERE state IN ('MANUAL_REVIEW','PROVISIONING_FAILED')""")
        for a in apps:
            ex = self._open_exceptions(a["id"])
            a["exceptions"] = [{"code": e["code"], "severity": e["severity"]} for e in ex]
            a["top_severity"] = min((sev.get(e["severity"], 3) for e in ex), default=3)
            a["waiting_s"] = round(now - a["state_entered_at"], 1)
            a["sla_due_in_s"] = round(a["state_entered_at"] + self.cfg.review_sla_s - now, 1)
            a["pending_approval"] = bool(self._one("SELECT 1 x FROM approvals WHERE app_id=? AND status='PENDING'",
                                                   (a["id"],)))
        return sorted(apps, key=lambda a: (a["top_severity"], a["sla_due_in_s"]))

    @transactional
    def dead_letters(self):
        return self._q("SELECT * FROM dead_letters ORDER BY id DESC")

    @transactional
    def metrics(self):
        now = self._now()
        apps = self._q("SELECT id, state, ever_reviewed, ever_needs_info, created_at, state_entered_at FROM applications")
        n = len(apps)
        by_state = {}
        for a in apps:
            by_state[a["state"]] = by_state.get(a["state"], 0) + 1
        hist = self._q("SELECT app_id, to_state, at FROM state_history ORDER BY app_id, id")
        dur = {}
        for i, h in enumerate(hist):
            nxt = hist[i + 1] if i + 1 < len(hist) and hist[i + 1]["app_id"] == h["app_id"] else None
            if h["to_state"] in (S.ACTIVE, S.REJECTED):
                continue
            dur.setdefault(h["to_state"], []).append((nxt["at"] if nxt else now) - h["at"])
        reached = {s: len({h["app_id"] for h in hist if h["to_state"] == s}) for s in
                   (S.SUBMITTED, S.VERIFYING, S.NEEDS_INFO, S.MANUAL_REVIEW, S.APPROVED, S.ACTIVE, S.REJECTED)}
        checks = {}
        for t in CHECK_TYPES:
            rows = self._q("SELECT status, started_at, completed_at, attempts FROM checks WHERE check_type=?", (t,))
            lat = [r["completed_at"] - r["started_at"] for r in rows
                   if r["completed_at"] and r["started_at"] and r["status"] in ("PASSED", "FAILED", "WAIVED")]
            st = {}
            for r in rows:
                st[r["status"]] = st.get(r["status"], 0) + 1
            checks[t] = {"by_status": st, "avg_latency_s": round(sum(lat) / len(lat), 2) if lat else None,
                         "retries": sum(max(r["attempts"] - 1, 0) for r in rows)}
        cb = self._q("SELECT outcome, COUNT(*) n, SUM(delivery_count-1) dups FROM callback_inbox GROUP BY outcome")
        ttl = self._q("""SELECT h.at - a.created_at d FROM applications a JOIN state_history h
                         ON h.app_id=a.id AND h.to_state='ACTIVE'""")
        llm = {}
        for r in self._q("""SELECT feature, COUNT(*) calls, SUM(status='ok') ok, AVG(latency_ms) lat,
                            SUM(input_tokens) tin, SUM(output_tokens) tout, SUM(cost_usd) cost
                            FROM llm_calls GROUP BY feature"""):
            llm[r["feature"]] = {"calls": r["calls"], "ok": r["ok"], "avg_latency_ms": int(r["lat"] or 0),
                                 "tokens_in": r["tin"] or 0, "tokens_out": r["tout"] or 0,
                                 "cost_usd": round(r["cost"] or 0, 4)}
        briefs = self._q("SELECT brief_json, feedback, final_decision FROM copilot_briefs")
        agree = [b for b in briefs if b["final_decision"]]
        match = sum(1 for b in agree if uj(b["brief_json"])["recommendation"].startswith(b["final_decision"]))
        docs = self._q("SELECT extraction_json, status FROM documents WHERE superseded=0")
        by_method = {}
        for d in docs:
            m = (uj(d["extraction_json"], {}) or {}).get("method") or d["status"].lower()
            by_method[m] = by_method.get(m, 0) + 1
        ex_codes = {r["code"]: r["n"] for r in self._q("SELECT code, COUNT(*) n FROM exceptions GROUP BY code ORDER BY n DESC")}
        queue = [a for a in apps if a["state"] in (S.MANUAL_REVIEW, S.PROVISIONING_FAILED)]
        return {
            "applications": n, "by_state": by_state, "funnel": reached,
            "exception_rate": round(sum(a["ever_reviewed"] for a in apps) / n, 3) if n else 0,
            "needs_info_rate": round(sum(a["ever_needs_info"] for a in apps) / n, 3) if n else 0,
            "straight_through_rate": round(sum(1 for a in apps if a["state"] == S.ACTIVE and not a["ever_reviewed"])
                                           / n, 3) if n else 0,
            "avg_time_to_active_s": round(sum(r["d"] for r in ttl) / len(ttl), 1) if ttl else None,
            "avg_time_in_state_s": {k: round(sum(v) / len(v), 1) for k, v in dur.items()},
            "exceptions_by_code": ex_codes,
            "checks": checks,
            "callbacks": {r["outcome"]: r["n"] for r in cb},
            "duplicate_deliveries_ignored": sum((r["dups"] or 0) for r in cb),
            "timeouts": self._one("SELECT COUNT(*) n FROM audit WHERE event='CHECK_TIMED_OUT'")["n"],
            "dead_letters_open": self._one("SELECT COUNT(*) n FROM dead_letters WHERE resolved_at IS NULL")["n"],
            "review_queue": len(queue),
            "review_sla_breached": sum(1 for a in queue if now - a["state_entered_at"] > self.cfg.review_sla_s),
            "documents_by_method": by_method,
            "jobs": {r["status"]: r["n"] for r in self._q("SELECT status, COUNT(*) n FROM jobs GROUP BY status")},
            "llm": {"provider": self.gw.status(), "by_feature": llm,
                    "total_cost_usd": round(sum(v["cost_usd"] for v in llm.values()), 4)},
            "copilot": {"briefs": len(briefs), "helpful": sum(1 for b in briefs if b["feedback"] == "helpful"),
                        "not_helpful": sum(1 for b in briefs if b["feedback"] == "not_helpful"),
                        "decided": len(agree), "matched_final_decision": match},
        }

    # ------------------------------------------------------------ merchant portal
    def merchant_auth(self, app_id, token):
        with self._lock:
            a = self._one("SELECT access_token_hash FROM applications WHERE id=?", (app_id,))
        if not a or not token or not hmac.compare_digest(a["access_token_hash"], hashlib.sha256(token.encode()).hexdigest()):
            raise NotFound(app_id)  # same answer for "no such app" and "wrong token"

    @transactional
    def merchant_view(self, app_id):
        app = self._app(app_id)
        title, text = MERCHANT_STATUS[app["state"]]
        needed = []
        for m in self._open_missing(app_id):
            act = MERCHANT_ACTION.get(m["code"], "reply")
            if act == "upload" or m["code"] == "MISSING_DOCUMENT":
                act = f"upload:{m['field'].split('.')[0]}"
            elif act == "correct" and m["field"]:
                act = f"correct:{m['field']}"
            needed.append({"message": m["message"], "action": act})
        resubmit = {m["field"] for m in self._open_missing(app_id)
                    if m["code"] in ("DOCUMENT_UNREADABLE", "WRONG_DOCUMENT_TYPE")}
        docs = [{"doc_type": d["doc_type"], "filename": d["filename"],
                 "status": "RESUBMIT" if d["doc_type"] in resubmit else d["status"], "uploaded_at": d["uploaded_at"]}
                for d in self._q("SELECT doc_type, filename, status, uploaded_at FROM documents WHERE app_id=? "
                                 "AND superseded=0 ORDER BY id", (app_id,))]
        steps = [("Application received", True)]
        reached = {h["to_state"] for h in self._q("SELECT to_state FROM state_history WHERE app_id=?", (app_id,))}
        steps.append(("Details verified", bool(reached & {S.APPROVED, S.ACTIVE})))
        steps.append(("Account set up", app["state"] == S.ACTIVE))
        return {
            "id": app_id, "legal_name": app["legal_name"], "status": title, "status_text": text,
            "state_group": {"NEEDS_INFO": "action", "ACTIVE": "done", "REJECTED": "closed"}.get(app["state"], "wait"),
            "needed": needed, "documents": docs, "steps": steps,
            "form": {k: mask(k, app[k]) for k in ("legal_name", "trade_name", "category", "pan", "gstin",
                                                   "bank_account", "ifsc", "email", "phone")},
            "merchant_id": app["merchant_id"], "capabilities": uj(app["capabilities_json"]),
            "messages": self._q("SELECT at, kind, subject, body, body_hi FROM notifications WHERE app_id=? ORDER BY id DESC",
                                (app_id,)),
        }

    @transactional
    def scenario_seed(self):
        """Demo helper: identifiers + PAN document of the latest application, so the
        'duplicate' and 'reused_document' scenarios have something to collide with."""
        a = self._one("SELECT id, pan, gstin, bank_account, ifsc FROM applications ORDER BY created_at DESC LIMIT 1")
        if not a:
            return None
        d = self._one("""SELECT doc_type, filename, content, quality, mime, blob FROM documents WHERE app_id=?
                         AND doc_type='PAN_CARD' AND superseded=0""", (a["id"],))
        if d:
            a["pan_doc"] = ({"doc_type": "PAN_CARD", "filename": d["filename"], "text": d["content"], "quality": d["quality"]}
                            if d["blob"] is None else None)
        return a

    @transactional
    def recent_activity(self, limit=40):
        """Cross-application feed of meaningful events for the overview page."""
        keep = ("APPLICATION_SUBMITTED", "STATE_CHANGED", "EXCEPTION_RAISED", "CHECK_TIMED_OUT", "CHECK_DEAD_LETTERED",
                "DUPLICATE_CALLBACK_IGNORED", "MERCHANT_ID_CREATED", "MID_CREATION_FAILED", "REVIEWER_APPROVED",
                "REVIEWER_REJECTED", "APPROVAL_REQUESTED", "NAME_MATCH_CONFIRMED_BY_AI", "AI_FALLBACK_USED",
                "DOCUMENT_EXTRACTED_BY_AI", "DOCUMENT_FIELDS_VERIFIED", "MERCHANT_NOTIFIED", "FAULT_MODE_CHANGED",
                "CALLBACK_RECEIVED")
        return self._q(f"""SELECT a.at, a.app_id, a.actor, a.event, a.detail_json, p.legal_name FROM audit a
                           LEFT JOIN applications p ON p.id=a.app_id WHERE a.event IN ({','.join('?' * len(keep))})
                           AND NOT (a.event='CALLBACK_RECEIVED' AND a.detail_json LIKE '%"outcome": "accepted"%')
                           ORDER BY a.id DESC LIMIT ?""", (*keep, limit))
