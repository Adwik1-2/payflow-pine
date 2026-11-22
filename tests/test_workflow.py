"""Core workflow: states, checks, callbacks, retries, provisioning, review."""
import json

import pytest

from app import scenarios
from app.providers import sign
from app.states import InvalidTransition, assert_transition
from app.workflow import ActionError, NotFound
from tests.harness import Harness


@pytest.fixture
def h():
    return Harness()


# ------------------------------------------------------------ happy path
def test_happy_path_goes_live_with_audit_trail(h):
    app_id, _, _ = h.submit("happy_path")
    assert h.state(app_id) == "VERIFYING"
    h.run(3)
    a = h.app(app_id)
    assert a["state"] == "ACTIVE" and a["merchant_id"].startswith("MID")
    assert all(c["status"] == "PASSED" for c in a["checks"])
    assert [x["to_state"] for x in a["history"]] == ["SUBMITTED", "VERIFYING", "APPROVED", "PROVISIONING", "ACTIVE"]
    ev = h.events(app_id)
    for e in ("INTAKE_EVALUATED", "BUSINESS_CATEGORISED", "CHECK_DISPATCHED", "CALLBACK_RECEIVED",
              "MERCHANT_ID_CREATED", "PAYMENT_CAPABILITIES_CONFIGURED", "MERCHANT_NOTIFIED"):
        assert e in ev, e
    assert a["capabilities"]["profile"] == "STANDARD" and a["capabilities"]["mcc"] == "5399"
    assert a["notifications"][-1]["kind"] == "LIVE"


def test_not_gst_registered_skips_gst(h):
    app_id, _, _ = h.submit("not_gst_registered")
    h.run(3)
    assert h.state(app_id) == "ACTIVE"
    assert h.check(app_id, "GST")["status"] == "NOT_REQUIRED"


def test_provider_requests_go_through_outbox_and_survive_a_crash(h):
    h.engine._flushing = True          # simulate a crash right after commit, before sending
    app_id, _, _ = h.submit("happy_path")
    h.engine._flushing = False
    st = {r["status"] for c in h.app(app_id)["checks"] for r in c["requests"]}
    assert st == {"QUEUED"}
    h.run(3)                           # the scheduler finds the queued requests and sends them
    assert h.state(app_id) == "ACTIVE"


# ------------------------------------------------------------ PAN passes, GST fails
def test_pan_passes_gst_cancelled_needs_two_reviewers(h):
    app_id, _, _ = h.submit("gst_cancelled")
    h.run(3)
    assert h.state(app_id) == "MANUAL_REVIEW"
    assert h.check(app_id, "PAN")["status"] == "PASSED"
    assert h.check(app_id, "GST")["reason_code"] == "GSTIN_CANCELLED"
    with pytest.raises(ActionError):
        h.engine.review_action(app_id, "approve", "anita", reason="")
    h.engine.review_action(app_id, "approve", "anita", reason="Re-registration proof checked")
    assert h.state(app_id) == "MANUAL_REVIEW"            # HIGH severity -> maker-checker
    assert h.app(app_id)["approval"]["status"] == "PENDING"
    with pytest.raises(ActionError):
        h.engine.review_action(app_id, "confirm_approval", "anita")   # same person cannot confirm
    h.engine.review_action(app_id, "confirm_approval", "vikram", reason="agree")
    a = h.app(app_id)
    assert a["state"] == "ACTIVE" and h.check(app_id, "GST")["status"] == "WAIVED"
    approved = [x for x in a["audit"] if x["event"] == "REVIEWER_APPROVED"][0]
    assert approved["detail"]["approvers"] == ["anita", "vikram"]


def test_pending_approval_is_invalidated_when_case_changes(h):
    app_id, form, _ = h.submit("high_risk")
    h.run(3)
    h.engine.review_action(app_id, "approve", "anita", reason="licensed operator")
    new_acct = "60300098765432"
    h.engine.update(app_id, fields={"bank_account": new_acct},
                    documents=[scenarios.cheque(new_acct, form["ifsc"], form["account_holder"])])
    assert h.app(app_id)["approval"]["status"] == "STALE"
    h.run(3)
    with pytest.raises(ActionError):
        h.engine.review_action(app_id, "confirm_approval", "vikram")


