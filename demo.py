"""Scripted end-to-end demonstration on a simulated clock (no server, ~1 second):
    python3 demo.py

1. Happy path: async callbacks (one delivered 3x), merchant-ID service fails once -> LIVE
2. PAN passes, GST cancelled -> escalation -> maker-checker approval -> LIVE
3. Photo documents read by the vision model; blurred cheque -> merchant re-uploads -> LIVE
4. Bank shows a truncated name -> AI name adjudication -> LIVE
5. Description reveals a restricted business -> AI flags it -> copilot brief
6. LLM outage -> documents fall back to human field entry -> LIVE
"""
from app import scenarios
from tests.harness import Harness
from tests.test_ai import img_doc

SKIP = {"DOCUMENT_UPLOADED", "PAYMENT_CAPABILITIES_CONFIGURED", "INTAKE_EVALUATED", "EXCEPTIONS_RESOLVED",
        "CHECK_UNBLOCKED", "INFO_RECEIVED"}
KEYS = ("check", "status", "reason", "outcome", "attempt", "from_state", "to_state", "merchant_id", "code", "error",
        "idempotent_replay", "message", "approvers", "model", "legibility", "category", "restricted", "similarity",
        "confidence", "kind", "drafted_by", "recommendation", "job")


def show(h, app_id, since=0):
    a = h.app(app_id)
    for e in a["audit"][since:]:
        if e["event"] in SKIP:
            continue
        d = e["detail"] or {}
        bits = ", ".join(f"{k}={v}" for k, v in d.items() if k in KEYS and v not in (None, ""))
        print(f"  t+{e['at'] - h.t0:5.1f}s  {e['actor']:<15.15} {e['event']:<38} {bits[:110]}")
    return len(a["audit"])


def banner(t, h):
    h.t0 = h.clock.now()
    print("\n" + t + "\n" + "-" * len(t))


def result(h, a):
    x = h.app(a)
    print(f"  => {x['state']}" + (f"  {x['merchant_id']} ({x['capabilities']['profile']})" if x["merchant_id"] else
                                 f": {x['summary'][:140]}"))


h = Harness()

banner("1. Happy path, duplicate callbacks, merchant-ID service fails once", h)
h.engine.set_mode("PAN", "duplicate")
h.engine.set_mode("MID", "fail_once")
a1, _, _ = h.submit("happy_path")
h.run(5)
show(h, a1)
result(h, a1)
h.engine.set_mode("PAN", "normal")
h.engine.set_mode("MID", "normal")

banner("2. PAN passes, GST cancelled -> reviewer + second reviewer", h)
a2, _, _ = h.submit("gst_cancelled")
h.run(3)
n = show(h, a2)
result(h, a2)
h.engine.review_action(a2, "approve", "reviewer.anita", reason="Re-registration acknowledgement verified on GST portal")
h.engine.review_action(a2, "confirm_approval", "reviewer.vikram", reason="agree")
show(h, a2, n)
result(h, a2)

banner("3. Photo documents read by the vision model; blurred cheque", h)
form, _ = scenarios.build("happy_path", h.rng)
docs = [img_doc("PAN_CARD", {"pan": form["pan"], "legal_name": form["legal_name"]}),
        img_doc("GST_CERTIFICATE", {"gstin": form["gstin"], "legal_name": form["legal_name"]}),
        img_doc("CANCELLED_CHEQUE", {k: form[k] for k in ("bank_account", "ifsc", "account_holder")}, blur=4.5)]
a3, _ = h.engine.submit(form, docs)
h.run(5)
n = show(h, a3)
result(h, a3)
print("  merchant uploads a sharp photo ->")
h.engine.update(a3, documents=[img_doc("CANCELLED_CHEQUE", {k: form[k] for k in ("bank_account", "ifsc", "account_holder")})])
h.run(4)
show(h, a3, n)
result(h, a3)

banner("4. Bank returns a truncated beneficiary name -> AI adjudication", h)
a4, _, _ = h.submit("abbreviated_bank_name")
h.run(4)
show(h, a4)
result(h, a4)

banner("5. Description reveals a restricted business -> AI flag + copilot", h)
a5, _, _ = h.submit("category_mismatch")
h.run(3)
show(h, a5)
result(h, a5)
b = h.engine.copilot(a5, "reviewer.anita")
print(f"  copilot ({b['model']}): {b['recommendation']} - {b['rationale']}")

banner("6. LLM outage -> documents go to a human for field entry", h)
h.engine.set_mode("LLM", "outage")
form, _ = scenarios.build("happy_path", h.rng)
truth = {"PAN_CARD": {"pan": form["pan"], "legal_name": form["legal_name"]},
         "GST_CERTIFICATE": {"gstin": form["gstin"], "legal_name": form["legal_name"]},
         "CANCELLED_CHEQUE": {k: form[k] for k in ("bank_account", "ifsc", "account_holder")}}
a6, _ = h.engine.submit(form, [img_doc(t, f) for t, f in truth.items()])
h.run(15)
n = show(h, a6)
result(h, a6)
print("  reviewer keys in the fields from the images ->")
for d in h.app(a6)["documents"]:
    h.engine.verify_document_fields(a6, d["id"], truth[d["doc_type"]], "reviewer.anita")
h.run(3)
show(h, a6, n)
result(h, a6)
h.engine.set_mode("LLM", "normal")

banner("Metrics", h)
m = h.engine.metrics()
for k in ("applications", "by_state", "straight_through_rate", "exception_rate", "duplicate_deliveries_ignored",
          "documents_by_method", "exceptions_by_code"):
    print(f"  {k:<30} {m[k]}")
print(f"  {'llm calls by feature':<30} { {f: v['calls'] for f, v in m['llm']['by_feature'].items()} }")
