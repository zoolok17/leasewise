"""Strict validation of card-2 reading-import documents.

The document shape is closed: every object carries exactly the keys of its
level, nothing more (docs/DESIGN.md section 7). Validation raises QuotaError
with a static message only - never text from the input document.

Reference resolution (machine / account / gateway aliases must exist in the
store) is intentionally NOT done here: it depends on the store's current
inventory, so store.reading_import performs it inside the write transaction.
This module only validates the self-contained shape and types.
"""

import datetime
import re

from .errors import (
    CODE_INVALID_CONFIG,
    CODE_SCHEMA_UNSUPPORTED,
    MSG_INVALID_CONFIG,
    MSG_SCHEMA_UNSUPPORTED,
    QuotaError,
)
from .reduce import (
    CONFIDENCE_OBSERVED,
    CONFIDENCE_UNKNOWN,
    FUTURE_SLOP_MS,
)
from .validate import ALIAS_RE

# The card-2 reading document version; card 2b-1 extends it to version 3
# (section 11): the gateway payload gains the money fields and its periods
# become required UTC-month bounds, and the provider_billing kind appears.
# Version-2 documents remain importable (their snapshots carry no money
# fields and nullable periods; the money evidence stays unknown, never
# zero).
DOC_VERSION = 2
DOC_VERSION_BILLING = 3

MAX_BPS = 10_000  # basis points; 10000 == 100%

_TOP_KEYS = frozenset({"version", "readings"})
_READING_KEYS = frozenset(
    {"event_id", "machine", "account", "kind", "observed_at", "payload"}
)
_WINDOW_ENTRY_KEYS = frozenset({"key", "used_bps", "resets_at"})
_CLI_WINDOW_KEYS = frozenset({"windows", "confidence", "source_stale", "covered_seq"})
_GATEWAY_SNAPSHOT_KEYS = frozenset(
    {
        "gateway", "ledger_epoch", "seq", "period_start", "period_end",
        "policy_hashes", "sized_ready", "reconciliation_required",
    }
)
# Version-3 gateway payload: existing metadata plus the four money fields.
# The first three are disjoint spend categories; unattributed is a subset
# of settled (the only arithmetic the closed shape can pin).
_GATEWAY_SNAPSHOT_KEYS_V3 = _GATEWAY_SNAPSHOT_KEYS | frozenset(
    {"settled_micro_eur", "reserved_micro_eur",
     "uncertain_micro_eur", "unattributed_micro_eur"}
)
_POLICY_HASH_KEYS = frozenset({"price", "child_cap"})
# Closed provider_billing payload (section 11): numbers and local aliases
# only; no provider IDs on the wire, no seat field at any level.
_BILLING_PAYLOAD_KEYS = frozenset(
    {"scope_alias", "scope_revision", "period_start", "period_end",
     "currency", "total_micro_eur", "non_ai_spend_present"}
)

KIND_CLI_WINDOW = "cli_window"
KIND_GATEWAY_SNAPSHOT = "gateway_snapshot"
KIND_PROVIDER_BILLING = "provider_billing"

# The provider bills in exactly one currency (section 11).
CURRENCY_EUR = "EUR"

_MS_PER_DAY = 86_400_000


def _invalid():
    raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)


# Section 7: checked signed-64-bit arithmetic. Every integer in a reading
# document is bounded to the non-negative signed-64 range BEFORE it can reach
# a SQLite binding (an out-of-range Python int would otherwise raise a driver
# OverflowError - a path-leaking traceback, not the error contract). The
# range also rejects negative timestamps and sequences (section 7: reject
# nonfinite/negative values).
INT64_MAX = 2 ** 63 - 1

# A policy hash is a fixed-length hex digest (SHA-256-style, 64 lowercase hex
# chars): free text and paths cannot masquerade as one (privacy).
_POLICY_HASH_RE = re.compile(r"[0-9a-f]{64}")

