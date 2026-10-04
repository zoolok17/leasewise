"""The money reader's durable memory (card R1-READER step 2, DESIGN-R1-READER.md 3.1, 7, 8, 10 and the decisions D-195 and D-203).

The rule the memory exists to keep: **the reader never skips or replaces a report number.** A report is numbered and saved as exact bytes
(`gwr_pending`); it stays the only thing the reader may send until Leasewise accepts exactly those bytes; only then does the reader take the
next number. Two progress numbers per generation say where it stands: `minted_seq` (the highest number saved) and `acked_seq` (the highest
accepted). The ONLY two consistent states are `minted == acked` with no pending row, and `minted == acked + 1` with a pending row of exactly
that number.

What is here (pure state functions over the reporter's own database; **nothing reads a gateway and nothing is sent**):

* `start`    the start-up check, and the first start (a complete absence of state creates a fresh incarnation, D-203). Anything inconsistent
             sets the halt `state_unavailable`; nothing may then be minted or sent.
* `mint`     in ONE transaction: the next number's exact bytes as the pending row, `minted_seq` + 1, the receipts already read, and a new
             epoch if the generation changed. Refused (nothing changed) while halted, while a pending row exists, or when the state is inconsistent. One exception:
             a number already at the largest possible value halts the reader (the halt itself is committed, nothing else is written).
* `accept`   in ONE transaction, only for HTTP 202 with `accepted` exactly `true` and the same `event_id`: the pending row is deleted and
             `acked_seq` advances to its number. Anything else changes nothing here.
* `reset`    in ONE transaction: the old incarnation, its halt and its pending bytes go to the archive (never erased), then a fresh
             incarnation and empty progress.

Only numbers, hex strings, counters and the exact report bytes are kept. No name of an agent, a message, a lease reference or a path.
Classifying refusals, counting them, the cycle and the operator commands are step 3.
"""

import json
import re
import secrets
import sqlite3
from collections import namedtuple

from ..validate_readings import derive_event_id

CONTINUITY_CONFLICT = "continuity_conflict"
SEND_REFUSED = "send_refused"
STATE_UNAVAILABLE = "state_unavailable"
HALT_WORDS = (CONTINUITY_CONFLICT, SEND_REFUSED, STATE_UNAVAILABLE)

INT64_MAX = 2 ** 63 - 1
# How many definitive refusals IN A ROW (HTTP 400 `INVALID_CONFIG` or `SCHEMA_UNSUPPORTED`) the reader endures for one pending report before it
# stops sending and halts as `send_refused`. A small positive number: a refusal that repeats this often is a defect or a wrong clock, not bad luck.
# The count lives on the pending row, so a restart keeps it. A retry answer does not reset it; only an acceptance clears it (with the row).
REFUSAL_THRESHOLD = 5
# The most raw evidence a reset keeps in the archive's quarantine column when the old state cannot be archived under the normal constraints.
QUARANTINE_MAX_BYTES = 256 * 1024
READER_AGENT = "_money_reader"                # the counters' agent word: starts with an underscore, so it can never be an agent's name
MAX_BODY_BYTES = 1024 * 1024
_HEX32 = re.compile(r"[0-9a-f]{32}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_PERIOD = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")

# --- the schema (a reporter schema step: `state.SCHEMA_VERSION` 5) -----------------------------------------------------------------
_HEX32_SQL = "length({c})=32 AND {c} NOT GLOB '*[^0-9a-f]*'"
_UUID_SQL = ("length({c})=36 AND {c} GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
             "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
             "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'")
_HALT_SQL = "{c} IN ('continuity_conflict','send_refused','state_unavailable')"
_BOUND = "{c}>=0 AND {c}<=9223372036854775807"

