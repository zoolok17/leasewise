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

# Card 2b closed billing errors (docs/DESIGN.md section 11). The billing
# codes exit 2 when an explicit usable-plan request fails (2b-2 onward);
# BILLING_BASIS_UNKNOWN is already raised by the pure 2b-1 conversion.
CODE_BILLING_SCOPE_MISMATCH = "BILLING_SCOPE_MISMATCH"
CODE_BILLING_BASIS_UNKNOWN = "BILLING_BASIS_UNKNOWN"
CODE_BILLING_UNKNOWN = "BILLING_UNKNOWN"
CODE_BILLING_STALE = "BILLING_STALE"
CODE_BILLING_DIVERGENCE = "BILLING_DIVERGENCE"
CODE_OPENING_UNSAFE = "OPENING_UNSAFE"

# Card 4 lease-store errors (docs/DESIGN.md section 7). All are
# client-side request conditions (refuse, re-estimate, wait out the
# retry_at, or send a different id/content) - the card-1 "invalid
# request" class, exit 2. None is a store fault (exit 3).
CODE_NOT_FOUND = "NOT_FOUND"
CODE_RECEIPT_CONFLICT = "RECEIPT_CONFLICT"
CODE_LEASE_EXPIRED = "LEASE_EXPIRED"
CODE_LEASE_CLOSED = "LEASE_CLOSED"
CODE_INPUT_LIMIT = "INPUT_LIMIT"
CODE_MONEY_LIMIT = "MONEY_LIMIT"
CODE_WINDOW_LIMIT = "WINDOW_LIMIT"
CODE_ACCOUNT_PARKED = "ACCOUNT_PARKED"
CODE_CONCURRENCY_LIMIT = "CONCURRENCY_LIMIT"

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
    # Card 3: all three are client-side request conditions (fix the request,
    # re-estimate, or stay within policy) - the card-1 "invalid request"
    # class, exit 2. None of them is a store fault (exit 3).
    CODE_INVALID_REQUEST: 2,
    CODE_CONTEXT_PROJECTION_EXCEEDED: 2,
    CODE_POLICY_DENIED: 2,
    # Card 4: the 404/409/410/429 contract of section 7 (exit 2: the
    # request, its id or its timing is the problem, not the store).
    CODE_NOT_FOUND: 2,
    CODE_RECEIPT_CONFLICT: 2,
    CODE_LEASE_EXPIRED: 2,
    CODE_LEASE_CLOSED: 2,
    CODE_INPUT_LIMIT: 2,
    CODE_MONEY_LIMIT: 2,
    CODE_WINDOW_LIMIT: 2,
    CODE_ACCOUNT_PARKED: 2,
    CODE_CONCURRENCY_LIMIT: 2,
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
MSG_INVALID_REQUEST = "the request is invalid"
MSG_CONTEXT_PROJECTION_EXCEEDED = (
    "the projected context exceeds the model input ceiling"
)
MSG_POLICY_DENIED = "the request is denied by policy"
MSG_NOT_FOUND = "the requested object was not found"
MSG_RECEIPT_CONFLICT = "a receipt for this lease conflicts with a stored receipt"
MSG_LEASE_EXPIRED = "the lease is expired"
MSG_LEASE_CLOSED = "the lease is settled and closed"
MSG_INPUT_LIMIT = "the input token limit is reached"
MSG_MONEY_LIMIT = "the money limit is reached"
MSG_WINDOW_LIMIT = "the window limit is reached"
MSG_ACCOUNT_PARKED = "the account is parked"
MSG_CONCURRENCY_LIMIT = "the concurrency limit is reached"


class QuotaError(Exception):
    """An expected, reportable failure carrying a stable code and message.

    `retry_at` (epoch milliseconds or None) is the 429 retry hint of the
    section-7 error shape; None means no timed retry.
    """

    def __init__(self, code, message, retry_at=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_at = retry_at