# Section 7: IDs are local UUIDs (operator aliases are the exception) -
# canonical lowercase form.
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def _is_int(value):
    # bool is a subclass of int; the design rejects booleans-as-integers, so
    # the type check is exact (type(...) is int).
    return type(value) is int


def _is_bool(value):
    return type(value) is bool


def _is_nonneg_int(value):
    # Bounded non-negative signed-64 (see INT64_MAX).
    return _is_int(value) and 0 <= value <= INT64_MAX


def _is_alias(value):
    return isinstance(value, str) and ALIAS_RE.fullmatch(value) is not None


def _is_policy_hash(value):
    return isinstance(value, str) and _POLICY_HASH_RE.fullmatch(value) is not None


def _is_uuid(value):
    return isinstance(value, str) and _UUID_RE.fullmatch(value) is not None


def _validate_window_entry(entry):
    if not isinstance(entry, dict) or frozenset(entry) != _WINDOW_ENTRY_KEYS:
        _invalid()
    key = entry["key"]
    used_bps = entry["used_bps"]
    resets_at = entry["resets_at"]
    # A window key is a local alias (privacy): reporter free text cannot be
    # echoed in the status projection.
    if not _is_alias(key):
        _invalid()
    # used_bps is an integer basis-points value or null when unknown.
    if used_bps is not None and not (_is_int(used_bps) and 0 <= used_bps <= MAX_BPS):
        _invalid()
    # resets_at is an epoch-ms value or null when unknown.
    if resets_at is not None and not _is_nonneg_int(resets_at):
        _invalid()
    return (key, used_bps, resets_at)


def _validate_cli_window(payload):
    if not isinstance(payload, dict) or frozenset(payload) != _CLI_WINDOW_KEYS:
        _invalid()
    if not isinstance(payload["windows"], list):
        _invalid()
    windows = [_validate_window_entry(w) for w in payload["windows"]]
    confidence = payload["confidence"]
    if confidence not in (CONFIDENCE_OBSERVED, CONFIDENCE_UNKNOWN):
        _invalid()
    if not _is_bool(payload["source_stale"]):
        _invalid()
    # Section 7: "null percentage/reset only when unknown". An observed
    # reading must carry both a value and a reset in every window; nulls
    # are the unknown reading's.
    if confidence == CONFIDENCE_OBSERVED:
        for _key, used_bps, resets_at in windows:
            if used_bps is None or resets_at is None:
                _invalid()
    covered_seq = payload["covered_seq"]
    if covered_seq is not None and not _is_nonneg_int(covered_seq):
        _invalid()
    return {
        "windows": windows,
        "confidence": confidence,
        "source_stale": payload["source_stale"],
        "covered_seq": covered_seq,
    }


def _validate_policy_hashes(value):
    if value is None:
        return None
    if not isinstance(value, dict) or frozenset(value) != _POLICY_HASH_KEYS:
        _invalid()
    # Fixed-length hex digests (privacy): free text cannot be stored or
    # projected.
    if not _is_policy_hash(value["price"]) or not _is_policy_hash(value["child_cap"]):
        _invalid()
    return {"price": value["price"], "child_cap": value["child_cap"]}


def _is_utc_month_start(ms):
    """True when ms is the first instant (00:00:00Z) of a UTC month."""
    if not _is_nonneg_int(ms) or ms % 1000 or ms % _MS_PER_DAY:
        return False
    try:
        return (datetime.datetime.fromtimestamp(ms // 1000,
                                                datetime.timezone.utc)
                .day == 1)
    except (OverflowError, OSError, ValueError):
        # A day-multiple outside the proleptic calendar: not a
        # representable month start.
        return False


def _next_utc_month_start(ms):
    """The first instant of the UTC month following the one `ms` is in;
    None when that month start is not representable."""
    try:
        dt = datetime.datetime.fromtimestamp(ms // 1000,
                                             datetime.timezone.utc)
        year, month = dt.year, dt.month
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
        return int(datetime.datetime(year, month, 1,
                                     tzinfo=datetime.timezone.utc)
                   .timestamp()) * 1000
    except (OverflowError, OSError, ValueError):
        return None