GWR_DDL = (
    # The reader's identity: one row (a random value, never changed; `reset` replaces the whole row). `halt` is null or a closed word.
    "CREATE TABLE gwr_identity(id INTEGER PRIMARY KEY CHECK(id=1),"
    " reader_incarnation TEXT NOT NULL CHECK(" + _HEX32_SQL.format(c="reader_incarnation") + "),"
    " halt TEXT CHECK(halt IS NULL OR " + _HALT_SQL.format(c="halt") + "),"
    " created_ms INTEGER NOT NULL CHECK(" + _BOUND.format(c="created_ms") + ")) STRICT",
    "CREATE TRIGGER gwr_identity_incarnation_fixed BEFORE UPDATE OF reader_incarnation ON gwr_identity BEGIN"
    " SELECT RAISE(ABORT, 'a reader incarnation never changes'); END",
    # The ledger epochs: numbers only grow; the current epoch is the newest row.
    "CREATE TABLE gwr_epoch(ledger_epoch INTEGER PRIMARY KEY CHECK(ledger_epoch>=1 AND ledger_epoch<=9223372036854775807),"
    " generation TEXT NOT NULL CHECK(" + _HEX32_SQL.format(c="generation") + ")) STRICT",
    # The two progress numbers per generation. The consistent pairs are the only ones the table accepts.
    "CREATE TABLE gwr_seq(generation TEXT PRIMARY KEY CHECK(" + _HEX32_SQL.format(c="generation") + "),"
    " minted_seq INTEGER NOT NULL CHECK(" + _BOUND.format(c="minted_seq") + "),"
    " acked_seq INTEGER NOT NULL CHECK(" + _BOUND.format(c="acked_seq") + "),"
    " CHECK(acked_seq<=minted_seq AND minted_seq<=acked_seq+1)) STRICT",
    # At most one pending report: the exact bytes, deleted only on acceptance.
    "CREATE TABLE gwr_pending(id INTEGER PRIMARY KEY CHECK(id=1),"
    " generation TEXT NOT NULL REFERENCES gwr_seq(generation) CHECK(" + _HEX32_SQL.format(c="generation") + "),"
    " seq INTEGER NOT NULL CHECK(seq>=1 AND seq<=9223372036854775807),"
    " body BLOB NOT NULL CHECK(length(body)>0 AND length(body)<=1048576 AND json_valid(CAST(body AS TEXT))),"
    " event_id TEXT NOT NULL CHECK(" + _UUID_SQL.format(c="event_id") + "),"
    " idempotency_key TEXT NOT NULL CHECK(" + _UUID_SQL.format(c="idempotency_key") + "),"
    " made_ms INTEGER NOT NULL CHECK(" + _BOUND.format(c="made_ms") + "),"
    " refusals INTEGER NOT NULL DEFAULT 0 CHECK(" + _BOUND.format(c="refusals") + ")) STRICT",
    # The receipts already read, per generation: the watermark, and the receipts whose month is unknown or spans months.
    "CREATE TABLE gwr_receipts(generation TEXT PRIMARY KEY REFERENCES gwr_seq(generation) CHECK(" + _HEX32_SQL.format(c="generation") + "),"
    " read_through_seq INTEGER NOT NULL CHECK(" + _BOUND.format(c="read_through_seq") + "),"
    " no_month INTEGER NOT NULL CHECK(" + _BOUND.format(c="no_month") + ")) STRICT",
    # ...and, per month, the number and total of the receipts whose cost lies wholly inside that month.
    "CREATE TABLE gwr_receipt_months(generation TEXT NOT NULL REFERENCES gwr_receipts(generation) CHECK(" + _HEX32_SQL.format(c="generation") + "),"
    " period TEXT NOT NULL CHECK(length(period)=7 AND period GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'"
    " AND substr(period,6,2) BETWEEN '01' AND '12'),"
    " receipts INTEGER NOT NULL CHECK(" + _BOUND.format(c="receipts") + "),"
    " total_micro_eur INTEGER NOT NULL CHECK(" + _BOUND.format(c="total_micro_eur") + "),"
    " PRIMARY KEY(generation, period)) STRICT",
    # What `reset` keeps; never changed, never erased. The incarnation is null only when the identity row was already gone.
    "CREATE TABLE gwr_archive(id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " reader_incarnation TEXT CHECK(reader_incarnation IS NULL OR (" + _HEX32_SQL.format(c="reader_incarnation") + ")),"
    " halt TEXT CHECK(halt IS NULL OR " + _HALT_SQL.format(c="halt") + "),"
    " pending_generation TEXT CHECK(pending_generation IS NULL OR (" + _HEX32_SQL.format(c="pending_generation") + ")),"
    " pending_seq INTEGER CHECK(pending_seq IS NULL OR (pending_seq>=1 AND pending_seq<=9223372036854775807)),"
    " pending_event_id TEXT CHECK(pending_event_id IS NULL OR (" + _UUID_SQL.format(c="pending_event_id") + ")),"
    " pending_body BLOB CHECK(pending_body IS NULL OR (length(pending_body)>0 AND length(pending_body)<=1048576)),"
    " archived_ms INTEGER NOT NULL CHECK(" + _BOUND.format(c="archived_ms") + "),"
    " CHECK((pending_generation IS NULL) = (pending_seq IS NULL) AND (pending_seq IS NULL) = (pending_event_id IS NULL)"
    " AND (pending_event_id IS NULL) = (pending_body IS NULL))) STRICT",
    "CREATE TRIGGER gwr_archive_immutable BEFORE UPDATE ON gwr_archive BEGIN SELECT RAISE(ABORT, 'the reader archive is immutable'); END",
    "CREATE TRIGGER gwr_archive_keep BEFORE DELETE ON gwr_archive BEGIN SELECT RAISE(ABORT, 'the reader archive is immutable'); END",
)
# Version 6 (card R1-READER step 3a): a step of its own, applied to a file at version 5 in one transaction. The archive gains a bounded quarantine
# column (a reset that cannot archive a damaged old state under the normal constraints keeps its raw evidence there), and the operator's acts get
# an immutable audit table.
GWR_V6_DDL = (
    "ALTER TABLE gwr_archive ADD COLUMN quarantine TEXT CHECK(quarantine IS NULL OR length(quarantine)<=262144)",
    "CREATE TABLE gwr_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " act TEXT NOT NULL CHECK(act IN ('resume','reset')),"
    " at_ms INTEGER NOT NULL CHECK(" + _BOUND.format(c="at_ms") + "),"
    " quarantined INTEGER NOT NULL CHECK(quarantined IN (0,1)),"
    " refusals_total INTEGER NOT NULL CHECK(" + _BOUND.format(c="refusals_total") + ")) STRICT",
    "CREATE TRIGGER gwr_audit_immutable BEFORE UPDATE ON gwr_audit BEGIN SELECT RAISE(ABORT, 'the reader audit is immutable'); END",
    "CREATE TRIGGER gwr_audit_keep BEFORE DELETE ON gwr_audit BEGIN SELECT RAISE(ABORT, 'the reader audit is immutable'); END",
)
TABLES_V5 = ("gwr_identity", "gwr_epoch", "gwr_seq", "gwr_pending", "gwr_receipts", "gwr_receipt_months", "gwr_archive")
TABLES = TABLES_V5 + ("gwr_audit",)

