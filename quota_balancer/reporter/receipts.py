"""The receipt source adapter (card L6a-1): the ONE place that knows how a gateway's receipts reach us.

A gateway keeps one final receipt for every quota lease it served. This module reads them from one
of two sources and hands back checked, closed values, or says why it could not:

* a fixture file (the tests, and the only source until agenttalk ships its receipt command): the
  whole list of receipts, of which only those after the last stored `seq` are returned;
* a configured local command: an argument list (never a shell), a time limit, a size cap, and an
  environment with nothing inherited but what a program needs to start. It is run with
  `--after <seq>` added and must print the closed list of the receipts after that `seq`.

Nothing from a receipt that is not one of the closed fields below, and nothing from a command's
error output or a fault's text, is ever kept. If agenttalk changes its receipt format, only this
file changes (the shape in `KEYS`, the checks in `_row`).

An answer is one of three words (`ReadResult.status`):
  ok          the receipts (possibly none), all checked;
  read_later  the source could not be read just now (missing file, command not started, too slow,
              a non-zero exit, text that is not JSON): nothing is stored and it is asked again;
  refused     it was read but is not what the contract allows (a wrong key, type, bound or order, a
              repeated key, too much): nothing from it is stored, and a counter says so.
"""

import json
import os
import re
import subprocess
import threading

OK = "ok"
READ_LATER = "read_later"
REFUSED = "refused"

# The closed reasons, per status. These words (and counts of them) are all the status ever shows.
LATER_REASONS = ("source_missing", "cannot_run", "timeout", "exit_code", "not_json")
REFUSED_REASONS = ("not_a_list", "too_many", "too_large", "duplicate_key", "bad_row", "bad_order")

# The closed fields of one receipt: the gateway's own columns, without the agent or the message
# (names stay on the gateway). `closed_at` is checked and then dropped: nothing here uses it.
KEYS = frozenset({"seq", "quota_lease_ref_sha256", "outcome", "calls", "input_tokens",
                  "output_tokens", "actual_micro_eur", "closed_at", "generation"})
OUTCOMES = ("completed", "cancelled", "failed", "provider_limit")

MAX_ROWS = 1000                      # one read; the gateway pages its own list at 100
MAX_CALLS = 1_000_000
MAX_TOKENS = 10 ** 12
MAX_MICRO_EUR = 10 ** 12             # a million euros
INT64_MAX = 2 ** 63 - 1
_HEX64 = re.compile(r"[0-9a-f]{64}")
_GENERATION = re.compile(r"[0-9a-f]{32}")

DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAX_BYTES = 1024 * 1024
_ENV_KEPT = ("SYSTEMROOT", "SYSTEMDRIVE", "PATH")      # what a program needs to start; no secrets


class Receipt:
    """One checked receipt. Only the fields the settle needs are kept."""

    __slots__ = ("seq", "ref_sha256", "outcome", "calls", "input_tokens", "output_tokens",
                 "actual_micro_eur", "generation")

    def __repr__(self):
        return "Receipt(<hidden>)"

    __str__ = __repr__


class ReadResult:
    __slots__ = ("status", "reason", "receipts")

    def __init__(self, status, reason=None, receipts=()):
        self.status = status
        self.reason = reason
        self.receipts = tuple(receipts)


class _Later(Exception):
    pass