def _is_utc_month_period(start, end):
    """True when [start, end) is bounded by UTC month bounds, end > start.

    The gateway v3 snapshot period is 'utc_month', pinned to month
    boundaries - a sub-month or mid-month bound is not a UTC-month
    period. (The provider billing period is stricter: exactly one month,
    _is_one_utc_month_period.)
    """
    return (_is_utc_month_start(start) and _is_utc_month_start(end)
            and end > start)


def _is_one_utc_month_period(start, end):
    """True when [start, end) is exactly one UTC month: start is the
    first instant of a UTC month and end the first instant of the month
    that follows it.

    A provider billing period is one UTC month - a multi-month span is
    not a period: it would fold distinct months into one row and leave
    the row's period_end stale once later months arrive.
    """
    return (_is_utc_month_start(start)
            and _next_utc_month_start(start) == end)


def _validate_gateway_snapshot(payload, billing):
    keys = _GATEWAY_SNAPSHOT_KEYS_V3 if billing else _GATEWAY_SNAPSHOT_KEYS
    if not isinstance(payload, dict) or frozenset(payload) != keys:
        _invalid()
    gateway = payload["gateway"]
    ledger_epoch = payload["ledger_epoch"]
    seq = payload["seq"]
    period_start = payload["period_start"]
    period_end = payload["period_end"]
    sized_ready = payload["sized_ready"]
    reconciliation_required = payload["reconciliation_required"]
    if not _is_alias(gateway):
        _invalid()
    if not _is_nonneg_int(ledger_epoch) or not _is_nonneg_int(seq):
        _invalid()
    if period_start is not None and not _is_nonneg_int(period_start):
        _invalid()
    if period_end is not None and not _is_nonneg_int(period_end):
        _invalid()
    normalized = None
    if billing:
        # Version 3: the periods are REQUIRED UTC-month bounds, and the
        # four money fields are present: non-negative int64 micro-EUR, the
        # first three disjoint categories, unattributed a subset of
        # settled. A legacy (v2) snapshot carries no money fields: its
        # money evidence stays unknown, never zero.
        if not _is_utc_month_period(period_start, period_end):
            _invalid()
        settled = payload["settled_micro_eur"]
        reserved = payload["reserved_micro_eur"]
        uncertain = payload["uncertain_micro_eur"]
        unattributed = payload["unattributed_micro_eur"]
        for value in (settled, reserved, uncertain, unattributed):
            if not _is_nonneg_int(value):
                _invalid()
        if unattributed > settled:
            _invalid()
        normalized = {
            "settled_micro_eur": settled,
            "reserved_micro_eur": reserved,
            "uncertain_micro_eur": uncertain,
            "unattributed_micro_eur": unattributed,
        }
    if not _is_bool(sized_ready) or not _is_bool(reconciliation_required):
        _invalid()
    policy_hashes = _validate_policy_hashes(payload["policy_hashes"])
    result = {
        "gateway": gateway,
        "ledger_epoch": ledger_epoch,
        "seq": seq,
        "period_start": period_start,
        "period_end": period_end,
        "policy_hashes": policy_hashes,
        "sized_ready": sized_ready,
        "reconciliation_required": reconciliation_required,
    }
    if normalized is not None:
        result.update(normalized)
    return result


