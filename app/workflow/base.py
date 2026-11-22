"""Core of the workflow engine: storage, transactions, state machine,
audit trail and the bookkeeping for missing items and exceptions."""
import functools
import json
import re
import sqlite3
import threading
from typing import Optional

from ..privacy import scrub_text
from ..states import S, assert_transition

CHECK_TYPES = ("PAN", "GST", "BANK", "RISK")
RUNNING = ("PENDING", "DISPATCHING", "IN_FLIGHT", "RETRY_WAIT", "ADJUDICATING")
FORM_FIELDS = ("legal_name", "trade_name", "business_type", "category", "gst_registered", "email", "phone",
               "pan", "gstin", "bank_account", "ifsc", "account_holder", "business_description", "website")
CHECK_INPUTS = {
    "PAN": ("pan", "legal_name"),
    "GST": ("gstin", "legal_name", "pan"),
    "BANK": ("bank_account", "ifsc", "account_holder", "legal_name", "trade_name"),
    "RISK": ("legal_name", "category", "email", "phone", "pan"),
}
UPDATABLE_STATES = (S.SUBMITTED, S.VERIFYING, S.NEEDS_INFO, S.MANUAL_REVIEW)
GATING_JOBS = ("extract_document", "categorize", "adjudicate_bank_name")

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications(
  id TEXT PRIMARY KEY, created_at REAL, updated_at REAL, state TEXT, state_entered_at REAL,
  legal_name TEXT, trade_name TEXT, business_type TEXT, category TEXT, gst_registered INTEGER,
  email TEXT, phone TEXT, pan TEXT, gstin TEXT, bank_account TEXT, ifsc TEXT, account_holder TEXT,
  business_description TEXT, website TEXT,
  pan_fp TEXT, gstin_fp TEXT, bank_fp TEXT, contact_fp TEXT, access_token_hash TEXT,
  extracted_json TEXT, ai_category_json TEXT, merchant_id TEXT, capabilities_json TEXT,
  assigned_to TEXT, claimed_at REAL, last_notified_keys TEXT, last_reminder_at REAL,
  reminders_sent INTEGER DEFAULT 0, channel TEXT,
  ever_reviewed INTEGER DEFAULT 0, ever_needs_info INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY AUTOINCREMENT, app_id TEXT, doc_type TEXT, filename TEXT, mime TEXT, size INTEGER,
  sha256 TEXT, content TEXT, blob BLOB, sidecar_json TEXT, quality REAL, status TEXT, extraction_json TEXT,
  uploaded_at REAL, superseded INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS checks(
  app_id TEXT, check_type TEXT, status TEXT, fingerprint TEXT, request_id TEXT,
  attempts INTEGER DEFAULT 0, next_attempt_at REAL, deadline_at REAL, started_at REAL,
  completed_at REAL, reason_code TEXT, category TEXT, result_json TEXT,
  PRIMARY KEY(app_id, check_type));
CREATE TABLE IF NOT EXISTS provider_requests(
  request_id TEXT PRIMARY KEY, app_id TEXT, check_type TEXT, fingerprint TEXT,
  attempt INTEGER, sent_at REAL, status TEXT, resolved_at REAL);
CREATE TABLE IF NOT EXISTS callback_inbox(
  event_id TEXT PRIMARY KEY, provider TEXT, request_id TEXT, app_id TEXT,
  received_at REAL, outcome TEXT, delivery_count INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS missing_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, app_id TEXT, key TEXT, code TEXT, field TEXT,
  message TEXT, source TEXT, blocks_json TEXT, created_at REAL, resolved_at REAL, resolution TEXT);
CREATE TABLE IF NOT EXISTS exceptions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, app_id TEXT, key TEXT, code TEXT, severity TEXT,
  message TEXT, source TEXT, detail_json TEXT, created_at REAL, resolved_at REAL,
  resolved_by TEXT, resolution TEXT);
CREATE TABLE IF NOT EXISTS dead_letters(
  id INTEGER PRIMARY KEY AUTOINCREMENT, app_id TEXT, kind TEXT, ref TEXT, error TEXT,
  detail_json TEXT, created_at REAL, resolved_at REAL, resolution TEXT);