# --- results ----------------------------------------------------------------------------------------------------------------------
# `halt` is None when the reader may go on, else a closed word. `persisted` is False only when there was no identity row to keep the halt in
# (D-203: an absent identity with surviving rows halts, and is found again by every start).
Start = namedtuple("Start", "halt incarnation fresh persisted")


class Refused(Exception):
    """A mint that must not happen. Carries one closed word and nothing else; nothing was changed."""

    def __init__(self, word):
        super().__init__(word)
        self.word = word


# The reasons a mint is refused (closed words, shown as counts by step 3).
HALTED = "halted"
PENDING_EXISTS = "pending_exists"
BAD_REPORT = "bad_report"
BAD_RECEIPTS = "bad_receipts"
BAD_GENERATION = "bad_generation"
NOT_RESUMABLE = "not_resumable"               # `resume` is offered only after `send_refused`
RESET_TOO_LARGE = "reset_too_large"           # the old state is too large for the quarantine form: nothing was changed


def _fault(step):
    """The fault-injection hook: called at HOOK POINTS of `mint`, `accept`, `resume` and `reset`, not after every SQL statement (the several
    writes of the receipt months share one later hook). A test stops at a hook point to see that the transaction left nothing behind; the
    complete evidence of atomicity is the test that fails each real SQL write (`tests/test_cardr1reader2_fix1.py`, `EveryWrite`)."""


# --- reading the state ------------------------------------------------------------------------------------------------------------

def _identity(state):
    return state.one("SELECT reader_incarnation, halt FROM gwr_identity WHERE id=1")


_SURVIVOR_QUERIES = ("SELECT 1 FROM gwr_pending LIMIT 1", "SELECT 1 FROM gwr_seq LIMIT 1", "SELECT 1 FROM gwr_epoch LIMIT 1",
                     "SELECT 1 FROM gwr_receipts LIMIT 1", "SELECT 1 FROM gwr_receipt_months LIMIT 1")
