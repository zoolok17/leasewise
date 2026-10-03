"""Pure functions: stable ids, request bodies and final totals.

Everything here derives from journal facts and the resolved configuration
only, so a restart or a lost answer yields the same bytes (DESIGN-7A.md
4.4 and 4.6). No clock, no I/O, no randomness except what the caller passes.
"""

import hashlib
import json
import re
import uuid

from .events import INT64_MAX

# A fixed namespace: ids derived from it must never change between releases.
NAMESPACE = uuid.UUID("5d0c8e4a-7a2b-4c61-9f3e-2b6a1c9d7e10")
MODE_UNKNOWN = "UNKNOWN"


# Card 7c-2: the ONLY fields a limit event may carry (the allow-list, DESIGN-7C2.md section 4).
# Leasewise adds a 30-second margin to `resets_at`, so the ceiling leaves room for it.
LIMIT_BODY_KEYS = frozenset({"event_id", "account_id", "turn_id", "observed_at", "evidence_kind",
                             "resets_at", "exhausted_windows"})
LIMIT_EVIDENCE_KINDS = frozenset({"claude_exhausted", "codex_exhausted", "provider_usage_429"})
LIMIT_RESET_CEILING = INT64_MAX - 30_000
# The ONLY window words a limit event may carry: the producer's own (agenttalk's park marker). The
# strict reader uses this same tuple, so what it lets through the queue always accepts, and a private
# phrase can never be stored or sent as a window.
LIMIT_WINDOWS = ("five_hour", "seven_day")
_LIMIT_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _plain_int(value, low, high):
    return type(value) is int and low <= value <= high


def limit_body_text(body):
    """The canonical text of a limit-event body, or ValueError if it is not exactly the
    allow-listed shape. The same canonical form the client sends, so a stored body and the
    bytes on the wire are the same bytes."""
    if type(body) is not dict or frozenset(body) != LIMIT_BODY_KEYS:
        raise ValueError("shape")
    for key in ("event_id", "account_id", "turn_id"):
        if type(body[key]) is not str or _LIMIT_UUID.fullmatch(body[key]) is None:
            raise ValueError("id")
    if not _plain_int(body["observed_at"], 1, INT64_MAX):
        raise ValueError("observed_at")
    if type(body["evidence_kind"]) is not str or body["evidence_kind"] not in LIMIT_EVIDENCE_KINDS:
        raise ValueError("evidence_kind")      # the type first: a list or an object is not hashable
    if body["resets_at"] is not None and not _plain_int(body["resets_at"], 1, LIMIT_RESET_CEILING):
        raise ValueError("resets_at")
    windows = body["exhausted_windows"]
    if type(windows) is not list or len(windows) > 4 or not all(
            type(w) is str and w in LIMIT_WINDOWS for w in windows):
        raise ValueError("windows")
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def limit_key(machine, account_alias, window, reset_epoch, parked_at_ms):
    """One limit fact: (installation, account alias, evidence kind, window, reset). With an unknown
    reset the marker's park time (milliseconds) joins the key. The seat, the message and the wrapper
    generation are NOT in it, so two seats on one account that hit one limit make one event."""
    tail = str(reset_epoch) if reset_epoch is not None else "unknown|%d" % parked_at_ms
    return "|".join((machine, account_alias, "claude_exhausted", window, tail))


def limit_event_id(key):
    return str(uuid.uuid5(NAMESPACE, "limit|" + key))


def limit_turn_id(key):
    """For a limit event `turn_id` names the limit fact, not a lease turn (Leasewise only checks that
    it is a valid id and keeps it for the replay comparison)."""
    return str(uuid.uuid5(NAMESPACE, "limit-turn|" + key))


def limit_body(key, account_id, parked_at_ms, reset_epoch, window):
    """The limit-event body, built field by field. `resets_at` is RAW (Leasewise adds the margin);
    there is no `model_id`, so the block is account-wide."""
    return {"event_id": limit_event_id(key), "account_id": account_id, "turn_id": limit_turn_id(key),
            "observed_at": parked_at_ms, "evidence_kind": "claude_exhausted",
            "resets_at": None if reset_epoch is None else reset_epoch * 1000,
            "exhausted_windows": [window]}


