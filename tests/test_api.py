"""HTTP-level tests against the real server: every route, auth, bad input.
The rule under test: the API never answers 5xx and never drops a connection."""
import base64
import json
import random

import pytest

from app import scenarios
from app.providers import sign
from tests.live_server import OPS, STAFF, LiveServer


@pytest.fixture(scope="module")
def srv():
    s = LiveServer()
    yield s
    s.stop()


def ok_form(rng=None):
    return scenarios.build("happy_path", rng or random.Random(1))


# ------------------------------------------------------------ happy path over HTTP
def test_full_flow_over_http(srv):
    form, docs = ok_form()
    code, out = srv.req("POST", "/api/merchant/applications", {"form": form, "documents": docs})
    assert code == 201 and out["token"]
    a = srv.wait_state(out["id"], {"ACTIVE"})
    assert a["merchant_id"]
    code, v = srv.req("GET", f"/api/merchant/applications/{out['id']}", headers={"X-App-Token": out["token"]})
    assert code == 200 and v["status"] == "You're live"


def test_pages_and_unknown_routes(srv):
    assert srv.req("GET", "/")[0] == 200
    assert srv.req("GET", "/apply")[0] == 200
    assert srv.req("GET", "/nope")[0] == 404
    assert srv.req("GET", "/api/nope", headers=STAFF)[0] == 404
    assert srv.req("GET", "/../../etc/passwd")[0] == 404


# ------------------------------------------------------------ auth
@pytest.mark.parametrize("method,path", [
    ("GET", "/api/applications"), ("GET", "/api/applications/APP-00000000"), ("GET", "/api/metrics"),
    ("GET", "/api/review-queue"), ("GET", "/api/dead-letters"), ("GET", "/api/activity"), ("GET", "/api/modes"),
    ("POST", "/api/scenarios/happy_path"), ("POST", "/api/applications/APP-00000000/actions"),
    ("POST", "/api/modes"), ("POST", "/api/dead-letters/1/replay"),
])
def test_staff_routes_need_a_role(srv, method, path):
    assert srv.req(method, path, {})[0] == 403


def test_ops_only_and_reviewer_only(srv):
    assert srv.req("POST", "/api/modes", {"target": "PAN", "mode": "normal"}, headers=STAFF)[0] == 403
    assert srv.req("POST", "/api/applications/APP-00000000/actions", {"action": "approve"}, headers=OPS)[0] == 403


def test_merchant_token_required(srv):
    form, docs = ok_form(random.Random(2))
    _, out = srv.req("POST", "/api/merchant/applications", {"form": form, "documents": docs})
    path = f"/api/merchant/applications/{out['id']}"
    assert srv.req("GET", path)[0] == 404
    assert srv.req("GET", path, headers={"X-App-Token": "wrong"})[0] == 404
    assert srv.req("GET", "/api/merchant/applications/APP-FFFFFFFF", headers={"X-App-Token": out["token"]})[0] == 404
    assert srv.req("GET", path, headers={"X-App-Token": out["token"]})[0] == 200


# ------------------------------------------------------------ malformed requests
def test_malformed_bodies(srv):
    p = "/api/merchant/applications"
    assert srv.req("POST", p, raw=b"{not json")[0] == 400
    assert srv.req("POST", p, raw=b"[1,2,3]")[0] == 400
    assert srv.req("POST", p, raw=b'"text"')[0] == 400
    assert srv.req("POST", p, {"form": [1, 2], "documents": []})[0] == 400
    assert srv.req("POST", p, {"form": {}, "documents": "nope"})[0] in (400, 409)
    assert srv.req("POST", p, {"form": {"legal_name": {"x": 1}}, "documents": []})[0] == 400
    assert srv.req("POST", p, {"form": {}, "documents": [1, "x", None]})[0] == 409
    assert srv.req("POST", p, {"form": {}, "documents": [{"doc_type": ["PAN_CARD"]}]})[0] == 409
    assert srv.req("POST", p, {"form": {}, "documents": [{"doc_type": "PASSPORT"}]})[0] == 409
    assert srv.req("POST", p, {"form": {}, "documents": [{"doc_type": "PAN_CARD", "quality": "high"}]})[0] == 409
    assert srv.req("POST", p, {"form": {}, "documents": [{"doc_type": "PAN_CARD", "text": 12}]})[0] == 409


