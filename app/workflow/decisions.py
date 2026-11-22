"""Decision rule, provisioning (merchant ID + payment capabilities) and
merchant notifications."""
from ..privacy import mask
from ..providers import MIDServiceError
from ..states import EVALUABLE, S
from .base import GATING_JOBS, RUNNING, j, uj


class DecisionsMixin:
    def _evaluate(self, app_id, actor):
        """The single place that decides where an application should be.
        Priority: merchant action > work in progress > human judgement > approval."""
        app = self._app(app_id)
        if app["state"] not in EVALUABLE:
            return
        checks = self._checks(app_id)
        missing = self._open_missing(app_id)
        exceptions = self._open_exceptions(app_id)
        running = [t for t, c in checks.items() if c["status"] in RUNNING]
        jobs = self._q(f"""SELECT kind FROM jobs WHERE app_id=? AND status IN ('QUEUED','RUNNING')
                           AND kind IN ({','.join('?' * len(GATING_JOBS))})""", (app_id, *GATING_JOBS))
        blocked = [t for t, c in checks.items() if c["status"] == "BLOCKED"]
        required = {t: c for t, c in checks.items() if c["status"] != "NOT_REQUIRED" and t in self.cfg.required_checks}

        if missing:
            target, why = S.NEEDS_INFO, f"{len(missing)} item(s) needed from merchant"
        elif running or jobs:
            target, why = S.VERIFYING, "awaiting " + ", ".join(running + sorted({x["kind"] for x in jobs}))
        elif exceptions:
            target, why = S.MANUAL_REVIEW, "exceptions: " + ", ".join(sorted({e["code"] for e in exceptions}))
        elif blocked:
            target, why = S.VERIFYING, "checks blocked: " + ", ".join(blocked)
        elif all(c["status"] in ("PASSED", "WAIVED") for c in required.values()):
            target, why = S.APPROVED, "all configured checks passed" + (
                " (reviewer waivers applied)" if any(c["status"] == "WAIVED" for c in required.values()) else "")
        else:
            bad = [t for t, c in required.items() if c["status"] not in ("PASSED", "WAIVED")]
            self._add_exception(app_id, "decision", "UNRESOLVED_CHECKS", "MEDIUM",
                                f"Checks not passed: {', '.join(bad)}", key="UNRESOLVED")
            target, why = S.MANUAL_REVIEW, "unresolved checks"

        if target != app["state"]:
            self._transition(app_id, target, actor, why)
        elif target == S.NEEDS_INFO:
            self._maybe_notify_missing(app_id)
        if target == S.APPROVED:
            self._start_provisioning(app_id)

    def _on_state_entered(self, app_id, state):
        if state == S.NEEDS_INFO:
            self._maybe_notify_missing(app_id)
        elif state == S.ACTIVE:
            app = self._app(app_id)
            caps = uj(app["capabilities_json"], {})
            self._notify(app_id, "LIVE", "Your payments account is live",
                         f"Hello {app['legal_name']},\n\nYour merchant account {app['merchant_id']} is active. "
                         f"Enabled: {', '.join(caps.get('methods', []))}. Settlement: {caps.get('settlement')}.",
                         f"नमस्ते {app['legal_name']},\n\nआपका मर्चेंट अकाउंट {app['merchant_id']} चालू हो गया है।",
                         "template")
        elif state == S.REJECTED:
            app = self._app(app_id)
            # Neutral wording on purpose: never reveal fraud/risk signals to the applicant.
            self._notify(app_id, "REJECTED", "Update on your application",
                         f"Hello {app['legal_name']},\n\nWe are unable to approve your application at this time. "
                         f"This decision is based on our onboarding policy.",
                         f"नमस्ते {app['legal_name']},\n\nहम अभी आपका आवेदन स्वीकार नहीं कर पा रहे हैं।", "template")

    # ------------------------------------------------------------ notifications
    def _maybe_notify_missing(self, app_id):
        items = self._open_missing(app_id)
        keys = sorted(m["key"] for m in items)
        app = self._app(app_id)
        if not keys or j(keys) == app["last_notified_keys"]:
            return
        self._x("UPDATE applications SET last_notified_keys=?, reminders_sent=0, last_reminder_at=? WHERE id=?",
                (j(keys), self._now(), app_id))
        if self._one("SELECT 1 x FROM jobs WHERE app_id=? AND kind='notify_merchant' AND status='QUEUED'", (app_id,)):
            return  # a message is already queued; it reads the open items when it runs (debounce)
        self._enqueue("notify_merchant", app_id, "INFO_REQUEST", {"kind": "INFO_REQUEST"}, gating=False,
                      delay=self.cfg.notify_debounce_s)

    def _send_reminders(self):
        due = self._now() - self.cfg.needs_info_reminder_s
        for a in self._q("""SELECT id FROM applications WHERE state='NEEDS_INFO' AND last_reminder_at<=?
                            AND reminders_sent<?""", (due, self.cfg.max_reminders)):
            items = self._open_missing(a["id"])
            self._x("UPDATE applications SET reminders_sent=reminders_sent+1, last_reminder_at=? WHERE id=?",
                    (self._now(), a["id"]))
            self._audit(a["id"], "scheduler", "REMINDER_SCHEDULED")
            if items:
                self._enqueue("notify_merchant", a["id"], "REMINDER", {"kind": "REMINDER"}, gating=False)

    def _notify(self, app_id, kind, subject, body, body_hi, drafted_by):
        app = self._app(app_id)
        self._x("""INSERT INTO notifications(app_id,at,kind,channel,recipient,subject,body,body_hi,drafted_by)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (app_id, self._now(), kind, "email+sms (simulated)", mask("email", app["email"]), subject, body,
                 body_hi, drafted_by))
        self._audit(app_id, "system", "MERCHANT_NOTIFIED", kind=kind, drafted_by=drafted_by)

    # ------------------------------------------------------------ provisioning
    def _start_provisioning(self, app_id):
        self._transition(app_id, S.PROVISIONING, "system", "creating merchant ID")
        self._x("""INSERT OR REPLACE INTO provisioning(app_id,idempotency_key,status,attempts,updated_at)
                   VALUES (?,?,?,0,?)""", (app_id, f"mid-create-{app_id}", "PENDING", self._now()))
        self._attempt_provisioning(app_id)

    def _attempt_provisioning(self, app_id):
        pr = self._one("SELECT * FROM provisioning WHERE app_id=?", (app_id,))
        app = self._app(app_id)
        attempt, now = pr["attempts"] + 1, self._now()
        try:
            mid, replayed = self.mid.create(pr["idempotency_key"], app["legal_name"], now)
        except MIDServiceError as e:
            self._audit(app_id, "system", "MID_CREATION_FAILED", attempt=attempt, error=str(e))
            if attempt >= self.cfg.max_provision_attempts:
                self._x("UPDATE provisioning SET status='FAILED', attempts=?, last_error=?, updated_at=? WHERE app_id=?",
                        (attempt, str(e), now, app_id))
                self._x("""INSERT INTO dead_letters(app_id,kind,ref,error,detail_json,created_at)
                           VALUES (?,?,?,?,?,?)""", (app_id, "PROVISIONING", "MID", str(e), j({"attempts": attempt}), now))
                self._transition(app_id, S.PROVISIONING_FAILED, "system", f"merchant ID creation failed x{attempt}")
                self._add_exception(app_id, "provisioning", "MID_CREATION_FAILED", "HIGH",
                                    f"Approved, but merchant ID creation failed {attempt} times: {e}. "
                                    f"Retrying is safe (idempotent).", key="MID_FAILED")
            else:
                delay = self._backoff(attempt)
                self._x("""UPDATE provisioning SET status='RETRY_WAIT', attempts=?, next_attempt_at=?, last_error=?,
                           updated_at=? WHERE app_id=?""", (attempt, now + delay, str(e), now, app_id))
                self._audit(app_id, "system", "RETRY_SCHEDULED", step="MID_CREATE", next_attempt=attempt + 1,
                            in_seconds=delay)
            return
        caps = self._capabilities(app_id)
        self._x("UPDATE provisioning SET status='DONE', attempts=?, updated_at=? WHERE app_id=?", (attempt, now, app_id))
        self._x("UPDATE applications SET merchant_id=?, capabilities_json=? WHERE id=?", (mid, j(caps), app_id))
        self._audit(app_id, "system", "MERCHANT_ID_CREATED", merchant_id=mid, attempt=attempt,
                    idempotent_replay=replayed)
        self._audit(app_id, "system", "PAYMENT_CAPABILITIES_CONFIGURED", **caps)
        self._transition(app_id, S.ACTIVE, "system", f"merchant {mid} live")

    def _capabilities(self, app_id):
        app = self._app(app_id)
        cat = uj(app["ai_category_json"], {}) or {}
        risk = self._check(app_id, "RISK")
        waived_restricted = self._one("""SELECT 1 x FROM exceptions WHERE app_id=? AND code='CATEGORY_MISMATCH'
                                         AND resolved_by<>'system'""", (app_id,))
        mcc = cat.get("mcc") or "5999"
        if risk["status"] == "WAIVED" or waived_restricted:
            return {"methods": ["UPI"], "daily_limit_inr": 50000, "settlement": "T+3", "profile": "RESTRICTED",
                    "mcc": mcc, "monitoring": "enhanced"}
        return {"methods": ["UPI", "CARDS", "NETBANKING"], "daily_limit_inr": 500000, "settlement": "T+1",
                "profile": "STANDARD", "mcc": mcc, "monitoring": "standard"}
