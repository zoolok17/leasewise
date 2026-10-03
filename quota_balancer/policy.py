"""Pure policy arithmetic (card 3).

No I/O, no clock, no store access: total functions over validated integer
inputs, per docs/DESIGN.md section 3 (context, estimates and honest input
enforcement), section 4 (money and composed limits), section 6 (profiles,
the 80% sweep, fallback tiers) and section 7 (the shared contract). The
card-3 store tables (models, profiles, projects, teams, seats,
project_accounts, limits) are config-owned; in this card the arithmetic
reads plain fixture rows, not the store - there is no wrapper and no HTTP.

Rate units: integer micro-EUR per 1,000,000 tokens, bound to the model's
installed policy_hash (section 3: the client never chooses its own rates).
The selected hold is always the request-sized hold at reserve rates; the
tariff-rate figure and the fixed reserve (M*231999) are comparisons only
(section 4, F05: small holds cannot use the fixed per-attempt path).

Every contract violation raises QuotaError with a static message
(INVALID_REQUEST / CONTEXT_PROJECTION_EXCEEDED / POLICY_DENIED); no raw
input text or OS error text is ever echoed (section 7-8 privacy contract).
"""

import re

from . import errors
from .errors import QuotaError

# --- the shared contract (section 7) --------------------------------------
INT64_MAX = 2 ** 63 - 1
# A usage total (settled money, open exposure) is an exact sum made safe for this pure rule: anything above the largest cap
# arrives as USAGE_MAX, which is above every cap (card R1-M; `money.saturate`).
USAGE_MAX = INT64_MAX + 1
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

# --- section 3: estimates and holds ----------------------------------------
MARGIN_BPS_DEFAULT = 12500
MARGIN_BPS_MIN = 10000
MARGIN_BPS_MAX = 20000
MAX_CALLS = 100
RATE_BASIS = 1000000                # rates are per 1,000,000 tokens
FIXED_RESERVE_PER_ATTEMPT = 231999  # legacy fixed reserve: comparison only

# --- section 6: profiles -----------------------------------------------------
SWEEP_DEFER_BPS = 8000  # cold_sweep defers above 80%
PARK = "park"

# --- section 7: limits --------------------------------------------------------
LIMIT_SCOPES = ("platform", "project", "team", "seat")
LIMIT_METRICS = ("input_tokens", "money_micro_eur", "window_bps",
                 "concurrency")
LIMIT_PERIODS = ("turn", "day", "month", "window")

_MODEL_KEYS = frozenset((
    "alias", "context_ceiling", "output_ceiling",
    "tariff_input_rate", "tariff_output_rate",
    "reserve_input_rate", "reserve_output_rate",
    "reservation_mode", "policy_hash", "effort_aliases"))
_LIMIT_KEYS = frozenset(("scope", "scope_id", "metric", "period",
                         "window_key", "soft", "hard"))


def _invalid():
    raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)


def _check_int(value, minimum=0, maximum=INT64_MAX):
    """Exact integer, booleans-as-integers rejected, within the signed-64
    range (section 7)."""
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid()
    if value < minimum or value > maximum:
        _invalid()
    return value


def _check_alias(value):
    # fullmatch, not match: a $ anchor with re.match still accepts a
    # trailing newline ("claude\n" is not an alias).
    if not isinstance(value, str) or not _ALIAS_RE.fullmatch(value):
        _invalid()
    return value


def _check_uuid(value):
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        _invalid()
    return value


