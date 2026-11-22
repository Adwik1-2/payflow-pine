"""Start the onboarding platform:  python run.py [--port 8000] [--reset] [--demo [names]]"""
import argparse
import os

# Work from the project folder so .env and data/ are found however the server is started.
os.chdir(os.path.dirname(os.path.abspath(__file__)))
_SHELL_KEYS = [k for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "LLM_MODEL_VISION", "LLM_PROVIDER") if k in os.environ]

from app.server import serve  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="data/onboarding.db")
    ap.add_argument("--reset", action="store_true", help="delete the database first")
    ap.add_argument("--demo", nargs="?", const="happy_path,gst_cancelled", default="",
                    help="pre-load sample applications (default: happy_path,gst_cancelled; see /api/scenarios for names)")
    a = ap.parse_args()
    from app.scenarios import SCENARIOS
    demo = [x.strip() for x in a.demo.split(",") if x.strip()]
    unknown = [x for x in demo if x not in SCENARIOS]
    if unknown:
        ap.error(f"unknown scenario(s): {', '.join(unknown)}. Choose from: {', '.join(SCENARIOS)}")
    if _SHELL_KEYS:
        print("NOTE: these settings come from this terminal and override .env:", ", ".join(_SHELL_KEYS))
        print("      If that is not intended, open a new terminal (or Remove-Item Env:<NAME>) and start again.")
    serve(a.port, a.db, a.reset, demo=demo)
