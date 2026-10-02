"""Strict validation of version-2 config documents (card L1a, turn 1).

Version 2 is the version-1 inventory (accounts, machines, gateways,
validated by validate.validate, unchanged) plus the seven policy tables:
projects, teams, project_accounts, seats, models, profiles, limits. Every
key is required at every level (an empty list is fine), no extra keys, and
every reference is an ALIAS path - never a UUID; the importer resolves the
aliases to stored ids. Validation raises QuotaError(INVALID_CONFIG) with the
static message only; nothing from the document is echoed.

Reused, not forked: the version-1 validator for the inventory part and the
card-3 validators (policy._validate_model, policy._validate_limit) for the
model and limit value rules; their INVALID_REQUEST is re-raised here as the
config contract's INVALID_CONFIG.
"""

from . import errors, policy
from .errors import QuotaError
from .validate import ALIAS_RE, validate

VERSION = 2

# The reserved platform singleton: platform caps are the scope='platform'
# rows carried by this id (lease.py reads the same constant).
PLATFORM_ID = "00000000-0000-0000-0000-000000000000"

INT64_MAX = 2 ** 63 - 1
PARK = policy.PARK

SEAT_ENFORCEMENTS = ("metered", "subscription_admission", "subscription_warn",
                     "shadow", "metered_advisory")

_TOP_KEYS = frozenset({
    "version", "accounts", "machines", "gateways", "projects", "teams",
    "project_accounts", "seats", "models", "profiles", "limits"})
_V1_KEYS = ("accounts", "machines", "gateways")
_LIST_KEYS = _V1_KEYS + ("projects", "teams", "project_accounts", "seats",
                         "models", "profiles", "limits")
_PROJECT_KEYS = frozenset({"alias"})
_TEAM_KEYS = frozenset({"project", "alias"})
_PROJECT_ACCOUNT_KEYS = frozenset({"project", "account"})
_SEAT_KEYS = frozenset({"team", "machine", "alias", "role", "enforcement"})
_SEAT_TEAM_KEYS = frozenset({"project", "team"})
_MODEL_KEYS = frozenset({
    "account", "alias", "context_ceiling", "output_ceiling",
    "tariff_input_rate", "tariff_output_rate", "reserve_input_rate",
    "reserve_output_rate", "reservation_mode", "policy_hash",
    "effort_aliases"})
_PROFILE_KEYS = frozenset({
    "task_class", "candidates", "context_rules", "calibration",
    "expected_calls", "growth_tokens", "margin_bps", "max_calls",
    "output_cap", "per_call_output_cap", "ttl_ms", "policy_revision"})
_LIMIT_COMMON_KEYS = frozenset({"scope", "metric", "period", "window_key",
                                "soft", "hard"})
_LIMIT_PATH_KEYS = {
    "platform": frozenset(),
    "project": frozenset({"project"}),
    "team": frozenset({"project", "team"}),
    "seat": frozenset({"machine", "seat"}),
}


def _invalid():
    raise QuotaError(errors.CODE_INVALID_CONFIG, errors.MSG_INVALID_CONFIG)


def _is_alias(value):
    return isinstance(value, str) and ALIAS_RE.fullmatch(value) is not None


def _is_int(value, minimum=0, maximum=INT64_MAX):
    # Exact int: a bool is not an integer here (section 7).
    return type(value) is int and minimum <= value <= maximum


def _closed(item, keys):
    if not isinstance(item, dict) or frozenset(item) != keys:
        _invalid()
    return item


def _list(doc, key):
    if not isinstance(doc[key], list):
        _invalid()
    return doc[key]


def _unique(values):
    if len(set(values)) != len(values):
        _invalid()


def _model(item):
    """The card-3 model rules over the document row; effort_aliases is a
    non-empty list of distinct aliases, stored comma-joined (the form
    lease.py splits)."""
    efforts = item["effort_aliases"]
    if (not isinstance(efforts, list) or not efforts
            or not all(_is_alias(e) for e in efforts)
            or len(set(efforts)) != len(efforts)):
        _invalid()
    row = {key: item[key] for key in _MODEL_KEYS - {"account"}}
    row["effort_aliases"] = ",".join(efforts)
    if not _is_alias(item["alias"]):
        _invalid()
    try:
        policy._validate_model(row)
    except QuotaError:
        _invalid()
    return row


def _profile(item, model_aliases):
    task_class = item["task_class"]
    if not _is_alias(task_class):
        _invalid()
    candidates = item["candidates"]
    if (not isinstance(candidates, list) or not candidates
            or not all(_is_alias(c) for c in candidates)
            or len(set(candidates)) != len(candidates)):
        _invalid()
    # 'park' is the terminal tier, last and at most once; the models before
    # it must exist. The terminal park is implicit in the arithmetic (every
    # candidate blocked parks), so only the model aliases are stored.
    models = candidates[:-1] if candidates[-1] == PARK else candidates
    if not models or any(c == PARK or c not in model_aliases for c in models):
        _invalid()
    # Reserved: their closed shapes belong to later cards.
    if item["context_rules"] != {} or item["calibration"] != {}:
        _invalid()
    numeric = (
        _is_int(item["expected_calls"], 1, 100),
        _is_int(item["growth_tokens"]),
        _is_int(item["margin_bps"], 10000, 20000),
        _is_int(item["max_calls"], 1, 100),
        _is_int(item["output_cap"]),
        _is_int(item["per_call_output_cap"]),
        _is_int(item["ttl_ms"], 1),
        _is_int(item["policy_revision"]),
    )
    if not all(numeric):
        _invalid()
    return {
        "task_class": task_class,
        "candidates": models,
        "expected_calls": item["expected_calls"],
        "growth_tokens": item["growth_tokens"],
        "margin_bps": item["margin_bps"],
        "max_calls": item["max_calls"],
        "output_cap": item["output_cap"],
        "per_call_output_cap": item["per_call_output_cap"],
        "ttl_ms": item["ttl_ms"],
        "policy_revision": item["policy_revision"],
    }


