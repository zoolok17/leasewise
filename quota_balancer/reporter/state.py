"""The reporter's durable state: one SQLite file with exclusive ownership.

Everything the reader sees is stored BEFORE a cursor moves (DESIGN-7A.md
4.2), so agenttalk may delete old journal segments. A final receipt is
stored once, never changed, and sent from the stored bytes (Part 12 D5).
"""

import contextlib
import json
import sqlite3

from . import decisions, mapper

SCHEMA_VERSION = 1


class StateError(Exception):
    """A static, content-free state failure."""


class StateOwned(StateError):
    """Another reporter owns the state file."""


_DDL = (
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
)


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
        elif version != SCHEMA_VERSION:
            raise StateError("the state file version is not supported")

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
                        (code or "UNKNOWN", row_id))
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