def _validate_provider_billing(payload, observed_at):
    """Closed provider_billing payload (section 11): local aliases,
    integer micro-EUR, UTC-ms timestamps, one exact boolean. No provider
    IDs, no seat field, no free text."""
    if not isinstance(payload, dict) or frozenset(payload) != _BILLING_PAYLOAD_KEYS:
        _invalid()
    scope_alias = payload["scope_alias"]
    scope_revision = payload["scope_revision"]
    period_start = payload["period_start"]
    period_end = payload["period_end"]
    currency = payload["currency"]
    total = payload["total_micro_eur"]
    non_ai = payload["non_ai_spend_present"]
    if not _is_alias(scope_alias):
        _invalid()
    # Positive integer scope revision.
    if not _is_nonneg_int(scope_revision) or scope_revision <= 0:
        _invalid()
    # period = [start, end) is exactly one UTC month; the source time is
    # within the bounds (the final sample at end is allowed).
    if not _is_one_utc_month_period(period_start, period_end):
        _invalid()
    if not (period_start <= observed_at <= period_end):
        _invalid()
    if currency != CURRENCY_EUR:
        _invalid()
    if not _is_nonneg_int(total):
        _invalid()
    # Exact boolean: an unverified non-AI scan is a schema failure, never
    # a false negative.
    if not _is_bool(non_ai):
        _invalid()
    return {
        "scope_alias": scope_alias,
        "scope_revision": scope_revision,
        "period_start": period_start,
        "period_end": period_end,
        "currency": currency,
        "total_micro_eur": total,
        "non_ai_spend_present": non_ai,
    }


def validate_readings(doc, now_ms):
    """Validate a reading document (card-2 version 2, or card 2b-1
    version 3); return the normalized readings.

    `now_ms` is the wall clock at ingest (epoch ms). A reading whose
    observed_at is more than FUTURE_SLOP_MS in the future is rejected
    (docs/DESIGN.md section 7).

    Version 3 extends the gateway snapshot payload with the money fields
    (whose periods are required UTC-month bounds) and adds the
    provider_billing kind, which is attributed to an account.

    Returns a list of normalized reading dicts, each carrying:
      event_id, machine (alias), account (alias or None), kind, observed_at,
      plus the kind-specific normalized payload fields.
    """
    if not isinstance(doc, dict):
        _invalid()

    version = DOC_VERSION
    if "version" in doc:
        version = doc["version"]
        if not _is_int(version) or version not in (
                DOC_VERSION, DOC_VERSION_BILLING):
            raise QuotaError(CODE_SCHEMA_UNSUPPORTED, MSG_SCHEMA_UNSUPPORTED)
    # Version 3 documents carry the money fields and the provider_billing
    # kind; version 2 (or an absent version, as in card 2) does not.
    billing = version == DOC_VERSION_BILLING

    if frozenset(doc) != _TOP_KEYS:
        _invalid()
    if not isinstance(doc["readings"], list):
        _invalid()

    readings = []
    for item in doc["readings"]:
        if not isinstance(item, dict) or frozenset(item) != _READING_KEYS:
            _invalid()
        event_id = item["event_id"]
        machine = item["machine"]
        account = item["account"]
        kind = item["kind"]
        observed_at = item["observed_at"]
        # Section 7: reading IDs are local UUIDs (operator aliases are the
        # exception); a non-UUID id is rejected, not stored.
        if not _is_uuid(event_id):
            _invalid()
        if not _is_alias(machine):
            _invalid()
        # account is an alias or null (null marks an ambiguous source that is
        # recorded but reduced into no account).
        if account is not None and not _is_alias(account):
            _invalid()
        if not _is_nonneg_int(observed_at):
            _invalid()
        # Reject future observations beyond the 30-second slop.
        if observed_at > now_ms + FUTURE_SLOP_MS:
            _invalid()

        reading = {
            "event_id": event_id,
            "machine": machine,
            "account": account,
            "kind": kind,
            "observed_at": observed_at,
            # The raw payload is kept for storage and the idempotency
            # comparison (canonicalized JSON of the source content).
            "payload": item["payload"],
        }
        if kind == KIND_CLI_WINDOW:
            reading.update(_validate_cli_window(item["payload"]))
        elif kind == KIND_GATEWAY_SNAPSHOT:
            reading.update(_validate_gateway_snapshot(item["payload"],
                                                      billing))
        elif kind == KIND_PROVIDER_BILLING:
            # The provider billing kind exists in version-3 documents only,
            # and a billing reading is attributed to an account (the
            # metered account the provider bill is for) - never ambiguous.
            if not billing or account is None:
                _invalid()
            reading.update(_validate_provider_billing(
                item["payload"], observed_at))
        else:
            _invalid()
        readings.append(reading)
    return readings
