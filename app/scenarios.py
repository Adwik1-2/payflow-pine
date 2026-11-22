"""Preset applications for demos and tests. Identifiers are synthetic and
randomised per call; magic values steer the mock providers:

  PAN digits 0000           -> PAN not found          (correctable)
  GSTIN 13th char '9'       -> GST registration CANCELLED
  account ending 0000       -> penny-drop fails       (correctable)
  account ending 1111       -> bank beneficiary name mismatch
  account ending 2222       -> bank shows an abbreviated name (AI name adjudication)
  category gambling/crypto  -> HIGH risk
"""
import random
import string

U = string.ascii_uppercase


def _pan(rng, holder="F", first="S", digits=None):
    return ("".join(rng.choice(U) for _ in range(3)) + holder + first
            + (digits or f"{rng.randint(1, 9999):04d}") + rng.choice(U))


def _gstin(rng, pan, entity="1"):
    return "27" + pan + entity + "Z" + rng.choice(U + string.digits)


def _acct(rng, suffix=None):
    base = "".join(rng.choice(string.digits) for _ in range(10))
    return (base + (suffix or f"{rng.randint(1000, 9998):04d}")).lstrip("0") or "1234567891234"


def _ifsc(rng):
    return rng.choice(["HDFC", "ICIC", "SBIN", "UTIB"]) + "0" + "".join(rng.choice(string.digits) for _ in range(6))


def _degrade(text, quality, rng):
    out = []
    for ch in text:
        if ch.isalnum() and rng.random() < (1 - quality) * 0.7:
            out.append(rng.choice("?#"))
        else:
            out.append(ch)
    return "".join(out)


def pan_card(pan, name, quality=0.95, rng=None):
    t = (f"INCOME TAX DEPARTMENT\nGOVT. OF INDIA\nPermanent Account Number Card\n{pan}\n"
         f"Name: {name}\nDate of Incorporation/Formation: 12/04/2016\n")
    return {"doc_type": "PAN_CARD", "filename": "pan_card.jpg", "quality": quality,
            "text": _degrade(t, quality, rng) if rng and quality < 0.6 else t}


def gst_cert(gstin, name, trade, quality=0.95, rng=None):
    t = (f"Government of India\nForm GST REG-06\nRegistration Certificate\nRegistration Number: {gstin}\n"
         f"Legal Name: {name}\nTrade Name: {trade}\nConstitution of Business: Partnership\n"
         f"Date of Liability: 01/07/2017\n")
    return {"doc_type": "GST_CERTIFICATE", "filename": "gst_certificate.pdf", "quality": quality,
            "text": _degrade(t, quality, rng) if rng and quality < 0.6 else t}


def cheque(acct, ifsc, holder, quality=0.95, rng=None):
    t = f"HDFC BANK LTD\nBranch: Andheri East, Mumbai\nIFSC: {ifsc}\nA/C No: {acct}\nAccount Holder: {holder}\nCANCELLED\n"
    return {"doc_type": "CANCELLED_CHEQUE", "filename": "cancelled_cheque.jpg", "quality": quality,
            "text": _degrade(t, quality, rng) if rng and quality < 0.6 else t}


SCENARIOS = {
    "happy_path": "Clean application - every check passes, merchant goes live",
    "gst_cancelled": "PAN verifies but GST registration is cancelled -> manual review",
    "blurred_document": "Cancelled cheque is blurred -> merchant asked to re-upload",
    "document_conflict": "PAN on the card differs from the form -> manual review",
    "high_risk": "Online gambling business -> risk HIGH -> manual review",
    "bank_name_mismatch": "Bank account belongs to a different name -> cannot be overridden",
    "not_gst_registered": "Small shop below GST threshold -> GST check not required",
    "pan_not_found": "Typo in PAN -> registry says not found -> merchant asked to correct",
    "duplicate": "Re-uses the identifiers of the latest application -> duplicate flagged",
    "abbreviated_bank_name": "Bank shows 'M/S SHARMA ELEC' -> AI confirms it is the same business",
    "category_mismatch": "Says 'retail' but the description is a real-money gaming app -> AI flags it",
    "reused_document": "Uploads the exact PAN card file of the latest applicant -> fraud signal",
}