def test_claimed_case_cannot_be_decided_by_someone_else(h):
    app_id, _, _ = h.submit("gst_cancelled")
    h.run(3)
    h.engine.review_action(app_id, "claim", "anita")
    with pytest.raises(ActionError):
        h.engine.review_action(app_id, "reject", "vikram", reason="no")
    h.engine.review_action(app_id, "release", "anita")
    h.engine.review_action(app_id, "reject", "vikram", reason="cancelled GST, no proof")
    assert h.state(app_id) == "REJECTED"


def test_corrected_gstin_reruns_only_gst(h):
    app_id, form, _ = h.submit("gst_cancelled")
    h.run(3)
    pan_attempts = h.check(app_id, "PAN")["attempts"]
    h.engine.review_action(app_id, "request_info", "anita", message="Please share your active GSTIN")
    assert h.state(app_id) == "NEEDS_INFO"
    new_gstin = form["gstin"][:12] + "1" + form["gstin"][13:]
    h.engine.update(app_id, fields={"gstin": new_gstin},
                    documents=[scenarios.gst_cert(new_gstin, form["legal_name"], form["trade_name"])])
    h.run(3)
    assert h.state(app_id) == "ACTIVE"
    assert h.check(app_id, "PAN")["attempts"] == pan_attempts


# ------------------------------------------------------------ callbacks
def test_duplicate_callbacks_processed_once(h):
    h.engine.set_mode("PAN", "duplicate")
    app_id, _, _ = h.submit("happy_path")
    h.run(3)
    a = h.app(app_id)
    assert a["state"] == "ACTIVE"
    pan_cb = [c for c in a["callbacks"] if c["provider"] == "PAN"]
    assert len(pan_cb) == 1 and pan_cb[0]["delivery_count"] == 3
    assert h.events(app_id).count("DUPLICATE_CALLBACK_IGNORED") == 2
    assert h.events(app_id).count("MERCHANT_ID_CREATED") == 1


def test_timeout_then_late_callback_is_accepted(h):
    h.engine.set_mode("BANK", "slow")
    app_id, _, _ = h.submit("happy_path")
    h.run(h.cfg.check_timeout_s + 0.5)
    ev = h.events(app_id)
    assert "CHECK_TIMED_OUT" in ev and "RETRY_SCHEDULED" in ev
    h.engine.set_mode("BANK", "normal")
    h.run(6)
    a = h.app(app_id)
    assert a["state"] == "ACTIVE"
    assert "late_accepted" in [c["outcome"] for c in a["callbacks"] if c["provider"] == "BANK"]


def test_callback_for_changed_inputs_is_discarded(h):
    app_id, form, _ = h.submit("happy_path")
    new_acct = "50200012345678"
    h.engine.update(app_id, fields={"bank_account": new_acct},
                    documents=[scenarios.cheque(new_acct, form["ifsc"], form["account_holder"])])
    h.run(3)
    a = h.app(app_id)
    outcomes = {c["outcome"] for c in a["callbacks"] if c["provider"] == "BANK"}
    assert "accepted" in outcomes and ("stale_inputs_discarded" in outcomes or "superseded_ignored" in outcomes)
    assert a["state"] == "ACTIVE"


def test_bank_change_after_verification_triggers_reverification(h):
    app_id, form, _ = h.submit("high_risk")
    h.run(3)
    assert h.check(app_id, "BANK")["status"] == "PASSED"
    new_acct = "60300098765432"
    h.engine.update(app_id, fields={"bank_account": new_acct},
                    documents=[scenarios.cheque(new_acct, form["ifsc"], form["account_holder"])])
    assert h.check(app_id, "BANK")["status"] == "IN_FLIGHT"
    assert h.state(app_id) == "VERIFYING"
    ev = h.events(app_id)
    assert "BANK_DETAILS_CHANGED_AFTER_VERIFICATION" in ev and "VERIFIED_DATA_CHANGED_REVERIFYING" in ev
    h.run(3)
    assert h.check(app_id, "BANK")["status"] == "PASSED"
    assert h.state(app_id) == "MANUAL_REVIEW"


def test_bank_change_after_activation_is_refused(h):
    app_id, _, _ = h.submit("happy_path")
    h.run(3)
    with pytest.raises(ActionError):
        h.engine.update(app_id, fields={"bank_account": "123456789012"})


def test_bad_signature_rejected(h):
    body = json.dumps({"event_id": "x", "provider": "PAN", "request_id": "r"}).encode()
    assert h.engine.handle_webhook("PAN", body, "deadbeef")[0] == 401
    assert h.engine.handle_webhook("PAN", body, sign(h.cfg.webhook_secret, body))[1]["outcome"] == "unknown_request"