class _Refused(Exception):
    pass


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def _row(doc):
    """One receipt from its JSON object, or _Refused. Closed keys, exact types, bounds."""
    if type(doc) is not dict or frozenset(doc) != KEYS:
        raise _Refused("bad_row")
    if not (_int(doc["seq"], 1, INT64_MAX) and _int(doc["calls"], 0, MAX_CALLS)
            and _int(doc["input_tokens"], 0, MAX_TOKENS) and _int(doc["output_tokens"], 0, MAX_TOKENS)
            and _int(doc["actual_micro_eur"], 0, MAX_MICRO_EUR) and _int(doc["closed_at"], 1, INT64_MAX)):
        raise _Refused("bad_row")
    if not (type(doc["quota_lease_ref_sha256"]) is str and _HEX64.fullmatch(doc["quota_lease_ref_sha256"])
            and type(doc["generation"]) is str and _GENERATION.fullmatch(doc["generation"])
            and type(doc["outcome"]) is str and doc["outcome"] in OUTCOMES):
        raise _Refused("bad_row")
    if doc["calls"] == 0 and (doc["input_tokens"] or doc["output_tokens"] or doc["actual_micro_eur"]):
        raise _Refused("bad_row")        # no call, yet spend: a receipt that contradicts itself
    row = Receipt()
    row.seq, row.ref_sha256, row.outcome = doc["seq"], doc["quota_lease_ref_sha256"], doc["outcome"]
    row.calls, row.input_tokens, row.output_tokens = doc["calls"], doc["input_tokens"], doc["output_tokens"]
    row.actual_micro_eur, row.generation = doc["actual_micro_eur"], doc["generation"]
    return row


def _no_repeats(pairs):
    keys = [key for key, _value in pairs]
    if len(set(keys)) != len(keys):
        raise _Refused("duplicate_key")
    return dict(pairs)


def _no_constant(_name):
    raise _Refused("bad_row")            # NaN and the infinities are not numbers a receipt may carry


def parse(raw, after_seq, *, drop_old=False):
    """The bytes a source gave -> a tuple of Receipt, in order. Raises _Later or _Refused.
    `drop_old` (the fixture, which holds the whole ledger) skips receipts at or before `after_seq`;
    a command was asked for the ones after it, so any older row is a wrong order."""
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_repeats, parse_constant=_no_constant)
    except _Refused:
        raise
    except (ValueError, RecursionError):         # UnicodeError is a ValueError
        raise _Later("not_json") from None
    if type(doc) is not list:
        raise _Refused("not_a_list")
    if len(doc) > MAX_ROWS:
        raise _Refused("too_many")
    rows = [_row(item) for item in doc]
    if drop_old:
        rows = [row for row in rows if row.seq > after_seq]
    last = after_seq
    for row in rows:
        if row.seq <= last:
            raise _Refused("bad_order")
        last = row.seq
    return tuple(rows)


def _read_fixture(path, max_bytes):
    try:
        with open(path, "rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError:
        raise _Later("source_missing") from None
    if len(raw) > max_bytes:
        raise _Refused("too_large")
    return raw


def _environment(extra):
    env = {name: os.environ[name] for name in _ENV_KEPT if name in os.environ}
    env.update(dict(extra))
    return env


def _read_command(source, after_seq):
    """Run the configured command once. No shell, no standard input, no error text kept, a time
    limit and a size cap on what it prints."""
    argv = list(source.command) + ["--after", str(after_seq)]
    try:
        proc = subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=_environment(source.env))
    except (OSError, ValueError):
        raise _Later("cannot_run") from None
    timed_out = threading.Event()

    def stop():
        timed_out.set()
        try:
            proc.kill()                  # this process, by its handle: never by name
        except OSError:
            pass

    timer = threading.Timer(source.timeout_seconds, stop)
    timer.start()
    try:
        raw = proc.stdout.read(source.max_bytes + 1)
        too_big = len(raw) > source.max_bytes
        if too_big:
            try:
                proc.kill()
            except OSError:
                pass
        code = proc.wait()
    finally:
        timer.cancel()
        proc.stdout.close()
        proc.wait()
    if timed_out.is_set():
        raise _Later("timeout")
    if too_big:
        raise _Refused("too_large")
    if code != 0:
        raise _Later("exit_code")
    return raw


def read_receipts(source, after_seq):
    """The receipts of `source` after `after_seq` -> ReadResult. Never raises on bad input."""
    try:
        if source.command is not None:
            rows = parse(_read_command(source, after_seq), after_seq)
        else:
            rows = parse(_read_fixture(source.fixture, source.max_bytes), after_seq, drop_old=True)
    except _Later as exc:
        return ReadResult(READ_LATER, exc.args[0])
    except _Refused as exc:
        return ReadResult(REFUSED, exc.args[0])
    return ReadResult(OK, None, rows)
