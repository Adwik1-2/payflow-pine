"""Sensitive-data handling: masking for every operator-facing output and
keyed fingerprints for duplicate detection without comparing raw values."""
import hashlib
import hmac
import json
import re


def mask_pan(v):
    return v[:3] + "X" * 5 + v[-2:] if v and len(v) >= 10 else _mask_generic(v)


def mask_gstin(v):
    return v[:2] + "X" * 10 + v[-3:] if v and len(v) >= 15 else _mask_generic(v)


def mask_account(v):
    return "X" * max(len(v) - 4, 0) + v[-4:] if v and len(v) > 4 else _mask_generic(v)


def mask_email(v):
    if not v or "@" not in v:
        return _mask_generic(v)
    user, dom = v.split("@", 1)
    return user[:1] + "***@" + dom


def mask_phone(v):
    return "X" * 6 + v[-4:] if v and len(v) >= 10 else _mask_generic(v)


def _mask_generic(v):
    if not v:
        return v
    return v[:1] + "*" * max(len(v) - 2, 1) + v[-1:] if len(v) > 2 else "**"


MASKERS = {
    "pan": mask_pan,
    "gstin": mask_gstin,
    "bank_account": mask_account,
    "email": mask_email,
    "phone": mask_phone,
}

SENSITIVE_FIELDS = set(MASKERS)


def mask(field, value):
    if value is None or value == "":
        return value
    fn = MASKERS.get(field)
    return fn(str(value)) if fn else value


def fingerprint(secret: str, value) -> str:
    """Keyed hash so identical identifiers can be matched without storing
    or comparing them in clear in the dedupe index."""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True)
    return hmac.new(secret.encode(), str(value).encode(), hashlib.sha256).hexdigest()[:32]


_PAN_RE = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
_ACCT_RE = re.compile(r"\b[0-9]{9,18}\b")


def scrub_text(text: str) -> str:
    """Defence in depth: redact anything that looks like a PAN or account
    number from free text before it reaches logs/audit."""
    text = _PAN_RE.sub(lambda m: mask_pan(m.group(0)), text)
    return _ACCT_RE.sub(lambda m: mask_account(m.group(0)), text)
