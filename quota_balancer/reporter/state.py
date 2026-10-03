"""The reporter's durable state: one SQLite file with exclusive ownership.

Everything the reader sees is stored BEFORE a cursor moves (DESIGN-7A.md
4.2), so agenttalk may delete old journal segments. A final receipt is
stored once, never changed, and sent from the stored bytes (Part 12 D5).
"""

import contextlib
import json
import re
import sqlite3

from . import decisions, mapper

SCHEMA_VERSION = 3       # 2: card 7c-2 added limit_event; 3: card L6a-1 added the receipt custody tables
                         # (older files are migrated; a reporter that knows less refuses a newer file)

# The agent word on a limit item. Real agent names start with a letter or a digit, so this can
# never be one: a limit event belongs to no seat and no message.
LIMIT_AGENT = "_limit_events"
# The same for a gateway receipt (card L6a-1): it belongs to one lease and to no seat and no message.
RECEIPT_AGENT = "_gateway_receipts"
RELEASE_BATCH = 500             # captures stage 2 works on in one cycle
PERMANENT_REASONS = ("wrong_gateway", "duplicate_reference")
# Why a captured receipt is held back (never sent): no binding for its reference yet, a binding for another gateway
# than its source's own, or a second receipt for one reference and generation.
QUARANTINE_REASONS = ("unknown_reference", "wrong_gateway", "duplicate_reference")
_ALIAS = re.compile(r"[a-z][a-z0-9_-]{0,63}")


# Facts about the reporter itself, kept in the `note` table (see _NOTE_DDL).
NOTE_LAST_401 = "last_401"                  # history: the last 401, kept
NOTE_CREDENTIAL_ACTIVE = "credential_active"  # present only while unresolved


def last_ok_note(agent):
    return "last_ok:" + agent


class StateError(Exception):
    """A static, content-free state failure."""


class StateOwned(StateError):
    """Another reporter owns the state file."""


# Facts about the reporter itself, for `status`: the last credential refusal,
# a journal it refuses, the last good call of each agent. Never a secret.
_NOTE_DDL = ("CREATE TABLE IF NOT EXISTS note(name TEXT PRIMARY KEY, code TEXT,"
             " at_ms INTEGER NOT NULL) STRICT")

