"""Verification checks: dispatch through an outbox, timeouts and retries,
signed callbacks (duplicate / late / stale / superseded), result
interpretation, and AI-assisted bank name adjudication."""
import hmac
import json
import sqlite3
import uuid

from .. import extraction
from ..privacy import scrub_text
from ..providers import ProviderUnavailable, sign
from ..states import S, TERMINAL
from .base import CHECK_INPUTS, j, transactional


class ChecksMixin:
    def _reset_check(self, app_id, t, status, fp, reason):
        old = self._check(app_id, t)
        self._x("""UPDATE provider_requests SET status='SUPERSEDED', resolved_at=? WHERE app_id=? AND check_type=?
                   AND status IN ('QUEUED','SENT','TIMED_OUT')""", (self._now(), app_id, t))
        self._x("""UPDATE jobs SET status='CANCELLED' WHERE app_id=? AND kind='adjudicate_bank_name'
                   AND status='QUEUED'""", (app_id,)) if t == "BANK" else None
        src = f"check:{t}"
        self._resolve_missing(app_id, src, f"check reset: {reason}")
        self._resolve_exceptions(app_id, "system", f"check reset: {reason}", source=src)
        self._resolve_dead_letters(app_id, "CHECK", t, f"check reset: {reason}")
        self._x("""UPDATE checks SET status=?, fingerprint=?, request_id=NULL, attempts=0, next_attempt_at=NULL,
                   deadline_at=NULL, started_at=NULL, completed_at=NULL, reason_code=NULL, category=NULL,
                   result_json=NULL WHERE app_id=? AND check_type=?""", (status, fp, app_id, t))
        if reason != "initial":
            event = "VERIFIED_DATA_CHANGED_REVERIFYING" if old["status"] == "PASSED" else "CHECK_RESET"
            self._audit(app_id, "system", event, check=t, previous_status=old["status"], new_status=status,
                        reason=reason)
            self._invalidate_pending_approval(app_id, f"{t} check reset")

    # ------------------------------------------------------------ dispatch via outbox
    def _dispatch_due(self, app_id=None):
        now = self._now()
        sql = """SELECT c.* FROM checks c JOIN applications a ON a.id=c.app_id
                 WHERE a.state IN ('SUBMITTED','VERIFYING','NEEDS_INFO','MANUAL_REVIEW')
                 AND (c.status='PENDING' OR (c.status='RETRY_WAIT' AND c.next_attempt_at<=?))"""
        p = [now]
        if app_id:
            sql += " AND c.app_id=?"
            p.append(app_id)
        touched = set()
        for c in self._q(sql, p):
            attempt = c["attempts"] + 1
            req_id = f"REQ-{c['check_type']}-{uuid.uuid4().hex[:10]}"
            self._x("""INSERT INTO provider_requests(request_id,app_id,check_type,fingerprint,attempt,sent_at,status)
                       VALUES (?,?,?,?,?,?,'QUEUED')""",
                    (req_id, c["app_id"], c["check_type"], c["fingerprint"], attempt, now))
            self._x("""UPDATE checks SET status='DISPATCHING', attempts=?, request_id=?, started_at=COALESCE(started_at,?)
                       WHERE app_id=? AND check_type=?""", (attempt, req_id, now, c["app_id"], c["check_type"]))
            self._outbox_dirty = True
            touched.add(c["app_id"])
        return touched

    @transactional
    def flush_outbox(self):
        """Send queued provider requests. Runs after the transaction that
        queued them has committed (and from the scheduler, after a crash)."""
        self._outbox_dirty = False
        touched = set()
        for r in self._q("SELECT * FROM provider_requests WHERE status='QUEUED' ORDER BY sent_at"):
            app = self._app(r["app_id"])
            t, now = r["check_type"], self._now()
            try:
                ack = self.sim.submit(t, r["request_id"], {k: app.get(k) for k in CHECK_INPUTS[t]})
            except ProviderUnavailable as e:
                self._x("UPDATE provider_requests SET status='SEND_FAILED', resolved_at=? WHERE request_id=?",
                        (now, r["request_id"]))
                self._audit(app["id"], "system", "PROVIDER_CALL_FAILED", check=t, attempt=r["attempt"], error=str(e))
                self._retry_or_dead(app["id"], t, r["attempt"], "PROVIDER_UNAVAILABLE", str(e))
                touched.add(app["id"])
                continue
            self._x("UPDATE provider_requests SET status='SENT', sent_at=? WHERE request_id=?", (now, r["request_id"]))
            self._x("""UPDATE checks SET status='IN_FLIGHT', deadline_at=? WHERE app_id=? AND check_type=?
                       AND request_id=?""", (now + self.cfg.check_timeout_s, app["id"], t, r["request_id"]))
            self._audit(app["id"], "system", "CHECK_DISPATCHED", check=t, attempt=r["attempt"],
                        request_id=r["request_id"], provider_eta_s=ack.get("eta_s"))
        for app_id in touched:
            self._evaluate(app_id, "system")

    def _retry_or_dead(self, app_id, t, attempt, code, error):
        if attempt >= self.cfg.max_check_attempts:
            self._x("UPDATE checks SET status='DEAD', reason_code=?, completed_at=? WHERE app_id=? AND check_type=?",
                    (code, self._now(), app_id, t))
            self._x("""INSERT INTO dead_letters(app_id,kind,ref,error,detail_json,created_at) VALUES (?,?,?,?,?,?)""",
                    (app_id, "CHECK", t, f"{code}: {error}", j({"attempts": attempt}), self._now()))
            self._add_exception(app_id, f"check:{t}", f"{t}_CHECK_EXHAUSTED", "HIGH",
                                f"{t} verification gave no usable answer after {attempt} attempts ({code}). "
                                f"Moved to the dead-letter queue.", {"last_error": error}, key=f"{t}_EXHAUSTED")
            self._audit(app_id, "system", "CHECK_DEAD_LETTERED", check=t, attempts=attempt, error=code)
        else:
            delay = self._backoff(attempt)
            self._x("UPDATE checks SET status='RETRY_WAIT', next_attempt_at=? WHERE app_id=? AND check_type=?",
                    (self._now() + delay, app_id, t))
            self._audit(app_id, "system", "RETRY_SCHEDULED", check=t, next_attempt=attempt + 1,
                        in_seconds=delay, reason=code)

    # ------------------------------------------------------------ callbacks
    @transactional
    def handle_webhook(self, provider, body: bytes, signature: str):
        if not signature or not hmac.compare_digest(sign(self.cfg.webhook_secret, body), signature):
            return 401, {"outcome": "rejected_bad_signature"}
        try:
            payload = json.loads(body)
        except ValueError:
            return 400, {"outcome": "malformed"}
        if (not isinstance(payload, dict) or payload.get("provider") != provider
                or not isinstance(payload.get("event_id"), str) or not isinstance(payload.get("request_id"), str)
                or len(payload["event_id"]) > 100 or len(payload["request_id"]) > 100
                or not isinstance(payload.get("result", {}), (dict, type(None)))):
            return 400, {"outcome": "malformed"}
        return 200, {"outcome": self._handle_callback(provider, payload)}

    def _handle_callback(self, provider, p):
        now = self._now()
        req = self._one("SELECT * FROM provider_requests WHERE request_id=?", (p["request_id"],))
        app_id = req["app_id"] if req else None
        try:
            self._x("""INSERT INTO callback_inbox(event_id,provider,request_id,app_id,received_at,outcome)
                       VALUES (?,?,?,?,?,'PROCESSING')""", (p["event_id"], provider, p["request_id"], app_id, now))
        except sqlite3.IntegrityError:
            self._x("UPDATE callback_inbox SET delivery_count=delivery_count+1 WHERE event_id=?", (p["event_id"],))
            if app_id:
                self._audit(app_id, f"provider:{provider}", "DUPLICATE_CALLBACK_IGNORED",
                            event_id=p["event_id"], request_id=p["request_id"])
            return "duplicate_ignored"
        outcome = self._apply_callback(provider, p, req)
        self._x("UPDATE callback_inbox SET outcome=? WHERE event_id=?", (outcome, p["event_id"]))
        if app_id:
            self._audit(app_id, f"provider:{provider}", "CALLBACK_RECEIVED", request_id=p["request_id"],
                        status=p.get("status"), outcome=outcome)
            self._evaluate(app_id, "system")
        return outcome

    def _apply_callback(self, provider, p, req):
        if not req or req["check_type"] != provider:
            return "unknown_request"
        app = self._app(req["app_id"])
        if app["state"] in TERMINAL or app["state"] in (S.APPROVED, S.PROVISIONING, S.PROVISIONING_FAILED):
            return "application_closed_ignored"
        c = self._check(app["id"], provider)
        if req["fingerprint"] != c["fingerprint"]:
            self._x("UPDATE provider_requests SET status='STALE', resolved_at=? WHERE request_id=?",
                    (self._now(), req["request_id"]))
            return "stale_inputs_discarded"
        if req["status"] == "SUPERSEDED":
            return "superseded_ignored"
        if c["status"] in ("PASSED", "FAILED", "WAIVED", "NOT_REQUIRED", "ADJUDICATING"):
            self._x("UPDATE provider_requests SET status='IGNORED', resolved_at=? WHERE request_id=?",
                    (self._now(), req["request_id"]))
            return "already_resolved_ignored"

        late = req["request_id"] != c["request_id"] or req["status"] == "TIMED_OUT" or c["status"] == "DEAD"
        if p.get("status") != "COMPLETED":
            self._x("UPDATE provider_requests SET status='ERROR', resolved_at=? WHERE request_id=?",
                    (self._now(), req["request_id"]))
            if late:
                return "late_error_ignored"
            self._retry_or_dead(app["id"], provider, c["attempts"], "PROVIDER_ERROR", p.get("error", "error"))
            return "provider_error_retrying"

        # A valid answer for the *same inputs* is useful even when late: accept it
        # and supersede any retry still in flight.
        self._x("UPDATE provider_requests SET status='ANSWERED', resolved_at=? WHERE request_id=?",
                (self._now(), req["request_id"]))
        self._x("""UPDATE provider_requests SET status='SUPERSEDED', resolved_at=? WHERE app_id=? AND check_type=?
                   AND request_id<>? AND status IN ('QUEUED','SENT','TIMED_OUT')""",
                (self._now(), app["id"], provider, req["request_id"]))
        if c["status"] == "DEAD":
            self._resolve_dead_letters(app["id"], "CHECK", provider, "late callback delivered a valid result")
            self._resolve_exceptions(app["id"], "system", "late callback delivered a valid result",
                                     source=f"check:{provider}", key=f"{provider}_EXHAUSTED")
        try:
            self._apply_result(app, provider, p.get("result") or {})
        except (AttributeError, TypeError, ValueError, KeyError) as e:   # provider sent a malformed result
            self._audit(app["id"], f"provider:{provider}", "MALFORMED_PROVIDER_RESULT", error=str(e)[:200])
            self._retry_or_dead(app["id"], provider, c["attempts"], "MALFORMED_RESULT", str(e)[:200])
            return "malformed_result_retrying"
        return "late_accepted" if late else "accepted"

    def _apply_result(self, app, t, r):
        thr = self.cfg.name_match_threshold
        status, code, cat, sev, msg = "PASSED", None, None, None, None
        if t == "PAN":
            if r.get("pan_status") != "VALID":
                status, code, cat = "FAILED", "PAN_NOT_FOUND", "CORRECTABLE"
                msg = "The PAN you entered was not found in the Income Tax registry. Please check and correct it."
            elif extraction.name_similarity(r.get("registered_name"), app["legal_name"]) < thr:
                status, code, cat, sev = "FAILED", "PAN_NAME_MISMATCH", "SUBSTANTIVE", "HIGH"
                msg = "Name registered against the PAN does not match the legal name."
        elif t == "GST":
            if r.get("gst_status") == "NOT_FOUND":
                status, code, cat = "FAILED", "GSTIN_NOT_FOUND", "CORRECTABLE"
                msg = "The GSTIN was not found on the GST portal. Please check and correct it."
            elif r.get("gst_status") == "CANCELLED":
                status, code, cat, sev = "FAILED", "GSTIN_CANCELLED", "SUBSTANTIVE", "HIGH"
                msg = "GST registration is CANCELLED on the GST portal while the PAN is valid."
            elif not r.get("pan_linked"):
                status, code, cat, sev = "FAILED", "GSTIN_PAN_MISMATCH", "SUBSTANTIVE", "HIGH"
                msg = "GSTIN is registered against a different PAN."
        elif t == "BANK":
            if r.get("account_status") != "VALID":
                status, code, cat = "FAILED", "BANK_ACCOUNT_INVALID", "CORRECTABLE"
                msg = "Penny-drop to your bank account failed. Please provide a different, active current account."
            else:
                sim = max(extraction.name_similarity(r.get("beneficiary_name"), app["legal_name"]),
                          extraction.name_similarity(r.get("beneficiary_name"), app.get("trade_name") or ""))
                r = {**r, "name_match": sim}
                if sim < thr:
                    if (self.cfg.ai_name_adjudication and self.gw.enabled and sim >= self.cfg.name_gray_zone_min):
                        self._x("""UPDATE checks SET status='ADJUDICATING', result_json=? WHERE app_id=? AND check_type=?""",
                                (scrub_text(j(r)), app["id"], t))
                        self._enqueue("adjudicate_bank_name", app["id"], "BANK",
                                      {"beneficiary": r.get("beneficiary_name"), "legal_name": app["legal_name"],
                                       "similarity": sim, "fingerprint": self._check(app["id"], t)["fingerprint"]})
                        self._audit(app["id"], "system", "NAME_ADJUDICATION_REQUESTED", check=t, similarity=sim)
                        return
                    status, code, cat, sev = "FAILED", "BANK_NAME_MISMATCH", "SUBSTANTIVE", "HIGH"
                    msg = (f"Bank beneficiary name '{r.get('beneficiary_name')}' does not match the business "
                           f"(similarity {sim:.2f}). Settlements cannot go to a third-party account.")
        elif t == "RISK":
            if r.get("band") in ("MEDIUM", "HIGH"):
                status, code, cat = "FAILED", f"RISK_{r['band']}", "SUBSTANTIVE"
                sev = "HIGH" if r["band"] == "HIGH" else "MEDIUM"
                msg = f"Risk score {r.get('score')} ({r['band']}): " + "; ".join(r.get("signals", []))
        self._complete_check(app["id"], t, status, code, cat, sev, msg, r)

    def _complete_check(self, app_id, t, status, code, cat, sev, msg, result):
        self._x("""UPDATE checks SET status=?, reason_code=?, category=?, result_json=?, completed_at=?
                   WHERE app_id=? AND check_type=?""",
                (status, code, cat, scrub_text(j(result)), self._now(), app_id, t))
        self._audit(app_id, "system", "CHECK_COMPLETED", check=t, status=status, reason=code)
        if status == "FAILED" and cat == "CORRECTABLE":
            self._add_missing(app_id, f"check:{t}", {"code": code, "field": t, "message": msg, "blocks": [t]})
        elif status == "FAILED":
            self._add_exception(app_id, f"check:{t}", code, sev, msg, {"check": t}, key=code)

    # ------------------------------------------------------------ scheduler
    @transactional
    def tick(self):
        now = self._now()
        touched = set()
        for c in self._q("SELECT * FROM checks WHERE status='IN_FLIGHT' AND deadline_at<=?", (now,)):
            self._x("UPDATE provider_requests SET status='TIMED_OUT' WHERE request_id=? AND status='SENT'",
                    (c["request_id"],))
            self._audit(c["app_id"], "scheduler", "CHECK_TIMED_OUT", check=c["check_type"], attempt=c["attempts"],
                        request_id=c["request_id"], timeout_s=self.cfg.check_timeout_s)
            self._retry_or_dead(c["app_id"], c["check_type"], c["attempts"], "TIMEOUT",
                                f"no callback within {self.cfg.check_timeout_s}s")
            touched.add(c["app_id"])
        touched |= self._dispatch_due()
        if self._one("SELECT 1 x FROM provider_requests WHERE status='QUEUED' LIMIT 1"):
            self._outbox_dirty = True   # recover requests queued before a crash
        for pr in self._q("SELECT * FROM provisioning WHERE status='RETRY_WAIT' AND next_attempt_at<=?", (now,)):
            self._attempt_provisioning(pr["app_id"])
        self._send_reminders()
        for app_id in touched:
            self._evaluate(app_id, "system")
        return len(touched)
