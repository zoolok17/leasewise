"""The error table (DESIGN-7A.md 4.5 and Part 12 D7) as pure functions.

`decide` looks at ONE answer from Leasewise (or a network failure) and
says what the reporter does next. It never touches the network or the
state; `state.State.apply` carries the decision out.
"""

from .. import errors as _errors

MAX_REBUILDS = 3

# The only error words the reporter ever stores or shows: the codes Leasewise itself
# can send (quota_balancer/errors.py) plus three words of its own. Whatever else an
# answer says (it could echo anything, even the identity) becomes UNKNOWN_CODE.
UNKNOWN_CODE = "UNKNOWN_CODE"
BAD_ANSWER = "BAD_ANSWER"                  # a success that lacked the lease id
MALFORMED_RESPONSE = "MALFORMED_RESPONSE"  # an answer that could not be decoded

LEASEWISE_CODES = frozenset(
    value for name, value in vars(_errors).items()
    if name.startswith("CODE_") and isinstance(value, str))
CLOSED_CODES = LEASEWISE_CODES | {UNKNOWN_CODE, BAD_ANSWER, MALFORMED_RESPONSE}


def closed_code(code):
    """`code` if it is one of the closed words, UNKNOWN_CODE if it is any other
    text, None if there is no code."""
    if code is None:
        return None
    return code if isinstance(code, str) and code in CLOSED_CODES         else UNKNOWN_CODE

ACQUIRE = "acquire"
REPORT = "report"
FINAL = "final"
LIMIT = "limit"                # card 7c-2: an account-level limit event (no agent, no message)
RECEIPT = "receipt"            # card L6a-1: a gateway's final receipt for one quota lease

# Actions.
DONE = "done"                  # accepted: record it
RETRY = "retry"                # later, same bytes
REBUILD = "rebuild"            # acquisition only: a new estimate time
FAIL = "fail"                  # this item fails for good, loudly
FAIL_AGENT = "fail_agent"      # configuration fault: stop this agent
DROP = "drop"                  # observation only: give up on this report

_AGENT_FAULTS = frozenset({"POLICY_DENIED", "NOT_FOUND", "FORBIDDEN",
                           "CONTEXT_UNKNOWN"})


class Response:
    """One answer. `status` None means no answer at all (network error or
    timeout). `code` is the error code of an error body, else None."""

    __slots__ = ("status", "code", "lease_id")

    def __init__(self, status=None, code=None, lease_id=None):
        self.status = status
        self.code = code
        self.lease_id = lease_id


class Decision:
    __slots__ = ("action", "code")

    def __init__(self, action, code=None):
        self.action = action
        self.code = code

    def __eq__(self, other):
        return (self.action, self.code) == (other.action, other.code)

    def __repr__(self):
        return "Decision(%r, %r)" % (self.action, self.code)


def decide(kind, response, rebuilds=0):
    """`kind` is ACQUIRE, REPORT, FINAL, LIMIT or RECEIPT; `rebuilds` is how many rebuilds
    this acquisition already used (it survives restarts in the state)."""
    status, code = response.status, closed_code(response.code)
    if status == 401:
        # A credential problem is reporter-wide and fixed by an operator, so
        # nothing is lost or failed for good: retry, and keep the code visible.
        return Decision(RETRY, code or "UNAUTHENTICATED")
    if status is None or status >= 500 or status == 429:
        return Decision(RETRY, code)
    if kind == LIMIT:
        # An account-level event belongs to no seat and no message: a refusal can only fail
        # THIS row, for good. Never an agent stop, a rebuild or a drop. 202 means "recorded".
        if status in (200, 201, 202):
            return Decision(DONE)
        return Decision(FAIL, code)
    if kind == RECEIPT:
        # A gateway's final receipt belongs to one lease and to no agent: nothing it is answered
        # can stop an agent, rebuild or drop. Only 200 is settled (a replay of the same event is a 200
        # too). LEASE_CLOSED is NOT a "done" word: a settle never answers it (only a lease check
        # does), so seeing it is an unexpected, permanent failure of THIS row like every other
        # refusal (DESIGN-6A 8.4). A refusal never erases the stored receipt and never stops
        # another row or pass.
        if status == 200:
            return Decision(DONE)
        if 200 < status < 300:
            # A settle succeeds ONLY on 200. Any other success status is not an answer this reporter knows, so it
            # is treated like an unreadable 2xx: the same bytes are sent again, and the word stays visible.
            return Decision(RETRY, MALFORMED_RESPONSE)
        return Decision(FAIL, code or UNKNOWN_CODE)
    if status in (200, 201):
        return Decision(DONE)
    if code == "ESTIMATE_STALE":
        # Only on an acquisition, and only because the stale check runs
        # AFTER the replay lookup (card L1d): no lease exists for the id.
        if kind != ACQUIRE:
            return Decision(FAIL, code)
        if rebuilds >= MAX_REBUILDS:
            return Decision(FAIL, code)
        return Decision(REBUILD, code)
    if code in _AGENT_FAULTS:
        return Decision(FAIL_AGENT, code)
    if kind == REPORT and code in ("LEASE_EXPIRED", "RECEIPT_CONFLICT",
                                   "LEASE_CLOSED"):
        return Decision(DROP, code)
    # A generic INVALID_REQUEST (or any other 4xx) is a bug in the request
    # or the configuration: never rebuilt, never retried blindly.
    return Decision(FAIL, code)


def backoff_ms(tries, jitter):
    """Seconds 5 .. 15 minutes, doubling; `jitter` is a number in [0, 1)
    supplied by the caller so tests stay deterministic."""
    base = min(900_000, 5_000 * (2 ** max(0, tries - 1)))
    return int(base * (0.5 + jitter / 2))
