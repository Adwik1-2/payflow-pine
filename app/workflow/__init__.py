"""Onboarding workflow engine.

Durable, event-driven state machine on SQLite. Event sources:
  merchant  - submit / update / upload            (intake.py)
  provider  - signed webhook callbacks            (checks.py)
  scheduler - timeouts, retries, reminders        (checks.py / decisions.py)
  jobs      - AI extraction, categorisation, ...  (jobs.py)
  reviewer  - claim, approve, reject, verify ...  (review.py)
The decision rule lives in one place: decisions._evaluate().
"""
from .base import ActionError, Base, NotFound  # noqa: F401
from .checks import ChecksMixin
from .decisions import DecisionsMixin
from .intake import IntakeMixin
from .jobs import JobsMixin
from .review import ReviewMixin
from .views import ViewsMixin


class Engine(IntakeMixin, ChecksMixin, DecisionsMixin, JobsMixin, ReviewMixin, ViewsMixin, Base):
    pass
