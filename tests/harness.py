"""Deterministic in-process harness: fake clock, mock LLM, direct (but still
signed) webhook delivery, manual scheduler ticks and job runs."""
import random

from app import scenarios
from app.config import Config, FakeClock
from app.llm import Gateway, LLMSettings
from app.providers import MerchantIdService, ProviderSimulator
from app.workflow import Engine


class Harness:
    def __init__(self, llm="mock", **cfg_overrides):
        self.cfg = Config(provider_min_delay_s=1.0, provider_max_delay_s=1.0, **cfg_overrides)
        self.clock = FakeClock()
        self.sim = ProviderSimulator(self.clock, self.cfg, seed=7)
        self.gw = Gateway(LLMSettings(provider=llm, models={"vision": "mock-vision", "reasoning": "mock-reasoning",
                                                            "fast": "mock-fast"}), sleep=lambda s: None)
        self.engine = Engine(":memory:", self.cfg, self.clock, self.sim, MerchantIdService, self.gw)
        self.sim.deliver = lambda p, body, sig: self.engine.handle_webhook(p, body, sig)
        self.rng = random.Random(42)
        self.tokens = {}

    def run(self, seconds, step=0.5):
        t = 0.0
        while t < seconds:
            self.engine.run_due_jobs()
            self.clock.advance(step)
            self.sim.deliver_due()
            self.engine.tick()
            self.engine.run_due_jobs()
            t += step

    def submit(self, scenario="happy_path", **form_overrides):
        form, docs = scenarios.build(scenario, self.rng, latest=self.engine.scenario_seed())
        form.update(form_overrides)
        app_id, token = self.engine.submit(form, docs)
        self.tokens[app_id] = token
        return app_id, form, docs

    def app(self, app_id):
        return self.engine.get_application(app_id)

    def state(self, app_id):
        return self.app(app_id)["state"]

    def check(self, app_id, t):
        return next(c for c in self.app(app_id)["checks"] if c["check_type"] == t)

    def events(self, app_id):
        return [a["event"] for a in self.app(app_id)["audit"]]

    def open_codes(self, app_id):
        return {e["code"] for e in self.app(app_id)["exceptions"] if not e["resolved_at"]}
