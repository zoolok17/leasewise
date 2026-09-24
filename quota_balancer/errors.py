"""Typed errors with stable codes and static messages.

Messages are intentionally static: they must never echo raw input text or
OS error details (Card 1 privacy contract, docs/DESIGN.md sections 7-8).
"""

CODE_INVALID_CONFIG = "INVALID_CONFIG"
CODE_SCHEMA_UNSUPPORTED = "SCHEMA_UNSUPPORTED"
CODE_STORE_ERROR = "STORE_ERROR"
CODE_IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
CODE_INVALID_REQUEST = "INVALID_REQUEST"
CODE_CONTEXT_PROJECTION_EXCEEDED = "CONTEXT_PROJECTION_EXCEEDED"
CODE_POLICY_DENIED = "POLICY_DENIED"

EXIT_CODES = {
    CODE_INVALID_CONFIG: 2,
    CODE_SCHEMA_UNSUPPORTED: 2,
    CODE_STORE_ERROR: 3,
    CODE_IDEMPOTENCY_CONFLICT: 2,
    # Card 3: all three are client-side request conditions (fix the request,
    # re-estimate, or stay within policy) - the card-1 "invalid request"
    # class, exit 2. None of them is a store fault (exit 3).
    CODE_INVALID_REQUEST: 2,
    CODE_CONTEXT_PROJECTION_EXCEEDED: 2,
    CODE_POLICY_DENIED: 2,
}

MSG_INVALID_CONFIG = "config document is invalid"
MSG_SCHEMA_UNSUPPORTED = "schema version is not supported"
MSG_STORE_FAILED = "store operation failed"
MSG_STORE_NOT_FOUND = "database not found"
MSG_IDEMPOTENCY_CONFLICT = "a reading with the same id has different content"
MSG_INVALID_REQUEST = "the request is invalid"
MSG_CONTEXT_PROJECTION_EXCEEDED = (
    "the projected context exceeds the model input ceiling"
)
MSG_POLICY_DENIED = "the request is denied by policy"


class QuotaError(Exception):
    """An expected, reportable failure carrying a stable code and message."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