# Children first, one fixed statement each (no table name is ever built into SQL text).
_CLEAR = (("cleared_pending", "DELETE FROM gwr_pending"), ("cleared_receipt_months", "DELETE FROM gwr_receipt_months"),
          ("cleared_receipts", "DELETE FROM gwr_receipts"), ("cleared_seq", "DELETE FROM gwr_seq"),
          ("cleared_epoch", "DELETE FROM gwr_epoch"), ("cleared_identity", "DELETE FROM gwr_identity"))


def _survivors(state):
    """True when any pending, progress, epoch or receipt row exists (the rows D-203 names; the archive is not one of them)."""
    return any(state.one(statement) is not None for statement in _SURVIVOR_QUERIES)


def _exact_int(value):
    """A whole number exactly (not a boolean, not a float, not a text), from 1 to 2^63-1 (Leasewise's validator also accepts 0 for both `seq` and `ledger_epoch`;
    the reader never makes a 0 and requires at least 1)."""
    return type(value) is int and 1 <= value <= INT64_MAX


def report_facts(body):
    """The facts a saved report states about itself: (event_id, incarnation, generation, seq, epoch), or None when the bytes are not a report
    document of the expected shape OR any of the five is not exactly what it must be: `event_id` a text in the form `derive_event_id` makes,
    the incarnation and the generation exactly 32 lowercase hex characters, `seq` and `ledger_epoch` exactly integers (a JSON `true` or `1.0` is
    not the number 1) from 1 to 2^63-1. Every type is checked BEFORE any comparison or lookup, so no value can raise. The ONE checker, used by
    the mint (a malformed report is `bad_report`, nothing written) and by the start-up check (malformed stored bytes halt as
    `state_unavailable`). It is also the one place that knows where the facts sit in the bytes the sender will post (step 3 builds them)."""
    try:
        doc = json.loads(bytes(body).decode("utf-8"))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None
    if type(doc) is not dict:
        return None
    payload = doc.get("payload")
    if type(payload) is not dict:
        return None
    event_id, incarnation = doc.get("event_id"), payload.get("reader_incarnation")
    generation, seq, epoch = payload.get("generation"), payload.get("seq"), payload.get("ledger_epoch")
    if not (type(event_id) is str and _UUID.fullmatch(event_id) and type(incarnation) is str and _HEX32.fullmatch(incarnation)
            and type(generation) is str and _HEX32.fullmatch(generation) and _exact_int(seq) and _exact_int(epoch)):
        return None
    return event_id, incarnation, generation, seq, epoch


def _int(value, low=0):
    return type(value) is int and low <= value <= INT64_MAX


def inconsistency(state):
    """Why the state cannot be trusted, as a short internal reason, or None when it is consistent. Reads only. Checked per generation:
    the two accepted number pairs, the integer bounds, no orphan row (a pending, receipt or epoch mapping without its progress row), no
    second unfinished generation, and for the pending row its bytes, its event id, the incarnation and the epoch mapping."""
    identity = _identity(state)
    if identity is None:
        return "no_identity" if _survivors(state) else None
    incarnation = identity[0]
    if type(incarnation) is not str or _HEX32.fullmatch(incarnation) is None:
        return "bad_incarnation"
    rows = state.all("SELECT generation, minted_seq, acked_seq FROM gwr_seq")
    progress = {}
    for generation, minted, acked in rows:
        if type(generation) is not str or _HEX32.fullmatch(generation) is None or not _int(minted) or not _int(acked):
            return "bad_progress"
        if not (acked == minted or minted == acked + 1):
            return "numbers"
        progress[generation] = (minted, acked)
    epochs = {}
    for epoch, generation in state.all("SELECT ledger_epoch, generation FROM gwr_epoch"):
        if not _int(epoch, 1) or type(generation) is not str or _HEX32.fullmatch(generation) is None:
            return "bad_epoch"
        epochs[epoch] = generation
    if any(generation not in epochs.values() for generation in progress):
        return "epoch_mapping"                                                   # a generation with progress must have been given an epoch
    if any(generation not in progress for generation in epochs.values()):
        return "orphan_epoch"                                                    # ...and an epoch must belong to a generation with progress
                                                                                 # (the mint creates both in one transaction: one alone is damage)
    unfinished = [g for g, (minted, acked) in progress.items() if minted != acked]
    pending = state.all("SELECT generation, seq, body, event_id, idempotency_key, made_ms, refusals FROM gwr_pending")
    if len(pending) > 1:
        return "two_pending"
    if len(unfinished) > 1:
        return "two_unfinished"
    if not pending:
        return "unfinished_without_pending" if unfinished else _receipts_inconsistency(state, progress)
    generation, seq, body, event_id, key, made_ms, refusals = pending[0]
    if generation not in progress:
        return "orphan_pending"
    if unfinished != [generation] or progress[generation][0] != seq or seq != progress[generation][1] + 1:
        return "pending_number"
    if not _int(made_ms) or not _int(refusals):
        return "bad_pending"
    if event_id != derive_event_id(incarnation, generation, seq) or key != event_id:
        return "pending_event_id"
    facts = report_facts(body) if isinstance(body, (bytes, bytearray)) and 0 < len(body) <= MAX_BODY_BYTES else None
    if facts is None or facts[:4] != (event_id, incarnation, generation, seq) or epochs.get(facts[4]) != generation:
        return "pending_bytes"
    return _receipts_inconsistency(state, progress)