CREATE TABLE IF NOT EXISTS provisioning(
  app_id TEXT PRIMARY KEY, idempotency_key TEXT, status TEXT, attempts INTEGER,
  next_attempt_at REAL, last_error TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS mid_registry(idempotency_key TEXT PRIMARY KEY, merchant_id TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS state_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT, app_id TEXT, from_state TEXT, to_state TEXT,
  at REAL, actor TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, app_id TEXT, at REAL, actor TEXT, event TEXT, detail_json TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, app_id TEXT, ref TEXT, payload_json TEXT, status TEXT,
  attempts INTEGER DEFAULT 0, run_after REAL, created_at REAL, started_at REAL, finished_at REAL,
  error TEXT, gating INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS llm_calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, feature TEXT, app_id TEXT, provider TEXT, model TEXT,
  attempt INTEGER, status TEXT, latency_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER,
  cost_usd REAL, error TEXT);
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT, app_id TEXT, at REAL, kind TEXT, channel TEXT, recipient TEXT,
  subject TEXT, body TEXT, body_hi TEXT, drafted_by TEXT);
CREATE TABLE IF NOT EXISTS approvals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, app_id TEXT, requested_by TEXT, reason TEXT, requested_at REAL,
  snapshot_json TEXT, status TEXT, decided_by TEXT, decided_at REAL, decision_reason TEXT);
CREATE TABLE IF NOT EXISTS copilot_briefs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, app_id TEXT, at REAL, requested_by TEXT, brief_json TEXT,
  feedback TEXT, feedback_by TEXT, feedback_at REAL, final_decision TEXT);
