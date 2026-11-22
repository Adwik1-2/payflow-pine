"""End-to-end check against a REAL model:  python3 tools/live_e2e.py

Needs ANTHROPIC_API_KEY (or OPENAI_API_KEY / GEMINI_API_KEY) in .env or the
environment. Each case runs on a fresh server and database, uploads the real
files in sample_docs/ through the merchant API, waits for the workflow to
settle and compares the outcome with what should happen.
Typical cost with Claude: a few US cents for the whole run.
"""
import base64
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from app.config import Config  # noqa: E402
from app.llm import LLMSettings  # noqa: E402
from tests.live_server import STAFF, LiveServer  # noqa: E402

SD = os.path.join(ROOT, "sample_docs")
SETTLED = {"NEEDS_INFO", "MANUAL_REVIEW", "ACTIVE", "REJECTED", "PROVISIONING_FAILED"}


def file_doc(doc_type, name):
    mime = "application/pdf" if name.endswith(".pdf") else "image/png"
    data = open(os.path.join(SD, name), "rb").read()
    return {"doc_type": doc_type, "filename": name, "mime": mime, "file_b64": base64.b64encode(data).decode()}


def wait(srv, app_id, timeout=240):
    end = time.time() + timeout
    while time.time() < end:
        _, a = srv.req("GET", f"/api/applications/{app_id}", headers=STAFF)
        if a["state"] in SETTLED and not any(j["status"] in ("QUEUED", "RUNNING") and j["kind"] != "notify_merchant"
                                             for j in a["jobs"]):
            return a
        time.sleep(1)
    return a


def show(a):
    print(f"   state: {a['state']}   {a['summary'][:160]}")
    for d in a["documents"]:
        if d["superseded"]:
            continue
        print(f"   doc {d['doc_type']:<17} {d['status']:<16} method={d['method']} model={d['model']} "
              f"quality={d['quality']} detected={d['detected_type']} injection={d['injection']} tamper={d['tamper_signals']}")
    for f in (a["extracted"] or {}).get("fields", []):
        print(f"     {f['doc_type']:<17} {f['field']:<15} form={f['form_value']!s:<22} doc={f['doc_value']!s:<22} "
              f"conf={f['confidence']} -> {f['status']}")
    if a.get("ai_category"):
        c = a["ai_category"]
        print(f"   category: {c['category']} mcc={c['mcc']} restricted={c['restricted']} method={c['method']} :: {c['rationale'][:120]}")
    print("   checks: " + ", ".join(f"{c['check_type']}={c['status']}" for c in a["checks"]))
    open_ex = [e["code"] for e in a["exceptions"] if not e["resolved_at"]]
    open_mi = [m["code"] for m in a["missing"] if not m["resolved_at"]]
    if open_ex:
        print(f"   open exceptions: {open_ex}")
    if open_mi:
        print(f"   asked merchant: {open_mi}")
    for n in a["notifications"]:
        print(f"   message ({n['drafted_by']}): {n['subject']}")


def bank_name_expect(a, b):
    """The model must recognise the truncated name. It goes live when the model is at least as sure as the
    configured threshold; below that the case is (correctly) held for a human with the model's view attached."""
    bank = next((c for c in a["checks"] if c["check_type"] == "BANK"), {})
    adj = (bank.get("result") or {}).get("name_adjudication") or {}
    if adj:
        print(f"   AI name check: same_entity={adj.get('same_entity')} confidence={adj.get('confidence')} :: {str(adj.get('rationale'))[:140]}")
    if a["state"] == "ACTIVE":
        return True, "AI confirmed the name and the merchant went live"
    if adj.get("same_entity") and a["state"] == "MANUAL_REVIEW":
        return True, "AI agreed it is the same business but was below the auto-accept threshold, so a human confirms"
    return False, "expected the model to recognise the truncated name as the same business"


def run_case(settings, title, form, docs=None, scenario=None, expect=None, copilot=False):
    print(f"\n== {title}")
    srv = LiveServer(cfg=Config(check_timeout_s=8, notify_debounce_s=1), llm_settings=settings)
    try:
        if scenario:
            _, out = srv.req("POST", f"/api/scenarios/{scenario}", {}, headers=STAFF)
        else:
            code, out = srv.req("POST", "/api/merchant/applications", {"form": form, "documents": docs})
            assert code == 201, out
        t0 = time.time()
        a = wait(srv, out["id"])
        print(f"   settled in {time.time() - t0:.1f}s")
        show(a)
        brief = None
        if copilot and a["state"] == "MANUAL_REVIEW":
            code, brief = srv.req("POST", f"/api/applications/{out['id']}/copilot", {}, headers=STAFF, timeout=240)
            print(f"   copilot ({brief.get('method')}, {brief.get('model')}): {brief.get('recommendation')} - {brief.get('rationale', '')[:200]}")
            if brief.get("policy_note"):
                print(f"   policy guard: {brief['policy_note']}")
        _, a = srv.req("GET", f"/api/applications/{out['id']}", headers=STAFF)
        ok, why = expect(a, brief) if expect else (True, "")
        print(f"   RESULT: {'PASS' if ok else 'CHECK'} {why}")
        calls = a["llm_calls"]
        return ok, calls
    finally:
        srv.stop()


