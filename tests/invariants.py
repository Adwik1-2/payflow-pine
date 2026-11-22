"""Consistency rules the database must satisfy once the system is quiet.
Used by the soak / concurrency / restart tests."""
import json

from app.states import TRANSITIONS

SETTLED = {"NEEDS_INFO", "MANUAL_REVIEW", "ACTIVE", "REJECTED", "PROVISIONING_FAILED"}
RUNNING = {"PENDING", "DISPATCHING", "IN_FLIGHT", "RETRY_WAIT", "ADJUDICATING"}


def check_invariants(engine, settled=True):
    """Returns a list of violations (empty = healthy)."""
    v = []
    with engine._lock:
        q = engine._q
        apps = q("SELECT * FROM applications")
        for a in apps:
            aid, st = a["id"], a["state"]
            checks = {c["check_type"]: c for c in q("SELECT * FROM checks WHERE app_id=?", (aid,))}
            open_ex = q("SELECT * FROM exceptions WHERE app_id=? AND resolved_at IS NULL", (aid,))
            open_mi = q("SELECT * FROM missing_items WHERE app_id=? AND resolved_at IS NULL", (aid,))
            hist = q("SELECT * FROM state_history WHERE app_id=? ORDER BY id", (aid,))
            if len(checks) != 4:
                v.append(f"{aid}: has {len(checks)} checks")
            if not hist or hist[-1]["to_state"] != st:
                v.append(f"{aid}: state {st} disagrees with history")
            for h in hist[1:]:
                if h["to_state"] not in TRANSITIONS.get(h["from_state"], set()):
                    v.append(f"{aid}: illegal transition {h['from_state']}->{h['to_state']}")
            if settled and st not in SETTLED:
                v.append(f"{aid}: not settled ({st})")
            if st == "ACTIVE":
                if not a["merchant_id"]:
                    v.append(f"{aid}: ACTIVE without merchant id")
                for t in ("PAN", "BANK"):
                    if checks[t]["status"] != "PASSED":
                        v.append(f"{aid}: ACTIVE but {t} is {checks[t]['status']}")
                for t, c in checks.items():
                    if c["status"] not in ("PASSED", "WAIVED", "NOT_REQUIRED"):
                        v.append(f"{aid}: ACTIVE but {t} is {c['status']}")
                if open_ex or open_mi:
                    v.append(f"{aid}: ACTIVE with open exceptions/missing items")
            if st == "NEEDS_INFO" and not open_mi:
                v.append(f"{aid}: NEEDS_INFO with nothing asked")
            if st == "MANUAL_REVIEW":
                if not open_ex:
                    v.append(f"{aid}: MANUAL_REVIEW without an exception")
                if open_mi:
                    v.append(f"{aid}: MANUAL_REVIEW with open merchant items")
            if st in ("MANUAL_REVIEW", "NEEDS_INFO") and settled:
                busy = [t for t, c in checks.items() if c["status"] in RUNNING]
                if busy:
                    v.append(f"{aid}: {st} with checks still running {busy}")
            if st in ("ACTIVE", "REJECTED") and q("SELECT 1 FROM provider_requests WHERE app_id=? AND status='QUEUED'", (aid,)):
                v.append(f"{aid}: closed but has queued provider requests")
            if st == "PROVISIONING_FAILED" and not open_ex:
                v.append(f"{aid}: PROVISIONING_FAILED without an exception")
            pend = q("SELECT COUNT(*) n FROM approvals WHERE app_id=? AND status='PENDING'", (aid,))[0]["n"]
            if pend > 1 or (pend and st != "MANUAL_REVIEW"):
                v.append(f"{aid}: {pend} pending approvals in {st}")
            blob = json.dumps(q("SELECT detail_json FROM audit WHERE app_id=?", (aid,)))
            for k in ("pan", "bank_account", "gstin"):
                if a[k] and a[k] in blob:
                    v.append(f"{aid}: raw {k} in audit trail")
        mids = [a["merchant_id"] for a in apps if a["merchant_id"]]
        if len(mids) != len(set(mids)):
            v.append("duplicate merchant ids")
        reg = q("SELECT COUNT(*) n FROM mid_registry")[0]["n"]
        if reg != len(q("SELECT DISTINCT app_id FROM provisioning WHERE status IN ('DONE','FAILED','RETRY_WAIT','PENDING') "
                         "AND app_id IN (SELECT app_id FROM provisioning)")) and reg > len(apps):
            v.append("more merchant-id registry rows than applications")
        if q("SELECT 1 FROM callback_inbox WHERE outcome='PROCESSING'"):
            v.append("callback left in PROCESSING")
        if settled:
            if q("SELECT 1 FROM jobs WHERE status IN ('RUNNING') "):
                v.append("jobs still RUNNING")
            if q("SELECT 1 FROM provider_requests WHERE status='QUEUED'"):
                v.append("provider requests still QUEUED")
    return v
