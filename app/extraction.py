"""Document intelligence: field extraction, confidence scoring, form validation
and document-vs-form consistency.

Documents arrive as structured payloads: {doc_type, filename, text, quality}
where `text` is OCR output and `quality` is an image-quality score (0..1).
This is the seam where a real OCR / vision-LLM extractor plugs in: it only has
to return the same {field: (value, confidence)} shape from `extract_document`.
"""
import difflib
import re

from .privacy import mask

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
ACCT_RE = re.compile(r"^[0-9]{9,18}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$")
PHONE_RE = re.compile(r"^[6-9][0-9]{9}$")

DOC_LABELS = {
    "PAN_CARD": "PAN card",
    "GST_CERTIFICATE": "GST registration certificate",
    "CANCELLED_CHEQUE": "cancelled cheque / bank letter",
}
DOC_BLOCKS_CHECK = {"PAN_CARD": "PAN", "GST_CERTIFICATE": "GST", "CANCELLED_CHEQUE": "BANK"}

FIELD_LABELS = {
    "legal_name": "Legal name", "trade_name": "Trade name", "business_type": "Business type",
    "category": "Business category", "email": "Email", "phone": "Phone", "pan": "PAN",
    "gstin": "GSTIN", "bank_account": "Bank account number", "ifsc": "IFSC",
    "account_holder": "Account holder name",
}
# Which verification check cannot run while this field is missing/invalid.
FIELD_BLOCKS = {
    "legal_name": ["PAN", "GST", "BANK", "RISK"], "category": ["RISK"], "email": ["RISK"],
    "phone": ["RISK"], "pan": ["PAN", "GST", "RISK"], "gstin": ["GST"],
    "bank_account": ["BANK"], "ifsc": ["BANK"], "account_holder": ["BANK"],
    "business_type": [], "trade_name": [],
}

# (doc_type, extracted_field) -> (form_field, kind, base_confidence, pattern)
_LBL = r"[ \t]*[:\-][ \t]*([^\n]+)"
EXTRACTORS = {
    "PAN_CARD": [
        ("pan", "id", 0.97, re.compile(r"\b([A-Z]{5}[0-9]{4}[A-Z])\b")),
        ("legal_name", "name", 0.90, re.compile(r"(?im)^\s*Name" + _LBL)),
    ],
    "GST_CERTIFICATE": [
        ("gstin", "id", 0.97, re.compile(r"\b([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b")),
        ("legal_name", "name", 0.90, re.compile(r"(?im)^\s*Legal Name" + _LBL)),
    ],
    "CANCELLED_CHEQUE": [
        ("bank_account", "id", 0.95, re.compile(r"(?i)(?:A/?C|Account)\s*(?:No\.?|Number)?\s*[:\-]?\s*([0-9]{9,18})\b")),
        ("ifsc", "id", 0.97, re.compile(r"\b([A-Z]{4}0[A-Z0-9]{6})\b")),
        ("account_holder", "name", 0.85, re.compile(r"(?im)^\s*(?:Account Holder|A/?c Holder)" + _LBL)),
    ],
}

_NAME_NOISE = {"M/S", "MS", "PVT", "PRIVATE", "LTD", "LIMITED", "LLP", "THE", "AND", "&", "CO", "COMPANY"}


def normalize_name(s: str) -> str:
    s = re.sub(r"[^A-Z0-9&/ ]", " ", (s or "").upper())
    return " ".join(t for t in s.split() if t not in _NAME_NOISE)


def name_similarity(a: str, b: str) -> float:
    a, b = normalize_name(a), normalize_name(b)
    if not a or not b:
        return 0.0
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    tok = len(ta & tb) / max(len(ta | tb), 1)
    return round(max(seq, tok), 3)


def estimate_quality(text: str) -> float:
    """Fallback when the payload carries no quality score: share of
    unreadable glyphs ('?' / '#') that OCR emitted."""
    if not text:
        return 0.0
    bad = sum(text.count(c) for c in "?#")
    return round(max(0.0, 1.0 - 4 * bad / max(len(text), 1)), 2)


