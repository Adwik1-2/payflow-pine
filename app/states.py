"""Application state machine: explicit states and the only legal transitions."""


class S:
    SUBMITTED = "SUBMITTED"
    VERIFYING = "VERIFYING"
    NEEDS_INFO = "NEEDS_INFO"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    APPROVED = "APPROVED"
    PROVISIONING = "PROVISIONING"
    PROVISIONING_FAILED = "PROVISIONING_FAILED"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"


TRANSITIONS = {
    S.SUBMITTED: {S.VERIFYING, S.NEEDS_INFO, S.MANUAL_REVIEW, S.APPROVED},
    S.VERIFYING: {S.NEEDS_INFO, S.MANUAL_REVIEW, S.APPROVED, S.REJECTED},
    S.NEEDS_INFO: {S.VERIFYING, S.MANUAL_REVIEW, S.APPROVED, S.REJECTED},
    S.MANUAL_REVIEW: {S.VERIFYING, S.NEEDS_INFO, S.APPROVED, S.REJECTED},
    S.APPROVED: {S.PROVISIONING},
    S.PROVISIONING: {S.ACTIVE, S.PROVISIONING_FAILED},
    S.PROVISIONING_FAILED: {S.PROVISIONING},
    S.ACTIVE: set(),
    S.REJECTED: set(),
}

TERMINAL = {S.ACTIVE, S.REJECTED}

# States in which the decision engine may move the application on its own.
EVALUABLE = {S.SUBMITTED, S.VERIFYING, S.NEEDS_INFO, S.MANUAL_REVIEW}


class InvalidTransition(Exception):
    pass


def assert_transition(frm: str, to: str) -> None:
    if to not in TRANSITIONS.get(frm, set()):
        raise InvalidTransition(f"Illegal transition {frm} -> {to}")
