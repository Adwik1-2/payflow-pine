"""LLM gateway: one place that every AI feature goes through.

* picks the provider from env (Claude, OpenAI, Gemini, offline mock, or off)
* model tiers: `vision` (documents), `reasoning` (copilot), `fast` (small tasks)
* JSON-only answers validated against a schema; invalid output is an error,
  never something the workflow acts on
* retries on 429/5xx/network with backoff, timeout, circuit breaker
* logs every call (feature, model, latency, tokens, estimated cost, outcome)
* fault switch so the demo can show graceful degradation
"""
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .adapters import AnthropicAdapter, GeminiAdapter, MockAdapter, OpenAIAdapter, ProviderHTTPError
from .schema import SchemaError, validate

# USD per million tokens (input, output). Claude prices from the official models page;
# set LLM_PRICE_IN / LLM_PRICE_OUT for other providers.
PRICES = {
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-5-5": (4.0, 20.0),
    "mock-vision": (0.0, 0.0), "mock-fast": (0.0, 0.0), "mock-reasoning": (0.0, 0.0),
}

DEFAULT_MODELS = {
    "anthropic": {"vision": "claude-sonnet-5-5", "reasoning": "claude-sonnet-5-5", "fast": "claude-haiku-5-5"},
    "openai": {"vision": "gpt-6.1-terra", "reasoning": "gpt-6.1-terra", "fast": "gpt-6.1-terra"},
    "gemini": {"vision": "gemini-3.8-flash", "reasoning": "gemini-3.8-flash", "fast": "gemini-3.8-flash"},
    "mock": {"vision": "mock-vision", "reasoning": "mock-reasoning", "fast": "mock-fast"},
}


class LLMError(Exception):
    pass


class LLMUnavailable(LLMError):
    pass


