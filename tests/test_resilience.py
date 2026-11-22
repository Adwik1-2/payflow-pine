"""Concurrency, crash/restart recovery and a short chaos soak against the
real multi-threaded server. Every run ends with the invariant check."""
import random
import threading
import time

import pytest

from app import scenarios
from tests.invariants import check_invariants
from tests.live_server import OPS, STAFF, LiveServer

SCEN = list(scenarios.SCENARIOS)


def settle(srv, timeout=40):
    """Wait until no application is in a transient state."""
    end = time.time() + timeout
    while time.time() < end:
        _, apps = srv.req("GET", "/api/applications", headers=STAFF)
        if all(a["state"] in ("NEEDS_INFO", "MANUAL_REVIEW", "ACTIVE", "REJECTED", "PROVISIONING_FAILED") for a in apps):
            time.sleep(1.0)   # let late callbacks / jobs land
            return apps
        time.sleep(0.3)
    raise AssertionError("did not settle: " + str({a["id"]: a["state"] for a in apps if a["state"] not in
                                                   ("NEEDS_INFO", "MANUAL_REVIEW", "ACTIVE", "REJECTED", "PROVISIONING_FAILED")}))


def all_normal(srv):
    for t in ("PAN", "GST", "BANK", "RISK", "MID", "LLM"):
        srv.req("POST", "/api/modes", {"target": t, "mode": "normal"}, headers=OPS)


def test_parallel_submissions_and_duplicate_callbacks():
    srv = LiveServer()
    try:
        srv.req("POST", "/api/modes", {"target": "PAN", "mode": "duplicate"}, headers=OPS)
        srv.req("POST", "/api/modes", {"target": "BANK", "mode": "duplicate"}, headers=OPS)
        errors, ids = [], []

        def worker(i):
            try:
                code, out = srv.req("POST", f"/api/scenarios/{SCEN[i % len(SCEN)]}", {}, headers=STAFF)
                assert code == 201, out
                ids.append(out["id"])
                for _ in range(3):   # merchants and reviewers hammering reads at the same time
                    srv.req("GET", f"/api/applications/{out['id']}", headers=STAFF)
                    srv.req("GET", "/api/metrics", headers=STAFF)
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        settle(srv)
        assert check_invariants(srv.engine) == []
        _, m = srv.req("GET", "/api/metrics", headers=STAFF)
        assert m["duplicate_deliveries_ignored"] > 0
    finally:
        srv.stop()


def test_restart_mid_flight_recovers():
    srv = LiveServer()
    try:
        srv.req("POST", "/api/modes", {"target": "BANK", "mode": "slow"}, headers=OPS)   # keep work in flight
        ids = [srv.req("POST", "/api/scenarios/happy_path", {}, headers=STAFF)[1]["id"] for _ in range(5)]
        time.sleep(0.5)
        srv.restart()   # simulator outbox is lost; queued jobs / in-flight checks must recover from the DB
        all_normal(srv)
        settle(srv, timeout=60)
        assert check_invariants(srv.engine) == []
        for i in ids:
            assert srv.req("GET", f"/api/applications/{i}", headers=STAFF)[1]["state"] == "ACTIVE"
    finally:
        srv.stop()


def test_reviewers_racing_on_the_same_case():
    srv = LiveServer()
    try:
        _, out = srv.req("POST", "/api/scenarios/high_risk", {}, headers=STAFF)
        srv.wait_state(out["id"], {"MANUAL_REVIEW"})
        path = f"/api/applications/{out['id']}/actions"
        results = []

        def act(who, action):
            results.append(srv.req("POST", path, {"action": action, "reason": "race"},
                                   headers={"X-Role": "reviewer", "X-Actor": who})[0])
        ts = [threading.Thread(target=act, args=(w, a)) for w, a in
              [("a", "approve"), ("b", "approve"), ("c", "reject"), ("d", "claim"), ("e", "approve")]]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert all(c in (200, 409) for c in results), results
        assert check_invariants(srv.engine, settled=False) == []
    finally:
        srv.stop()


@pytest.mark.parametrize("seed", [1, 2])
def test_chaos_soak(seed):
    """~20 seconds of random applications, random faults (providers, MID, LLM),
    random merchant and reviewer actions. Then everything back to normal and
    the database must be consistent."""
    rng = random.Random(seed)
    srv = LiveServer()
    modes = {"PAN": ["normal", "slow", "duplicate", "outage", "flaky", "error"],
             "GST": ["normal", "slow", "duplicate", "outage", "flaky", "error"],
             "BANK": ["normal", "slow", "duplicate", "outage", "flaky", "error"],
             "RISK": ["normal", "slow", "duplicate", "outage", "flaky", "error"],
             "MID": ["normal", "fail_once", "timeout_after_commit", "fail_always"],
             "LLM": ["normal", "outage", "garbage"]}
    reviewers = ["reviewer.anita", "reviewer.vikram"]
    try:
        tokens = {}
        end = time.time() + 20
        while time.time() < end:
            r = rng.random()
            if r < 0.30:
                code, out = srv.req("POST", f"/api/scenarios/{rng.choice(SCEN)}", {}, headers=STAFF)
                assert code == 201
                tokens[out["id"]] = out["token"]
            elif r < 0.45:
                t = rng.choice(list(modes))
                assert srv.req("POST", "/api/modes", {"target": t, "mode": rng.choice(modes[t])}, headers=OPS)[0] == 200
            elif r < 0.85 and tokens:
                app = rng.choice(list(tokens))
                action = rng.choice(["approve", "confirm_approval", "reject", "request_info", "retry_check",
                                     "retry_provisioning", "claim", "release"])
                code, _ = srv.req("POST", f"/api/applications/{app}/actions",
                                  {"action": action, "reason": "chaos", "message": "please re-check",
                                   "check_type": rng.choice(["PAN", "GST", "BANK", "RISK"])},
                                  headers={"X-Role": "reviewer", "X-Actor": rng.choice(reviewers)})
                assert code in (200, 409), code
            elif tokens:
                app = rng.choice(list(tokens))
                kind = rng.choice(["reupload", "change_bank", "correct_field"])
                body = {"kind": kind, "doc_type": rng.choice(["PAN_CARD", "CANCELLED_CHEQUE", "GST_CERTIFICATE"]),
                        "bank_account": str(rng.randint(10**11, 10**12)), "field": rng.choice(["pan", "gstin"]),
                        "value": "auto"}
                code, res = srv.req("POST", f"/api/applications/{app}/simulate-merchant", body, headers=STAFF)
                assert code in (200, 400, 409), (code, res)
            time.sleep(rng.uniform(0.01, 0.08))
        all_normal(srv)
        # clear what retries could not: replay dead letters, retry failed provisioning
        for _ in range(3):
            settle(srv, timeout=60)
            _, dl = srv.req("GET", "/api/dead-letters", headers=STAFF)
            for d in dl:
                if not d["resolved_at"]:
                    srv.req("POST", f"/api/dead-letters/{d['id']}/replay", {}, headers=STAFF)
        apps = settle(srv, timeout=60)
        v = check_invariants(srv.engine)
        assert v == [], v
        assert len(apps) >= 10
        for p in ("/api/metrics", "/api/review-queue", "/api/activity"):
            assert srv.req("GET", p, headers=STAFF)[0] == 200
    finally:
        srv.stop()