_DDL_V1 = (
    "CREATE TABLE meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL) STRICT",
    "CREATE TABLE cursor(stream TEXT NOT NULL, segment INTEGER NOT NULL,"
    " offset INTEGER NOT NULL, PRIMARY KEY(stream, segment)) STRICT",
    "CREATE TABLE stream(stream TEXT PRIMARY KEY, agent TEXT NOT NULL,"
    " previous_stream TEXT, started_ms INTEGER NOT NULL,"
    " in_record INTEGER NOT NULL, closed INTEGER NOT NULL DEFAULT 0,"
    " closed_ms INTEGER, last_seq INTEGER, closed_dropped INTEGER) STRICT",
    "CREATE TABLE segment(stream TEXT NOT NULL, segment INTEGER NOT NULL,"
    " after_seq INTEGER, header_ms INTEGER, torn INTEGER NOT NULL DEFAULT 0,"
    " torn_ms INTEGER, PRIMARY KEY(stream, segment)) STRICT",
    "CREATE TABLE event(stream TEXT NOT NULL, seq INTEGER NOT NULL,"
    " agent TEXT NOT NULL, kind TEXT NOT NULL, at_ms INTEGER NOT NULL,"
    " dropped_total INTEGER NOT NULL, PRIMARY KEY(stream, seq)) STRICT",
    "CREATE TABLE dispatch(agent TEXT NOT NULL, message_id TEXT NOT NULL,"
    " turn_id TEXT NOT NULL, stream TEXT NOT NULL, started INTEGER NOT NULL"
    " DEFAULT 0, ended INTEGER NOT NULL DEFAULT 0, start_ms INTEGER,"
    " end_ms INTEGER, message_at TEXT, has_message_at INTEGER NOT NULL"
    " DEFAULT 0, launched INTEGER, outcome TEXT, usage_json TEXT,"
    " PRIMARY KEY(agent, message_id, turn_id)) STRICT",
    "CREATE TABLE disposal(agent TEXT NOT NULL, message_id TEXT NOT NULL,"
    " stream TEXT NOT NULL, seq INTEGER NOT NULL, at_ms INTEGER NOT NULL,"
    " disposition TEXT NOT NULL, message_at TEXT, round_no INTEGER NOT NULL,"
    " PRIMARY KEY(agent, message_id, stream, seq)) STRICT",
    "CREATE TABLE acquisition(id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " agent TEXT NOT NULL, message_id TEXT NOT NULL, turn_uuid TEXT NOT NULL,"
    " request_id TEXT NOT NULL, body_original TEXT NOT NULL,"
    " body_current TEXT NOT NULL, rebuilds INTEGER NOT NULL DEFAULT 0,"
    " state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN"
    " ('pending','acquired','failed_permanent')), lease_id TEXT,"
    " tries INTEGER NOT NULL DEFAULT 0, not_before_ms INTEGER NOT NULL"
    " DEFAULT 0, last_error_code TEXT, UNIQUE(agent, message_id)) STRICT",
    "CREATE TABLE report(id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " event_id TEXT NOT NULL UNIQUE, agent TEXT NOT NULL,"
    " message_id TEXT NOT NULL, turn_id TEXT NOT NULL, body TEXT NOT NULL,"
    " state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN"
    " ('pending','sent','dropped','failed_permanent')),"
    " tries INTEGER NOT NULL DEFAULT 0, not_before_ms INTEGER NOT NULL"
    " DEFAULT 0, last_error_code TEXT) STRICT",
    "CREATE TABLE final(id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " agent TEXT NOT NULL, message_id TEXT NOT NULL, event_id TEXT NOT NULL,"
    " body TEXT NOT NULL, totals_json TEXT NOT NULL, complete INTEGER NOT"
    " NULL, reason TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending'"
    " CHECK(state IN ('pending','sent','failed_permanent')),"
    " tries INTEGER NOT NULL DEFAULT 0, not_before_ms INTEGER NOT NULL"
    " DEFAULT 0, last_error_code TEXT, UNIQUE(agent, message_id)) STRICT",
    # The bytes of a final never change once stored (D5).
    "CREATE TRIGGER final_immutable BEFORE UPDATE OF agent, message_id,"
    " event_id, body, totals_json, complete, reason ON final BEGIN"
    " SELECT RAISE(ABORT, 'final is immutable'); END",
    "CREATE TRIGGER final_keep BEFORE DELETE ON final BEGIN"
    " SELECT RAISE(ABORT, 'final is immutable'); END",
    "CREATE TRIGGER acquisition_original BEFORE UPDATE OF body_original,"
    " request_id, turn_uuid ON acquisition BEGIN"
    " SELECT RAISE(ABORT, 'the original body is immutable'); END",
    "CREATE TABLE late(agent TEXT NOT NULL, message_id TEXT NOT NULL,"
    " count INTEGER NOT NULL, PRIMARY KEY(agent, message_id)) STRICT",
    "CREATE TABLE message_status(agent TEXT NOT NULL, message_id TEXT NOT"
    " NULL, complete INTEGER NOT NULL, reason TEXT NOT NULL, reasons TEXT"
    " NOT NULL, disposition TEXT, PRIMARY KEY(agent, message_id)) STRICT",
    "CREATE TABLE agent_state(agent TEXT PRIMARY KEY, failed_code TEXT)"
    " STRICT",
    "CREATE TABLE counter(agent TEXT NOT NULL, name TEXT NOT NULL,"
    " value INTEGER NOT NULL, PRIMARY KEY(agent, name)) STRICT",
    _NOTE_DDL,
)

# Version 2 (card 7c-2): the limit-event outbox. One row per limit fact, keyed by `event_id`; the
# body is the frozen canonical JSON text and is never recomputed. Identity and body cannot change
# and a row cannot be deleted; only the delivery columns move.
_LIMIT_DDL = (
    "CREATE TABLE limit_event(id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " event_id TEXT NOT NULL UNIQUE, account_alias TEXT NOT NULL,"
    " body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN"
    " ('pending','sent','failed_permanent')), tries INTEGER NOT NULL DEFAULT 0,"
    " not_before_ms INTEGER NOT NULL DEFAULT 0, last_error_code TEXT,"
    " captured_ms INTEGER NOT NULL) STRICT",
    "CREATE TRIGGER limit_event_immutable BEFORE UPDATE OF event_id, account_alias, body,"
    " captured_ms ON limit_event BEGIN"
    " SELECT RAISE(ABORT, 'a limit event is immutable'); END",
    "CREATE TRIGGER limit_event_keep BEFORE DELETE ON limit_event BEGIN"
    " SELECT RAISE(ABORT, 'a limit event is immutable'); END",
)

