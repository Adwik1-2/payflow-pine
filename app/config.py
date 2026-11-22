"""Runtime configuration and clock abstraction.

All workflow timing (timeouts, retry backoff) reads time through a Clock so
tests can drive the system deterministically with FakeClock.
"""
import time
from dataclasses import dataclass


@dataclass
class Config:
    # Verification checks
    check_timeout_s: float = 8.0          # callback must arrive within this window
    max_check_attempts: int = 3           # dispatch attempts before dead-lettering
    retry_backoff_base_s: float = 2.0     # exponential: base * 2^(attempt-1)
    required_checks: tuple = ("PAN", "GST", "BANK", "RISK")
    non_overridable_checks: tuple = ("PAN", "BANK")  # reviewer can never waive these

    # Document intelligence
    min_field_confidence: float = 0.75    # below this a field is "low confidence"
    min_doc_quality: float = 0.50         # below this a document is "unreadable"
    name_match_threshold: float = 0.80

    # Merchant ID provisioning
    max_provision_attempts: int = 3

    # Human review
    review_sla_s: float = 900.0               # time a case may wait in the review queue
    four_eyes_on_high_severity: bool = True   # waiving a HIGH exception needs a second reviewer

    # Merchant communication
    needs_info_reminder_s: float = 120.0      # demo-friendly; days in production
    max_reminders: int = 2
    notify_debounce_s: float = 3.0           # batch several requests into one message

    # AI
    ai_name_adjudication: bool = True         # LLM may confirm abbreviation-style bank name matches
    name_gray_zone_min: float = 0.55          # below this a name mismatch is never sent to the LLM
    ai_name_min_confidence: float = 0.90
    job_max_attempts: int = 3
    max_upload_bytes: int = 5 * 1024 * 1024

    # Mock provider behaviour
    provider_min_delay_s: float = 1.0
    provider_max_delay_s: float = 3.0
    slow_mode_extra_delay_s: float = 2.0  # "slow" = timeout + this

    # Security
    webhook_secret: str = "dev-only-webhook-secret"
    fingerprint_secret: str = "dev-only-fingerprint-secret"


class Clock:
    def now(self) -> float:
        return time.time()


class FakeClock(Clock):
    def __init__(self, start: float = 1_700_000_000.0):
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds
