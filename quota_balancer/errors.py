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
CODE_CONTEXT_UNKNOWN = "CONTEXT_UNKNOWN"
CODE_ESTIMATE_STALE = "ESTIMATE_STALE"
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
# Card L6a-0 (Codex F1): a final receipt left some INPUT usage unknown in a window, so input admission for
# that scope and window waits until the window rolls over (a conservative refusal, never a guess).
CODE_INPUT_USAGE_UNKNOWN = "INPUT_USAGE_UNKNOWN"

# Card R1, step R1-0: the refusal words that docs/DESIGN.md section 7 names (the error list) and that were not
# defined yet. They are defined together, each with its wire status, exit code and fixed message, so that later
# steps and cards only have to emit them. Nothing emits them yet.
CODE_CONTEXT_ROTATION_REQUIRED = "CONTEXT_ROTATION_REQUIRED"      # 400
CODE_UNSUPPORTED_EFFORT = "UNSUPPORTED_EFFORT"                    # 400 (defined only: an unknown effort alias
#                                                                   stays INVALID_REQUEST, privacy rule privacy_4)
CODE_CONTEXT_UNAVAILABLE = "CONTEXT_UNAVAILABLE"                  # 403
CODE_ENVELOPE_INCOMPATIBLE = "ENVELOPE_INCOMPATIBLE"              # 403
CODE_INPUT_BOUND_UNAVAILABLE = "INPUT_BOUND_UNAVAILABLE"          # 403
CODE_POLICY_MISMATCH = "POLICY_MISMATCH"                          # 409
CODE_METER_STALE = "METER_STALE"                                  # 503
CODE_RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"          # 503
CODE_SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"                  # 503

# Card 5 private-HTTP transport errors (docs/DESIGN.md sections 6-7).
# These never occur off the HTTP transport (there is no CLI request to
# authenticate), so they are not in EXIT_CODES; the HTTP layer maps them
# to 401/403 directly (docs/STEP-CARD-5.md).
CODE_UNAUTHENTICATED = "UNAUTHENTICATED"
CODE_FORBIDDEN = "FORBIDDEN"

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
    CODE_CONTEXT_UNKNOWN: 2,
    CODE_ESTIMATE_STALE: 2,
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
    CODE_INPUT_USAGE_UNKNOWN: 2,
    # Card R1-0: conditions of the request or of the evidence behind it exit 2, like the codes above; a service
    # that cannot answer at all exits 3, like a store fault.
    CODE_CONTEXT_ROTATION_REQUIRED: 2,
    CODE_UNSUPPORTED_EFFORT: 2,
    CODE_CONTEXT_UNAVAILABLE: 2,
    CODE_ENVELOPE_INCOMPATIBLE: 2,
    CODE_INPUT_BOUND_UNAVAILABLE: 2,
    CODE_POLICY_MISMATCH: 2,
    CODE_METER_STALE: 2,
    CODE_RECONCILIATION_REQUIRED: 2,
    CODE_SERVICE_UNAVAILABLE: 3,
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
MSG_CONTEXT_UNKNOWN = "an unknown context is accepted for shadow leases only"
MSG_ESTIMATE_STALE = "the admission estimate is stale"
MSG_POLICY_DENIED = "the request is denied by policy"
MSG_NOT_FOUND = "the requested object was not found"
MSG_RECEIPT_CONFLICT = "a receipt for this lease conflicts with a stored receipt"
MSG_LEASE_EXPIRED = "the lease is expired"
MSG_LEASE_CLOSED = "the lease is settled and closed"
MSG_INPUT_LIMIT = "the input token limit is reached"
MSG_INPUT_USAGE_UNKNOWN = "input usage in this window is unknown until the window rolls over"
MSG_MONEY_LIMIT = "the money limit is reached"
MSG_WINDOW_LIMIT = "the window limit is reached"
MSG_ACCOUNT_PARKED = "the account is parked"
MSG_CONCURRENCY_LIMIT = "the concurrency limit is reached"
MSG_UNAUTHENTICATED = "client identity is missing or unrecognized"
MSG_FORBIDDEN = "the identified machine is not authorized for this request"
# Card R1-0. Each says what happened and what to do, and carries no value (no account, gateway, amount, hash or id).
MSG_CONTEXT_ROTATION_REQUIRED = "the session context must be rotated before this request can be granted"
MSG_UNSUPPORTED_EFFORT = "the requested effort is not supported by this model; choose another effort"
MSG_CONTEXT_UNAVAILABLE = "the context this request needs is not available; try again when it is"
MSG_ENVELOPE_INCOMPATIBLE = "the requested limits do not fit the limits of the gateway; ask for less"
MSG_INPUT_BOUND_UNAVAILABLE = "no validated input size bound exists for this request; it cannot be sized"
MSG_POLICY_MISMATCH = "the request names a policy revision that is not current; fetch the policy and try again"
MSG_METER_STALE = "the usage evidence is too old to decide; wait for a fresh report and try again"
MSG_RECONCILIATION_REQUIRED = "the gateway needs an operator reconciliation before this request can be decided"
MSG_SERVICE_UNAVAILABLE = "the coordinator cannot answer right now; try again later"

# Every code and its fixed message, in one place: a test checks that no code is missing here and that no two
# codes say the same thing.
MESSAGES = {
    CODE_INVALID_CONFIG: MSG_INVALID_CONFIG,
    CODE_SCHEMA_UNSUPPORTED: MSG_SCHEMA_UNSUPPORTED,
    CODE_STORE_ERROR: MSG_STORE_FAILED,
    CODE_IDEMPOTENCY_CONFLICT: MSG_IDEMPOTENCY_CONFLICT,
    CODE_INVALID_REQUEST: MSG_INVALID_REQUEST,
    CODE_CONTEXT_PROJECTION_EXCEEDED: MSG_CONTEXT_PROJECTION_EXCEEDED,
    CODE_CONTEXT_UNKNOWN: MSG_CONTEXT_UNKNOWN,
    CODE_ESTIMATE_STALE: MSG_ESTIMATE_STALE,
    CODE_POLICY_DENIED: MSG_POLICY_DENIED,
    CODE_BILLING_SCOPE_MISMATCH: MSG_BILLING_SCOPE_MISMATCH,
    CODE_BILLING_BASIS_UNKNOWN: MSG_BILLING_BASIS_UNKNOWN,
    CODE_BILLING_UNKNOWN: MSG_BILLING_UNKNOWN,
    CODE_BILLING_STALE: MSG_BILLING_STALE,
    CODE_BILLING_DIVERGENCE: MSG_BILLING_DIVERGENCE,
    CODE_OPENING_UNSAFE: MSG_OPENING_UNSAFE,
    CODE_NOT_FOUND: MSG_NOT_FOUND,
    CODE_RECEIPT_CONFLICT: MSG_RECEIPT_CONFLICT,
    CODE_LEASE_EXPIRED: MSG_LEASE_EXPIRED,
    CODE_LEASE_CLOSED: MSG_LEASE_CLOSED,
    CODE_INPUT_LIMIT: MSG_INPUT_LIMIT,
    CODE_MONEY_LIMIT: MSG_MONEY_LIMIT,
    CODE_WINDOW_LIMIT: MSG_WINDOW_LIMIT,
    CODE_ACCOUNT_PARKED: MSG_ACCOUNT_PARKED,
    CODE_CONCURRENCY_LIMIT: MSG_CONCURRENCY_LIMIT,
    CODE_INPUT_USAGE_UNKNOWN: MSG_INPUT_USAGE_UNKNOWN,
    CODE_UNAUTHENTICATED: MSG_UNAUTHENTICATED,
    CODE_FORBIDDEN: MSG_FORBIDDEN,
    CODE_CONTEXT_ROTATION_REQUIRED: MSG_CONTEXT_ROTATION_REQUIRED,
    CODE_UNSUPPORTED_EFFORT: MSG_UNSUPPORTED_EFFORT,
    CODE_CONTEXT_UNAVAILABLE: MSG_CONTEXT_UNAVAILABLE,
    CODE_ENVELOPE_INCOMPATIBLE: MSG_ENVELOPE_INCOMPATIBLE,
    CODE_INPUT_BOUND_UNAVAILABLE: MSG_INPUT_BOUND_UNAVAILABLE,
    CODE_POLICY_MISMATCH: MSG_POLICY_MISMATCH,
    CODE_METER_STALE: MSG_METER_STALE,
    CODE_RECONCILIATION_REQUIRED: MSG_RECONCILIATION_REQUIRED,
    CODE_SERVICE_UNAVAILABLE: MSG_SERVICE_UNAVAILABLE,
}


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
