"""Typed errors with stable codes and static messages.

Messages are intentionally static: they must never echo raw input text or
OS error details (Card 1 privacy contract, docs/DESIGN.md sections 7-8).
"""

CODE_INVALID_CONFIG = "INVALID_CONFIG"
CODE_SCHEMA_UNSUPPORTED = "SCHEMA_UNSUPPORTED"
CODE_STORE_ERROR = "STORE_ERROR"
CODE_IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"

# Card 2b closed billing errors (docs/DESIGN.md section 11). The billing
# codes exit 2 when an explicit usable-plan request fails (2b-2 onward);
# BILLING_BASIS_UNKNOWN is already raised by the pure 2b-1 conversion.
CODE_BILLING_SCOPE_MISMATCH = "BILLING_SCOPE_MISMATCH"
CODE_BILLING_BASIS_UNKNOWN = "BILLING_BASIS_UNKNOWN"
CODE_BILLING_UNKNOWN = "BILLING_UNKNOWN"
CODE_BILLING_STALE = "BILLING_STALE"
CODE_BILLING_DIVERGENCE = "BILLING_DIVERGENCE"
CODE_OPENING_UNSAFE = "OPENING_UNSAFE"

EXIT_CODES = {
    CODE_INVALID_CONFIG: 2,
    CODE_SCHEMA_UNSUPPORTED: 2,
    CODE_STORE_ERROR: 3,
    CODE_IDEMPOTENCY_CONFLICT: 2,
    CODE_BILLING_SCOPE_MISMATCH: 2,
    CODE_BILLING_BASIS_UNKNOWN: 2,
    CODE_BILLING_UNKNOWN: 2,
    CODE_BILLING_STALE: 2,
    CODE_BILLING_DIVERGENCE: 2,
    CODE_OPENING_UNSAFE: 2,
}

MSG_INVALID_CONFIG = "config document is invalid"
MSG_SCHEMA_UNSUPPORTED = "schema version is not supported"
MSG_STORE_FAILED = "store operation failed"
MSG_STORE_NOT_FOUND = "database not found"
MSG_IDEMPOTENCY_CONFLICT = "a reading with the same id has different content"
MSG_BILLING_SCOPE_MISMATCH = "the billing reading does not match the account policy"
MSG_BILLING_BASIS_UNKNOWN = "the tax basis is unknown or the required rate is missing"
MSG_BILLING_UNKNOWN = "no usable provider billing sample"
MSG_BILLING_STALE = "the provider billing sample is stale"
MSG_BILLING_DIVERGENCE = "provider and ledger spend diverge beyond tolerance"
MSG_OPENING_UNSAFE = "the opening plan is not safe to propose"


class QuotaError(Exception):
    """An expected, reportable failure carrying a stable code and message."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