def main():
    settings = LLMSettings.from_env()
    if settings.provider in ("mock", "off"):
        print("No API key found. Put ANTHROPIC_API_KEY=... (or OPENAI_API_KEY / GEMINI_API_KEY) in .env and re-run.")
        sys.exit(2)
    print(f"provider={settings.provider} models={settings.models} effort={settings.effort}")
    m = json.load(open(os.path.join(SD, "sample_merchant.json")))
    codes = lambda a: {e["code"] for e in a["exceptions"] if not e["resolved_at"]}          # noqa: E731
    asked = lambda a: {x["code"] for x in a["missing"] if not x["resolved_at"]}            # noqa: E731
    good = [file_doc("PAN_CARD", "pan_card.png"), file_doc("GST_CERTIFICATE", "gst_certificate.pdf"),
            file_doc("CANCELLED_CHEQUE", "cancelled_cheque.png")]
    cases = [
        ("Real PNG + PDF documents -> extracted by the model -> live", dict(form=m, docs=good,
         expect=lambda a, b: (a["state"] == "ACTIVE" and all(d["method"] == "llm" for d in a["documents"]),
                              "expected ACTIVE with every document read by the model"))),
        ("Blurred cheque -> merchant asked to re-upload", dict(form=m, docs=[good[0], good[1], file_doc("CANCELLED_CHEQUE", "cancelled_cheque_blurred.png")],
         expect=lambda a, b: ("DOCUMENT_UNREADABLE" in asked(a) or "FIELD_VERIFICATION_REQUIRED" in codes(a),
                              "expected DOCUMENT_UNREADABLE (or a reviewer field check)"))),
        ("Hidden instruction in PAN card -> flagged, not obeyed", dict(form=m, docs=[file_doc("PAN_CARD", "pan_card_with_hidden_instruction.png"), good[1], good[2]],
         expect=lambda a, b: ("DOCUMENT_TAMPER_SUSPECTED" in codes(a) and a["state"] != "ACTIVE",
                              "expected DOCUMENT_TAMPER_SUSPECTED and not approved"))),
        ("Description reveals a real-money gaming app -> category flag + copilot", dict(form=None, scenario="category_mismatch", copilot=True,
         expect=lambda a, b: ("CATEGORY_MISMATCH" in codes(a) and b and b["recommendation"] in ("ESCALATE", "REJECT", "REQUEST_INFO"),
                              "expected CATEGORY_MISMATCH and a cautious copilot recommendation"))),
        ("Bank returns a truncated name -> model confirms same business", dict(form=None, scenario="abbreviated_bank_name",
         expect=bank_name_expect)),
        ("PAN passes, GST cancelled -> copilot must not recommend plain approval", dict(form=None, scenario="gst_cancelled", copilot=True,
         expect=lambda a, b: (bool(b) and b["recommendation"] != "APPROVE", "expected anything but plain APPROVE"))),
    ]
    results, all_calls = [], []
    for title, kw in cases:
        ok, calls = run_case(settings, title, kw.get("form"), kw.get("docs"), kw.get("scenario"), kw.get("expect"), kw.get("copilot", False))
        results.append((title, ok))
        all_calls += calls
    print("\n== AI calls")
    feats = {}
    for c in all_calls:
        f = feats.setdefault(c["feature"], {"n": 0, "ok": 0, "lat": 0, "cost": 0.0, "statuses": set()})
        f["n"] += 1
        f["ok"] += c["status"] == "ok"
        f["lat"] += c["latency_ms"] or 0
        f["cost"] += c["cost_usd"] or 0
        f["statuses"].add(c["status"])
    for k, f in feats.items():
        print(f"   {k:<24} calls={f['n']:<3} ok={f['ok']:<3} avg={f['lat'] // max(f['n'], 1)} ms  cost=${f['cost']:.4f}  {sorted(f['statuses'])}")
    for c in all_calls:
        if c["status"] != "ok":
            print(f"   ! {c['feature']} {c['status']}: {c['error']}")
    print(f"   total cost ${sum(c['cost_usd'] or 0 for c in all_calls):.4f}")
    print("\n== Summary")
    for t, ok in results:
        print(f"   {'PASS ' if ok else 'CHECK'} {t}")
    sys.exit(0 if all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()