CREATE INDEX IF NOT EXISTS ix_audit_app ON audit(app_id);
CREATE INDEX IF NOT EXISTS ix_req_app ON provider_requests(app_id, check_type);
CREATE INDEX IF NOT EXISTS ix_jobs ON jobs(status, run_after);
CREATE INDEX IF NOT EXISTS ix_docs_sha ON documents(sha256);
"""


class ActionError(Exception):
    """Well-formed request that is not allowed right now (HTTP 409)."""


class NotFound(Exception):
    pass


def j(x):
    return json.dumps(x, sort_keys=True, default=str)


def uj(s, default=None):
    return json.loads(s) if s else default


def transactional(fn):
    """One lock + one SQLite transaction per public operation. After the
    outermost commit, queued provider requests are sent (transactional outbox:
    a request is only sent once the state that expects its answer is durable)."""
    @functools.wraps(fn)
    def wrapper(self, *a, **k):
        with self._lock:
            outer = self._depth == 0
            self._depth += 1
            try:
                result = fn(self, *a, **k)
                if outer:
                    self.db.commit()
            except Exception:
                if outer:
                    self.db.rollback()
                raise
            finally:
                self._depth -= 1
            if outer and self._outbox_dirty and not self._flushing:
                self._flushing = True
                try:
                    self.flush_outbox()
                finally:
                    self._flushing = False
            return result
    return wrapper


MAX_FIELD_LEN = 200


def normalize_form(form: dict, base: Optional[dict] = None) -> dict:
    if not isinstance(form, dict):
        raise ValueError("form must be an object")
    f = dict(base or {})
    for k in FORM_FIELDS:
        if k in form:
            v = form[k]
            if isinstance(v, (dict, list)):
                raise ValueError(f"{k} must be a string")
            if isinstance(v, str) and k != "business_description":
                v = v[:MAX_FIELD_LEN]
            f[k] = v
    for k in ("pan", "gstin", "ifsc"):
        if f.get(k):
            f[k] = re.sub(r"\s", "", str(f[k])).upper()
    if f.get("bank_account"):
        f["bank_account"] = re.sub(r"\D", "", str(f["bank_account"]))
    if f.get("phone"):
        f["phone"] = re.sub(r"\D", "", str(f["phone"]))[-10:]
    if f.get("email"):
        f["email"] = str(f["email"]).strip().lower()
    if f.get("category"):
        f["category"] = str(f["category"]).strip().lower()
    for k in ("legal_name", "trade_name", "account_holder", "business_type", "website"):
        if f.get(k):
            f[k] = " ".join(str(f[k]).split())
    if f.get("business_description"):
        f["business_description"] = str(f["business_description"]).strip()[:1000]
    g = f.get("gst_registered", True)
    f["gst_registered"] = g if isinstance(g, bool) else str(g).lower() in ("1", "true", "yes", "on")
    if not f["gst_registered"]:
        f["gstin"] = None
    return f


class Base:
    def __init__(self, db_path, cfg, clock, simulator, mid_service_factory, gateway):
        self.cfg, self.clock, self.sim, self.gw = cfg, clock, simulator, gateway
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.mid = mid_service_factory(self.db)
        self._lock = threading.RLock()
        self._depth = 0
        self._outbox_dirty = False
        self._flushing = False
        self.gw.log = self._log_llm_call
        # Single process owns this database: anything marked RUNNING belonged to a
        # previous process that stopped, so put it back in the queue.
        self.db.execute("UPDATE jobs SET status='QUEUED', run_after=0 WHERE status='RUNNING'")
        self.db.commit()

    # ------------------------------------------------------------ helpers
    def _q(self, sql, p=()):
        return [dict(r) for r in self.db.execute(sql, p).fetchall()]

    def _one(self, sql, p=()):
        r = self.db.execute(sql, p).fetchone()
        return dict(r) if r else None

    def _x(self, sql, p=()):
        return self.db.execute(sql, p)

    def _now(self):
        return self.clock.now()

    def _app(self, app_id):
        a = self._one("SELECT * FROM applications WHERE id=?", (app_id,))
        if not a:
            raise NotFound(app_id)
        return a

    def _form(self, app):
        f = {k: app.get(k) for k in FORM_FIELDS}
        f["gst_registered"] = bool(f["gst_registered"])
        return f

    def _check(self, app_id, t):
        return self._one("SELECT * FROM checks WHERE app_id=? AND check_type=?", (app_id, t))

    def _checks(self, app_id):
        return {c["check_type"]: c for c in self._q("SELECT * FROM checks WHERE app_id=?", (app_id,))}

    def _audit(self, app_id, actor, event, **detail):
        self._x("INSERT INTO audit(app_id,at,actor,event,detail_json) VALUES (?,?,?,?,?)",
                (app_id, self._now(), actor, event, scrub_text(j(detail))))

    def _backoff(self, attempt):
        return self.cfg.retry_backoff_base_s * (2 ** max(attempt - 1, 0))

    def _log_llm_call(self, rec):
        # Called from job threads without the engine lock: use a short write under the lock.
        with self._lock:
            self._x("""INSERT INTO llm_calls(at,feature,app_id,provider,model,attempt,status,latency_ms,
                       input_tokens,output_tokens,cost_usd,error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (self._now(), rec.get("feature"), rec.get("app_id"), rec.get("provider"), rec.get("model"),
                     rec.get("attempt"), rec.get("status"), rec.get("latency_ms"), rec.get("input_tokens"),
                     rec.get("output_tokens"), rec.get("cost_usd"), rec.get("error")))
            if self._depth == 0:
                self.db.commit()

    # ------------------------------------------------------------ state machine
    def _transition(self, app_id, to, actor, reason):
        app = self._app(app_id)
        frm = app["state"]
        if frm == to:
            return
        assert_transition(frm, to)
        if to == S.APPROVED:
            self._approval_guard(app_id)
        now = self._now()
        self._x("""UPDATE applications SET state=?, state_entered_at=?, updated_at=?,
                   ever_reviewed = ever_reviewed OR ?, ever_needs_info = ever_needs_info OR ?
                   WHERE id=?""",
                (to, now, now, int(to == S.MANUAL_REVIEW), int(to == S.NEEDS_INFO), app_id))
        if frm == S.MANUAL_REVIEW:
            self._x("UPDATE applications SET assigned_to=NULL, claimed_at=NULL WHERE id=?", (app_id,))
        self._x("INSERT INTO state_history(app_id,from_state,to_state,at,actor,reason) VALUES (?,?,?,?,?,?)",
                (app_id, frm, to, now, actor, reason))
        self._audit(app_id, actor, "STATE_CHANGED", from_state=frm, to_state=to, reason=reason)
        self._on_state_entered(app_id, to)

    def _approval_guard(self, app_id):
        """Hard gate in front of APPROVED: every configured check PASSED, or
        WAIVED where waiving is allowed; nothing open."""
        for t, c in self._checks(app_id).items():
            if c["status"] == "NOT_REQUIRED" or t not in self.cfg.required_checks:
                continue
            ok = c["status"] == "PASSED" or (c["status"] == "WAIVED" and t not in self.cfg.non_overridable_checks)
            if not ok:
                raise ActionError(f"Approval blocked: {t} check is {c['status']}"
                                  + (" and cannot be waived" if t in self.cfg.non_overridable_checks else ""))
        if self._open_missing(app_id) or self._open_exceptions(app_id):
            raise ActionError("Approval blocked: open missing items or exceptions")

    # ------------------------------------------------------------ missing items & exceptions
    def _open_missing(self, app_id, source=None):
        sql, p = "SELECT * FROM missing_items WHERE app_id=? AND resolved_at IS NULL", [app_id]
        if source:
            sql += " AND source=?"
            p.append(source)
        return self._q(sql, p)

    def _open_exceptions(self, app_id):
        return self._q("SELECT * FROM exceptions WHERE app_id=? AND resolved_at IS NULL", (app_id,))

    def _add_missing(self, app_id, source, item):
        key = item.get("key") or f"{item['code']}:{item['field']}"
        self._x("""INSERT INTO missing_items(app_id,key,code,field,message,source,blocks_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (app_id, key, item["code"], item["field"], item["message"], source,
                 j(item.get("blocks", [])), self._now()))
        self._audit(app_id, item.get("actor", "system"), "INFO_REQUESTED", code=item["code"], field=item["field"],
                    message=item["message"])

    def _add_exception(self, app_id, source, code, severity, message, detail=None, key=None):
        key = key or code
        self._x("""INSERT INTO exceptions(app_id,key,code,severity,message,source,detail_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (app_id, key, code, severity, scrub_text(message), source, j(detail or {}), self._now()))
        self._audit(app_id, "system", "EXCEPTION_RAISED", code=code, severity=severity, message=message)
        self._invalidate_pending_approval(app_id, f"new exception {code}")

    def _resolve_missing(self, app_id, source, resolution, key=None):
        sql = "UPDATE missing_items SET resolved_at=?, resolution=? WHERE app_id=? AND source=? AND resolved_at IS NULL"
        p = [self._now(), resolution, app_id, source]
        if key:
            sql += " AND key=?"
            p.append(key)
        self._x(sql, p)

    def _resolve_exceptions(self, app_id, by, resolution, source=None, key=None, ids=None):
        sql = "UPDATE exceptions SET resolved_at=?, resolved_by=?, resolution=? WHERE app_id=? AND resolved_at IS NULL"
        p = [self._now(), by, resolution, app_id]
        if source:
            sql += " AND source=?"
            p.append(source)
        if key:
            sql += " AND key=?"
            p.append(key)
        if ids is not None:
            sql += f" AND id IN ({','.join('?' * len(ids)) or 'NULL'})"
            p.extend(ids)
        n = self._x(sql, p).rowcount
        if n:
            self._audit(app_id, by, "EXCEPTIONS_RESOLVED", count=n, source=source, resolution=resolution)

    def _resolve_dead_letters(self, app_id, kind, ref, resolution):
        self._x("""UPDATE dead_letters SET resolved_at=?, resolution=? WHERE app_id=? AND kind=? AND ref=?
                   AND resolved_at IS NULL""", (self._now(), resolution, app_id, kind, ref))

    def _sync_missing(self, app_id, source, new_items):
        existing = {m["key"]: m for m in self._open_missing(app_id, source)}
        for key in existing.keys() - new_items.keys():
            self._resolve_missing(app_id, source, "fixed by merchant", key=key)
            self._audit(app_id, "system", "INFO_RECEIVED", key=key)
        for key in new_items.keys() - existing.keys():
            self._add_missing(app_id, source, {**new_items[key], "key": key})

    def _sync_exceptions(self, app_id, source, new_items):
        existing = {e["key"]: e for e in self._open_exceptions(app_id) if e["source"] == source}
        for key in existing.keys() - new_items.keys():
            self._resolve_exceptions(app_id, "system", "condition no longer present", source=source, key=key)
        for key in new_items.keys() - existing.keys():
            e = new_items[key]
            self._add_exception(app_id, source, e["code"], e["severity"], e["message"], e.get("detail"), key=key)

    def _invalidate_pending_approval(self, app_id, why):
        n = self._x("""UPDATE approvals SET status='STALE', decided_at=?, decision_reason=? WHERE app_id=?
                       AND status='PENDING'""", (self._now(), why, app_id)).rowcount
        if n:
            self._audit(app_id, "system", "APPROVAL_INVALIDATED", reason=why)

    def _on_state_entered(self, app_id, state):  # overridden in decisions mixin
        pass