def test_bad_uploads(srv):
    p = "/api/merchant/applications"
    form, _ = ok_form(random.Random(3))
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 50).decode()
    cases = [
        {"doc_type": "PAN_CARD", "file_b64": "!!!not base64!!!", "mime": "image/png"},
        {"doc_type": "PAN_CARD", "file_b64": png, "mime": "application/x-msdownload"},
        {"doc_type": "PAN_CARD", "file_b64": base64.b64encode(b"MZ executable").decode(), "mime": "image/png"},
        {"doc_type": "PAN_CARD", "file_b64": 12345, "mime": "image/png"},
        {"doc_type": "PAN_CARD", "file_b64": base64.b64encode(b"\x89PNG" + b"0" * (6 * 1024 * 1024)).decode(), "mime": "image/png"},
    ]
    for d in cases:
        code, out = srv.req("POST", p, {"form": form, "documents": [d]})
        assert code == 409, (d.get("mime"), code, out)
    assert srv.req("POST", p, raw=b"x" * 10, headers={"Content-Length": "abc"})[0] in (400, 413)


def test_review_actions_validate_input(srv):
    _, out = srv.req("POST", "/api/scenarios/gst_cancelled", {}, headers=STAFF)
    app = out["id"]
    srv.wait_state(app, {"MANUAL_REVIEW"})
    path = f"/api/applications/{app}/actions"
    assert srv.req("POST", path, {"action": "fly"}, headers=STAFF)[0] == 409
    assert srv.req("POST", path, {"action": "approve", "reason": 42}, headers=STAFF)[0] == 409
    assert srv.req("POST", path, {"action": "retry_check", "check_type": ["GST"]}, headers=STAFF)[0] == 409
    assert srv.req("POST", path, {"action": "retry_provisioning"}, headers=STAFF)[0] == 409
    assert srv.req("POST", path, {"action": "confirm_approval"}, headers=STAFF)[0] == 409
    assert srv.req("POST", f"/api/applications/{app}/reveal", {"field": "password", "purpose": "x"}, headers=STAFF)[0] == 409
    assert srv.req("POST", f"/api/applications/{app}/reveal", {"field": "pan"}, headers=STAFF)[0] == 409
    assert srv.req("POST", f"/api/applications/{app}/documents/999999/verify", {"values": {}}, headers=STAFF)[0] == 409
    assert srv.req("GET", f"/api/applications/{app}/documents/999999/file", headers=STAFF)[0] == 404
    assert srv.req("POST", f"/api/applications/{app}/copilot/999/feedback", {"feedback": "meh"}, headers=STAFF)[0] == 409
    assert srv.req("POST", "/api/applications/APP-FFFFFFFF/actions", {"action": "approve"}, headers=STAFF)[0] == 404
    assert srv.req("POST", "/api/dead-letters/999999/replay", {}, headers=STAFF)[0] == 409
    assert srv.req("POST", "/api/modes", {"target": "PAN", "mode": "explode"}, headers=OPS)[0] == 400
    assert srv.req("POST", "/api/modes", {"target": None, "mode": None}, headers=OPS)[0] == 400
    assert srv.req("POST", "/api/scenarios/does_not_exist", {}, headers=STAFF)[0] == 400
    code, _ = srv.req("POST", f"/api/applications/{app}/simulate-merchant", {"kind": "correct_field", "field": "pan"}, headers=STAFF)
    assert code == 400


def test_document_of_another_application_is_not_served(srv):
    _, a = srv.req("POST", "/api/scenarios/happy_path", {}, headers=STAFF)
    _, b = srv.req("POST", "/api/scenarios/happy_path", {}, headers=STAFF)
    _, va = srv.req("GET", f"/api/applications/{a['id']}", headers=STAFF)
    doc = va["documents"][0]["id"]
    assert srv.req("GET", f"/api/applications/{b['id']}/documents/{doc}/file", headers=STAFF)[0] == 404


# ------------------------------------------------------------ webhooks
def test_webhook_rejects_bad_input(srv):
    secret = srv.cfg.webhook_secret
    def post(payload_bytes, sig=None):
        return srv.req("POST", "/api/callbacks/PAN", raw=payload_bytes,
                       headers={"X-Signature": sig if sig is not None else sign(secret, payload_bytes)})[0]
    assert post(b'{"x":1}', "bad") == 401
    assert post(b'{"x":1}', "") == 401
    assert post(b"not json") == 400
    assert post(b"[1,2]") == 400
    assert post(json.dumps({"event_id": 5, "request_id": "r", "provider": "PAN"}).encode()) == 400
    assert post(json.dumps({"event_id": "e", "request_id": "r", "provider": "GST"}).encode()) == 400
    assert post(json.dumps({"event_id": "e1", "request_id": "r", "provider": "PAN", "result": [1]}).encode()) == 400
    assert post(json.dumps({"event_id": "e2", "request_id": "r", "provider": "PAN", "result": {}}).encode()) == 200


