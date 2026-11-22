"""Diagnose why the Anthropic API rejects a key, without printing the key.

    python tools/check_key.py

Shows where ANTHROPIC_API_KEY comes from (Windows/OS environment or .env), its
prefix, length and stray characters, any ANTHROPIC_BASE_URL override, and the
result of one tiny request to the API.
"""
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)


def read_dotenv(path=".env"):
    found = []
    if not os.path.exists(path):
        return found, False
    raw = open(path, "rb").read()
    bom = raw.startswith(b"\xef\xbb\xbf") or raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff")
    text = raw.decode("utf-8-sig", errors="replace") if not raw.startswith((b"\xff\xfe", b"\xfe\xff")) else raw.decode("utf-16", errors="replace")
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip().lstrip("﻿") == "ANTHROPIC_API_KEY":
            found.append((n, v.strip()))
    return found, bom


def describe(v):
    clean = v.strip().strip('"').strip("'")
    kind = ("API key (correct type)" if clean.startswith("sk-ant-api") else
            "OAuth/subscription token - does NOT work with the API" if clean.startswith("sk-ant-oat") else
            "admin key - does NOT work for messages" if clean.startswith("sk-ant-admin") else
            "empty" if not clean else "unknown format (expected sk-ant-api03-...)")
    odd = [c for c in clean if not (c.isascii() and (c.isalnum() or c in "-_"))]
    return clean, (f"starts with {clean[:12]!r}  length {len(clean)}  type: {kind}"
                   + (f"  odd characters: {sorted(set(map(repr, odd)))}" if odd else ""))


print("== Where the key comes from")
os_key = os.environ.get("ANTHROPIC_API_KEY")
dot, bom = read_dotenv()
if os_key is not None:
    print("  Windows/OS environment has ANTHROPIC_API_KEY ->", describe(os_key)[1])
    print("  NOTE: an OS environment variable WINS over .env. If this is an old key, remove it:")
    print("        PowerShell:  [Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY', $null, 'User')")
    print("        then close and reopen the terminal.")
else:
    print("  Windows/OS environment: not set (good, .env will be used)")
if not os.path.exists(".env"):
    print("  .env: NOT FOUND in", ROOT)
else:
    print(f"  .env: found{' (saved with a BOM/UTF-16 - re-save as UTF-8 if problems persist)' if bom else ''}")
    if not dot:
        print("  .env has no ANTHROPIC_API_KEY line")
    for n, v in dot:
        print(f"  .env line {n}:", describe(v)[1])
    if len(dot) > 1:
        print("  NOTE: the key appears more than once in .env; only the FIRST line is used. Keep one line.")

sys.path.insert(0, ROOT)
from app.llm.gateway import LLMSettings          # noqa: E402  (loads .env the same way the app does)
from app.llm.adapters import AnthropicAdapter, ProviderHTTPError   # noqa: E402

st = LLMSettings.from_env()
if st.provider != "anthropic" or not st.api_key:
    sys.exit(f"\nThe app would use provider={st.provider!r}; no Anthropic key found to test.")
ad = AnthropicAdapter(st.api_key)
print("\n== API address")
print("  calling:", ad.url, "(Microsoft Foundry / Azure: sends api-key header too)" if ad.azure else "")
print("  key used:", describe(st.api_key)[1] if not ad.azure else f"Azure key, length {len(st.api_key.strip())}")

print("\n== One tiny call per model tier (costs a fraction of a cent)")
hints = {401: "Key not accepted by this address. On Azure: copy KEY 1 from the resource's 'Keys and Endpoint' page.",
         403: "Key works but lacks permission for this deployment.",
         404: "Deployment/model name not found. On Azure set LLM_MODEL_VISION / LLM_MODEL_REASONING / LLM_MODEL_FAST in .env to your deployment names.",
         400: "Request rejected (see message). 'credit balance' means add credits; otherwise send this output to the developer.",
         429: "Rate limited - wait a minute and retry."}
ok_all = True
for tier, model in st.models.items():
    rq = {"model": model, "system": "Reply with the single word OK.", "user_text": "ping", "max_tokens": 64, "attachments": []}
    try:
        text, _, _, _ = ad.call(rq, 40)
        print(f"  {tier:<9} {model:<24} OK{'  (fell back to plain JSON mode)' if ad.plain else ''}")
    except ProviderHTTPError as e:
        ok_all = False
        print(f"  {tier:<9} {model:<24} FAILED {str(e)[:220]}")
        print(f"            hint: {hints.get(e.status, 'network problem' if e.status == 599 else 'see message above')}")
if not ok_all:
    print("\n== Looking for Claude deployments on this resource (404s are free)")
    names = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5", "claude-fable-5-2",
             "claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-6",
             "claude-sonnet-4-5", "claude-opus-4-5", "claude-haiku-4-5", "claude-opus-4-1",
             "claude-sonnet-4", "claude-opus-4", "claude-3-7-sonnet", "claude-3-5-haiku"]
    extra = [x.strip() for x in os.environ.get("CHECK_DEPLOYMENTS", "").split(",") if x.strip()]
    found = []
    for name in extra + [n for n in names if n not in st.models.values()]:
        rq = {"model": name, "system": "Reply with the single word OK.", "user_text": "ping", "max_tokens": 64, "attachments": []}
        try:
            ad.call(rq, 40)
            found.append(name)
            print(f"  FOUND  {name}")
        except ProviderHTTPError as e:
            if e.status != 404:
                print(f"  {name}: HTTP {e.status} {str(e)[:120]}")
    if found:
        big = next((n for n in found if "opus" in n or "sonnet" in n or "fable" in n), found[0])
        small = next((n for n in found if "haiku" in n), big)
        print("\nAdd these lines to .env, then run this again:")
        print(f"  LLM_MODEL_VISION={big}\n  LLM_MODEL_REASONING={big}\n  LLM_MODEL_FAST={small}")
    else:
        print("  None of the usual names exist. Open ai.azure.com > your resource > Deployments, copy the")
        print("  deployment NAME(s) and put them in .env as LLM_MODEL_VISION / LLM_MODEL_REASONING / LLM_MODEL_FAST.")
print("\nAll good - run: python tools\\live_e2e.py" if ok_all else "")