# ------------------------------------------------------------ outages, retries, DLQ
def test_provider_outage_dead_letters_then_replay(h):
    h.engine.set_mode("GST", "outage")
    app_id, _, _ = h.submit("happy_path")
    h.run(20)
    assert h.state(app_id) == "MANUAL_REVIEW"
    assert h.check(app_id, "GST")["status"] == "DEAD"
    assert h.check(app_id, "GST")["attempts"] == h.cfg.max_check_attempts
    dl = [d for d in h.engine.dead_letters() if d["ref"] == "GST" and not d["resolved_at"]]
    assert dl
    h.engine.set_mode("GST", "normal")
    h.engine.replay_dead_letter(dl[0]["id"], "ops1")
    h.run(3)
    assert h.state(app_id) == "ACTIVE"
    assert not any(not d["resolved_at"] for d in h.engine.dead_letters())


def test_flaky_provider_is_retried(h):
    h.engine.set_mode("RISK", "flaky")
    app_id, _, _ = h.submit("happy_path")
    h.run(8)
    assert h.state(app_id) == "ACTIVE"
    assert h.check(app_id, "RISK")["attempts"] == 2


# ------------------------------------------------------------ provisioning
def test_mid_creation_fails_once_then_succeeds(h):
    h.engine.set_mode("MID", "fail_once")
    app_id, _, _ = h.submit("happy_path")
    h.run(1.5)
    assert h.state(app_id) == "PROVISIONING"
    h.run(3)
    assert h.state(app_id) == "ACTIVE"
    assert "MID_CREATION_FAILED" in h.events(app_id)


def test_mid_timeout_after_commit_does_not_create_two_merchants(h):
    h.engine.set_mode("MID", "timeout_after_commit")
    app_id, _, _ = h.submit("happy_path")
    h.run(5)
    a = h.app(app_id)
    assert a["state"] == "ACTIVE"
    assert h.engine._one("SELECT COUNT(*) n FROM mid_registry")["n"] == 1
    created = [x for x in a["audit"] if x["event"] == "MERCHANT_ID_CREATED"][0]
    assert created["detail"]["idempotent_replay"] is True


def test_mid_failure_exhausted_then_retry(h):
    h.engine.set_mode("MID", "fail_always")
    app_id, _, _ = h.submit("happy_path")
    h.run(15)
    assert h.state(app_id) == "PROVISIONING_FAILED"
    assert any(q["id"] == app_id for q in h.engine.review_queue())
    h.engine.set_mode("MID", "normal")
    h.engine.review_action(app_id, "retry_provisioning", "ops1")
    assert h.state(app_id) == "ACTIVE"


# ------------------------------------------------------------ documents (text payloads)
def test_blurred_document_requests_reupload_while_other_checks_run(h):
    app_id, form, _ = h.submit("blurred_document")
    h.run(3)
    a = h.app(app_id)
    assert a["state"] == "NEEDS_INFO"
    assert any(m["code"] == "DOCUMENT_UNREADABLE" and not m["resolved_at"] for m in a["missing"])
    assert h.check(app_id, "BANK")["status"] == "BLOCKED"
    assert h.check(app_id, "PAN")["status"] == "PASSED"
    h.engine.update(app_id, documents=[scenarios.cheque(form["bank_account"], form["ifsc"], form["account_holder"])])
    h.run(3)
    assert h.state(app_id) == "ACTIVE"


def test_document_conflicts_with_form_goes_to_review(h):
    app_id, _, _ = h.submit("document_conflict")
    h.run(3)
    a = h.app(app_id)
    assert a["state"] == "MANUAL_REVIEW" and "DOC_FORM_CONFLICT" in h.open_codes(app_id)
    pan_row = next(f for f in a["extracted"]["fields"] if f["field"] == "pan")
    assert pan_row["status"] == "MISMATCH" and pan_row["confidence"] > 0.9


def test_missing_document_and_invalid_field(h):
    form, docs = scenarios.build("happy_path", h.rng)
    form["ifsc"] = "BAD"
    app_id, _ = h.engine.submit(form, [d for d in docs if d["doc_type"] != "GST_CERTIFICATE"])
    h.run(1)
    codes = {m["code"] for m in h.app(app_id)["missing"]}
    assert {"MISSING_DOCUMENT", "INVALID_FORMAT"} <= codes
    assert h.state(app_id) == "NEEDS_INFO"