DESCRIPTIONS = {
    "retail": "Neighbourhood shop selling everyday products to walk-in customers; wants UPI and card payments.",
    "gambling": "Online rummy and fantasy cricket platform with real-money contests.",
}

NAMES = ["Sharma Electronics", "Gupta Sweets", "Patel Hardware", "Iyer Textiles", "Khan Auto Parts",
         "Mehta Pharma Distributors", "Reddy Organics", "Das Book House", "Kapoor Furnishings",
         "Nair Spices", "Joshi Stationers", "Verma Mobile Hub"]


def build(name, rng=None, latest=None):
    rng = rng or random.Random()
    base = rng.choice(NAMES)
    legal = f"{base} LLP" if rng.random() < 0.5 else base
    trade = base
    pan = _pan(rng, first=base[0])
    form = {"legal_name": legal, "trade_name": trade, "business_type": "Partnership", "category": "retail",
            "gst_registered": True, "email": f"accounts@{base.split()[0].lower()}{rng.randint(10, 99)}.in",
            "phone": f"9{rng.randint(100000000, 999999999)}", "pan": pan, "gstin": _gstin(rng, pan),
            "bank_account": _acct(rng), "ifsc": _ifsc(rng), "account_holder": legal,
            "business_description": DESCRIPTIONS["retail"]}
    docs_q = {"pan": 0.95, "gst": 0.93, "bank": 0.92}

    if name == "gst_cancelled":
        form["gstin"] = _gstin(rng, pan, entity="9")
    elif name == "blurred_document":
        docs_q["bank"] = 0.32
    elif name == "document_conflict":
        pass
    elif name == "high_risk":
        form["category"] = "gambling"
        form["business_description"] = DESCRIPTIONS["gambling"]
        form["legal_name"] = form["account_holder"] = "Lucky Star Gaming LLP"
        form["trade_name"] = "Lucky Star"
    elif name == "bank_name_mismatch":
        form["bank_account"] = _acct(rng, suffix="1111")
    elif name == "not_gst_registered":
        form.update(gst_registered=False, gstin=None, business_type="Proprietorship",
                    legal_name="Lakshmi Kirana Store", trade_name="Lakshmi Kirana", account_holder="Lakshmi Kirana Store")
        form["pan"] = _pan(rng, holder="P", first="L")
    elif name == "pan_not_found":
        form["pan"] = form["pan"][:5] + "0000" + form["pan"][-1]
        form["gstin"] = _gstin(rng, form["pan"])
    elif name == "duplicate" and latest:
        for k in ("pan", "gstin", "bank_account", "ifsc"):
            form[k] = latest[k]
        form["gst_registered"] = bool(latest["gstin"])
    elif name == "abbreviated_bank_name":
        form["bank_account"] = _acct(rng, suffix="2222")
    elif name == "category_mismatch":
        form["business_description"] = ("Mobile app where users play rummy and poker tournaments for real money "
                                         "and withdraw winnings to their bank account.")
    elif name == "reused_document":
        pass
    elif name not in SCENARIOS:
        raise KeyError(name)

    pan_on_card = form["pan"]
    if name == "document_conflict":
        pan_on_card = pan_on_card[:5] + f"{(int(pan_on_card[5:9]) + 1111) % 10000:04d}" + pan_on_card[-1]
    docs = [pan_card(pan_on_card, form["legal_name"], docs_q["pan"], rng),
            cheque(form["bank_account"], form["ifsc"], form["account_holder"], docs_q["bank"], rng)]
    if form["gst_registered"]:
        docs.append(gst_cert(form["gstin"], form["legal_name"], form["trade_name"], docs_q["gst"], rng))
    if name == "reused_document" and latest and latest.get("pan_doc"):
        docs[0] = dict(latest["pan_doc"])
    return form, docs
