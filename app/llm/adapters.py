"""Provider adapters. Each turns one generic request into an HTTP call and
returns (text, input_tokens, output_tokens). Standard library only.

Generic request:
    {system, user_text, attachments: [(mime, bytes, filename)], schema, max_tokens, model}
"""
import base64
import json
import os
import socket
import urllib.error
import urllib.request


class ProviderHTTPError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.retryable = status in (408, 409, 429) or status >= 500


def _post(url, headers, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ProviderHTTPError(e.code, e.read().decode("utf-8", "replace")) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        err = ProviderHTTPError(599, f"network error: {e}")
        raise err from None


def _b64(data):
    return base64.b64encode(data).decode()


class AnthropicAdapter:
    name = "anthropic"

    def __init__(self, api_key, version=None, base_url=None):
        self.key = api_key
        base = (base_url or os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL")
                or (f"https://{os.environ['ANTHROPIC_FOUNDRY_RESOURCE']}.services.ai.azure.com/anthropic"
                    if os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE") else "https://api.anthropic.com"))
        base = base.strip().strip('"').rstrip("/")
        if base.endswith("/v1/messages"):
            base = base[: -len("/v1/messages")]
        if base.endswith("/v1"):
            base = base[:-3]
        self.url = base + "/v1/messages"
        # Claude in Microsoft Foundry (Azure) accepts the key in an `api-key` header.
        self.azure = ".azure.com" in base or ".azure.net" in base
        self.version = version or os.environ.get("ANTHROPIC_VERSION", "2026-10-21")
        self.plain = False   # set after a 400 that rejects output_config: fall back to prompt-only JSON

    def build(self, rq):
        content = []
        for mime, data, _fn in rq.get("attachments", []):
            if mime == "application/pdf":
                content.append({"type": "document", "source": {"type": "base64", "media_type": mime, "data": _b64(data)}})
            else:
                content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": _b64(data)}})
        content.append({"type": "text", "text": rq["user_text"]})
        body = {"model": rq["model"], "max_tokens": rq.get("max_tokens", 4000), "system": rq["system"],
                "messages": [{"role": "user", "content": content}]}
        oc = {}
        if self.plain:
            pass
        elif rq.get("schema"):
            oc["format"] = {"type": "json_schema", "schema": rq["schema"]}
        if rq.get("effort") and not self.plain:
            oc["effort"] = rq["effort"]   # thinking counts toward max_tokens; effort keeps it proportionate
        if oc:
            body["output_config"] = oc
        headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01" if self.plain else self.version}
        if self.azure:
            headers["api-key"] = self.key
        return self.url, headers, body

    def parse(self, resp):
        text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        u = resp.get("usage", {})
        stop = resp.get("stop_reason")
        return text, u.get("input_tokens", 0), u.get("output_tokens", 0), stop if stop in ("max_tokens", "refusal") else None

    def call(self, rq, timeout):
        url, h, b = self.build(rq)
        try:
            return self.parse(_post(url, h, b, timeout))
        except ProviderHTTPError as e:
            # Some hosts (e.g. older Foundry deployments) reject newer request fields or API versions.
            # Retry once without them; the prompt still asks for JSON and the schema check still runs.
            msg = str(e).lower()
            if self.plain or e.status != 400 or not any(w in msg for w in ("output_config", "effort", "json_schema", "anthropic-version", "extra inputs", "not permitted", "unexpected")):
                raise
            self.plain = True
            url, h, b = self.build(rq)
            return self.parse(_post(url, h, b, timeout))


class OpenAIAdapter:
    name = "openai"

    def __init__(self, api_key, base_url=None):
        self.key = api_key
        self.url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/") + "/chat/completions"

    def build(self, rq):
        content = [{"type": "text", "text": rq["user_text"]}]
        for mime, data, fn in rq.get("attachments", []):
            url = f"data:{mime};base64,{_b64(data)}"
            if mime == "application/pdf":
                content.append({"type": "file", "file": {"filename": fn or "document.pdf", "file_data": url}})
            else:
                content.append({"type": "image_url", "image_url": {"url": url}})
        body = {"model": rq["model"], "max_completion_tokens": rq.get("max_tokens", 4000),
                "messages": [{"role": "system", "content": rq["system"]}, {"role": "user", "content": content}]}
        if rq.get("schema"):
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": rq.get("schema_name", "result"), "strict": True,
                                                       "schema": rq["schema"]}}
        return self.url, {"Authorization": f"Bearer {self.key}"}, body

    def parse(self, resp):
        ch = resp["choices"][0]
        text = ch["message"].get("content") or ""
        u = resp.get("usage", {})
        stop = {"length": "max_tokens", "content_filter": "refusal"}.get(ch.get("finish_reason"))
        if ch["message"].get("refusal"):
            stop = "refusal"
        return text, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), stop

    def call(self, rq, timeout):
        url, h, b = self.build(rq)
        return self.parse(_post(url, h, b, timeout))


class GeminiAdapter:
    name = "gemini"

    def __init__(self, api_key, base_url=None):
        self.key = api_key
        self.base = (base_url or os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com")).rstrip("/")

    def build(self, rq):
        parts = [{"text": rq["user_text"]}]
        for mime, data, _fn in rq.get("attachments", []):
            parts.append({"inline_data": {"mime_type": mime, "data": _b64(data)}})
        text = rq["user_text"]
        if rq.get("schema"):
            parts[0] = {"text": text + "\n\nReturn JSON matching this JSON Schema exactly:\n" + json.dumps(rq["schema"])}
        body = {"systemInstruction": {"parts": [{"text": rq["system"]}]},
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"responseMimeType": "application/json",
                                     "maxOutputTokens": rq.get("max_tokens", 4000)}}
        url = f"{self.base}/v1beta/models/{rq['model']}:generateContent"
        return url, {"x-goog-api-key": self.key}, body

    def parse(self, resp):
        cands = resp.get("candidates") or []
        if not cands:
            raise ProviderHTTPError(200, f"no candidates: {json.dumps(resp)[:200]}")
        text = "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []))
        u = resp.get("usageMetadata", {})
        stop = {"MAX_TOKENS": "max_tokens", "SAFETY": "refusal", "RECITATION": "refusal"}.get(cands[0].get("finishReason"))
        return text, u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0), stop

    def call(self, rq, timeout):
        url, h, b = self.build(rq)
        return self.parse(_post(url, h, b, timeout))


class MockAdapter:
    """Offline stand-in. Each AI feature passes a `mock` callable that produces a
    plausible answer from the inputs; it still flows through parsing,
    validation, logging and cost accounting like a real call."""
    name = "mock"

    def call(self, rq, timeout):
        fn = rq.get("mock")
        if not fn:
            raise ProviderHTTPError(500, "mock adapter has no answer for this request")
        out = fn()
        text = json.dumps(out)
        return text, len(rq["user_text"]) // 4 + 400 * len(rq.get("attachments", [])), len(text) // 4
