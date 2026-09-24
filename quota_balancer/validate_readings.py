"""Strict validation of card-2 reading-import documents.

The document shape is closed: every object carries exactly the keys of its
level, nothing more (docs/DESIGN.md section 7). Validation raises QuotaError
with a static message only - never text from the input document.

Reference resolution (machine / account / gateway aliases must exist in the
store) is intentionally NOT done here: it depends on the store's current
inventory, so store.reading_import performs it inside the write transaction.
This module only validates the self-contained shape and types.
"""

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

# The card-2 reading document version.
DOC_VERSION = 2

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
_POLICY_HASH_KEYS = frozenset({"price", "child_cap"})

KIND_CLI_WINDOW = "cli_window"
KIND_GATEWAY_SNAPSHOT = "gateway_snapshot"


def _invalid():
    raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)


def _is_int(value):
    # bool is a subclass of int; the design rejects booleans-as-integers, so
    # the type check is exact (type(...) is int).
    return type(value) is int


def _is_bool(value):
    return type(value) is bool


def _is_nonneg_int(value):
    return _is_int(value) and value >= 0


def _is_alias(value):
    return isinstance(value, str) and ALIAS_RE.fullmatch(value) is not None


def _is_nonempty_str(value):
    return isinstance(value, str) and value != ""


def _validate_window_entry(entry):
    if not isinstance(entry, dict) or frozenset(entry) != _WINDOW_ENTRY_KEYS:
        _invalid()
    key = entry["key"]
    used_bps = entry["used_bps"]
    resets_at = entry["resets_at"]
    if not _is_nonempty_str(key):
        _invalid()
    # used_bps is an integer basis-points value or null when unknown.
    if used_bps is not None and not (_is_int(used_bps) and 0 <= used_bps <= MAX_BPS):
        _invalid()
    # resets_at is an epoch-ms value or null when unknown.
    if resets_at is not None and not _is_int(resets_at):
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
    covered_seq = payload["covered_seq"]
    if covered_seq is not None and not _is_int(covered_seq):
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
    if not _is_nonempty_str(value["price"]) or not _is_nonempty_str(value["child_cap"]):
        _invalid()
    return {"price": value["price"], "child_cap": value["child_cap"]}


def _validate_gateway_snapshot(payload):
    if not isinstance(payload, dict) or frozenset(payload) != _GATEWAY_SNAPSHOT_KEYS:
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
    if period_start is not None and not _is_int(period_start):
        _invalid()
    if period_end is not None and not _is_int(period_end):
        _invalid()
    if not _is_bool(sized_ready) or not _is_bool(reconciliation_required):
        _invalid()
    policy_hashes = _validate_policy_hashes(payload["policy_hashes"])
    return {
        "gateway": gateway,
        "ledger_epoch": ledger_epoch,
        "seq": seq,
        "period_start": period_start,
        "period_end": period_end,
        "policy_hashes": policy_hashes,
        "sized_ready": sized_ready,
        "reconciliation_required": reconciliation_required,
    }


def validate_readings(doc, now_ms):
    """Validate a card-2 reading document; return the normalized readings.

    `now_ms` is the wall clock at ingest (epoch ms). A reading whose
    observed_at is more than FUTURE_SLOP_MS in the future is rejected
    (docs/DESIGN.md section 7).

    Returns a list of normalized reading dicts, each carrying:
      event_id, machine (alias), account (alias or None), kind, observed_at,
      plus the kind-specific normalized payload fields.
    """
    if not isinstance(doc, dict):
        _invalid()

    if "version" in doc:
        version = doc["version"]
        if not _is_int(version) or version != DOC_VERSION:
            raise QuotaError(CODE_SCHEMA_UNSUPPORTED, MSG_SCHEMA_UNSUPPORTED)

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
        if not _is_nonempty_str(event_id):
            _invalid()
        if not _is_alias(machine):
            _invalid()
        # account is an alias or null (null marks an ambiguous source that is
        # recorded but reduced into no account).
        if account is not None and not _is_alias(account):
            _invalid()
        if not _is_int(observed_at):
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
            reading.update(_validate_gateway_snapshot(item["payload"]))
        else:
            _invalid()
        readings.append(reading)
    return readings
