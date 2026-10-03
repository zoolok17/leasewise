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

SCHEMA_VERSION = 2       # 2: card 7c-2 added the limit_event table (version 1 files are migrated)

# The agent word on a limit item. Real agent names start with a letter or a digit, so this can
# never be one: a limit event belongs to no seat and no message.
LIMIT_AGENT = "_limit_events"
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

_DDL = _DDL_V1 + _LIMIT_DDL


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
        elif version == 1 and SCHEMA_VERSION >= 2:
            # Version 1 -> 2, inside the caller's transaction: all or nothing.
            self.conn.execute(_NOTE_DDL)            # a file made before card 7a-3
            for statement in _LIMIT_DDL:
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
        try:
            text = mapper.limit_body_text(body)
        except ValueError:
            raise StateError("a limit event body is not valid") from None
        if type(account_alias) is not str or _ALIAS.fullmatch(account_alias) is None:
            raise StateError("a limit event body is not valid")
        with self.txn():
            added = self.conn.execute(
                "INSERT OR IGNORE INTO limit_event(event_id, account_alias, body, captured_ms)"
                " VALUES(?,?,?,?)", (body["event_id"], account_alias, text, now_ms))
        return added.rowcount == 1

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
                        "WHERE id=?" % table, (row_id,))
            elif decision.action == decisions.RETRY:
                tries = self.one("SELECT tries FROM %s WHERE id=?" % table,
                                 (row_id,))[0] + 1
                self.conn.execute(
                    "UPDATE %s SET tries=?, not_before_ms=?, "
                    "last_error_code=? WHERE id=?" % table,
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
                        "last_error_code=? WHERE id=?" % table,
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