def _limit(item):
    scope = item.get("scope") if isinstance(item, dict) else None
    if not isinstance(scope, str) or scope not in _LIMIT_PATH_KEYS:
        _invalid()
    path_keys = _LIMIT_PATH_KEYS[scope]
    _closed(item, _LIMIT_COMMON_KEYS | path_keys)
    path = tuple(item[key] for key in sorted(path_keys))
    if not all(_is_alias(part) for part in path):
        _invalid()
    # The card-3 value rules (metric/period pairing, window_key only for
    # window_bps/window, soft <= hard, bps <= 10000) on a placeholder id.
    try:
        policy._validate_limit({
            "scope": scope, "scope_id": PLATFORM_ID,
            "metric": item["metric"], "period": item["period"],
            "window_key": item["window_key"], "soft": item["soft"],
            "hard": item["hard"]})
    except QuotaError:
        _invalid()
    return {"scope": scope, "path": dict(zip(sorted(path_keys), path)),
            "metric": item["metric"], "period": item["period"],
            "window_key": item["window_key"], "soft": item["soft"],
            "hard": item["hard"]}


def validate_v2(doc):
    """Validate a version-2 document (its version already checked equal to
    2). Returns (accounts, machines, gateways, policy) where the first
    three are validate.validate's tuples and policy is a dict of the
    normalized policy sections (aliases only, no ids)."""
    _closed(doc, _TOP_KEYS)
    for key in _LIST_KEYS:
        _list(doc, key)
    accounts, machines, gateways = validate(
        {"version": 1, **{key: doc[key] for key in _V1_KEYS}})
    account_aliases = {a[0] for a in accounts}
    machine_aliases = set(machines)

    projects = []
    for item in doc["projects"]:
        alias = _closed(item, _PROJECT_KEYS)["alias"]
        if not _is_alias(alias):
            _invalid()
        projects.append(alias)
    _unique(projects)

    teams = []
    for item in doc["teams"]:
        _closed(item, _TEAM_KEYS)
        if not _is_alias(item["alias"]) or item["project"] not in projects \
                or not _is_alias(item["project"]):
            _invalid()
        teams.append((item["project"], item["alias"]))
    _unique(teams)

    project_accounts = []
    for item in doc["project_accounts"]:
        _closed(item, _PROJECT_ACCOUNT_KEYS)
        if not _is_alias(item["project"]) or not _is_alias(item["account"]) \
                or item["project"] not in projects \
                or item["account"] not in account_aliases:
            _invalid()
        project_accounts.append((item["project"], item["account"]))
    _unique(project_accounts)

    seats = []
    for item in doc["seats"]:
        _closed(item, _SEAT_KEYS)
        team = _closed(item["team"], _SEAT_TEAM_KEYS)
        if (not _is_alias(item["alias"]) or not _is_alias(item["role"])
                or not isinstance(item["enforcement"], str)
                or item["enforcement"] not in SEAT_ENFORCEMENTS
                or (team["project"], team["team"]) not in teams
                or not _is_alias(item["machine"])
                or item["machine"] not in machine_aliases):
            _invalid()
        seats.append({"project": team["project"], "team": team["team"],
                      "machine": item["machine"], "alias": item["alias"],
                      "role": item["role"],
                      "enforcement": item["enforcement"]})
    _unique([(s["machine"], s["alias"]) for s in seats])

    models = []
    for item in doc["models"]:
        _closed(item, _MODEL_KEYS)
        if not _is_alias(item["account"]) \
                or item["account"] not in account_aliases \
                or item["alias"] == PARK:
            _invalid()
        row = _model(item)
        row["account"] = item["account"]
        models.append(row)
    # Stricter than the schema (account, alias): a candidate alias must
    # never be ambiguous, so a model alias is unique across all accounts.
    _unique([m["alias"] for m in models])
    model_aliases = {m["alias"] for m in models}

    profiles = [_profile(_closed(item, _PROFILE_KEYS), model_aliases)
                for item in doc["profiles"]]
    _unique([p["task_class"] for p in profiles])

    limits = [_limit(item) for item in doc["limits"]]
    seat_keys = {(s["machine"], s["alias"]) for s in seats}
    for lim in limits:
        path = lim["path"]
        if lim["scope"] == "project" and path["project"] not in projects:
            _invalid()
        if lim["scope"] == "team" and (path["project"],
                                       path["team"]) not in teams:
            _invalid()
        if lim["scope"] == "seat" and (path["machine"],
                                       path["seat"]) not in seat_keys:
            _invalid()
    _unique([limit_key(lim) for lim in limits])

    return accounts, machines, gateways, {
        "projects": projects, "teams": teams,
        "project_accounts": project_accounts, "seats": seats,
        "models": models, "profiles": profiles, "limits": limits}


def limit_key(lim):
    """The identity of a limit row: the section-7 uniqueness key over the
    alias path (the store key swaps the path for the resolved scope id)."""
    return (lim["scope"], tuple(sorted(lim["path"].items())), lim["metric"],
            lim["period"], lim["window_key"] or "")