def _receipts_inconsistency(state, progress):
    rows = state.all("SELECT generation, read_through_seq, no_month FROM gwr_receipts")
    for generation, through, no_month in rows:
        if generation not in progress or not _int(through) or not _int(no_month):
            return "bad_receipts"
    known = {row[0] for row in rows}
    if any(generation not in known for generation in progress):
        return "progress_without_receipts"                                       # the mint writes a generation's progress and receipts together
    for generation, period, count, total in state.all("SELECT generation, period, receipts, total_micro_eur FROM gwr_receipt_months"):
        if generation not in known or type(period) is not str or _PERIOD.fullmatch(period) is None or not _int(count) or not _int(total):
            return "bad_receipts"
    return None


# --- the start-up check, and the first start --------------------------------------------------------------------------------------

def start(state, now_ms, new_incarnation=None):
    """The start-up check (at start, and before minting) -> `Start`. In one transaction:

    * a **complete absence** of state (no identity and no pending, progress, epoch or receipt row) is a first start (D-203): a fresh
      incarnation is created, with nothing to archive;
    * an **absent identity with any surviving row** is inconsistent and halts as `state_unavailable`; it is never read as a first start.
      There is no identity row to keep the halt in, so it is not persisted: every start finds it again;
    * a state that is not one of the two consistent pictures sets `halt = state_unavailable` (a halt already set, for example
      `continuity_conflict`, is kept: only `reset` clears either);
    * otherwise the state is accepted as it is (including `0/0`, and a return to a fully acknowledged generation)."""
    with state.txn():
        identity = _identity(state)
        if identity is None:
            if _survivors(state):
                return Start(STATE_UNAVAILABLE, None, False, False)
            fresh = new_incarnation or secrets.token_hex(16)
            state.conn.execute("INSERT INTO gwr_identity(id, reader_incarnation, halt, created_ms) VALUES(1,?,NULL,?)", (fresh, now_ms))
            return Start(None, fresh, True, True)
        why = inconsistency(state)
        halt = identity[1]
        if why is not None and halt is None:
            state.conn.execute("UPDATE gwr_identity SET halt=? WHERE id=1", (STATE_UNAVAILABLE,))
            halt = STATE_UNAVAILABLE
        return Start(halt, identity[0], False, True)


# --- mint ---------------------------------------------------------------------------------------------------------------------------

def _store_receipts(state, generation, totals):
    """Replace the receipts already read for `generation` by `totals` (a `gwreport.Totals`); the watermark never goes down."""
    if totals.generation != generation or not _int(totals.read_through) or not _int(totals.no_month):
        raise Refused(BAD_RECEIPTS)
    current = state.one("SELECT read_through_seq FROM gwr_receipts WHERE generation=?", (generation,))
    if current is not None and totals.read_through < current[0]:
        raise Refused(BAD_RECEIPTS)
    state.conn.execute("INSERT OR REPLACE INTO gwr_receipts(generation, read_through_seq, no_month) VALUES(?,?,?)",
                       (generation, totals.read_through, totals.no_month))
    _fault("receipts")
    state.conn.execute("DELETE FROM gwr_receipt_months WHERE generation=?", (generation,))
    for period, count, total in totals.by_period:
        if type(period) is not str or _PERIOD.fullmatch(period) is None or not _int(count) or not _int(total):
            raise Refused(BAD_RECEIPTS)
        state.conn.execute("INSERT INTO gwr_receipt_months(generation, period, receipts, total_micro_eur) VALUES(?,?,?,?)",
                           (generation, period, count, total))
    _fault("receipt_months")


