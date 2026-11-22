"""Runs the real HTTP server (scheduler + job workers) in background threads
for API-level tests, and a small client for calling it."""
import json
import os
import socket
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

from app.config import Config
from app.llm import LLMSettings
from app.server import build_app, job_loop, make_handler, scheduler_loop

STAFF = {"X-Role": "reviewer", "X-Actor": "reviewer.anita"}
OPS = {"X-Role": "ops", "X-Actor": "ops.rahul"}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class LiveServer:
    def __init__(self, cfg=None, db_path=None, llm="mock", llm_settings=None):
        self.port = free_port()
        self.tmp = None
        if db_path is None:
            self.tmp = tempfile.mkdtemp()
            db_path = os.path.join(self.tmp, "t.db")
        self.db_path = db_path
        self.cfg = cfg or Config(check_timeout_s=3, retry_backoff_base_s=0.5, provider_min_delay_s=0.2,
                                 provider_max_delay_s=0.6, slow_mode_extra_delay_s=1.0, notify_debounce_s=0.5)
        self.llm = llm
        self.llm_settings = llm_settings
        self.start()

    def start(self):
        settings = self.llm_settings or LLMSettings(provider=self.llm, models={
            "vision": "mock-vision", "reasoning": "mock-reasoning", "fast": "mock-fast"})
        self.engine, self.sim = build_app(self.db_path, self.cfg, self.port, settings)
        if not self.llm_settings:
            self.engine.gw.sleep = lambda s: None
        self.stop_evt = threading.Event()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), make_handler(self.engine, self.sim))
        self.httpd.daemon_threads = True
        self.threads = [threading.Thread(target=self.httpd.serve_forever, daemon=True),
                        threading.Thread(target=scheduler_loop, args=(self.engine, self.sim, self.stop_evt), daemon=True)]
        self.threads += [threading.Thread(target=job_loop, args=(self.engine, self.stop_evt), daemon=True) for _ in range(3)]
        for t in self.threads:
            t.start()

    def stop(self):
        self.stop_evt.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        # Let background workers finish an in-flight step (a real model call can take a while) before closing the DB.
        for t in self.threads[1:]:
            t.join(timeout=self.engine.gw.s.timeout_s + 5 if self.engine.gw.s.provider not in ("mock", "off") else 2)
        self.engine.db.close()

    def restart(self):
        self.stop()
        self.start()

    def req(self, method, path, body=None, headers=None, raw=None, timeout=15):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": "application/json", **(headers or {})}
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=h)
        try:
            with OPENER.open(r, timeout=timeout) as resp:
                payload = resp.read()
                code = resp.status
        except urllib.error.HTTPError as e:
            payload, code = e.read(), e.code
        try:
            return code, json.loads(payload)
        except ValueError:
            return code, payload

    def wait_state(self, app_id, states, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            code, a = self.req("GET", f"/api/applications/{app_id}", headers=STAFF)
            if code == 200 and a["state"] in states:
                return a
            time.sleep(0.2)
        raise AssertionError(f"{app_id} not in {states} after {timeout}s (state {a.get('state')})")
