"""Simulated external dependencies.

* Verification providers (PAN, GST, BANK penny-drop, RISK) accept a request
  synchronously and answer later through a signed webhook callback.
* Merchant-ID service creates the merchant in the "core payments" system.

Business outcomes are driven by magic test values (like a card network's
test card numbers) so scenarios are reproducible. Operational faults are
driven by per-provider *modes* that an operator can flip at runtime:

    normal     - callback after 1-3s
    slow       - callback after the timeout window (exercises timeout + late callback)
    duplicate  - every callback delivered 3 times (exercises idempotency)
    outage     - request submission fails (exercises retry/backoff/DLQ)
    flaky      - every other submission fails
    error      - provider accepts, then calls back with status=ERROR
"""
import hashlib
import hmac
import json
import random
import threading
import uuid

from .extraction import normalize_name

PROVIDER_MODES = ("normal", "slow", "duplicate", "outage", "flaky", "error")
MID_MODES = ("normal", "fail_once", "timeout_after_commit", "fail_always")
HIGH_RISK_CATEGORIES = {"gambling", "online gaming", "crypto", "forex", "adult", "betting"}
FREE_EMAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "rediffmail.com"}


class ProviderUnavailable(Exception):
    pass


class MIDServiceError(Exception):
    pass


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- verdicts
def pan_verdict(p):
    pan = p["pan"]
    if pan[5:9] == "0000":
        return {"pan_status": "NOT_FOUND"}
    return {"pan_status": "VALID", "registered_name": p["legal_name"].upper(),
            "holder_type": {"P": "INDIVIDUAL", "C": "COMPANY", "F": "FIRM"}.get(pan[3], "OTHER")}


def gst_verdict(p):
    g = p["gstin"]
    if g[7:11] == "0000":
        return {"gst_status": "NOT_FOUND"}
    status = "CANCELLED" if g[12] == "9" else "ACTIVE"
    return {"gst_status": status, "legal_name": p["legal_name"].upper(),
            "pan_linked": g[2:12] == p["pan"], "state_code": g[:2]}


def bank_verdict(p):
    acct = p["bank_account"]
    if acct.endswith("0000"):
        return {"account_status": "INVALID"}
    if acct.endswith("1111"):
        name = "RAJESH KUMAR TRADERS"                       # someone else's account
    elif acct.endswith("2222"):                             # bank truncates/abbreviates the name
        n = normalize_name(p["legal_name"])
        name = "M/S " + n[:max(4, int(len(n) * 0.6))].strip()   # core-banking field truncation
    else:
        name = p["account_holder"].upper()
    return {"account_status": "VALID", "beneficiary_name": name, "penny_drop_amount": 1.00}


def risk_verdict(p):
    score, signals = 15, []
    cat = (p.get("category") or "").lower()
    if cat in HIGH_RISK_CATEGORIES:
        score += 60
        signals.append(f"high-risk category: {cat}")
    if (p.get("email") or "").split("@")[-1] in FREE_EMAIL:
        score += 10
        signals.append("free email domain")
    if "TEST" in normalize_name(p.get("legal_name")).split():
        score += 30
        signals.append("name looks like test data")
    band = "HIGH" if score >= 70 else "MEDIUM" if score >= 40 else "LOW"
    return {"score": score, "band": band, "signals": signals}


VERDICTS = {"PAN": pan_verdict, "GST": gst_verdict, "BANK": bank_verdict, "RISK": risk_verdict}


class ProviderSimulator:
    """Holds the callbacks each provider 'owes' us and delivers them when due.

    `deliver(provider, body_bytes, signature)` is injected: in the server it is
    an HTTP POST to our own webhook endpoint; in tests it calls the engine's
    webhook handler directly. Either way the signature is verified."""

    def __init__(self, clock, cfg, deliver=None, seed=None):
        self.clock, self.cfg, self.deliver = clock, cfg, deliver
        self.modes = {k: "normal" for k in VERDICTS}
        self.outbox = []  # (due_at, provider, event_dict)
        self.lock = threading.Lock()
        self.rng = random.Random(seed)
        self.calls = {k: 0 for k in VERDICTS}
        self.delivered = 0

    def set_mode(self, provider, mode):
        if provider not in self.modes or mode not in PROVIDER_MODES:
            raise ValueError(f"unknown provider/mode {provider}/{mode}")
        self.modes[provider] = mode

    def submit(self, provider, request_id, payload):
        mode = self.modes[provider]
        with self.lock:
            self.calls[provider] += 1
            n = self.calls[provider]
        if mode == "outage" or (mode == "flaky" and n % 2 == 1):
            raise ProviderUnavailable(f"{provider} provider returned 503")
        delay = self.rng.uniform(self.cfg.provider_min_delay_s, self.cfg.provider_max_delay_s)
        if mode == "slow":
            delay = self.cfg.check_timeout_s + self.cfg.slow_mode_extra_delay_s
        event = {"event_id": "EVT-" + uuid.uuid4().hex[:16], "provider": provider,
                 "request_id": request_id, "sent_at": self.clock.now() + delay}
        if mode == "error":
            event.update(status="ERROR", error="provider internal error")
        else:
            event.update(status="COMPLETED", result=VERDICTS[provider](payload))
        due = self.clock.now() + delay
        with self.lock:
            copies = 3 if mode == "duplicate" else 1
            for i in range(copies):
                self.outbox.append((due + 0.2 * i, provider, event))
        return {"accepted": True, "provider_ref": "PRV-" + uuid.uuid4().hex[:10], "eta_s": round(delay, 1)}

    def pending(self):
        with self.lock:
            return len(self.outbox)

    def deliver_due(self):
        now = self.clock.now()
        with self.lock:
            due = [e for e in self.outbox if e[0] <= now]
            self.outbox = [e for e in self.outbox if e[0] > now]
        due.sort(key=lambda e: e[0])
        for _, provider, event in due:
            body = json.dumps(event).encode()
            try:
                self.deliver(provider, body, sign(self.cfg.webhook_secret, body))
                self.delivered += 1
            except Exception:  # provider would retry; we rely on our own timeouts
                pass
        return len(due)


class MerchantIdService:
    """Mock core-payments merchant creation. Idempotent on idempotency_key,
    which is what makes 'created but response lost' safe to retry."""

    def __init__(self, db):
        self.db = db
        self.mode = "normal"
        self._failed_once = set()
        self._timed_out = set()

    def set_mode(self, mode):
        if mode not in MID_MODES:
            raise ValueError(mode)
        self.mode = mode
        self._failed_once.clear()
        self._timed_out.clear()

    def create(self, idempotency_key, legal_name, now):
        if self.mode == "fail_always":
            raise MIDServiceError("merchant service 503 Service Unavailable")
        if self.mode == "fail_once" and idempotency_key not in self._failed_once:
            self._failed_once.add(idempotency_key)
            raise MIDServiceError("merchant service 500 Internal Error")
        row = self.db.execute("SELECT merchant_id FROM mid_registry WHERE idempotency_key=?",
                              (idempotency_key,)).fetchone()
        if row:
            return row[0], True  # replayed
        n = self.db.execute("SELECT COUNT(*) FROM mid_registry").fetchone()[0]
        mid = f"MID{100000 + n + 1:07d}"
        self.db.execute("INSERT INTO mid_registry VALUES (?,?,?)", (idempotency_key, mid, now))
        if self.mode == "timeout_after_commit" and idempotency_key not in self._timed_out:
            self._timed_out.add(idempotency_key)
            raise MIDServiceError("timeout waiting for merchant service response (record may exist)")
        return mid, False