class AgentConfig:
    """One agent's already-resolved request settings (7a-3 builds these
    from the configuration file and a `policy-ids --json` snapshot)."""

    __slots__ = ("agent", "seat_id", "model_id", "task_class", "effort",
                 "expected_calls", "max_calls", "output_cap",
                 "per_call_output_cap", "ttl_ms")

    def __init__(self, agent, seat_id, model_id, task_class, effort,
                 expected_calls, max_calls, output_cap, per_call_output_cap,
                 ttl_ms):
        self.agent = agent
        self.seat_id = seat_id
        self.model_id = model_id
        self.task_class = task_class
        self.effort = effort
        self.expected_calls = expected_calls
        self.max_calls = max_calls
        self.output_cap = output_cap
        self.per_call_output_cap = per_call_output_cap
        self.ttl_ms = ttl_ms


def turn_uuid(seat_id, message_id):
    return str(uuid.uuid5(NAMESPACE, "turn|%s|%s" % (seat_id, message_id)))


def request_id(turn):
    return str(uuid.uuid5(NAMESPACE, "acquire|" + turn))


def dispatch_event_id(turn, dispatch_turn_id):
    return str(uuid.uuid5(NAMESPACE, "dispatch|%s|%s" % (turn, dispatch_turn_id)))


def final_event_id(turn):
    return str(uuid.uuid5(NAMESPACE, "final|" + turn))


def acquisition_body(config, message_id, observed_at_ms):
    """POST /v1/leases for an unknown-size (shadow) lease. The server
    derives shadow from the seat; nothing here can ask for anything else."""
    turn = turn_uuid(config.seat_id, message_id)
    return {
        "request_id": request_id(turn),
        "turn_id": turn,
        "seat_id": config.seat_id,
        "task_class": config.task_class,
        "model_id": config.model_id,
        "effort": config.effort,
        "gateway_id": None,
        "parent_lease_id": None,
        "context": {"mode": MODE_UNKNOWN, "initial_tokens": None,
                    "dispatch_tokens": None, "source": "unknown",
                    "observed_at": observed_at_ms,
                    "growth_tokens_per_call": None},
        "expected_calls": config.expected_calls,
        "max_calls": config.max_calls,
        "output_cap": config.output_cap,
        "per_call_output_cap": config.per_call_output_cap,
        "ttl_ms": config.ttl_ms,
    }


def rebuild_body(original, observed_at_ms):
    """The same request with a fresh estimate time (D7). The original body
    is kept by the caller; this returns a new dict."""
    body = dict(original)
    body["context"] = dict(original["context"])
    body["context"]["observed_at"] = observed_at_ms
    return body


def per_call_tokens(usage):
    """(input_total, cached_input, output) for ONE dispatch; each may be
    None. Claude: `input_tokens` excludes cache reads and writes, so the
    input total is the sum of the three, and the cached input is the read
    part (a subset of the input, DESIGN.md section 3). Any null part makes
    the total null; unknown is never 0."""
    if usage is None:
        return None, None, None
    parts = (usage["input_tokens"], usage["cache_read_tokens"],
             usage["cache_write_tokens"])
    total = None if any(p is None for p in parts) else _checked_sum(parts)
    return total, usage["cache_read_tokens"], usage["output_tokens"]


def _checked_sum(values):
    total = 0
    for value in values:
        total += value
        if total > INT64_MAX:
            return None
    return total


def dispatch_report_body(event_id, outcome, usage):
    """One non-final receipt for ONE launched dispatch (an observation)."""
    tokens_in, tokens_cached, tokens_out = per_call_tokens(usage)
    return {
        "event_id": event_id,
        "outcome": "completed" if outcome == "success" else "failed",
        "calls": 1,
        "input_tokens": tokens_in,
        "cached_input_tokens": tokens_cached,
        "output_tokens": tokens_out,
        "actual_micro_eur": None,
        "gateway_id": None,
        "seq": None,
        "final": False,
    }