def extract_document(doc: dict) -> dict:
    """Returns {quality, fields: {form_field: {value, confidence}}}."""
    text = doc.get("content") or doc.get("text") or ""
    q = doc.get("quality")
    quality = float(q) if q is not None else estimate_quality(text)
    out = {}
    for field, kind, base, rx in EXTRACTORS.get(doc["doc_type"], []):
        m = rx.search(text)
        if not m:
            continue
        val = m.group(1).strip()
        if kind == "id":
            val = re.sub(r"\s", "", val).upper()
        out[field] = {"value": val, "confidence": round(base * min(1.0, quality / 0.9), 3)}
    return {"quality": quality, "fields": out}


def validate_form(form: dict) -> list:
    """Completeness + format + internal-consistency rules on the form itself."""
    issues = []

    def add(field, code, msg):
        issues.append({"code": code, "field": field, "message": msg, "blocks": FIELD_BLOCKS.get(field, [])})

    required = ["legal_name", "business_type", "category", "email", "phone", "pan",
                "bank_account", "ifsc", "account_holder"]
    if form.get("gst_registered"):
        required.append("gstin")
    for f in required:
        if not form.get(f):
            add(f, "MISSING_FIELD", f"{FIELD_LABELS[f]} is required.")

    checks = [("pan", PAN_RE, "PAN must look like ABCDE1234F."),
              ("ifsc", IFSC_RE, "IFSC must be 11 characters, e.g. HDFC0001234."),
              ("bank_account", ACCT_RE, "Account number must be 9-18 digits."),
              ("email", EMAIL_RE, "Email address is not valid."),
              ("phone", PHONE_RE, "Phone must be a 10-digit Indian mobile number.")]
    if form.get("gst_registered"):
        checks.append(("gstin", GSTIN_RE, "GSTIN must be 15 characters, e.g. 27ABCDE1234F1Z5."))
    for f, rx, msg in checks:
        if form.get(f) and not rx.match(form[f]):
            add(f, "INVALID_FORMAT", msg)

    pan, gstin = form.get("pan") or "", form.get("gstin") or ""
    if form.get("gst_registered") and GSTIN_RE.match(gstin) and PAN_RE.match(pan) and gstin[2:12] != pan:
        add("gstin", "INCONSISTENT", "GSTIN does not contain the PAN given in the form (characters 3-12 must equal the PAN).")
    return issues


COMPARE = {
    "PAN_CARD": [("pan", "id"), ("legal_name", "name")],
    "GST_CERTIFICATE": [("gstin", "id"), ("legal_name", "name")],
    "CANCELLED_CHEQUE": [("bank_account", "id"), ("ifsc", "id"), ("account_holder", "name")],
}


