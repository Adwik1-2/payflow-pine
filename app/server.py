"""HTTP API, operator console (/) and merchant portal (/apply). Standard library only.

Auth model (prototype):
  * console endpoints: X-Role (reviewer | ops) + X-Actor headers - mocked staff login
  * merchant endpoints: application id + per-application access token (X-App-Token),
    issued once at submission; only its hash is stored
  * provider callbacks: HMAC-SHA256 signature in X-Signature
"""
import json
import os
import re
import threading
import time
import traceback
import uuid
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import scenarios
from .config import Clock, Config
from .llm import Gateway, LLMSettings
from .providers import MerchantIdService, ProviderSimulator
from .states import InvalidTransition
from .workflow import ActionError, Engine, NotFound

STATIC = os.path.join(os.path.dirname(__file__), "static")
PAGES = {"/": "console.html", "/console": "console.html", "/apply": "apply.html"}
ASSETS = {"/static/ui.js": ("ui.js", "application/javascript; charset=utf-8"),
          "/static/ui.css": ("ui.css", "text/css; charset=utf-8"),
          "/static/fonts/fira-Regular.woff": ("fonts/fira-Regular.woff", "font/woff"),
          "/static/fonts/fira-Medium.woff": ("fonts/fira-Medium.woff", "font/woff"),
          "/static/fonts/fira-SemiBold.woff": ("fonts/fira-SemiBold.woff", "font/woff"),
          "/static/fonts/fira-Bold.woff": ("fonts/fira-Bold.woff", "font/woff"),
}
STAFF = {"reviewer", "ops"}
MAX_BODY = 12 * 1024 * 1024


def build_app(db_path, cfg=None, port=8000, llm_settings=None):
    cfg = cfg or Config()
    clock = Clock()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # callbacks go to localhost directly

    def deliver(provider, body, signature):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/callbacks/{provider}", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Signature": signature})
        opener.open(req, timeout=5).read()

    sim = ProviderSimulator(clock, cfg, deliver)
    gw = Gateway(llm_settings or LLMSettings.from_env())
    engine = Engine(db_path, cfg, clock, sim, MerchantIdService, gw)
    return engine, sim