def test_malformed_provider_result_is_retried_not_crashed(srv):
    srv.req("POST", "/api/modes", {"target": "RISK", "mode": "outage"}, headers=OPS)   # hold RISK in flight state
    _, out = srv.req("POST", "/api/scenarios/happy_path", {}, headers=STAFF)
    srv.req("POST", "/api/modes", {"target": "RISK", "mode": "normal"}, headers=OPS)
    # find an in-flight request for this app and answer it with garbage
    for _ in range(50):
        _, a = srv.req("GET", f"/api/applications/{out['id']}", headers=STAFF)
        reqs = [r for c in a["checks"] if c["check_type"] == "PAN" for r in c["requests"] if r["status"] == "SENT"]
        if reqs:
            break
        __import__("time").sleep(0.05)
    if reqs:
        body = json.dumps({"event_id": "garbage-1", "request_id": reqs[0]["request_id"], "provider": "PAN",
                           "status": "COMPLETED", "result": {"pan_status": "VALID", "registered_name": 12345}}).encode()
        code, res = srv.req("POST", "/api/callbacks/PAN", raw=body, headers={"X-Signature": sign(srv.cfg.webhook_secret, body)})
        assert code == 200, res
    srv.wait_state(out["id"], {"ACTIVE", "MANUAL_REVIEW"}, timeout=30)


# ------------------------------------------------------------ fuzzing
JUNK = [None, True, 0, -1, 1e308, "", "x" * 5000, "<script>", "'; DROP TABLE applications;--", [], [None], {}, {"a": {"b": []}},
        "APP-FFFFFFFF", "PAN_CARD", "approve", "\u0000", "ü✓€"]


def junk(rng, depth=0):
    if depth < 2 and rng.random() < 0.3:
        return {k: junk(rng, depth + 1) for k in rng.sample(["form", "documents", "fields", "action", "reason", "values",
                                                            "doc_type", "text", "file_b64", "mime", "legal_name", "pan",
                                                            "target", "mode", "kind", "field", "value", "feedback"], 3)}
    return rng.choice(JUNK)


def test_fuzz_every_post_endpoint_never_500(srv):
    _, out = srv.req("POST", "/api/scenarios/gst_cancelled", {}, headers=STAFF)
    app, token = out["id"], out["token"]
    srv.wait_state(app, {"MANUAL_REVIEW"})
    _, a = srv.req("GET", f"/api/applications/{app}", headers=STAFF)
    doc = a["documents"][0]["id"]
    targets = [
        ("/api/merchant/applications", {}), (f"/api/merchant/applications/{app}/documents", {"X-App-Token": token}),
        (f"/api/merchant/applications/{app}/fields", {"X-App-Token": token}), (f"/api/applications/{app}/actions", STAFF),
        (f"/api/applications/{app}/reveal", STAFF), (f"/api/applications/{app}/documents/{doc}/verify", STAFF),
        (f"/api/applications/{app}/simulate-merchant", STAFF), (f"/api/applications/{app}/copilot/1/feedback", STAFF),
        ("/api/modes", OPS), ("/api/callbacks/PAN", {}),
    ]
    rng = random.Random(7)
    for i in range(400):
        path, h = targets[i % len(targets)]
        body = {k: junk(rng) for k in rng.sample(["form", "documents", "fields", "action", "reason", "message", "values",
                                                   "field", "value", "kind", "target", "mode", "feedback", "purpose",
                                                   "check_type", "doc_type"], rng.randint(0, 5))}
        code, res = srv.req("POST", path, body, headers=h)
        assert code < 500, (path, body, res)
    srv.req("POST", "/api/modes", {"target": "LLM", "mode": "normal"}, headers=OPS)
    for t in ("PAN", "GST", "BANK", "RISK"):
        srv.req("POST", "/api/modes", {"target": t, "mode": "normal"}, headers=OPS)
    srv.req("POST", "/api/modes", {"target": "MID", "mode": "normal"}, headers=OPS)
    for p in ("/api/applications", "/api/metrics", "/api/review-queue", "/api/dead-letters", "/api/activity",
              f"/api/applications/{app}"):
        assert srv.req("GET", p, headers=STAFF)[0] == 200