def _validate_model(model):
    """Closed-shape validation of a models row (fixture dict).

    Returns (context_ceiling, output_ceiling, tariff_input_rate,
    tariff_output_rate, reserve_input_rate, reserve_output_rate).
    """
    if not isinstance(model, dict) or set(model) != _MODEL_KEYS:
        _invalid()
    _check_alias(model["alias"])
    context_ceiling = _check_int(model["context_ceiling"], 1)
    output_ceiling = _check_int(model["output_ceiling"], 1)
    tariff_in = _check_int(model["tariff_input_rate"])
    tariff_out = _check_int(model["tariff_output_rate"])
    reserve_in = _check_int(model["reserve_input_rate"])
    reserve_out = _check_int(model["reserve_output_rate"])
    # The reserve rates bound the reservation: below the tariff (the
    # settlement) rates the sized hold would under-reserve the money it
    # covers. The design's own rates are 1.2x the tariff (section 3).
    if reserve_in < tariff_in or reserve_out < tariff_out:
        _invalid()
    if model["reservation_mode"] not in ("sized", "fixed"):
        _invalid()
    if not isinstance(model["policy_hash"], str) or not _HASH_RE.fullmatch(
            model["policy_hash"]):
        _invalid()
    effort = model["effort_aliases"]
    if not isinstance(effort, str):
        _invalid()
    for part in effort.split(","):
        _check_alias(part)
    return (context_ceiling, output_ceiling, tariff_in, tariff_out,
            reserve_in, reserve_out)


# --- section 3: the estimate formulas ----------------------------------------

def expected_input_tokens(c, n, g):
    """E = N*C + G*N*(N-1)/2: the total expected input over N calls."""
    _check_int(c)
    _check_int(n, 1, MAX_CALLS)
    _check_int(g)
    e = n * c + g * n * (n - 1) // 2
    if e > INT64_MAX:
        _invalid()
    return e


def input_with_margin(e, margin_bps=MARGIN_BPS_DEFAULT):
    """I = ceil(E*margin_bps/10000): the margined input cap (the default
    margin is 12500 bps = 125%)."""
    _check_int(e)
    _check_int(margin_bps, MARGIN_BPS_MIN, MARGIN_BPS_MAX)
    i = (e * margin_bps + 9999) // 10000
    if i > INT64_MAX:
        _invalid()
    return i


def hold_at_rates(i, o, m, input_rate, output_rate):
    """H = ceil((I*input_rate + O*output_rate)/1e6) + 2*M - 1.

    The 2*M-1 allowance covers separate input/output ceilings on every
    attempt (section 3).
    """
    _check_int(i)
    _check_int(o)
    _check_int(m, 1, MAX_CALLS)
    _check_int(input_rate)
    _check_int(output_rate)
    h = ((i * input_rate + o * output_rate) + RATE_BASIS - 1) // RATE_BASIS
    h += 2 * m - 1
    if h > INT64_MAX:
        _invalid()
    return h


def hold_sized(i, o, m, model):
    """The selected hold: the request-sized hold at the model's reserve
    rates (bound to the installed policy_hash)."""
    rates = _validate_model(model)
    return hold_at_rates(i, o, m, rates[4], rates[5])


def hold_fixed(m):
    """H_fixed = M*231999: the legacy fixed reserve, a comparison only.

    Never the selected lease mode (section 4, F05): small holds cannot use
    the fixed per-attempt path.
    """
    _check_int(m, 1, MAX_CALLS)
    h = m * FIXED_RESERVE_PER_ATTEMPT
    if h > INT64_MAX:
        _invalid()
    return h


def projected_final_context(c, n, g):
    """C + G*(N-1): the context size the last expected call would see."""
    _check_int(c)
    _check_int(n, 1, MAX_CALLS)
    _check_int(g)
    p = c + g * (n - 1)
    if p > INT64_MAX:
        _invalid()
    return p


def check_projection(c, n, g, model):
    """Deny CONTEXT_PROJECTION_EXCEEDED when C+G*(N-1) exceeds the model's
    input ceiling (section 3). Splitting the work or constructing a
    SUMMARY/FRESH re-estimate is the remedy; an oversized bound is never
    clipped. Returns the projection when admitted."""
    p = projected_final_context(c, n, g)
    context_ceiling = _validate_model(model)[0]
    if p > context_ceiling:
        raise QuotaError(errors.CODE_CONTEXT_PROJECTION_EXCEEDED,
                         errors.MSG_CONTEXT_PROJECTION_EXCEEDED)
    return p


def check_per_call_output_cap(model, per_call_output_cap):
    """The per-call output cap must not exceed the model's output ceiling
    (section 7: the output cap is total, the per-call cap <= model
    ceiling)."""
    output_ceiling = _validate_model(model)[1]
    _check_int(per_call_output_cap)
    if per_call_output_cap > output_ceiling:
        _invalid()