# Version 3 (card L6a-1, DESIGN-6A 8.3): the receipt custody chain, in two durable stages.
#   receipt_source   who the receipts come from: a configured name AND the gateway it was bound to. A source mapped to
#                    another gateway is another source (another identity, its own cursors); nothing is rebound.
#   receipt_cursor   one per (source, generation): the last receipt number taken. `page_ord` says which generation
#                    was read most recently, so a generation seen again (A, B, A) takes its kept cursor back;
#                    `first_seen_ms` says when it first appeared.
#   receipt_binding  copied from the admission store's granted rows: reference hash -> lease, gateway.
#   receipt_capture  stage 1: the evidence exactly as received, even when no binding is known. The gateway of its
#                    source is frozen on the row.
#   receipt_delivery stage 2: the frozen canonical settle body, created in one transaction only when a binding for
#                    the frozen gateway matches.
#   receipt_state    the ONLY mutable table: captured, quarantined (with a closed reason), pending, sent, failed.
# Source, binding, capture and delivery rows cannot be changed or deleted; the state row cannot be deleted.
_RECEIPT_DDL = (
    "CREATE TABLE receipt_source(id INTEGER PRIMARY KEY AUTOINCREMENT, alias TEXT NOT NULL,"
    " gateway_id TEXT NOT NULL, created_ms INTEGER NOT NULL, UNIQUE(alias, gateway_id)) STRICT",
    "CREATE TABLE receipt_cursor(source_id INTEGER NOT NULL REFERENCES receipt_source(id),"
    " generation TEXT NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0, page_ord INTEGER NOT NULL,"
    " first_seen_ms INTEGER NOT NULL,"
    " PRIMARY KEY(source_id, generation)) STRICT",
    "CREATE TABLE receipt_binding(ref_sha256 TEXT PRIMARY KEY, lease_id TEXT NOT NULL, gateway_id TEXT NOT NULL,"
    " agent TEXT NOT NULL, message_id TEXT NOT NULL, expires_ms INTEGER NOT NULL, copied_ms INTEGER NOT NULL) STRICT",
    "CREATE TABLE receipt_capture(id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " source_id INTEGER NOT NULL REFERENCES receipt_source(id), gateway_id TEXT NOT NULL,"
    " generation TEXT NOT NULL, seq INTEGER NOT NULL, ref_sha256 TEXT NOT NULL, outcome TEXT NOT NULL,"
    " calls INTEGER NOT NULL, input_tokens INTEGER, output_tokens INTEGER, actual_micro_eur INTEGER NOT NULL,"
    " closed_at TEXT NOT NULL, captured_ms INTEGER NOT NULL, UNIQUE(source_id, generation, seq)) STRICT",
    "CREATE INDEX receipt_capture_ref ON receipt_capture(ref_sha256)",
    "CREATE TABLE receipt_delivery(id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " capture_id INTEGER NOT NULL UNIQUE REFERENCES receipt_capture(id), event_id TEXT NOT NULL UNIQUE,"
    " lease_id TEXT NOT NULL, gateway_id TEXT NOT NULL, body TEXT NOT NULL, created_ms INTEGER NOT NULL) STRICT",
    "CREATE TABLE receipt_state(capture_id INTEGER PRIMARY KEY REFERENCES receipt_capture(id),"
    " state TEXT NOT NULL DEFAULT 'captured' CHECK(state IN"
    " ('captured','quarantined','pending','sent','failed_permanent')), reason TEXT,"
    " tries INTEGER NOT NULL DEFAULT 0, not_before_ms INTEGER NOT NULL DEFAULT 0, last_error_code TEXT) STRICT",
) + tuple(
    statement
    for table, what in (("receipt_source", "a receipt source"), ("receipt_binding", "a receipt binding"),
                        ("receipt_capture", "a receipt capture"), ("receipt_delivery", "a receipt delivery"))
    for statement in (
        "CREATE TRIGGER %s_immutable BEFORE UPDATE ON %s BEGIN SELECT RAISE(ABORT, '%s is immutable'); END"
        % (table, table, what),
        "CREATE TRIGGER %s_keep BEFORE DELETE ON %s BEGIN SELECT RAISE(ABORT, '%s is immutable'); END"
        % (table, table, what))
) + (
    "CREATE TRIGGER receipt_state_keep BEFORE DELETE ON receipt_state BEGIN"
    " SELECT RAISE(ABORT, 'a receipt state is never deleted'); END",
)

_DDL = _DDL_V1 + _LIMIT_DDL + _RECEIPT_DDL


# The column that names a row in each outbox table (every table but the receipt state is keyed by `id`).
_KEY_COLUMN = {"receipt_state": "capture_id"}


class _Evidence:
    """The fields of a captured receipt that make a settle body (the shape receipts.Receipt has)."""

    __slots__ = ("seq", "ref_sha256", "outcome", "calls", "input_tokens", "output_tokens", "actual_micro_eur")

    def __init__(self, seq, ref_sha256, outcome, calls, input_tokens, output_tokens, actual_micro_eur):
        self.seq, self.ref_sha256, self.outcome, self.calls = seq, ref_sha256, outcome, calls
        self.input_tokens, self.output_tokens, self.actual_micro_eur = input_tokens, output_tokens, actual_micro_eur