# ------------------------------------------------------------ guards, fraud signals, privacy
def test_reviewer_cannot_override_bank_name_mismatch(h):
    app_id, _, _ = h.submit("bank_name_mismatch")
    h.run(3)
    assert h.state(app_id) == "MANUAL_REVIEW"
    with pytest.raises(ActionError):
        h.engine.review_action(app_id, "approve", "anita", reason="looks fine")
    h.engine.review_action(app_id, "reject", "anita", reason="third-party settlement account")
    assert h.state(app_id) == "REJECTED"
    msg = h.app(app_id)["notifications"][-1]
    assert msg["kind"] == "REJECTED"   # neutral template: never reveals the reason to the applicant
    assert "mismatch" not in msg["body"].lower() and "risk" not in msg["body"].lower()


def test_high_risk_waiver_is_restricted_and_four_eyes(h):
    app_id, _, _ = h.submit("high_risk")
    h.run(3)
    h.engine.review_action(app_id, "approve", "anita", reason="licensed operator, enhanced monitoring")
    h.engine.review_action(app_id, "confirm_approval", "vikram")
    a = h.app(app_id)
    assert a["state"] == "ACTIVE" and a["capabilities"]["profile"] == "RESTRICTED"


def test_duplicate_application_detected(h):
    first, _, _ = h.submit("happy_path")
    h.run(3)
    dup, _, _ = h.submit("duplicate")
    h.run(3)
    ex = [e for e in h.app(dup)["exceptions"] if e["code"] == "DUPLICATE_APPLICATION"]
    assert ex and ex[0]["detail"]["other_app"] == first
    assert h.state(dup) == "MANUAL_REVIEW"


def test_reused_document_file_is_flagged(h):
    first, _, _ = h.submit("happy_path")
    h.run(3)
    second, _, _ = h.submit("reused_document")
    h.run(3)
    assert "DOCUMENT_REUSED" in h.open_codes(second)


def test_invalid_transition_rejected():
    with pytest.raises(InvalidTransition):
        assert_transition("SUBMITTED", "ACTIVE")
    with pytest.raises(InvalidTransition):
        assert_transition("REJECTED", "VERIFYING")


def test_no_raw_identifiers_in_audit_or_views(h):
    app_id, form, _ = h.submit("happy_path")
    h.run(3)
    a = h.app(app_id)
    blob = json.dumps({k: a[k] for k in ("audit", "form", "extracted", "checks", "notifications")})
    for k in ("pan", "gstin", "bank_account"):
        assert form[k] not in blob, k
    assert h.engine.reveal(app_id, "pan", "anita", purpose="KYC call-back") == form["pan"]
    assert "SENSITIVE_FIELD_REVEALED" in h.events(app_id)


# ------------------------------------------------------------ merchant side
def test_merchant_token_and_view_do_not_leak_internal_signals(h):
    app_id, _, _ = h.submit("high_risk")
    h.run(3)
    with pytest.raises(NotFound):
        h.engine.merchant_auth(app_id, "wrong-token")
    h.engine.merchant_auth(app_id, h.tokens[app_id])
    v = h.engine.merchant_view(app_id)
    assert v["status"] == "Under review"
    assert "RISK" not in json.dumps(v) and "gambling" not in json.dumps(v["needed"])


def test_merchant_is_notified_once_per_request_and_reminded(h):
    app_id, _, _ = h.submit("pan_not_found")        # PAN and GST both come back 'not found'
    h.run(6)
    a = h.app(app_id)
    info = [n for n in a["notifications"] if n["kind"] == "INFO_REQUEST"]
    assert len(info) == 1 and "PAN" in info[0]["body"] and "GSTIN" in info[0]["body"]
    h.run(h.cfg.needs_info_reminder_s + 2)
    h.run(h.cfg.needs_info_reminder_s + 2)
    h.run(h.cfg.needs_info_reminder_s + 2)
    rem = [n for n in h.app(app_id)["notifications"] if n["kind"] == "REMINDER"]
    assert len(rem) == h.cfg.max_reminders


def test_metrics(h):
    h.submit("happy_path")
    h.submit("gst_cancelled")
    h.run(4)
    m = h.engine.metrics()
    assert m["applications"] == 2 and m["exception_rate"] == 0.5
    assert m["by_state"] == {"ACTIVE": 1, "MANUAL_REVIEW": 1}
    assert m["funnel"]["SUBMITTED"] == 2 and m["funnel"]["ACTIVE"] == 1
    assert m["llm"]["by_feature"]["business_categorisation"]["calls"] == 2