def estimate(c, n, g, o, m, model, margin_bps=MARGIN_BPS_DEFAULT):
    """The full section-3 estimate record for one request:

    expected_input            E = N*C + G*N*(N-1)/2
    input_cap                 I = ceil(E*margin_bps/10000)
    hold_sized                the selected hold, at the model's reserve rates
    hold_tariff_comparison    the analogous upper estimate at tariff rates -
                              NOT the reservation (section 3, F12)
    hold_fixed_comparison     M*231999, the legacy fixed reserve - a
                              comparison only, never a selected mode
    projected_final_context   C + G*(N-1), checked against the ceiling

    Raises INVALID_REQUEST on contract violations (N<=M<=100, the margin
    range, closed shapes, booleans-as-integers, the signed-64 bound, and
    O - the TOTAL output, section 3 - exceeding M calls at the model
    output ceiling) and CONTEXT_PROJECTION_EXCEEDED when the projection
    exceeds the model input ceiling.
    """
    _check_int(c)
    _check_int(n, 1, MAX_CALLS)
    _check_int(g)
    _check_int(o)
    _check_int(m, 1, MAX_CALLS)
    if n > m:
        _invalid()
    rates = _validate_model(model)
    # O is the total output over the estimate: it cannot exceed M calls at
    # the model output ceiling (section 7: the per-call cap <= model
    # ceiling).
    if o > m * rates[1]:
        _invalid()
    e = expected_input_tokens(c, n, g)
    i = input_with_margin(e, margin_bps)
    check_projection(c, n, g, model)  # raises CONTEXT_PROJECTION_EXCEEDED
    h_sized = hold_at_rates(i, o, m, rates[4], rates[5])
    return {
        "expected_input": e,
        "input_cap": i,
        "hold_sized": h_sized,
        "hold_tariff_comparison": hold_at_rates(i, o, m, rates[2], rates[3]),
        "hold_fixed_comparison": hold_fixed(m),
        "projected_final_context": c + g * (n - 1),
    }


# --- section 4: composed limits and the admission rule ------------------------

def _validate_limit(row):
    if not isinstance(row, dict) or set(row) != _LIMIT_KEYS:
        _invalid()
    scope = row["scope"]
    if scope not in LIMIT_SCOPES:
        _invalid()
    _check_uuid(row["scope_id"])
    metric = row["metric"]
    if metric not in LIMIT_METRICS:
        _invalid()
    period = row["period"]
    if period not in LIMIT_PERIODS:
        _invalid()
    window_key = row["window_key"]
    if window_key is not None:
        _check_alias(window_key)
    soft = row["soft"]
    if soft is not None:
        _check_int(soft)
    hard = _check_int(row["hard"])
    if soft is not None and soft > hard:
        _invalid()
    # The section-7 pairing: input/money use turn/day/month (and carry no
    # window), window_bps uses window with a window_key (bps: 10000 is
    # 100%), concurrency counts the live/uncertain leases of the turn.
    if metric in ("input_tokens", "money_micro_eur"):
        if period not in ("turn", "day", "month") or window_key is not None:
            _invalid()
    elif metric == "window_bps":
        if period != "window" or window_key is None:
            _invalid()
        if hard > 10000:
            _invalid()
    else:  # concurrency
        if period != "turn" or window_key is not None:
            _invalid()
    return (scope, metric, period, window_key, soft, hard)