def run_intake(form: dict, documents: list, cfg) -> dict:
    """One intake pass over the form and the *already extracted* documents.

    documents: [{id, doc_type, filename, status, extraction}] where status is
    EXTRACTED | EXTRACTING | MANUAL_REQUIRED and extraction is
    {quality, fields{field:{value,confidence}}, method, document_type_detected?, tamper_signals?, injection?}

    Returns
      fields      - comparison table (masked, for display)
      missing     - things only the merchant can fix
      conflicts   - high-confidence doc vs form mismatches (reviewer judgement)
      verify      - low-confidence reads a reviewer should confirm against the image
      manual      - documents nobody could read automatically (reviewer keys them in)
      tamper      - tampering / embedded-instruction signals
      pending     - checks waiting for an extraction still running
    """
    missing = validate_form(form)
    invalid_fields = {m["field"] for m in missing}
    conflicts, fields, doc_quality, verify, manual, tamper, pending = [], [], [], [], [], [], set()

    required_docs = ["PAN_CARD", "CANCELLED_CHEQUE"] + (["GST_CERTIFICATE"] if form.get("gst_registered") else [])
    by_type = {d["doc_type"]: d for d in documents}

    for dt in required_docs:
        if dt not in by_type:
            missing.append({"code": "MISSING_DOCUMENT", "field": dt,
                            "message": f"Please upload your {DOC_LABELS[dt]}.",
                            "blocks": [DOC_BLOCKS_CHECK[dt]]})

    for dt, doc in by_type.items():
        if dt not in COMPARE or dt not in required_docs:
            continue
        chk = DOC_BLOCKS_CHECK[dt]
        meta = {"doc_id": doc.get("id"), "doc_type": dt, "filename": doc.get("filename"), "status": doc["status"]}
        if doc["status"] == "EXTRACTING":
            pending.add(chk)
            doc_quality.append({**meta, "quality": None, "readable": None})
            continue
        if doc["status"] == "MANUAL_REQUIRED" or not doc.get("extraction"):
            manual.append({"doc_id": doc.get("id"), "doc_type": dt, "check": chk})
            doc_quality.append({**meta, "quality": None, "readable": None})
            continue
        ex = doc["extraction"]
        human = ex.get("method") == "human"
        readable = human or ex["quality"] >= cfg.min_doc_quality
        doc_quality.append({**meta, "quality": ex["quality"], "readable": readable, "method": ex.get("method")})
        detected = ex.get("document_type_detected")
        if detected and detected not in ("OTHER", dt):
            missing.append({"code": "WRONG_DOCUMENT_TYPE", "field": dt,
                            "message": f"The file uploaded as your {DOC_LABELS[dt]} looks like a "
                                       f"{DOC_LABELS.get(detected, detected)}. Please upload the right document.",
                            "blocks": [chk]})
            continue
        if ex.get("injection") or ex.get("tamper_signals"):
            tamper.append({"doc_id": doc.get("id"), "doc_type": dt, "signals": ex.get("tamper_signals") or [],
                           "injection": bool(ex.get("injection"))})
        if not readable:
            missing.append({"code": "DOCUMENT_UNREADABLE", "field": dt,
                            "message": f"Your {DOC_LABELS[dt]} is blurred or unreadable "
                                       f"(quality {int(ex['quality'] * 100)}%). Please upload a clearer image.",
                            "blocks": [chk]})

        low = []
        for field, kind in COMPARE[dt]:
            got = ex["fields"].get(field)
            form_val = form.get(field)
            row = {"field": field, "label": FIELD_LABELS[field], "doc_type": dt, "doc_id": doc.get("id"),
                   "form_value": mask(field, form_val), "doc_value": None, "confidence": None,
                   "method": ex.get("method")}
            if not readable:
                row["status"] = "UNREADABLE"
                fields.append(row)
                continue
            if not got:
                row["status"] = "NOT_FOUND_IN_DOC"
                fields.append(row)
                low.append(field)
                continue
            row["doc_value"] = mask(field, got["value"])
            row["confidence"] = got["confidence"]
            if got["confidence"] < cfg.min_field_confidence and not human:
                row["status"] = "LOW_CONFIDENCE"
                low.append(field)
            elif not form_val or field in invalid_fields:
                row["status"] = "FORM_INVALID"
            else:
                if kind == "id":
                    same = got["value"] == form_val
                    sim = 1.0 if same else 0.0
                else:
                    sim = name_similarity(got["value"], form_val)
                    same = sim >= cfg.name_match_threshold
                row["similarity"] = sim
                row["status"] = "MATCH" if same else "MISMATCH"
                if not same:
                    conflicts.append({
                        "field": field, "doc_type": dt, "confidence": got["confidence"], "doc_id": doc.get("id"),
                        "message": f"{FIELD_LABELS[field]} on the {DOC_LABELS[dt]} does not match the "
                                   f"application form (doc {mask(field, got['value'])} vs form "
                                   f"{mask(field, form_val)}, " + ("confirmed by a reviewer" if human else
                                   f"extraction confidence {got['confidence']:.2f}") + ").",
                    })
            fields.append(row)
        if low and readable:
            verify.append({"doc_id": doc.get("id"), "doc_type": dt, "fields": low})

    return {"fields": fields, "doc_quality": doc_quality, "missing": missing, "conflicts": conflicts,
            "verify": verify, "manual": manual, "tamper": tamper, "pending": pending}