class State:
    def __init__(self, path):
        self.path = path
        try:
            conn = sqlite3.connect(path, timeout=0, isolation_level=None)
        except sqlite3.Error:
            raise StateError("the state file cannot be opened") from None
        self.conn = conn
        try:
            conn.execute("PRAGMA locking_mode=EXCLUSIVE")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")        # takes the file lock
            self._migrate()
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            self._abort()
            if "locked" in str(exc) or "busy" in str(exc):
                raise StateOwned("state is owned by another reporter") from None
            raise StateError("the state file cannot be used") from None
        except StateError:
            self._abort()
            raise
        except sqlite3.Error:
            self._abort()
            raise StateError("the state file cannot be used") from None

    def _abort(self):
        with contextlib.suppress(sqlite3.Error):
            self.conn.execute("ROLLBACK")
        with contextlib.suppress(sqlite3.Error):
            self.conn.close()

    def _migrate(self):
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            for statement in _DDL:
                self.conn.execute(statement)
            self.conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
        elif version in (1, 2) and SCHEMA_VERSION >= 3:
            # Version 1 -> 3 or 2 -> 3, inside the caller's transaction: all or nothing. (An earlier version 3
            # with another layout never shipped, so there is nothing to carry over.)
            if version == 1:
                self.conn.execute(_NOTE_DDL)        # a file made before card 7a-3
                for statement in _LIMIT_DDL:
                    self.conn.execute(statement)
            for statement in _RECEIPT_DDL:
                self.conn.execute(statement)
            self.conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
        elif version != SCHEMA_VERSION:
            raise StateError("the state file version is not supported")
        else:
            self.conn.execute(_NOTE_DDL)    # a file made before card 7a-3

    def close(self):
        with contextlib.suppress(sqlite3.Error):
            self.conn.close()

    @contextlib.contextmanager
    def txn(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # --- small accessors -------------------------------------------------

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def round_number(self):
        row = self.one("SELECT value FROM meta WHERE key='round'")
        return row[0] if row else 0

    def cursor(self, stream, segment):
        row = self.one("SELECT offset FROM cursor WHERE stream=? AND "
                       "segment=?", (stream, segment))
        return row[0] if row else 0

    def bump(self, agent, name, amount=1):
        self.conn.execute(
            "INSERT INTO counter(agent, name, value) VALUES(?,?,?) "
            "ON CONFLICT(agent, name) DO UPDATE SET value=value+?",
            (agent, name, amount, amount))

    def counter(self, agent, name):
        row = self.one("SELECT value FROM counter WHERE agent=? AND name=?",
                       (agent, name))
        return row[0] if row else 0

    def set_note(self, name, code, at_ms):
        self.conn.execute(
            "INSERT INTO note(name, code, at_ms) VALUES(?,?,?) ON CONFLICT"
            "(name) DO UPDATE SET code=excluded.code, at_ms=excluded.at_ms",
            (name, code, at_ms))

    def clear_note(self, name):
        self.conn.execute("DELETE FROM note WHERE name=?", (name,))

    def note(self, name):
        """(code, at_ms) or None."""
        return self.one("SELECT code, at_ms FROM note WHERE name=?", (name,))

    def clear_agent_stops(self):
        """A start-up forgives every configuration stop, so an operator who
        fixed the cause only has to restart. Items that already failed for
        good stay failed; a still-broken agent stops again on its next answer."""
        return self.conn.execute("DELETE FROM agent_state").rowcount

    # --- the limit-event outbox (card 7c-2) -----------------------------------

    def add_limit_event(self, account_alias, body, now_ms):
        """Store one limit event, frozen, in one transaction. True if it is new, False if a row
        with that `event_id` already exists (the first bytes stay; nothing is changed). The body
        must be exactly the allow-listed shape: anything else raises StateError with a fixed
        sentence that carries no value."""
        with self.txn():
            return self._put_limit_event(account_alias, body, now_ms)

    def _put_limit_event(self, account_alias, body, now_ms):
        """The insert itself, inside the caller's transaction."""
        try:
            text = mapper.limit_body_text(body)
        except ValueError:
            raise StateError("a limit event body is not valid") from None
        if type(account_alias) is not str or _ALIAS.fullmatch(account_alias) is None:
            raise StateError("a limit event body is not valid")
        added = self.conn.execute(
            "INSERT OR IGNORE INTO limit_event(event_id, account_alias, body, captured_ms)"
            " VALUES(?,?,?,?)", (body["event_id"], account_alias, text, now_ms))
        return added.rowcount == 1

    def record_capture(self, items, counts, now_ms):
        """One scan's result, in ONE transaction: each new event (a row that already exists is left
        exactly as it is), the cumulative `captured` count, and the counts of the LAST scan (absolute,
        so a stale file seen every cycle does not pile up). -> the number of new events."""
        added = 0
        with self.txn():
            for account_alias, body in items:
                if self._put_limit_event(account_alias, body, now_ms):
                    added += 1
            if added:
                self.bump(LIMIT_AGENT, "captured", added)
            for name, value in counts.items():
                self.conn.execute(
                    "INSERT INTO counter(agent, name, value) VALUES(?,?,?) ON CONFLICT(agent, name) "
                    "DO UPDATE SET value=excluded.value", (LIMIT_AGENT, "scan:" + name, value))
            self.set_note("limit_last_scan", None, now_ms)
        return added

    def note_capture_fault(self, now_ms):
        """A capture pass failed as a whole: count it (no text) and say when."""
        with self.txn():
            self.bump(LIMIT_AGENT, "capture_fault")
            self.set_note("limit_last_fault", None, now_ms)

    def next_limit_item(self, now_ms):
        """The oldest limit row that is pending and due, as an Item, or None. Limit events belong
        to no agent and no message: nothing about a lease or a stopped agent holds one back."""
        row = self.one(
            "SELECT id, event_id, body FROM limit_event WHERE state='pending' AND not_before_ms<=?"
            " ORDER BY id LIMIT 1", (now_ms,))
        if row is None:
            return None
        return Item(decisions.LIMIT, LIMIT_AGENT, "", "/v1/limit-events", json.loads(row[2]),
                    row[1], ("limit_event", row[0]), None)

    # --- the receipt custody chain (card L6a-1, DESIGN-6A 8.3) ---------------------------------------

    def ensure_source(self, alias, gateway_id, now_ms):
        """The id of the source (alias, gateway). A source is bound to ONE gateway for good: the same name mapped
        to another gateway is another source."""
        with self.txn():
            self.conn.execute("INSERT OR IGNORE INTO receipt_source(alias, gateway_id, created_ms) VALUES(?,?,?)",
                              (alias, gateway_id, now_ms))
            return self.one("SELECT id FROM receipt_source WHERE alias=? AND gateway_id=?", (alias, gateway_id))[0]

    def current_cursor(self, source_id):
        """(generation, last_seq) of the generation read most recently for this source, or (None, 0)."""
        row = self.one("SELECT generation, last_seq FROM receipt_cursor WHERE source_id=?"
                       " ORDER BY page_ord DESC LIMIT 1", (source_id,))
        return (None, 0) if row is None else (row[0], row[1])

    def cursor_for(self, source_id, generation):
        """The kept cursor of one generation, or None if that generation was never seen."""
        row = self.one("SELECT last_seq FROM receipt_cursor WHERE source_id=? AND generation=?",
                       (source_id, generation))
        return None if row is None else row[0]

    def _next_page_ord(self):
        return self.one("SELECT COALESCE(MAX(page_ord), 0) + 1 FROM receipt_cursor")[0]

    def switch_generation(self, source_id, generation, now_ms):
        """Make `generation` the current one for this source: a generation never seen starts its own cursor at 0,
        and one seen before takes its KEPT cursor back (A, B, A). Old cursors stay. -> its last_seq."""
        with self.txn():
            order = self._next_page_ord()
            self.conn.execute("INSERT OR IGNORE INTO receipt_cursor(source_id, generation, last_seq, page_ord, first_seen_ms)"
                              " VALUES(?,?,0,?,?)", (source_id, generation, order, now_ms))
            self.conn.execute("UPDATE receipt_cursor SET page_ord=? WHERE source_id=? AND generation=?",
                              (order, source_id, generation))
            self.bump(RECEIPT_AGENT, "generation_change")
            return self.cursor_for(source_id, generation)

    def capture_page(self, source_id, gateway_id, page, now_ms):
        """Stage 1, ONE transaction: every receipt of an accepted page as immutable evidence (a row that already
        exists is left exactly as it is), its state row, and the cursor moved to the page's last number. This
        happens even when no binding is known. A page that was not asked for at the cursor stores nothing
        (StateError). -> the number of new captures."""
        added = 0
        with self.txn():
            if self.cursor_for(source_id, page.generation) is None:
                self.conn.execute("INSERT INTO receipt_cursor(source_id, generation, last_seq, page_ord, first_seen_ms)"
                                  " VALUES(?,?,0,?,?)", (source_id, page.generation, self._next_page_ord(), now_ms))
            if self.cursor_for(source_id, page.generation) != page.after_seq:
                raise StateError("a receipt page does not start at the stored cursor")
            for receipt in page.receipts:
                row = self.conn.execute(
                    "INSERT OR IGNORE INTO receipt_capture(source_id, gateway_id, generation, seq, ref_sha256,"
                    " outcome, calls, input_tokens, output_tokens, actual_micro_eur, closed_at, captured_ms)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (source_id, gateway_id, page.generation, receipt.seq, receipt.ref_sha256, receipt.outcome,
                     receipt.calls, receipt.input_tokens, receipt.output_tokens, receipt.actual_micro_eur,
                     receipt.closed_at, now_ms))
                if row.rowcount == 1:
                    added += 1
                    self.conn.execute("INSERT INTO receipt_state(capture_id) VALUES(?)", (row.lastrowid,))
            if page.receipts:
                self.conn.execute("UPDATE receipt_cursor SET last_seq=? WHERE source_id=? AND generation=?",
                                  (page.next_seq, source_id, page.generation))
            if added:
                self.bump(RECEIPT_AGENT, "stored", added)
            self.set_note("receipt_last_read", "ok", now_ms)
        return added

    def note_receipt_read(self, status, reason, now_ms):
        """A read that stored nothing (`read_later` or `refused`): count it by its closed reason and say when. Never
        any text from the source."""
        with self.txn():
            self.bump(RECEIPT_AGENT, "%s:%s" % (status, reason))
            self.set_note("receipt_last_read", status, now_ms)

    def copy_bindings(self, bindings, now_ms):
        """Copy the admission store's granted rows into this state (immutable). A reference already copied is left
        exactly as it is; one that now names another lease or gateway is counted and never replaces it. -> new rows."""
        added = 0
        with self.txn():
            for one in bindings:
                row = self.conn.execute(
                    "INSERT OR IGNORE INTO receipt_binding(ref_sha256, lease_id, gateway_id, agent, message_id,"
                    " expires_ms, copied_ms) VALUES(?,?,?,?,?,?,?)",
                    (one.ref_sha256, one.lease_id, one.gateway_id, one.agent, one.message_id, one.expires_ms,
                     now_ms))
                if row.rowcount == 1:
                    added += 1
                elif self.one("SELECT lease_id, gateway_id FROM receipt_binding WHERE ref_sha256=?",
                              (one.ref_sha256,)) != (one.lease_id, one.gateway_id):
                    self.bump(RECEIPT_AGENT, "binding_changed")
        return added

    def _insert_delivery(self, capture_id, body, text, now_ms):
        """The delivery row of stage 2. -> True if it is new, False if its event id already has a delivery."""
        return self.conn.execute(
            "INSERT OR IGNORE INTO receipt_delivery(capture_id, event_id, lease_id, gateway_id, body, created_ms)"
            " VALUES(?,?,?,?,?,?)", (capture_id, body["event_id"], body["_lease_id"], body["gateway_id"], text,
                                     now_ms)).rowcount == 1

    def _mark_pending(self, capture_id):
        self.conn.execute("UPDATE receipt_state SET state='pending', reason=NULL, last_error_code=NULL"
                          " WHERE capture_id=?", (capture_id,))

    def _release_candidates(self):
        """The captures stage 2 works on in this cycle (card L6a-1 fix round 2, D1): the ones whose reference NOW HAS
        a binding, wherever they sit in the list, oldest first, at most RELEASE_BATCH, found by a direct join on the
        reference hash. Each of them leaves the selection when it is handled (it is released, or it becomes
        `wrong_gateway` or `duplicate_reference`, which are permanent), so the work is always on rows that can change
        and nothing waits behind rows that cannot. New captures without a binding are labelled `unknown_reference`,
        all at once, by one statement; they are not looked at again until a binding for them exists."""
        with self.txn():
            self.conn.execute(
                "UPDATE receipt_state SET state='quarantined', reason='unknown_reference' WHERE state='captured'"
                " AND NOT EXISTS (SELECT 1 FROM receipt_capture c JOIN receipt_binding b ON b.ref_sha256=c.ref_sha256"
                " WHERE c.id=receipt_state.capture_id)")
        return [row[0] for row in self.all(
            "SELECT s.capture_id FROM receipt_binding b JOIN receipt_capture c ON c.ref_sha256=b.ref_sha256"
            " JOIN receipt_state s ON s.capture_id=c.id WHERE s.state='captured'"
            " OR (s.state='quarantined' AND s.reason='unknown_reference') ORDER BY s.capture_id LIMIT ?",
            (RELEASE_BATCH,))]

    def release_waiting(self, now_ms):
        """Stage 2: look at the captures that are not yet released (`_release_candidates` says which). Each one is
        its own `BEGIN IMMEDIATE`. A binding that matches the capture's FROZEN gateway creates the immutable
        delivery body and sets `pending` together or not at all; no binding is `unknown_reference`; a binding for
        another gateway is `wrong_gateway`; a second capture of one reference and generation is
        `duplicate_reference`. None of these ever creates a sendable body. -> the number released."""
        ids = self._release_candidates()
        released = 0
        for capture_id in ids:
            with self.txn():
                row = self.one(
                    "SELECT s.state, c.gateway_id, c.generation, c.seq, c.ref_sha256, c.outcome, c.calls,"
                    " c.input_tokens, c.output_tokens, c.actual_micro_eur, s.reason FROM receipt_capture c"
                    " JOIN receipt_state s ON s.capture_id=c.id WHERE c.id=?", (capture_id,))
                if row[0] not in ("captured", "quarantined") or row[10] in PERMANENT_REASONS:
                    continue
                (_state, frozen, generation, seq, ref, outcome, calls, tokens_in, tokens_out, money, _why) = row
                binding = self.one("SELECT lease_id, gateway_id FROM receipt_binding WHERE ref_sha256=?", (ref,))
                reason = None
                if binding is None:
                    reason = "unknown_reference"
                elif binding[1] != frozen:
                    reason = "wrong_gateway"
                else:
                    capture = _Evidence(seq, ref, outcome, calls, tokens_in, tokens_out, money)
                    body = mapper.receipt_body(frozen, generation, capture)
                    try:
                        text = mapper.receipt_body_text(body)
                    except ValueError:
                        raise StateError("a receipt body is not valid") from None
                    body["_lease_id"] = binding[0]
                    if self._insert_delivery(capture_id, body, text, now_ms):
                        self._mark_pending(capture_id)
                        released += 1
                    else:
                        reason = "duplicate_reference"
                if reason is not None:
                    self.conn.execute("UPDATE receipt_state SET state='quarantined', reason=? WHERE capture_id=?",
                                      (reason, capture_id))
        return released

    def next_receipt_item(self, now_ms):
        """The oldest pending delivery that is due, as an Item, or None. A receipt belongs to one lease and to no
        seat and no message: nothing about an agent or another lease holds it back. The body sent is the stored
        delivery body, unchanged."""
        row = self.one(
            "SELECT d.event_id, d.body, d.lease_id, d.capture_id FROM receipt_delivery d"
            " JOIN receipt_state s ON s.capture_id=d.capture_id WHERE s.state='pending' AND s.not_before_ms<=?"
            " ORDER BY d.id LIMIT 1", (now_ms,))
        if row is None:
            return None
        return Item(decisions.RECEIPT, RECEIPT_AGENT, "", "/v1/leases/%s/settle" % row[2], json.loads(row[1]),
                    row[0], ("receipt_state", row[3]), row[2])

    # --- the outbox -------------------------------------------------------

    def next_item(self, now_ms):
        """The ONE next request to send, or None. Order: oldest message
        first; for a message the acquisition, then its non-final reports,
        then its final. A message whose acquisition is not acknowledged
        sends nothing else. Agents stopped by a configuration fault send
        nothing."""
        rows = self.all(
            "SELECT a.id, a.agent, a.message_id, a.state, a.body_current,"
            " a.request_id, a.lease_id, a.not_before_ms FROM acquisition a"
            " LEFT JOIN agent_state s ON s.agent=a.agent"
            " WHERE s.failed_code IS NULL AND a.state<>'failed_permanent'"
            " ORDER BY a.id")
        for (aid, agent, message_id, state, body, request_id, lease_id,
             not_before) in rows:
            if state == "pending":
                if not_before <= now_ms:
                    return Item(decisions.ACQUIRE, agent, message_id,
                                "/v1/leases", json.loads(body), request_id,
                                ("acquisition", aid), None)
                continue
            for rid, event_id, rbody, rnot in self.all(
                    "SELECT id, event_id, body, not_before_ms FROM report "
                    "WHERE agent=? AND message_id=? AND state='pending' "
                    "ORDER BY id", (agent, message_id)):
                if rnot <= now_ms:
                    return Item(decisions.REPORT, agent, message_id,
                                "/v1/leases/%s/settle" % lease_id,
                                json.loads(rbody), event_id,
                                ("report", rid), lease_id)
                break
            else:
                row = self.one(
                    "SELECT id, event_id, body, not_before_ms FROM final "
                    "WHERE agent=? AND message_id=? AND state='pending'",
                    (agent, message_id))
                if row is not None and row[3] <= now_ms:
                    return Item(decisions.FINAL, agent, message_id,
                                "/v1/leases/%s/settle" % lease_id,
                                json.loads(row[2]), row[1],
                                ("final", row[0]), lease_id)
        return None

    def apply(self, item, response, now_ms, jitter=0.0):
        """Carry out `decisions.decide` for one answer. Returns the
        decision. Everything is one transaction."""
        table, row_id = item.ref
        key = _KEY_COLUMN.get(table, "id")
        with self.txn():
            rebuilds = 0
            if table == "acquisition":
                rebuilds = self.one("SELECT rebuilds FROM acquisition WHERE "
                                    "id=?", (row_id,))[0]
            decision = decisions.decide(item.kind, response, rebuilds)
            code = decision.code
            # The credential history moves in THIS transaction, with the answer
            # that causes it: an accepted request ends a credential fault, a 401
            # starts one. A crash can never leave the two out of step.
            if decision.action == decisions.DONE:
                self.set_note(last_ok_note(item.agent), None, now_ms)
                self.clear_note(NOTE_CREDENTIAL_ACTIVE)
            elif response.status == 401:
                self.set_note(NOTE_LAST_401, code, now_ms)
                self.set_note(NOTE_CREDENTIAL_ACTIVE, code, now_ms)
            if decision.action == decisions.DONE:
                if table == "acquisition":
                    self.conn.execute(
                        "UPDATE acquisition SET state='acquired', lease_id=?,"
                        " last_error_code=NULL WHERE id=?",
                        (response.lease_id, row_id))
                else:
                    self.conn.execute(
                        "UPDATE %s SET state='sent', last_error_code=NULL "
                        "WHERE %s=?" % (table, key), (row_id,))
            elif decision.action == decisions.RETRY:
                tries = self.one("SELECT tries FROM %s WHERE %s=?" % (table, key),
                                 (row_id,))[0] + 1
                self.conn.execute(
                    "UPDATE %s SET tries=?, not_before_ms=?, "
                    "last_error_code=? WHERE %s=?" % (table, key),
                    (tries, now_ms + decisions.backoff_ms(tries, jitter),
                     code, row_id))
            elif decision.action == decisions.REBUILD:
                original = json.loads(self.one(
                    "SELECT body_original FROM acquisition WHERE id=?",
                    (row_id,))[0])
                fresh = mapper.rebuild_body(original, now_ms)
                self.conn.execute(
                    "UPDATE acquisition SET body_current=?, rebuilds=?,"
                    " last_error_code=?, not_before_ms=0 WHERE id=?",
                    (json.dumps(fresh, sort_keys=True), rebuilds + 1, code,
                     row_id))
            elif decision.action == decisions.DROP:
                self.conn.execute(
                    "UPDATE report SET state='dropped', last_error_code=? "
                    "WHERE id=?", (code, row_id))
                self.bump(item.agent, "reports_dropped")
            else:  # FAIL, FAIL_AGENT
                if decision.action == decisions.FAIL_AGENT:
                    # The agent stops: its queued reports (observations only)
                    # are dropped and counted here, in this transaction.
                    # Its OTHER finals are never dropped; they wait, unsent.
                    # The item that got the fault fails for good below,
                    # even when it is a final (DESIGN-7A 4.5).
                    dropped = self.conn.execute(
                        "UPDATE report SET state='dropped', "
                        "last_error_code=? WHERE agent=? AND "
                        "state='pending'", (code, item.agent)).rowcount
                    if dropped:
                        self.bump(item.agent, "reports_dropped", dropped)
                    self.conn.execute(
                        "INSERT INTO agent_state(agent, failed_code) "
                        "VALUES(?,?) ON CONFLICT(agent) DO UPDATE SET "
                        "failed_code=excluded.failed_code",
                        (item.agent, code))
                if table != "report" or decision.action == decisions.FAIL:
                    self.conn.execute(
                        "UPDATE %s SET state='failed_permanent', "
                        "last_error_code=? WHERE %s=?" % (table, key),
                        (code or decisions.UNKNOWN_CODE, row_id))
        return decision


class Item:
    """One request the reporter wants sent."""

    __slots__ = ("kind", "agent", "message_id", "path", "body", "key", "ref",
                 "lease_id")

    def __init__(self, kind, agent, message_id, path, body, key, ref,
                 lease_id):
        self.kind = kind
        self.agent = agent
        self.message_id = message_id
        self.path = path
        self.body = body
        self.key = key              # the Idempotency-Key to send
        self.ref = ref
        self.lease_id = lease_id