def mint(state, generation, build, totals, now_ms):
    """Number, save and keep the next report of `generation`, in ONE transaction -> the pending row as (seq, event_id, body). A refusal or a
    failure changes nothing, with ONE exception: a `minted_seq` (or a needed new epoch) already at 2^63-1 cannot be incremented, so the mint
    commits the halt `state_unavailable` first (that single write) and then refuses with `halted`; no number, epoch or report is written.

    Allowed only when the state is consistent, no halt is set and no pending row exists; that is exactly `acked_seq == minted_seq` for the
    generation (a consistent state with the two numbers apart always has a pending row; without one it is refused as inconsistent). The
    caller has already read and validated every receipt page up to the report's `N` and passes the resulting `gwreport.Totals`; `build(
    incarnation, epoch, seq)` returns the exact bytes of the report (a pure function; the facts it states about itself are checked against
    the state before anything is kept). A change of generation takes a NEW epoch number, a return to an earlier generation too; the
    numbers of a generation keep growing."""
    if type(generation) is not str or _HEX32.fullmatch(generation) is None:
        raise Refused(BAD_GENERATION)
    exhausted = False
    with state.txn():
        identity = _identity(state)
        if identity is None or identity[1] is not None or inconsistency(state) is not None:
            raise Refused(HALTED)
        incarnation = identity[0]
        if state.one("SELECT 1 FROM gwr_pending") is not None:
            raise Refused(PENDING_EXISTS)
        row = state.one("SELECT minted_seq, acked_seq FROM gwr_seq WHERE generation=?", (generation,))
        # (No pending row and a consistent state mean `minted_seq == acked_seq` here: any other pair was refused above as inconsistent.)
        latest = state.one("SELECT ledger_epoch, generation FROM gwr_epoch ORDER BY ledger_epoch DESC LIMIT 1")
        needs_epoch = latest is None or latest[1] != generation
        # The largest number cannot be incremented: the next report number (or the next epoch number) would leave the range Leasewise accepts.
        # That halts as `state_unavailable` (the operator's `reset` recovers); nothing is numbered, saved or created.
        if (row is not None and row[0] >= INT64_MAX) or (needs_epoch and latest is not None and latest[0] >= INT64_MAX):
            state.conn.execute("UPDATE gwr_identity SET halt=? WHERE id=1", (STATE_UNAVAILABLE,))
            exhausted = True
        else:
            seq, event_id, body = _mint_next(state, generation, incarnation, row, latest, needs_epoch, build, totals, now_ms)
    if exhausted:
        raise Refused(HALTED)
    return seq, event_id, body


def _mint_next(state, generation, incarnation, row, latest, needs_epoch, build, totals, now_ms):
    """The writes of a mint, inside its transaction."""
    if row is None:
        state.conn.execute("INSERT INTO gwr_seq(generation, minted_seq, acked_seq) VALUES(?,0,0)", (generation,))
        row = (0, 0)
        _fault("seq_row")
    if needs_epoch:
        epoch = 1 if latest is None else latest[0] + 1
        state.conn.execute("INSERT INTO gwr_epoch(ledger_epoch, generation) VALUES(?,?)", (epoch, generation))
        _fault("epoch")
    else:
        epoch = latest[0]
    seq = row[0] + 1
    event_id = derive_event_id(incarnation, generation, seq)
    body = build(incarnation, epoch, seq)
    if not isinstance(body, (bytes, bytearray)) or not 0 < len(body) <= MAX_BODY_BYTES:
        raise Refused(BAD_REPORT)
    body = bytes(body)
    if report_facts(body) != (event_id, incarnation, generation, seq, epoch):
        raise Refused(BAD_REPORT)                             # the bytes must state the number, identity and epoch the state gives them, exactly
    state.conn.execute("INSERT INTO gwr_pending(id, generation, seq, body, event_id, idempotency_key, made_ms, refusals) VALUES(1,?,?,?,?,?,?,0)",
                       (generation, seq, body, event_id, event_id, now_ms))
    _fault("pending")
    _store_receipts(state, generation, totals)
    state.conn.execute("UPDATE gwr_seq SET minted_seq=? WHERE generation=?", (seq, generation))
    _fault("minted")
    return seq, event_id, body