def message_totals(launched_usages, complete):
    """The final receipt's numbers. `launched_usages` holds one usage (dict
    or None) per LAUNCHED dispatch observed. A field is the checked sum of
    the per-dispatch values only when the observation is complete and every
    launched dispatch knows that value; otherwise it is None."""
    totals = {"calls": len(launched_usages), "input_tokens": None,
              "cached_input_tokens": None, "output_tokens": None}
    if not complete:
        return totals
    per_call = [per_call_tokens(u) for u in launched_usages]
    for index, name in enumerate(("input_tokens", "cached_input_tokens",
                                  "output_tokens")):
        values = [row[index] for row in per_call]
        if all(v is not None for v in values):
            totals[name] = _checked_sum(values)
    return totals


def final_body(event_id, disposition, totals):
    """The one final receipt. completed -> completed; dead_lettered and
    delivery_failed -> failed. outcome_unknown has no final (the caller
    never asks for one)."""
    if disposition == "completed":
        outcome = "completed"
    elif disposition in ("dead_lettered", "delivery_failed"):
        outcome = "failed"
    else:
        raise ValueError("this disposition has no final")
    body = {"event_id": event_id, "outcome": outcome}
    body.update(totals)
    body.update({"actual_micro_eur": None, "gateway_id": None, "seq": None,
                 "final": True})
    return body


# Card L6a-1: a gateway's final receipt becomes ONE final `/settle` body. These are the only fields
# the body may carry (Leasewise's own wire keys for a settle), and the only outcome words.
RECEIPT_BODY_KEYS = frozenset({"event_id", "outcome", "calls", "input_tokens", "cached_input_tokens",
                               "output_tokens", "actual_micro_eur", "gateway_id", "seq", "final"})
RECEIPT_OUTCOMES = ("completed", "cancelled", "failed", "provider_limit")
# Decision O2: the gateway records no cached input, so the body says 0. 0 means "the gateway
# records none", never "none was used".
CACHED_INPUT_TOKENS = 0
_HEX64 = re.compile(r"[0-9a-f]{64}")


def lease_ref_hash(lease_id):
    """The reference the gateway keeps for a quota lease: the sha256 of the lease id's own text
    (lower-case hex). The reference handed to the gateway IS the lease id; the gateway stores only
    this hash, and so does the reporter, to find the lease again."""
    return hashlib.sha256(lease_id.encode("ascii")).hexdigest()


def receipt_event_id(ref_sha256, generation):
    """The settle's `event_id`: derived from the reference hash and the ledger's own generation,
    never from a position, a time or a counter, so reading the same receipt twice, or after a
    restart, always gives the same id (and Leasewise answers a replay with the first answer)."""
    return str(uuid.uuid5(NAMESPACE, "receipt|%s|%s" % (ref_sha256, generation)))


def receipt_body(gateway_id, receipt):
    """The final settle body for one validated receipt (see receipts.Receipt)."""
    return {
        "event_id": receipt_event_id(receipt.ref_sha256, receipt.generation),
        "outcome": receipt.outcome,
        "calls": receipt.calls,
        "input_tokens": receipt.input_tokens,
        "cached_input_tokens": CACHED_INPUT_TOKENS,
        "output_tokens": receipt.output_tokens,
        "actual_micro_eur": receipt.actual_micro_eur,
        "gateway_id": gateway_id,
        "seq": receipt.seq,
        "final": True,
    }


def receipt_body_text(body):
    """The canonical text of a receipt body, or ValueError if it is not exactly the allow-listed
    shape. The same canonical form the client sends."""
    if type(body) is not dict or frozenset(body) != RECEIPT_BODY_KEYS:
        raise ValueError("shape")
    for key in ("event_id", "gateway_id"):
        if type(body[key]) is not str or _LIMIT_UUID.fullmatch(body[key]) is None:
            raise ValueError("id")
    if type(body["outcome"]) is not str or body["outcome"] not in RECEIPT_OUTCOMES:
        raise ValueError("outcome")
    for key in ("calls", "input_tokens", "output_tokens", "actual_micro_eur", "seq"):
        if not _plain_int(body[key], 0, INT64_MAX):
            raise ValueError(key)
    if body["cached_input_tokens"] != CACHED_INPUT_TOKENS or type(body["cached_input_tokens"]) is not int:
        raise ValueError("cached_input_tokens")
    if body["final"] is not True:
        raise ValueError("final")
    return json.dumps(body, sort_keys=True, separators=(",", ":"))
