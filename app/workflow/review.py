"""Human-in-the-loop: claim/release, approve (with maker-checker for high
severity), reject, ask merchant, retry, field verification against the
document image, audited reveal of sensitive values, DLQ replay, copilot."""
import re

from .. import ai, extraction
from ..privacy import SENSITIVE_FIELDS
from ..states import S
from .base import ActionError, NotFound, j, transactional, uj

REVIEW_STATES = (S.MANUAL_REVIEW, S.PROVISIONING_FAILED)


class ReviewMixin:
    @transactional
    def review_action(self, app_id, action, actor, reason=None, check_type=None, message=None):
        app = self._app(app_id)
        st = app["state"]
        for name, v in (("reason", reason), ("message", message), ("check_type", check_type)):
            if v is not None and not isinstance(v, str):
                raise ActionError(f"{name} must be text")
        reason = (reason or "").strip()[:1000]
        message = (message or "").strip()[:1000] or None
        handler = {
            "claim": self._act_claim, "release": self._act_release, "approve": self._act_approve,
            "confirm_approval": self._act_confirm, "decline_approval": self._act_decline,
            "reject": self._act_reject, "request_info": self._act_request_info,
            "retry_check": self._act_retry_check, "retry_provisioning": self._act_retry_provisioning,
        }.get(action)
        if not handler:
            raise ActionError(f"Unknown action {action}")
        if action in ("approve", "reject", "request_info") and app["assigned_to"] and app["assigned_to"] != actor:
            raise ActionError(f"Case is claimed by {app['assigned_to']}")
        return handler(app, st, actor, reason, check_type, message)

    def _act_claim(self, app, st, actor, *_):
        if st not in REVIEW_STATES:
            raise ActionError("Only cases waiting for a reviewer can be claimed")
        if app["assigned_to"] and app["assigned_to"] != actor:
            raise ActionError(f"Already claimed by {app['assigned_to']}")
        self._x("UPDATE applications SET assigned_to=?, claimed_at=? WHERE id=?", (actor, self._now(), app["id"]))
        self._audit(app["id"], actor, "CASE_CLAIMED")

    def _act_release(self, app, st, actor, *_):
        if app["assigned_to"] != actor:
            raise ActionError("You have not claimed this case")
        self._x("UPDATE applications SET assigned_to=NULL, claimed_at=NULL WHERE id=?", (app["id"],))
        self._audit(app["id"], actor, "CASE_RELEASED")

    def _pending_approval(self, app_id):
        return self._one("SELECT * FROM approvals WHERE app_id=? AND status='PENDING' ORDER BY id DESC LIMIT 1",
                         (app_id,))

    def _act_approve(self, app, st, actor, reason, *_):
        if st != S.MANUAL_REVIEW:
            raise ActionError("Only applications waiting for a reviewer can be approved")
        if not reason:
            raise ActionError("A reason is required to approve")
        if self._pending_approval(app["id"]):
            raise ActionError("An approval is already waiting for a second reviewer")
        checks = self._checks(app["id"])
        for t in self.cfg.non_overridable_checks:
            if checks[t]["status"] not in ("PASSED", "NOT_REQUIRED"):
                raise ActionError(f"{t} check is {checks[t]['status']}; it must pass and cannot be overridden. "
                                  f"Ask the merchant for corrected details, or reject.")
        if self._open_missing(app["id"]):
            raise ActionError("Merchant still has open requests")
        open_ex = self._open_exceptions(app["id"])
        waive = [t for t, c in checks.items() if c["status"] in ("FAILED", "DEAD")]
        if not app["assigned_to"]:
            self._x("UPDATE applications SET assigned_to=?, claimed_at=? WHERE id=?", (actor, self._now(), app["id"]))
        high = [e["code"] for e in open_ex if e["severity"] == "HIGH"]
        if self.cfg.four_eyes_on_high_severity and high:
            self._x("""INSERT INTO approvals(app_id,requested_by,reason,requested_at,snapshot_json,status)
                       VALUES (?,?,?,?,?,'PENDING')""",
                    (app["id"], actor, reason, self._now(),
                     j({"exceptions": sorted(e["id"] for e in open_ex), "waive": waive})))
            self._audit(app["id"], actor, "APPROVAL_REQUESTED", reason=reason, high_severity=high, waive=waive,
                        note="second reviewer must confirm (maker-checker)")
            return
        self._apply_approval(app["id"], [actor], reason, waive)

    def _act_confirm(self, app, st, actor, reason, *_):
        pa = self._pending_approval(app["id"])
        if not pa:
            raise ActionError("No approval is waiting for confirmation")
        if pa["requested_by"] == actor:
            raise ActionError("Maker-checker: a different reviewer must confirm this approval")
        snap = uj(pa["snapshot_json"])
        now_open = sorted(e["id"] for e in self._open_exceptions(app["id"]))
        waive_now = [t for t, c in self._checks(app["id"]).items() if c["status"] in ("FAILED", "DEAD")]
        if now_open != snap["exceptions"] or sorted(waive_now) != sorted(snap["waive"]) or st != S.MANUAL_REVIEW:
            self._invalidate_pending_approval(app["id"], "case changed since approval was requested")
            raise ActionError("The case changed after the first approval; it needs a fresh review")
        self._x("UPDATE approvals SET status='CONFIRMED', decided_by=?, decided_at=?, decision_reason=? WHERE id=?",
                (actor, self._now(), reason, pa["id"]))
        self._apply_approval(app["id"], [pa["requested_by"], actor], pa["reason"], snap["waive"])

    def _act_decline(self, app, st, actor, reason, *_):
        pa = self._pending_approval(app["id"])
        if not pa:
            raise ActionError("No approval is waiting for confirmation")
        if not reason:
            raise ActionError("A reason is required to decline")
        self._x("UPDATE approvals SET status='DECLINED', decided_by=?, decided_at=?, decision_reason=? WHERE id=?",
                (actor, self._now(), reason, pa["id"]))
        self._audit(app["id"], actor, "APPROVAL_DECLINED", reason=reason, requested_by=pa["requested_by"])

    def _apply_approval(self, app_id, approvers, reason, waive):
        for t in waive:
            c = self._check(app_id, t)
            self._x("UPDATE checks SET status='WAIVED' WHERE app_id=? AND check_type=?", (app_id, t))
            self._resolve_dead_letters(app_id, "CHECK", t, f"waived by {', '.join(approvers)}")
            self._audit(app_id, approvers[-1], "CHECK_WAIVED", check=t, previous_status=c["status"],
                        reason_code=c["reason_code"], reason=reason)
        self._resolve_exceptions(app_id, " + ".join(approvers), f"approved: {reason}")
        self._audit(app_id, approvers[-1], "REVIEWER_APPROVED", approvers=approvers, reason=reason)
        self._record_copilot_outcome(app_id, "APPROVE")
        self._evaluate(app_id, approvers[-1])

    def _act_reject(self, app, st, actor, reason, *_):
        if st not in (S.MANUAL_REVIEW, S.NEEDS_INFO, S.VERIFYING):
            raise ActionError(f"Cannot reject while {st}")
        if not reason:
            raise ActionError("A reason is required to reject")
        app_id = app["id"]
        self._x("UPDATE provider_requests SET status='SUPERSEDED' WHERE app_id=? AND status IN ('QUEUED','SENT','TIMED_OUT')",
                (app_id,))
        self._x("UPDATE jobs SET status='CANCELLED' WHERE app_id=? AND status='QUEUED' AND gating=1", (app_id,))
        self._invalidate_pending_approval(app_id, "rejected")
        self._resolve_exceptions(app_id, actor, f"rejected: {reason}")
        self._x("UPDATE missing_items SET resolved_at=?, resolution='application rejected' WHERE app_id=? "
                "AND resolved_at IS NULL", (self._now(), app_id))
        self._audit(app_id, actor, "REVIEWER_REJECTED", reason=reason)
        self._record_copilot_outcome(app_id, "REJECT")
        self._transition(app_id, S.REJECTED, actor, reason)

    def _act_request_info(self, app, st, actor, reason, check_type, message):
        if st not in (S.MANUAL_REVIEW, S.VERIFYING):
            raise ActionError(f"Cannot ask the merchant while {st}")
        message = (message or reason or "").strip()
        if not message:
            raise ActionError("Write the message to the merchant")
        self._invalidate_pending_approval(app["id"], "information requested")
        self._add_missing(app["id"], "reviewer", {"code": "REVIEWER_REQUEST", "field": "reviewer", "message": message,
                                                  "blocks": [], "actor": actor,
                                                  "key": f"REVIEWER:{int(self._now() * 1000)}"})
        self._record_copilot_outcome(app["id"], "REQUEST_INFO")
        self._evaluate(app["id"], actor)

    def _act_retry_check(self, app, st, actor, reason, check_type, _m):
        if st not in (S.MANUAL_REVIEW, S.VERIFYING):
            raise ActionError(f"Cannot retry checks while {st}")
        c = self._check(app["id"], check_type or "")
        if not c or c["status"] not in ("FAILED", "DEAD"):
            raise ActionError("Only FAILED or DEAD checks can be retried")
        self._audit(app["id"], actor, "CHECK_RETRY_REQUESTED", check=check_type, reason=reason)
        self._reset_check(app["id"], check_type, "PENDING", c["fingerprint"], f"retry requested by {actor}")
        self._dispatch_due(app["id"])
        self._evaluate(app["id"], actor)

    def _act_retry_provisioning(self, app, st, actor, *_):
        if st != S.PROVISIONING_FAILED:
            raise ActionError("Only applications whose merchant ID creation failed can retry it")
        app_id = app["id"]
        self._resolve_exceptions(app_id, actor, "provisioning retried", source="provisioning")
        self._resolve_dead_letters(app_id, "PROVISIONING", "MID", f"retried by {actor}")
        self._x("UPDATE provisioning SET status='PENDING', attempts=0 WHERE app_id=?", (app_id,))
        self._transition(app_id, S.PROVISIONING, actor, "provisioning retried")
        self._attempt_provisioning(app_id)

    # ------------------------------------------------------------ dead-letter replay
    @transactional
    def replay_dead_letter(self, dl_id, actor):
        d = self._one("SELECT * FROM dead_letters WHERE id=?", (dl_id,))
        if not d or d["resolved_at"]:
            raise ActionError("Dead letter not found or already resolved")
        if d["kind"] == "CHECK":
            return self.review_action(d["app_id"], "retry_check", actor, reason="replayed from DLQ", check_type=d["ref"])
        return self.review_action(d["app_id"], "retry_provisioning", actor)

    # ------------------------------------------------------------ documents: view + human verification
    @transactional
    def document_for_review(self, app_id, doc_id, actor):
        d = self._one("SELECT * FROM documents WHERE id=? AND app_id=?", (doc_id, app_id))
        if not d:
            raise NotFound(doc_id)
        self._audit(app_id, actor, "DOCUMENT_VIEWED", doc_id=doc_id, doc_type=d["doc_type"])
        if d["blob"] is not None:
            return d["mime"], bytes(d["blob"])
        return "text/plain; charset=utf-8", (d["content"] or "").encode()

    @transactional
    def document_extraction(self, app_id, doc_id, actor):
        """Unmasked extracted values, for a reviewer verifying against the image (audited)."""
        d = self._one("SELECT * FROM documents WHERE id=? AND app_id=?", (doc_id, app_id))
        if not d:
            raise NotFound(doc_id)
        self._audit(app_id, actor, "SENSITIVE_FIELD_REVEALED", doc_id=doc_id, purpose="field verification")
        ex = uj(d["extraction_json"], {}) or {}
        fields = {f: (ex.get("fields", {}).get(f) or {}).get("value", "") for f, _ in extraction.COMPARE[d["doc_type"]]}
        return {"doc_id": doc_id, "doc_type": d["doc_type"], "status": d["status"], "fields": fields,
                "confidence": {f: (ex.get("fields", {}).get(f) or {}).get("confidence") for f in fields},
                "method": ex.get("method"), "quality": ex.get("quality")}

    @transactional
    def verify_document_fields(self, app_id, doc_id, values: dict, actor):
        app = self._app(app_id)
        if app["state"] not in (S.MANUAL_REVIEW, S.VERIFYING, S.NEEDS_INFO):
            raise ActionError(f"Cannot verify documents while {app['state']}")
        d = self._one("SELECT * FROM documents WHERE id=? AND app_id=? AND superseded=0", (doc_id, app_id))
        if not d:
            raise ActionError("Document not found or replaced")
        if not isinstance(values, dict):
            raise ActionError("values must be an object")
        allowed = [f for f, _ in extraction.COMPARE[d["doc_type"]]]
        clean = {}
        for f in allowed:
            v = values.get(f)
            if v is not None and not isinstance(v, str):
                raise ActionError(f"{extraction.FIELD_LABELS[f]} must be text")
            v = (v or "").strip()[:200]
            if not v:
                raise ActionError(f"{extraction.FIELD_LABELS[f]} is required")
            if f in ("pan", "gstin", "ifsc"):
                v = re.sub(r"\s", "", v).upper()
            if f == "bank_account":
                v = re.sub(r"\D", "", v)
            clean[f] = v
        prev = uj(d["extraction_json"], {}) or {}
        ex = {"quality": 1.0, "method": "human", "verified_by": actor, "previous_method": prev.get("method"),
              "fields": {f: {"value": v, "confidence": 1.0} for f, v in clean.items()},
              "document_type_detected": d["doc_type"],
              "tamper_signals": prev.get("tamper_signals", []), "injection": prev.get("injection", False)}
        self._x("UPDATE documents SET status='EXTRACTED', extraction_json=?, quality=1.0 WHERE id=?", (j(ex), doc_id))
        changed = [f for f in clean if (prev.get("fields", {}).get(f) or {}).get("value") != clean[f]]
        self._audit(app_id, actor, "DOCUMENT_FIELDS_VERIFIED", doc_id=doc_id, doc_type=d["doc_type"],
                    corrected_fields=changed, previous_method=prev.get("method"))
        self._after_intake(app_id)

    @transactional
    def reveal(self, app_id, field, actor, purpose):
        if field not in SENSITIVE_FIELDS:
            raise ActionError("Field is not sensitive or unknown")
        if not (purpose or "").strip():
            raise ActionError("A purpose is required to reveal sensitive data")
        app = self._app(app_id)
        self._audit(app_id, actor, "SENSITIVE_FIELD_REVEALED", field=field, purpose=purpose)
        return app[field]

    # ------------------------------------------------------------ copilot
    def copilot(self, app_id, actor):
        """Runs outside the engine lock (LLM latency); stores the brief afterwards."""
        case = self.get_application(app_id)
        brief = ai.copilot_brief(self.gw, case)
        return self._store_brief(app_id, actor, brief)

    @transactional
    def _store_brief(self, app_id, actor, brief):
        cur = self._x("INSERT INTO copilot_briefs(app_id,at,requested_by,brief_json) VALUES (?,?,?,?)",
                      (app_id, self._now(), actor, j(brief)))
        self._audit(app_id, actor, "COPILOT_BRIEF_GENERATED", recommendation=brief["recommendation"],
                    method=brief.get("method"), policy_adjusted=bool(brief.get("policy_note")))
        return {**brief, "id": cur.lastrowid}

    @transactional
    def copilot_feedback(self, app_id, brief_id, feedback, actor):
        if feedback not in ("helpful", "not_helpful"):
            raise ActionError("feedback must be helpful or not_helpful")
        self._x("UPDATE copilot_briefs SET feedback=?, feedback_by=?, feedback_at=? WHERE id=? AND app_id=?",
                (feedback, actor, self._now(), brief_id, app_id))
        self._audit(app_id, actor, "COPILOT_FEEDBACK", feedback=feedback)

    def _record_copilot_outcome(self, app_id, decision):
        b = self._one("SELECT id FROM copilot_briefs WHERE app_id=? AND final_decision IS NULL ORDER BY id DESC LIMIT 1",
                      (app_id,))
        if b:
            self._x("UPDATE copilot_briefs SET final_decision=? WHERE id=?", (decision, b["id"]))

    # ------------------------------------------------------------ runtime controls
    @transactional
    def set_mode(self, target, mode, actor="ops"):
        if target == "MID":
            self.mid.set_mode(mode)
        elif target == "LLM":
            if mode not in ("normal", "outage", "garbage"):
                raise ValueError(mode)
            self.gw.fault_mode = mode
            if mode == "normal":
                self.gw.reset_breaker()
        else:
            self.sim.set_mode(target, mode)
        self._audit(None, actor, "FAULT_MODE_CHANGED", target=target, mode=mode)

    def modes(self):
        return {**self.sim.modes, "MID": self.mid.mode, "LLM": self.gw.fault_mode}
