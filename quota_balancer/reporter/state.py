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

SCHEMA_VERSION = 3       # 2: card 7c-2 added limit_event; 3: card L6a-1 added gateway_receipt and lease_ref
                         # (older files are migrated; a reporter that knows less refuses a newer file)

# The agent word on a limit item. Real agent names start with a letter or a digit, so this can
# never be one: a limit event belongs to no seat and no message.
LIMIT_AGENT = "_limit_events"
# The same for a gateway receipt (card L6a-1): it belongs to one lease and to no seat and no message.
RECEIPT_AGENT = "_gateway_receipts"
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

# Version 3 (card L6a-1): the gateway-receipt outbox and the lease references. One receipt row per
# (gateway, ledger generation, seq), keyed by `event_id`; the body is the frozen canonical settle
# text and is never recomputed. A receipt whose reference matches no lease of ours is stored as
# failed for good (so its `seq` is still passed) and has no lease id. `lease_ref` maps the sha256
# of a lease id (what the gateway keeps) back to the lease id.
_RECEIPT_DDL = (
    "CREATE TABLE gateway_receipt(id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " event_id TEXT NOT NULL UNIQUE, gateway_alias TEXT NOT NULL, generation TEXT NOT NULL,"
    " seq INTEGER NOT NULL, ref_sha256 TEXT NOT NULL, lease_id TEXT, body TEXT NOT NULL,"
    " state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','sent','failed_permanent')),"
    " tries INTEGER NOT NULL DEFAULT 0, not_before_ms INTEGER NOT NULL DEFAULT 0,"
    " last_error_code TEXT, stored_ms INTEGER NOT NULL,"
    " CHECK(lease_id IS NOT NULL OR state='failed_permanent'),"
    " UNIQUE(gateway_alias, generation, seq)) STRICT",
    "CREATE TRIGGER gateway_receipt_immutable BEFORE UPDATE OF event_id, gateway_alias, generation,"
    " seq, ref_sha256, lease_id, body, stored_ms ON gateway_receipt BEGIN"
    " SELECT RAISE(ABORT, 'a gateway receipt is immutable'); END",
    "CREATE TRIGGER gateway_receipt_keep BEFORE DELETE ON gateway_receipt BEGIN"
    " SELECT RAISE(ABORT, 'a gateway receipt is immutable'); END",
    "CREATE TABLE lease_ref(ref_sha256 TEXT PRIMARY KEY, lease_id TEXT NOT NULL UNIQUE) STRICT",
)

_DDL = _DDL_V1 + _LIMIT_DDL + _RECEIPT_DDL


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
            # Version 1 -> 3 or 2 -> 3, inside the caller's transaction: all or nothing.
            if version == 1:
                self.conn.execute(_NOTE_DDL)        # a file made before card 7a-3
                for statement in _LIMIT_DDL:
                    self.conn.execute(statement)
            for statement in _RECEIPT_DDL:
                self.conn.execute(statement)
            # Every lease already acquired can be named by a receipt, so its reference is kept.
            for (lease_id,) in self.conn.execute(
                    "SELECT lease_id FROM acquisition WHERE lease_id IS NOT NULL").fetchall():
                self.remember_lease(lease_id)
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

    # --- the gateway-receipt outbox (card L6a-1) ------------------------------

    def remember_lease(self, lease_id):
        """Keep the reference of one lease (the sha256 of its id) so that a gateway receipt naming
        that reference can be sent to the lease. Inside the caller's transaction."""
        self.conn.execute("INSERT OR IGNORE INTO lease_ref(ref_sha256, lease_id) VALUES(?,?)",
                          (mapper.lease_ref_hash(lease_id), lease_id))

    def receipt_cursor(self, gateway_alias):
        """The last receipt `seq` stored for this gateway (0 if none)."""
        row = self.one("SELECT value FROM meta WHERE key=?", ("receipt_seq:" + gateway_alias,))
        return row[0] if row else 0

    def record_receipts(self, gateway_alias, gateway_id, receipts, now_ms):
        """One read's result, in ONE transaction: each new receipt as a frozen outbox row (a row that
        already exists is left exactly as it is), and the last `seq` moved to the last receipt of the
        read. A failure anywhere stores nothing and moves nothing. -> the number of new rows."""
        added = 0
        with self.txn():
            last = self.receipt_cursor(gateway_alias)
            for receipt in receipts:
                try:
                    body = mapper.receipt_body(gateway_id, receipt)
                    text = mapper.receipt_body_text(body)
                except ValueError:
                    raise StateError("a receipt body is not valid") from None
                found = self.one("SELECT lease_id FROM lease_ref WHERE ref_sha256=?", (receipt.ref_sha256,))
                lease_id = None if found is None else found[0]
                state = "pending" if lease_id is not None else "failed_permanent"
                code = None if lease_id is not None else decisions.UNKNOWN_LEASE
                row = self.conn.execute(
                    "INSERT OR IGNORE INTO gateway_receipt(event_id, gateway_alias, generation, seq,"
                    " ref_sha256, lease_id, body, state, last_error_code, stored_ms)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (body["event_id"], gateway_alias, receipt.generation, receipt.seq,
                     receipt.ref_sha256, lease_id, text, state, code, now_ms))
                added += row.rowcount
                last = max(last, receipt.seq)
            if receipts:
                self.conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET"
                    " value=excluded.value", ("receipt_seq:" + gateway_alias, last))
            if added:
                self.bump(RECEIPT_AGENT, "stored", added)
            self.set_note("receipt_last_read", "ok", now_ms)
        return added

    def note_receipt_read(self, status, reason, now_ms):
        """A read that stored nothing (`read_later` or `refused`): count it by its closed reason and
        say when. Never any text from the source."""
        with self.txn():
            self.bump(RECEIPT_AGENT, "%s:%s" % (status, reason))
            self.set_note("receipt_last_read", status, now_ms)

    def next_receipt_item(self, now_ms):
        """The oldest receipt row that is pending and due, as an Item, or None. A receipt belongs to
        one lease and to no seat and no message: nothing about an agent or another lease holds it back."""
        row = self.one(
            "SELECT id, event_id, body, lease_id FROM gateway_receipt WHERE state='pending'"
            " AND not_before_ms<=? ORDER BY id LIMIT 1", (now_ms,))
        if row is None:
            return None
        return Item(decisions.RECEIPT, RECEIPT_AGENT, "", "/v1/leases/%s/settle" % row[3],
                    json.loads(row[2]), row[1], ("gateway_receipt", row[0]), row[3])

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
                    self.remember_lease(response.lease_id)
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