# --- acceptance ---------------------------------------------------------------------------------------------------------------------

def accept(state, status, answer):
    """Leasewise's answer to the pending report -> True if it was accepted and the state advanced, else False with nothing changed.

    Accepted means exactly: HTTP status 202 (not 200), an object answer whose `accepted` is exactly `true` (not the text "true", not 1) and
    whose `event_id` is the pending report's own. Then, in ONE transaction, the pending row is deleted and `acked_seq` advances to its
    number. Every other answer (and counting definitive refusals) is step 3."""
    if status != 202 or type(status) is not int or type(answer) is not dict or answer.get("accepted") is not True:
        return False
    with state.txn():
        pending = state.one("SELECT generation, seq, event_id FROM gwr_pending")
        if pending is None or answer.get("event_id") != pending[2] or type(answer.get("event_id")) is not str:
            return False
        if inconsistency(state) is not None:
            return False
        state.conn.execute("DELETE FROM gwr_pending WHERE id=1")
        _fault("deleted")
        state.conn.execute("UPDATE gwr_seq SET acked_seq=? WHERE generation=? AND minted_seq=?", (pending[1], pending[0], pending[1]))
        _fault("acked")
    return True


# --- reset --------------------------------------------------------------------------------------------------------------------------

_RAW = (("identity", "SELECT * FROM gwr_identity"), ("epoch", "SELECT * FROM gwr_epoch"), ("seq", "SELECT * FROM gwr_seq"),
        ("pending", "SELECT * FROM gwr_pending"), ("receipts", "SELECT * FROM gwr_receipts"),
        ("receipt_months", "SELECT * FROM gwr_receipt_months"))


def _raw_value(value):
    return {"hex": bytes(value).hex()} if isinstance(value, (bytes, bytearray)) else value


def _quarantine(state):
    """The whole old state as raw evidence (JSON text), or `Refused(RESET_TOO_LARGE)` when it does not fit `QUARANTINE_MAX_BYTES`. Nothing is
    discarded or repaired: every column of every row is kept as it is (bytes as hex)."""
    evidence, size = {}, 2
    for name, statement in _RAW:
        rows = []
        for row in state.all(statement):
            rows.append([_raw_value(value) for value in row])
            size += len(json.dumps(rows[-1], default=repr)) + 2
            if size > QUARANTINE_MAX_BYTES:
                raise Refused(RESET_TOO_LARGE)
        evidence[name] = rows
    text = json.dumps(evidence, sort_keys=True, default=repr)
    if len(text.encode("utf-8")) > QUARANTINE_MAX_BYTES:
        raise Refused(RESET_TOO_LARGE)
    return text


