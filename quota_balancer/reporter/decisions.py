"""The error table (DESIGN-7A.md 4.5 and Part 12 D7) as pure functions.

`decide` looks at ONE answer from Leasewise (or a network failure) and
says what the reporter does next. It never touches the network or the
state; `state.State.apply` carries the decision out.
"""

MAX_REBUILDS = 3

ACQUIRE = "acquire"
REPORT = "report"
FINAL = "final"

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
    """`kind` is ACQUIRE, REPORT or FINAL; `rebuilds` is how many rebuilds
    this acquisition already used (it survives restarts in the state)."""
    status, code = response.status, response.code
    if status == 401:
        # A credential problem is reporter-wide and fixed by an operator, so
        # nothing is lost or failed for good: retry, and keep the code visible.
        return Decision(RETRY, code or "UNAUTHENTICATED")
    if status is None or status >= 500 or status == 429:
        return Decision(RETRY, code)
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
