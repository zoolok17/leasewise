"""Typed errors with stable codes and static messages.

Messages are intentionally static: they must never echo raw input text or
OS error details (Card 1 privacy contract, docs/DESIGN.md sections 7-8).
"""

CODE_INVALID_CONFIG = "INVALID_CONFIG"
CODE_SCHEMA_UNSUPPORTED = "SCHEMA_UNSUPPORTED"
CODE_STORE_ERROR = "STORE_ERROR"

EXIT_CODES = {
    CODE_INVALID_CONFIG: 2,
    CODE_SCHEMA_UNSUPPORTED: 2,
    CODE_STORE_ERROR: 3,
}

MSG_INVALID_CONFIG = "config document is invalid"
MSG_SCHEMA_UNSUPPORTED = "schema version is not supported"
MSG_STORE_FAILED = "store operation failed"
MSG_STORE_NOT_FOUND = "database not found"


class QuotaError(Exception):
    """An expected, reportable failure carrying a stable code and message."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