def composed_limits(rows, account_budget=None):
    """VIEW ONLY - never feed this to admission: it collapses the tightest
    ceiling across scopes into one figure, which cannot be enforced
    against per-scope usage (feeding it to a check would reintroduce the
    d0a9edf defect). Admission goes through check_admission, which
    enforces each cap against its own scope's usage plus the account
    budget.

    The effective cap per (metric, period, window_key): the tightest
    hard ceiling across the applicable scopes (section 4: limits compose
    by their tightest ceiling, never by adding duplicate caps). Limits for
    different windows stay separate - window_key is part of the cap
    identity, never dropped.

    `rows` are limits rows (fixture dicts). `account_budget` is the
    account's one money home (accounts.budget_micro_eur, F04); it tightens
    the (money_micro_eur, month, None) cap. A soft bound is carried only
    while it sits at or below the effective hard (a warning above the cap
    carries no information). A (metric, period, window_key) with no cap is
    absent: unbounded.
    """
    if isinstance(rows, (str, dict)) or not isinstance(rows, (list, tuple)):
        _invalid()
    if account_budget is not None:
        _check_int(account_budget, 1)
    caps = {}
    for row in rows:
        _scope, metric, period, window_key, soft, hard = _validate_limit(row)
        key = (metric, period, window_key)
        current = caps.get(key)
        if current is None:
            caps[key] = {"hard": hard, "soft": soft}
        else:
            current["hard"] = min(current["hard"], hard)
            if soft is not None:
                if current["soft"] is None:
                    current["soft"] = soft
                else:
                    current["soft"] = min(current["soft"], soft)
    for entry in caps.values():
        if entry["soft"] is not None and entry["soft"] > entry["hard"]:
            entry["soft"] = None
    if account_budget is not None:
        key = ("money_micro_eur", "month", None)
        current = caps.get(key)
        if current is None:
            caps[key] = {"hard": account_budget, "soft": None}
        else:
            current["hard"] = min(current["hard"], account_budget)
            if current["soft"] is not None and current["soft"] > current["hard"]:
                current["soft"] = None
    return caps


def check_admission(rows, metric, period, usage, new, window_key=None,
                    chain=None, account_id=None, account_budget=None):
    """The admission rule (section 4): for EVERY limit row applicable to
    this (metric, period[, window_key]) AND to this requester, that
    scope's settled charges + unresolved holds + the new hold must fit the
    row's hard cap; the account budget (accounts.budget_micro_eur, F04)
    is the one mandatory (money_micro_eur, month) cap - section 4 says
    there is NO account/month limits row, so it is passed in, not read
    from `rows`. Else POLICY_DENIED. Caps from different scopes are never
    collapsed into one figure: each is enforced against its own scope's
    usage.

    `rows` are limits rows (fixture dicts, validated as in
    composed_limits). `usage` maps (scope, scope_id) to the (settled,
    holds) pair for that scope; every applicable scope must be present -
    a scope whose usage is unknown fails closed as INVALID_REQUEST, not
    zero. Entries for non-applicable scopes are ignored. `new` is the new
    hold size for this request. For period "window" the caller must name
    the window (window_key); for other periods window_key must be None.

    `chain` maps each scope name to the requester's scope id there (or
    None: the requester has no such scope), e.g.
    {"platform": P, "project": Pr, "team": T-or-None, "seat": S}. With a
    chain, a row applies only when row.scope_id == chain[row.scope]; a
    row whose scope is not a key of the chain cannot be verified and is
    INVALID_REQUEST. Without a chain (None) the caller must pass exactly
    the rows applicable to the requester: rows for other seats/teams/
    projects would be enforced against this request, and their missing
    usage would fail closed.

    `account_id`/`account_budget` (both, or neither): the account's UUID
    and its canonical monthly money budget in micro-EUR (accounts.
    budget_micro_eur, F04). Enforced on a (money_micro_eur, month) check
    as an extra hard cap on usage[("account", account_id)], whose entry
    is required (missing fails closed); the budget never applies to any
    other (metric, period), and passing it there is INVALID_REQUEST.

    Soft bounds are not consulted here: they annotate the grant (card 4).
    A (metric, period, window_key) with no cap is unbounded. composed_
    limits is a view, never an input to this function (see its
    docstring).
    """
    if metric not in LIMIT_METRICS or period not in LIMIT_PERIODS:
        _invalid()
    if (period == "window") != (window_key is not None):
        _invalid()
    if window_key is not None:
        _check_alias(window_key)
    _check_int(new)
    if isinstance(rows, (str, dict)) or not isinstance(rows, (list, tuple)):
        _invalid()
    if not isinstance(usage, dict):
        _invalid()
    # The account budget (N1): the one mandatory (money_micro_eur, month)
    # cap. Validate it with the other contract checks; enforce its cap
    # with the other policy decisions, after the rows.
    if (account_id is None) != (account_budget is None):
        _invalid()
    budget_settled = budget_holds = None
    if account_budget is not None:
        if (metric, period) != ("money_micro_eur", "month"):
            _invalid()  # the budget is a monthly money cap, not a general one
        _check_uuid(account_id)
        _check_int(account_budget, 1)
        key = ("account", account_id)
        if key not in usage:
            _invalid()  # fail closed: unknown usage is not zero
        pair = usage[key]
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            _invalid()
        budget_settled = _check_int(pair[0], 0, USAGE_MAX)
        budget_holds = _check_int(pair[1], 0, USAGE_MAX)
    if chain is not None:
        if not isinstance(chain, dict):
            _invalid()
        for scope, scope_id in chain.items():
            if scope not in LIMIT_SCOPES:
                _invalid()
            if scope_id is not None:
                _check_uuid(scope_id)
    for row in rows:
        scope, m, p, wk, _soft, hard = _validate_limit(row)
        if (m, p, wk) != (metric, period, window_key):
            continue  # not applicable to this check
        if chain is not None:
            if scope not in chain:
                _invalid()  # fail closed: applicability is unverifiable
            if chain[scope] is None or row["scope_id"] != chain[scope]:
                continue  # the row's scope is not the requester's
        key = (scope, row["scope_id"])
        if key not in usage:
            _invalid()  # fail closed: unknown usage is not zero
        pair = usage[key]
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            _invalid()
        settled = _check_int(pair[0], 0, USAGE_MAX)
        holds = _check_int(pair[1], 0, USAGE_MAX)
        if settled + holds + new > hard:
            raise QuotaError(errors.CODE_POLICY_DENIED,
                             errors.MSG_POLICY_DENIED)
    if account_budget is not None:
        if budget_settled + budget_holds + new > account_budget:
            raise QuotaError(errors.CODE_POLICY_DENIED,
                             errors.MSG_POLICY_DENIED)