def reset(state, now_ms, new_incarnation=None):
    """The operator's reset, as ONE transaction (the command is `reporter reset --confirm`) -> the new incarnation. The old incarnation, its halt
    and its pending bytes go to `gwr_archive` (never erased); then every progress, epoch, pending and receipt row is emptied, a fresh identity
    replaces the old one and an audit row is written. A crash at any point leaves the old state or the new one, never a mix. Leasewise sees a
    new reader that must be chosen by adoption; nothing of the old sequence is carried over.

    **Quarantine form.** If an old value cannot be archived under the archive's normal constraints (a damaged pending `event_id`, a damaged
    incarnation...), the archive instead keeps the whole old state as raw evidence in its bounded `quarantine` column, never discarding it. A
    state too large for `QUARANTINE_MAX_BYTES` is refused as `reset_too_large` with nothing changed (the file-level recovery is in the step
    record)."""
    fresh = secrets.token_hex(16) if new_incarnation is None else new_incarnation
    with state.txn():
        identity = _identity(state)
        pending = state.one("SELECT generation, seq, event_id, body FROM gwr_pending")
        quarantined = 0
        try:
            state.conn.execute(
                "INSERT INTO gwr_archive(reader_incarnation, halt, pending_generation, pending_seq, pending_event_id, pending_body, archived_ms)"
                " VALUES(?,?,?,?,?,?,?)",
                (None if identity is None else identity[0], None if identity is None else identity[1],
                 None if pending is None else pending[0], None if pending is None else pending[1],
                 None if pending is None else pending[2], None if pending is None else pending[3], now_ms))
        except sqlite3.IntegrityError:
            evidence = _quarantine(state)                           # or Refused(RESET_TOO_LARGE): nothing has changed
            state.conn.execute("INSERT INTO gwr_archive(quarantine, archived_ms) VALUES(?,?)", (evidence, now_ms))
            quarantined = 1
        _fault("archived")
        for step, statement in _CLEAR:
            state.conn.execute(statement)
            _fault(step)
        state.conn.execute("INSERT INTO gwr_identity(id, reader_incarnation, halt, created_ms) VALUES(1,?,NULL,?)", (fresh, now_ms))
        _fault("created")
        state.conn.execute("INSERT INTO gwr_audit(act, at_ms, quarantined, refusals_total) VALUES('reset',?,?,?)",
                           (now_ms, quarantined, state.counter(READER_AGENT, "refused_answers")))
        state.bump(READER_AGENT, "resets")
        _fault("audited")
    return fresh


# --- the send side (card R1-READER step 3a): what an answer does to the state --------------------------------------------------------

def pending(state):
    """The pending report as (generation, seq, body, event_id, key, refusals), or None."""
    row = state.one("SELECT generation, seq, body, event_id, idempotency_key, refusals FROM gwr_pending")
    return None if row is None else (row[0], row[1], bytes(row[2]), row[3], row[4], row[5])


def halt_word(state):
    """The halt that is set, or None."""
    row = _identity(state)
    return None if row is None else row[1]


def note_conflict(state):
    """Leasewise answered `IDEMPOTENCY_CONFLICT`: the reader halts (`continuity_conflict`) until the operator resets it. Counted. This holds even
    if Leasewise could not record a `sequence_conflict`."""
    with state.txn():
        state.conn.execute("UPDATE gwr_identity SET halt=? WHERE id=1", (CONTINUITY_CONFLICT,))
        state.bump(READER_AGENT, CONTINUITY_CONFLICT)
        state.bump(READER_AGENT, "refused_answers")
        _fault("conflict")


def note_refusal(state):
    """A definitive refusal of the pending report: the count on the pending row goes up (one transaction with the counters). Reaching
    `REFUSAL_THRESHOLD` in a row halts the reader as `send_refused`. -> the new consecutive count."""
    with state.txn():
        state.conn.execute("UPDATE gwr_pending SET refusals=refusals+1 WHERE id=1")
        count = state.one("SELECT refusals FROM gwr_pending")[0]
        state.bump(READER_AGENT, "refused_answers")
        if count >= REFUSAL_THRESHOLD and _identity(state)[1] is None:
            state.conn.execute("UPDATE gwr_identity SET halt=? WHERE id=1", (SEND_REFUSED,))
            state.bump(READER_AGENT, SEND_REFUSED)
        _fault("refusal")
    return count


def resume(state, now_ms):
    """The operator's `resume`, ONE transaction. Offered only after `send_refused` (`Refused(NOT_RESUMABLE)` otherwise, never after
    `continuity_conflict` or `state_unavailable`; nothing changed). It clears the halt and gives the pending report a NEW retry budget (the
    consecutive count goes back to 0) while the cumulative diagnostics (`refused_answers`, the other counters) are kept; one audit row is written.
    The next send resends the same saved bytes."""
    with state.txn():
        identity = _identity(state)
        if identity is None or identity[1] != SEND_REFUSED:
            raise Refused(NOT_RESUMABLE)
        state.conn.execute("UPDATE gwr_identity SET halt=NULL WHERE id=1")
        _fault("halt_cleared")
        state.conn.execute("UPDATE gwr_pending SET refusals=0")
        _fault("budget_renewed")
        state.conn.execute("INSERT INTO gwr_audit(act, at_ms, quarantined, refusals_total) VALUES('resume',?,0,?)",
                           (now_ms, state.counter(READER_AGENT, "refused_answers")))
        state.bump(READER_AGENT, "resumed")
        _fault("audited")
