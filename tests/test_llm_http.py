"""LLM adapters over real HTTP against a local fake provider: success,
429/529 then success, persistent 500, timeouts, junk bodies, and a full
document extraction through the gateway for each provider format."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import ai
from app.llm import Gateway, LLMError, LLMSettings
from app.llm import adapters
from tests.live_server import free_port

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
EXTRACTED = {"document_type": "PAN_CARD", "legibility": 0.95, "tamper_signals": [], "embedded_instructions_detected": False,
             "notes": "", "fields": {f: {"value": "", "confidence": 0} for f in ai.ALL_FIELDS}}
EXTRACTED["fields"]["pan"] = {"value": "ABCPS1234K", "confidence": 0.98}
EXTRACTED["fields"]["legal_name"] = {"value": "Sharma Electronics LLP", "confidence": 0.96}


class Fake:
    script = []      # list of (status, body_dict_or_bytes, delay)
    seen = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        Fake.seen.append((self.path, dict(self.headers), json.loads(self.rfile.read(n))))
        status, body, delay = Fake.script.pop(0) if Fake.script else (500, {"error": "no script"}, 0)
        time.sleep(delay)
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass


@pytest.fixture(scope="module")
def base():
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def anthropic_ok(payload, tin=100, tout=50):
    return {"content": [{"type": "text", "text": json.dumps(payload)}], "usage": {"input_tokens": tin, "output_tokens": tout}}


def openai_ok(payload):
    return {"choices": [{"message": {"content": json.dumps(payload)}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def gemini_ok(payload):
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}],
            "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3}}


def gateway(adapter, model="claude-sonnet-5-5", timeout=5):
    gw = Gateway(LLMSettings(provider="mock", models={"vision": model, "fast": model, "reasoning": model},
                             timeout_s=timeout), sleep=lambda s: None)
    gw.adapter = adapter
    calls = []
    gw.log = calls.append
    return gw, calls


@pytest.mark.parametrize("make,ok", [
    (lambda b: adapters.AnthropicAdapter("k", base_url=b), anthropic_ok),
    (lambda b: adapters.OpenAIAdapter("k", base_url=b + "/v1"), openai_ok),
    (lambda b: adapters.GeminiAdapter("k", base_url=b), gemini_ok),
])
def test_extraction_end_to_end_over_http(base, make, ok):
    Fake.script = [(200, ok(EXTRACTED), 0)]
    Fake.seen.clear()
    gw, calls = gateway(make(base))
    out = ai.extract_document(gw, PNG, "image/png", "PAN_CARD", "pan.png")
    assert out["fields"]["pan"]["value"] == "ABCPS1234K" and out["fields"]["pan"]["confidence"] > 0.9
    assert calls[-1]["status"] == "ok"
    path, headers, body = Fake.seen[-1]
    assert body  # request was valid JSON


def test_rate_limit_then_success_is_retried(base):
    Fake.script = [(429, {"error": "rate"}, 0), (529, {"error": "overloaded"}, 0), (200, anthropic_ok(EXTRACTED), 0)]
    gw, calls = gateway(adapters.AnthropicAdapter("k", base_url=base))
    out = ai.extract_document(gw, PNG, "image/png", "PAN_CARD")
    assert out["fields"]["pan"]["value"] == "ABCPS1234K"
    assert [c["status"] for c in calls] == ["error", "error", "ok"]
    assert calls[-1]["cost_usd"] == round((100 * 2 + 50 * 10) / 1e6, 6)


def test_auth_error_is_not_retried(base):
    Fake.script = [(401, {"error": "invalid x-api-key"}, 0), (200, anthropic_ok(EXTRACTED), 0)]
    gw, calls = gateway(adapters.AnthropicAdapter("k", base_url=base))
    with pytest.raises(LLMError):
        ai.extract_document(gw, PNG, "image/png", "PAN_CARD")
    assert len(calls) == 1
    Fake.script.clear()


def test_persistent_server_error_raises(base):
    Fake.script = [(500, {"e": 1}, 0)] * 3
    gw, calls = gateway(adapters.AnthropicAdapter("k", base_url=base))
    with pytest.raises(LLMError):
        ai.categorize_business(gw, "X", "shop", "retail")
    assert len(calls) == 3


def test_timeout_is_handled(base):
    Fake.script = [(200, anthropic_ok(EXTRACTED), 2.0)] * 3
    gw, calls = gateway(adapters.AnthropicAdapter("k", base_url=base), timeout=0.5)
    with pytest.raises(LLMError):
        ai.extract_document(gw, PNG, "image/png", "PAN_CARD")
    assert all(c["status"] == "error" for c in calls)
    time.sleep(2.5)
    Fake.script.clear()


@pytest.mark.parametrize("body", [b"<html>gateway error</html>", b"{}", json.dumps({"content": []}).encode(),
                                  json.dumps(anthropic_ok({"document_type": "PASSPORT"})).encode()])
def test_junk_responses_are_rejected(base, body):
    Fake.script = [(200, body, 0)] * 3
    gw, calls = gateway(adapters.AnthropicAdapter("k", base_url=base))
    with pytest.raises(LLMError):
        ai.extract_document(gw, PNG, "image/png", "PAN_CARD")
    Fake.script.clear()


def test_unreachable_provider(base):
    gw, calls = gateway(adapters.AnthropicAdapter("k", base_url="http://127.0.0.1:1"))
    with pytest.raises(LLMError):
        ai.categorize_business(gw, "X", "shop", "retail")


def test_truncated_output_is_retried_with_more_tokens(base):
    cut = {"content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": '{"document_type": "PAN'}],
           "usage": {"input_tokens": 100, "output_tokens": 16000}, "stop_reason": "max_tokens"}
    Fake.script = [(200, cut, 0), (200, anthropic_ok(EXTRACTED), 0)]
    Fake.seen.clear()
    gw, calls = gateway(adapters.AnthropicAdapter("k", base_url=base))
    out = ai.extract_document(gw, PNG, "image/png", "PAN_CARD")
    assert out["fields"]["pan"]["value"] == "ABCPS1234K"
    assert [c["status"] for c in calls] == ["truncated", "ok"]
    assert Fake.seen[0][2]["max_tokens"] == 16000 and Fake.seen[1][2]["max_tokens"] == 32000


def test_refusal_falls_back_without_retry(base):
    Fake.script = [(200, {"content": [{"type": "text", "text": "I can't help with that."}], "usage": {"input_tokens": 5, "output_tokens": 5},
                          "stop_reason": "refusal"}, 0), (200, anthropic_ok(EXTRACTED), 0)]
    gw, calls = gateway(adapters.AnthropicAdapter("k", base_url=base))
    from app.llm import LLMUnavailable
    with pytest.raises(LLMUnavailable):
        ai.extract_document(gw, PNG, "image/png", "PAN_CARD")
    assert [c["status"] for c in calls] == ["refusal"]
    Fake.script.clear()


def test_anthropic_request_carries_effort_and_schema(base):
    Fake.script = [(200, anthropic_ok({"same_entity": True, "confidence": 0.95, "explanation": "abbreviation"}), 0)]
    Fake.seen.clear()
    gw = Gateway(LLMSettings(provider="anthropic", api_key="k", models={"fast": "claude-haiku-5-5"}), sleep=lambda s: None)
    gw.adapter = adapters.AnthropicAdapter("k", base_url=base)
    out = ai.adjudicate_names(gw, "Sharma Electronics LLP", "M/S SHARMA ELEC", "bank")
    assert out["same_entity"] and out["rationale"] == "abbreviation"
    path, headers, body = Fake.seen[-1]
    hl = {k.lower(): v for k, v in headers.items()}
    assert path == "/v1/messages" and hl["anthropic-version"] == "2026-10-21" and hl["x-api-key"] == "k"
    assert body["output_config"]["effort"] == "low" and body["output_config"]["format"]["type"] == "json_schema"
    assert body["model"] == "claude-haiku-5-5" and body["max_tokens"] == 16000


def test_every_schema_meets_structured_output_rules():
    """additionalProperties false and every property required on every object; no unsupported keywords."""
    bad_kw = {"minimum", "maximum", "minLength", "maxLength", "oneOf", "not", "multipleOf"}
    def walk(s, path):
        assert not (bad_kw & set(s)), (path, bad_kw & set(s))
        if s.get("type") == "object":
            assert s.get("additionalProperties") is False, path
            assert set(s.get("required", [])) == set(s.get("properties", {})), path
            for k, v in s["properties"].items():
                walk(v, f"{path}.{k}")
        if s.get("type") == "array":
            walk(s["items"], path + "[]")
    for name in ("EXTRACTION_SCHEMA", "CATEGORY_SCHEMA", "NAME_SCHEMA", "COPILOT_SCHEMA", "MESSAGE_SCHEMA"):
        walk(getattr(ai, name), name)
        assert "rationale" not in json.dumps(getattr(ai, name)) and "reasoning" not in json.dumps(getattr(ai, name))


def test_azure_foundry_sends_api_key_header_and_normalises_url(base, monkeypatch):
    """Claude in Microsoft Foundry: https://<resource>.services.ai.azure.com/anthropic + api-key header."""
    ad = adapters.AnthropicAdapter("azkey", base_url="https://checkered.services.ai.azure.com/anthropic/")
    assert ad.azure and ad.url == "https://checkered.services.ai.azure.com/anthropic/v1/messages"
    _, h, _ = ad.build({"model": "m", "system": "s", "user_text": "u"})
    assert h["api-key"] == "azkey" and h["x-api-key"] == "azkey"
    assert adapters.AnthropicAdapter("k", base_url="https://x.services.ai.azure.com/anthropic/v1").url.endswith("/anthropic/v1/messages")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_RESOURCE", "myres")
    assert adapters.AnthropicAdapter("k").url == "https://myres.services.ai.azure.com/anthropic/v1/messages"
    _, h, _ = adapters.AnthropicAdapter("k", base_url=base).build({"model": "m", "system": "s", "user_text": "u"})
    assert "api-key" not in h


def test_host_that_rejects_output_config_falls_back_to_plain_json(base):
    Fake.script = [(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "output_config: Extra inputs are not permitted"}}, 0),
                   (200, anthropic_ok({"same_entity": True, "confidence": 0.95, "explanation": "abbreviation"}), 0)]
    Fake.seen.clear()
    gw = Gateway(LLMSettings(provider="anthropic", api_key="k", models={"fast": "claude-haiku-5-5"}), sleep=lambda s: None)
    gw.adapter = adapters.AnthropicAdapter("k", base_url=base)
    out = ai.adjudicate_names(gw, "Sharma Electronics LLP", "M/S SHARMA ELEC", "bank")
    assert out["same_entity"]
    first, second = Fake.seen[-2][2], Fake.seen[-1][2]
    assert "output_config" in first and "output_config" not in second
    assert {k.lower(): v for k, v in Fake.seen[-1][1].items()}["anthropic-version"] == "2023-06-01"