# --- section 6: the 80% sweep and fallback tiers --------------------------------

def sweep_deferred(used_bps):
    """The 80% sweep (section 6): a cold sweep defers when a selected
    window is above 80% used. Section 5: "no cold_sweep if any selected
    window >80%" - the caller passes the MAX utilization across the
    selected windows. "Above 80%" is strictly greater - at exactly 8000
    bps the sweep still runs. A deferred sweep parks; it never routes to a
    premium tier automatically (its fallback list is [park]).

    A window whose utilization is UNKNOWN (never observed) is not a bps
    value: the readings layer (not this arithmetic) decides what that
    means; here the input must be a known 0..10000 bps."""
    _check_int(used_bps, 0, 10000)
    return used_bps > SWEEP_DEFER_BPS


def select_tier(candidates, fallback, blocked):
    """Walk the ordered candidate list, then the ordered fallback tiers
    (each a model alias or 'park'); the first tier that is not blocked wins
    (the section-6 profile table). A fallback hit starts a new
    estimate/request/session - only the tier is returned, never a per-tool
    hop. 'park' must be the last tier, at most once.

    Returns {"model": alias-or-None, "tier": "candidate"|"fallback"|"park",
    "index": int}.
    """
    if (isinstance(candidates, (str, dict))
            or not isinstance(candidates, (list, tuple))
            or not candidates):
        _invalid()
    if (isinstance(fallback, (str, dict))
            or not isinstance(fallback, (list, tuple))):
        _invalid()
    if (isinstance(blocked, (str, dict))
            or not isinstance(blocked, (set, frozenset))):
        _invalid()
    for alias in candidates:
        _check_alias(alias)
        if alias == PARK:
            _invalid()  # 'park' is the fallback sentinel, not a model alias
    for tier in fallback:
        if tier != PARK:
            _check_alias(tier)
    if PARK in fallback:
        if fallback[-1] != PARK or fallback.count(PARK) != 1:
            _invalid()
    for alias in blocked:
        if alias == PARK:
            _invalid()
        _check_alias(alias)
    for index, alias in enumerate(candidates):
        if alias not in blocked:
            return {"model": alias, "tier": "candidate", "index": index}
    for index, tier in enumerate(fallback):
        if tier == PARK:
            return {"model": None, "tier": "park", "index": index}
        if tier not in blocked:
            return {"model": tier, "tier": "fallback", "index": index}
    return {"model": None, "tier": "park", "index": len(fallback)}