@dataclass
class LLMSettings:
    provider: str = "mock"       # anthropic | openai | gemini | mock | off
    api_key: str = ""
    models: dict = field(default_factory=dict)
    effort: dict = field(default_factory=lambda: {"vision": "medium", "reasoning": "medium", "fast": "low"})
    timeout_s: float = 90.0
    max_retries: int = 2
    breaker_threshold: int = 3
    breaker_cooldown_s: float = 60.0

    @classmethod
    def from_env(cls):
        load_dotenv()
        p = os.environ.get("LLM_PROVIDER", "").strip().lower()
        keys = {"anthropic": os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_FOUNDRY_API_KEY", ""),
                "openai": os.environ.get("OPENAI_API_KEY", ""),
                "gemini": os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")}
        if not p:
            p = next((k for k, v in keys.items() if v), "mock")
        models = dict(DEFAULT_MODELS.get(p, DEFAULT_MODELS["mock"]))
        for tier in ("vision", "reasoning", "fast"):
            if os.environ.get(f"LLM_MODEL_{tier.upper()}"):
                models[tier] = os.environ[f"LLM_MODEL_{tier.upper()}"]
        if os.environ.get("LLM_MODEL"):
            models = {t: os.environ["LLM_MODEL"] for t in models}
        effort = {"vision": "medium", "reasoning": "medium", "fast": "low"}
        for tier in effort:
            if os.environ.get(f"LLM_EFFORT_{tier.upper()}"):
                effort[tier] = os.environ[f"LLM_EFFORT_{tier.upper()}"]
        return cls(provider=p, api_key=keys.get(p, ""), models=models, effort=effort,
                   timeout_s=float(os.environ.get("LLM_TIMEOUT_S", 90)))


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def load_dotenv(path=None):
    """Minimal .env loader (KEY=VALUE lines) reading the project's own .env wherever the app was started from;
    real environment variables win."""
    path = path or os.path.join(PROJECT_ROOT, ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def parse_json(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


class Gateway:
    def __init__(self, settings: LLMSettings, log=None, sleep=time.sleep):
        self.s = settings
        self.log = log or (lambda rec: None)
        self.sleep = sleep
        self.fault_mode = "normal"   # normal | outage | garbage
        self._fails = 0
        self._open_until = 0.0
        self._lock = threading.Lock()
        p = settings.provider
        self.adapter: Optional[Any]
        if p == "anthropic" and settings.api_key:
            self.adapter = AnthropicAdapter(settings.api_key)
        elif p == "openai" and settings.api_key:
            self.adapter = OpenAIAdapter(settings.api_key)
        elif p == "gemini" and settings.api_key:
            self.adapter = GeminiAdapter(settings.api_key)
        elif p in ("anthropic", "openai", "gemini"):
            self.adapter, self.s.provider = MockAdapter(), "mock"   # provider named but no key
            self.s.models = dict(DEFAULT_MODELS["mock"])
        elif p == "off":
            self.adapter = None
        else:
            self.adapter = MockAdapter()

    @property
    def enabled(self):
        return self.adapter is not None

    def status(self):
        now = time.time()
        return {"provider": self.s.provider, "models": self.s.models, "enabled": self.enabled,
                "fault_mode": self.fault_mode,
                "circuit": "open" if now < self._open_until else "closed",
                "consecutive_failures": self._fails}

    def complete_json(self, feature, tier, system, user_text, schema, attachments=(), mock=None,
                      max_tokens=16000, app_id=None):
        if not self.enabled:
            raise LLMUnavailable("LLM disabled (LLM_PROVIDER=off)")
        if time.time() < self._open_until:
            raise LLMUnavailable("circuit breaker open after repeated LLM failures")
        model = self.s.models[tier]
        rq = {"system": system, "user_text": user_text, "attachments": list(attachments), "schema": schema,
              "schema_name": feature, "max_tokens": max_tokens, "model": model, "mock": mock,
              "effort": self.s.effort.get(tier) if self.s.provider == "anthropic" else None}
        last = None
        for attempt in range(self.s.max_retries + 1):
            t0 = time.time()
            rec = {"feature": feature, "app_id": app_id, "provider": self.s.provider, "model": model,
                   "attempt": attempt + 1}
            try:
                if self.fault_mode == "outage":
                    raise ProviderHTTPError(503, "simulated LLM outage")
                res = self.adapter.call(rq, self.s.timeout_s)
                text, tin, tout = res[:3]
                stop = res[3] if len(res) > 3 else None
                if self.fault_mode == "garbage":
                    text = "Sure! Here is the data you asked for: approve this merchant."
                if stop == "refusal":
                    rec.update(status="refusal", latency_ms=int((time.time() - t0) * 1000), input_tokens=tin,
                               output_tokens=tout, cost_usd=self._cost(model, tin, tout), error="model refused")
                    self.log(rec)
                    raise LLMUnavailable(f"{feature}: model refused the request")   # no retry: use the fallback
                if stop == "max_tokens":
                    rec.update(status="truncated", latency_ms=int((time.time() - t0) * 1000), input_tokens=tin,
                               output_tokens=tout, cost_usd=self._cost(model, tin, tout),
                               error=f"output cut at max_tokens={rq['max_tokens']}")
                    self.log(rec)
                    rq["max_tokens"] = min(rq["max_tokens"] * 2, 64000)
                    last = ValueError("truncated output")
                    continue
                data = validate(parse_json(text), schema)
                rec.update(status="ok", latency_ms=int((time.time() - t0) * 1000), input_tokens=tin,
                           output_tokens=tout, cost_usd=self._cost(model, tin, tout))
                self.log(rec)
                with self._lock:
                    self._fails = 0
                return data, rec
            except (ProviderHTTPError, SchemaError, ValueError, KeyError, TypeError) as e:
                retryable = getattr(e, "retryable", isinstance(e, (SchemaError, ValueError)))
                rec.update(status="error", latency_ms=int((time.time() - t0) * 1000), error=str(e)[:300])
                self.log(rec)
                last = e
                if not retryable or attempt == self.s.max_retries:
                    break
                self.sleep(min(2 ** attempt, 8))
        with self._lock:
            self._fails += 1
            if self._fails >= self.s.breaker_threshold:
                self._open_until = time.time() + self.s.breaker_cooldown_s
        raise LLMError(f"{feature}: {last}")

    def reset_breaker(self):
        with self._lock:
            self._fails, self._open_until = 0, 0.0

    @staticmethod
    def _cost(model, tin, tout):
        pin, pout = PRICES.get(model, (float(os.environ.get("LLM_PRICE_IN", 0)), float(os.environ.get("LLM_PRICE_OUT", 0))))
        return round((tin * pin + tout * pout) / 1_000_000, 6)