def make_handler(engine, sim):
    routes = []

    def route(method, pattern, role=None):
        def deco(fn):
            routes.append((method, re.compile("^" + pattern + "$"), role, fn))
            return fn
        return deco

    APP = r"(APP-[A-Z0-9]+)"

    # ---------------------------------------------------------------- merchant portal
    @route("POST", r"/api/merchant/applications")
    def m_submit(h, m, b):
        app_id, token = engine.submit(b.get("form") or {}, b.get("documents") or [], "merchant", channel="portal")
        return 201, {"id": app_id, "token": token}

    @route("GET", rf"/api/merchant/applications/{APP}", role="merchant")
    def m_view(h, m, b):
        return 200, engine.merchant_view(m.group(1))

    @route("POST", rf"/api/merchant/applications/{APP}/documents", role="merchant")
    def m_upload(h, m, b):
        engine.update(m.group(1), documents=b.get("documents") or [], actor="merchant")
        return 200, engine.merchant_view(m.group(1))


    @route("POST", rf"/api/merchant/applications/{APP}/fields", role="merchant")
    def m_fields(h, m, b):
        allowed = {"legal_name", "trade_name", "category", "business_description", "pan", "gstin", "bank_account",
                   "ifsc", "account_holder", "email", "phone", "gst_registered", "website"}
        fields = b.get("fields") or {}
        if not isinstance(fields, dict):
            raise ValueError("fields must be an object")
        engine.update(m.group(1), fields={k: v for k, v in fields.items() if k in allowed},
                      documents=b.get("documents") or [], actor="merchant")
        return 200, engine.merchant_view(m.group(1))

    # ---------------------------------------------------------------- console
    @route("GET", r"/api/applications", role="staff")
    def list_apps(h, m, b):
        return 200, engine.list_applications()

    @route("GET", rf"/api/applications/{APP}", role="staff")
    def get_app(h, m, b):
        return 200, engine.get_application(m.group(1))

    @route("POST", rf"/api/applications/{APP}/actions", role="reviewer")
    def action(h, m, b):
        engine.review_action(m.group(1), b.get("action"), h.actor, b.get("reason"), b.get("check_type"), b.get("message"))
        return 200, engine.get_application(m.group(1))

    @route("POST", rf"/api/applications/{APP}/reveal", role="reviewer")
    def reveal(h, m, b):
        return 200, {"field": b.get("field"), "value": engine.reveal(m.group(1), b.get("field"), h.actor, b.get("purpose"))}

    @route("GET", rf"/api/applications/{APP}/documents/(\d+)/file", role="reviewer")
    def doc_file(h, m, b):
        mime, data = engine.document_for_review(m.group(1), int(m.group(2)), h.actor)
        return 200, ("raw", mime, data)

    @route("GET", rf"/api/applications/{APP}/documents/(\d+)/extraction", role="reviewer")
    def doc_extraction(h, m, b):
        return 200, engine.document_extraction(m.group(1), int(m.group(2)), h.actor)

    @route("POST", rf"/api/applications/{APP}/documents/(\d+)/verify", role="reviewer")
    def doc_verify(h, m, b):
        engine.verify_document_fields(m.group(1), int(m.group(2)), b.get("values") or {}, h.actor)
        return 200, engine.get_application(m.group(1))

    @route("POST", rf"/api/applications/{APP}/copilot", role="reviewer")
    def copilot(h, m, b):
        return 200, engine.copilot(m.group(1), h.actor)

    @route("POST", rf"/api/applications/{APP}/copilot/(\d+)/feedback", role="reviewer")
    def copilot_fb(h, m, b):
        engine.copilot_feedback(m.group(1), int(m.group(2)), b.get("feedback"), h.actor)
        return 200, {"ok": True}

    @route("POST", rf"/api/applications/{APP}/simulate-merchant", role="staff")
    def simulate_merchant(h, m, b):
        """Demo helper: plays the merchant from the console. Documents are regenerated
        server-side from stored values because the console only sees masked data."""
        app_id, kind = m.group(1), b.get("kind")
        with engine._lock:
            a = engine._app(app_id)
        q = float(b.get("quality", 0.95))
        if kind == "reupload":
            dt = b.get("doc_type")
            if dt == "GST_CERTIFICATE" and not a["gstin"]:
                raise ValueError("merchant is not GST-registered")
            doc = {"PAN_CARD": lambda: scenarios.pan_card(a["pan"], a["legal_name"], q),
                   "GST_CERTIFICATE": lambda: scenarios.gst_cert(a["gstin"], a["legal_name"], a["trade_name"], q),
                   "CANCELLED_CHEQUE": lambda: scenarios.cheque(a["bank_account"], a["ifsc"], a["account_holder"], q)}[dt]()
            engine.update(app_id, documents=[doc], actor="merchant")
        elif kind == "change_bank":
            acct, ifsc = str(b.get("bank_account") or ""), str(b.get("ifsc") or a["ifsc"])
            if not acct:
                raise ValueError("bank_account is required")
            engine.update(app_id, fields={"bank_account": acct, "ifsc": ifsc},
                          documents=[scenarios.cheque(acct, ifsc, a["account_holder"], q)], actor="merchant")
        elif kind == "correct_field":
            field, value = b.get("field"), str(b.get("value") or "").strip()
            if field not in ("pan", "gstin", "category", "business_description", "legal_name"):
                raise ValueError("field not supported")
            if not value:
                raise ValueError("value is required")
            fields, docs = {field: value}, []
            if field == "pan":
                value = (a["pan"][:5] + "1234" + a["pan"][-1]) if value == "auto" and a["pan"] else value.upper()
                fields = {"pan": value}
                docs.append(scenarios.pan_card(value, a["legal_name"], q))
                if a["gstin"]:
                    g = a["gstin"][:2] + value + a["gstin"][12:]
                    fields["gstin"] = g
                    docs.append(scenarios.gst_cert(g, a["legal_name"], a["trade_name"], q))
            elif field == "gstin":
                if value == "auto" and not a["gstin"]:
                    raise ValueError("no GSTIN on file to correct")
                value = (a["gstin"][:12] + "1" + a["gstin"][13:]) if value == "auto" else value.upper()
                fields = {"gstin": value}
                docs.append(scenarios.gst_cert(value, a["legal_name"], a["trade_name"], q))
            engine.update(app_id, fields=fields, documents=docs, actor="merchant")
        else:
            raise ValueError("kind must be reupload | change_bank | correct_field")
        return 200, engine.get_application(app_id)

    @route("GET", r"/api/review-queue", role="staff")
    def queue(h, m, b):
        return 200, engine.review_queue()

    @route("GET", r"/api/activity", role="staff")
    def activity(h, m, b):
        return 200, engine.recent_activity()

    @route("GET", r"/api/dead-letters", role="staff")
    def dlq(h, m, b):
        return 200, engine.dead_letters()

    @route("POST", r"/api/dead-letters/(\d+)/replay", role="reviewer")
    def dlq_replay(h, m, b):
        engine.replay_dead_letter(int(m.group(1)), h.actor)
        return 200, {"ok": True}

    @route("GET", r"/api/metrics", role="staff")
    def metrics(h, m, b):
        return 200, {**engine.metrics(), "callbacks_in_flight": sim.pending()}

    @route("GET", r"/api/modes", role="staff")
    def modes(h, m, b):
        return 200, engine.modes()

    @route("POST", r"/api/modes", role="ops")
    def set_mode(h, m, b):
        engine.set_mode(b.get("target"), b.get("mode"), h.actor)
        return 200, engine.modes()

    @route("GET", r"/api/scenarios", role="staff")
    def list_scen(h, m, b):
        return 200, scenarios.SCENARIOS

    @route("POST", r"/api/scenarios/([a-z_]+)", role="staff")
    def run_scen(h, m, b):
        form, docs = scenarios.build(m.group(1), latest=engine.scenario_seed())
        app_id, token = engine.submit(form, docs, "merchant", channel="scenario")
        return 201, {"id": app_id, "token": token}

    @route("POST", rf"/api/callbacks/([A-Z]+)")
    def callback(h, m, b):
        return engine.handle_webhook(m.group(1), h.raw_body, h.headers.get("X-Signature"))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # paths and bodies can contain identifiers; keep request logs off

        def _send(self, code, obj):
            if isinstance(obj, tuple) and obj[0] == "raw":
                _, ctype, data = obj
            elif isinstance(obj, bytes):
                ctype, data = "text/html; charset=utf-8", obj
            else:
                ctype, data = "application/json", json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method):
            path = self.path.split("?")[0]
            if method == "GET" and path in PAGES:
                with open(os.path.join(STATIC, PAGES[path]), "rb") as f:
                    return self._send(200, f.read())
            if method == "GET" and path in ASSETS:
                name, ctype = ASSETS[path]
                with open(os.path.join(STATIC, name), "rb") as f:
                    return self._send(200, ("raw", ctype, f.read()))
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, {"error": "bad Content-Length"})
            if n < 0:
                return self._send(400, {"error": "bad Content-Length"})
            if n > MAX_BODY:
                return self._send(413, {"error": "request too large"})
            self.raw_body = self.rfile.read(n) if n else b""
            self.actor = (self.headers.get("X-Actor") or "anonymous")[:64]
            role = self.headers.get("X-Role")
            for meth, rx, need, fn in routes:
                mt = rx.match(path)
                if meth != method or not mt:
                    continue
                try:
                    if need == "merchant":
                        engine.merchant_auth(mt.group(1), self.headers.get("X-App-Token"))
                    elif need == "staff" and role not in STAFF:
                        return self._send(403, {"error": "staff role required"})
                    elif need in STAFF and role != need:
                        return self._send(403, {"error": f"role '{need}' required"})
                    body = json.loads(self.raw_body) if self.raw_body and not path.startswith("/api/callbacks") else {}
                    if not isinstance(body, dict):
                        return self._send(400, {"error": "request body must be a JSON object"})
                    code, out = fn(self, mt, body)
                    return self._send(code, out)
                except NotFound:
                    return self._send(404, {"error": "not found"})
                except (ActionError, InvalidTransition) as e:
                    return self._send(409, {"error": str(e)})
                except (ValueError, KeyError, TypeError) as e:
                    return self._send(400, {"error": f"bad request: {e}"})
                except Exception as e:  # noqa: BLE001 - never leak a stack trace or drop the connection
                    ref = uuid.uuid4().hex[:8]
                    print(f"[error {ref}] {method} {path}: {e!r}")
                    traceback.print_exc()
                    return self._send(500, {"error": f"internal error (ref {ref})"})
            return self._send(404, {"error": "no route"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return Handler


def scheduler_loop(engine, sim, stop):
    while not stop.is_set():
        try:
            sim.deliver_due()
            engine.tick()
        except Exception as e:  # noqa: BLE001 - keep the scheduler alive
            if stop.is_set():
                break
            print("scheduler error:", repr(e))
        time.sleep(0.25)


def job_loop(engine, stop):
    while not stop.is_set():
        try:
            if not engine.run_due_jobs(limit=2):
                time.sleep(0.3)
        except Exception as e:  # noqa: BLE001
            if stop.is_set():
                break   # shutting down: the database may already be closed
            print("job worker error:", repr(e))
            time.sleep(1)


def serve(port=8000, db_path="data/onboarding.db", reset=False, workers=3, demo=None):
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    if reset and os.path.exists(db_path):
        os.remove(db_path)
    engine, sim = build_app(db_path, port=port)
    stop = threading.Event()
    threading.Thread(target=scheduler_loop, args=(engine, sim, stop), daemon=True).start()
    for _ in range(workers):
        threading.Thread(target=job_loop, args=(engine, stop), daemon=True).start()
    for name in demo or []:   # pre-load sample applications for a demo; they run through the normal workflow
        form, docs = scenarios.build(name, latest=engine.scenario_seed())
        app_id, _ = engine.submit(form, docs, "merchant", channel="scenario")
        print(f"Demo application : {app_id}  ({scenarios.SCENARIOS[name]})")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(engine, sim))
    httpd.daemon_threads = True
    st = engine.gw.status()
    print(f"LLM: {st['provider']}  models: {st['models']}")
    if getattr(engine.gw, "adapter", None) is not None and getattr(engine.gw.adapter, "url", None):
        print(f"LLM address: {engine.gw.adapter.url}")
    print(f"Operator console : http://127.0.0.1:{port}/")
    print(f"Merchant portal  : http://127.0.0.1:{port}/apply")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
