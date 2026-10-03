"""The admission store: its table layout, and the reporter's read-only reader (card L6a-1, DESIGN-6A 8.6).

The admission store is a separate SQLite file. `reporter admit` (card L6a-2) writes it; the running reporter only
READS it, to learn which lease a gateway receipt belongs to (the binding). This module holds the one definition of
its layout, so that the writer of card L6a-2 and the tests build the same tables, and the reader that this card
needs: it opens the file read-only and answers "read again later" for a missing, busy or unusable file, never an
error that stops the reporter.

Only the `admission` table is read here. `admission_attempt` (every request body sent, saved before it is sent) is
part of the layout so that the writer has it, and is not touched by the reader.
"""

import contextlib
import hashlib
import os
import re
import sqlite3
import urllib.parse

SCHEMA_VERSION = 1

# What the reader may answer, with a closed reason when it is not "ok".
OK = "ok"
LATER = "later"
STORE_MISSING = "store_missing"
STORE_BUSY = "store_busy"
STORE_UNREADABLE = "store_unreadable"
STORE_HISTORY_LIMIT = "history_limit"      # more granted rows than one read may take (MAX_BINDINGS)

# The layout (DESIGN-6A 8.6). One row per admitted message: the request (saved before anything is sent), its state
# (`requested` until the grant is saved, then `granted`), the grant, and when the wrapper acknowledged the close.
DDL = (
    "CREATE TABLE admission(id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " agent TEXT NOT NULL, message_id TEXT NOT NULL, request_id TEXT NOT NULL, body_current TEXT NOT NULL,"
    " rebuilds INTEGER NOT NULL DEFAULT 0,"
    " state TEXT NOT NULL DEFAULT 'requested' CHECK(state IN ('requested','granted')),"
    " last_error_code TEXT, not_before_ms INTEGER NOT NULL DEFAULT 0, last_pending_ms INTEGER,"
    " return_seq INTEGER, replay_tried INTEGER NOT NULL DEFAULT 0,"
    " lease_id TEXT, gateway_id TEXT, reference TEXT, ref_sha256 TEXT, max_calls INTEGER, max_micro_eur INTEGER,"
    " ttl_seconds INTEGER, expires_ms INTEGER, closed_ms INTEGER,"
    " CHECK(state='requested' OR (lease_id IS NOT NULL AND gateway_id IS NOT NULL AND reference IS NOT NULL"
    " AND ref_sha256 IS NOT NULL AND ttl_seconds IS NOT NULL AND expires_ms IS NOT NULL)),"
    " UNIQUE(agent, message_id), UNIQUE(ref_sha256)) STRICT",
    # A durable counter for the order `pending` returns entries in (card L6a-2): it only ever grows, so a clock that
    # stands still or goes back cannot change who is "least recently returned".
    "CREATE TABLE admission_meta(name TEXT PRIMARY KEY, value INTEGER NOT NULL) STRICT",
    "INSERT INTO admission_meta(name, value) VALUES('return_seq', 0)",
    "CREATE TABLE admission_attempt(id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " admission_id INTEGER NOT NULL REFERENCES admission(id), body TEXT NOT NULL, sent_ms INTEGER NOT NULL) STRICT",
    # Identity, request and grant never change once written; an attempt is never changed or deleted; nothing is deleted.
    "CREATE TRIGGER admission_key BEFORE UPDATE OF agent, message_id, request_id ON admission BEGIN"
    " SELECT RAISE(ABORT, 'an admission key is immutable'); END",
    "CREATE TRIGGER admission_grant_once BEFORE UPDATE OF lease_id, gateway_id, reference, ref_sha256, max_calls,"
    " max_micro_eur, ttl_seconds, expires_ms ON admission WHEN OLD.lease_id IS NOT NULL BEGIN"
    " SELECT RAISE(ABORT, 'an admission grant is immutable'); END",
    "CREATE TRIGGER admission_keep BEFORE DELETE ON admission BEGIN"
    " SELECT RAISE(ABORT, 'an admission is never deleted'); END",
    "CREATE TRIGGER admission_attempt_immutable BEFORE UPDATE ON admission_attempt BEGIN"
    " SELECT RAISE(ABORT, 'an attempt is immutable'); END",
    "CREATE TRIGGER admission_attempt_keep BEFORE DELETE ON admission_attempt BEGIN"
    " SELECT RAISE(ABORT, 'an attempt is never deleted'); END",
)

_HEX64 = re.compile(r"[0-9a-f]{64}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MAX_BINDINGS = 100_000


def create(conn):
    """Create the layout in an open connection (the writer's first open, and the tests). Idempotent only on a new
    file: it refuses a file that already has the table."""
    for statement in DDL:
        conn.execute(statement)
    conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)


class Binding:
    """One granted admission, as the receipt side needs it. Nothing else is read."""

    __slots__ = ("ref_sha256", "lease_id", "gateway_id", "agent", "message_id", "expires_ms")

    def __repr__(self):
        return "Binding(<hidden>)"

    __str__ = __repr__


def _uri(path):
    # A read-only connection by URI. The path is made absolute and escaped; nothing from it is ever echoed.
    absolute = urllib.parse.quote(os.path.abspath(path).replace("\\", "/"), safe="/:")
    return "file:///" + absolute if re.match(r"[A-Za-z]:", absolute) else "file://" + absolute


def read_bindings(path):
    """-> (OK, [Binding, ...]) or (LATER, reason). Opens the file read-only (`mode=ro`, `query_only`) and reads the
    granted rows of the table. A missing file, a locked one, a file that is not this layout or any other failure
    means "read again later" with a closed reason: the reporter never stops for it and nothing is written."""
    if not os.path.isfile(path):
        return LATER, STORE_MISSING
    conn = None
    try:
        conn = sqlite3.connect(_uri(path) + "?mode=ro", uri=True, timeout=0, isolation_level=None)
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            return LATER, STORE_UNREADABLE
        rows = conn.execute(
            "SELECT ref_sha256, lease_id, gateway_id, agent, message_id, expires_ms, reference FROM admission"
            " WHERE state='granted' ORDER BY id LIMIT ?", (MAX_BINDINGS + 1,)).fetchall()
    except sqlite3.OperationalError as exc:
        text = str(exc)
        return LATER, STORE_BUSY if ("locked" in text or "busy" in text) else STORE_UNREADABLE
    except (sqlite3.Error, OSError, ValueError):
        return LATER, STORE_UNREADABLE
    finally:
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
    if len(rows) > MAX_BINDINGS:
        return LATER, STORE_HISTORY_LIMIT               # nothing is copied: no new binding can be taken (see README)
    found = []
    for ref, lease_id, gateway_id, agent, message_id, expires_ms, reference in rows:
        if not (type(ref) is str and _HEX64.fullmatch(ref) and type(lease_id) is str and _UUID.fullmatch(lease_id)
                and type(gateway_id) is str and _UUID.fullmatch(gateway_id) and type(agent) is str
                and type(message_id) is str and type(expires_ms) is int):
            return LATER, STORE_UNREADABLE              # a damaged row is never half used
        # The relations the receipt side relies on: the reference IS the lease id's own text, and the hash IS the
        # SHA-256 of that text. A row where they disagree could send one lease's receipt to another lease, so the
        # whole batch is refused (nothing is copied).
        if reference != lease_id or ref != hashlib.sha256(lease_id.encode("ascii")).hexdigest():
            return LATER, STORE_UNREADABLE
        one = Binding()
        one.ref_sha256, one.lease_id, one.gateway_id = ref, lease_id, gateway_id
        one.agent, one.message_id, one.expires_ms = agent, message_id, expires_ms
        found.append(one)
    return OK, found


# --- the writer (card L6a-2): only `reporter admit` opens the store this way -------------------------------------

BUSY_TIMEOUT_MS = 3000          # a short wait for another writer; the caller's deadline is always shorter or equal


class StoreBusy(Exception):
    """Another process holds the file for longer than the short wait: the caller answers "busy" and tries later."""


class StoreUnusable(Exception):
    """The file is not this layout (another version, or not a database). Nothing is changed."""


COLUMNS = ("id", "agent", "message_id", "request_id", "body_current", "rebuilds", "state", "last_error_code",
           "not_before_ms", "last_pending_ms", "return_seq", "replay_tried", "lease_id", "gateway_id", "reference",
           "ref_sha256", "max_calls", "max_micro_eur", "ttl_seconds", "expires_ms", "closed_ms")


def _busy(exc):
    text = str(exc)
    return "locked" in text or "busy" in text


class Writer:
    """The one writer of the store. Every change is its own short `BEGIN IMMEDIATE` transaction, so a crash leaves
    either the old rows or the new ones. WAL mode and full synchronous writes make a committed row survive a power
    cut. The file and its folder are created on first use."""

    def __init__(self, path):
        self.path = path
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None)
        except (OSError, sqlite3.Error):
            raise StoreUnusable() from None
        self.conn = conn
        try:
            conn.execute("PRAGMA busy_timeout=%d" % BUSY_TIMEOUT_MS)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
                with self.txn():
                    if conn.execute("PRAGMA user_version").fetchone()[0] == 0:       # another helper may have won
                        create(conn)
            if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise StoreUnusable()
        except sqlite3.OperationalError as exc:
            self.close()
            raise (StoreBusy() if _busy(exc) else StoreUnusable()) from None
        except (sqlite3.Error, StoreUnusable):
            self.close()
            raise StoreUnusable() from None

    def close(self):
        with contextlib.suppress(sqlite3.Error):
            self.conn.close()

    @contextlib.contextmanager
    def txn(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise (StoreBusy() if _busy(exc) else StoreUnusable()) from None
        try:
            yield self.conn
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def lookup(self, agent, message_id):
        """The row of this message as a dict, or None."""
        try:
            row = self.conn.execute("SELECT " + ",".join(COLUMNS) + " FROM admission WHERE agent=? AND message_id=?",
                                    (agent, message_id)).fetchone()
        except sqlite3.OperationalError as exc:
            raise (StoreBusy() if _busy(exc) else StoreUnusable()) from None
        return None if row is None else dict(zip(COLUMNS, row))

    def begin_request(self, agent, message_id, request_id, body_text, now_ms):
        """Save the request and its first body, once. A row that already exists is returned as it is."""
        with self.txn() as conn:
            row = self.lookup(agent, message_id)
            if row is not None:
                return row
            cursor = conn.execute("INSERT INTO admission(agent, message_id, request_id, body_current) VALUES(?,?,?,?)",
                                  (agent, message_id, request_id, body_text))
            conn.execute("INSERT INTO admission_attempt(admission_id, body, sent_ms) VALUES(?,?,?)",
                         (cursor.lastrowid, body_text, now_ms))
            return self.lookup(agent, message_id)

    def rebuild(self, admission_id, body_text, now_ms):
        """A new body for the same request, saved (as the current one and as an attempt) before it is sent."""
        with self.txn() as conn:
            conn.execute("UPDATE admission SET body_current=?, rebuilds=rebuilds+1 WHERE id=? AND state='requested'",
                         (body_text, admission_id))
            conn.execute("INSERT INTO admission_attempt(admission_id, body, sent_ms) VALUES(?,?,?)",
                         (admission_id, body_text, now_ms))

    def attempts(self, admission_id, limit=4):
        """The saved bodies, newest first."""
        return [row[0] for row in self.conn.execute(
            "SELECT body FROM admission_attempt WHERE admission_id=? ORDER BY id DESC LIMIT ?",
            (admission_id, limit))]

    def mark_replayed(self, admission_id, count):
        """`count` of the newest bodies have been replayed without finding the winning grant (never goes down)."""
        with self.txn() as conn:
            conn.execute("UPDATE admission SET replay_tried=MAX(replay_tried, ?) WHERE id=?", (count, admission_id))

    def note_error(self, admission_id, code, not_before_ms, restart_recovery):
        """Remember why the last try ended and when to try again. `restart_recovery` forgets the recovery progress
        (the rebuild count and the replayed bodies) because the next run starts a new episode; a run that ran out of
        time keeps it."""
        with self.txn() as conn:
            conn.execute("UPDATE admission SET last_error_code=?, not_before_ms=? WHERE id=? AND state='requested'",
                         (code, not_before_ms, admission_id))
            if restart_recovery:
                conn.execute("UPDATE admission SET rebuilds=0, replay_tried=0 WHERE id=? AND state='requested'",
                             (admission_id,))

    def save_grant(self, admission_id, grant):
        """Fill the grant, once, and only if the row is still `requested`. Returns the row as stored: if another helper
        already saved a grant, that one, never this caller's."""
        with self.txn() as conn:
            conn.execute(
                "UPDATE admission SET state='granted', lease_id=?, gateway_id=?, reference=?, ref_sha256=?, max_calls=?,"
                " max_micro_eur=?, ttl_seconds=?, expires_ms=?, last_error_code=NULL, not_before_ms=0"
                " WHERE id=? AND state='requested'",
                (grant["lease_id"], grant["gateway_id"], grant["lease_id"], grant["ref_sha256"], grant["max_calls"],
                 grant["max_micro_eur"], grant["ttl_seconds"], grant["expires_ms"], admission_id))
            row = conn.execute("SELECT " + ",".join(COLUMNS) + " FROM admission WHERE id=?", (admission_id,)).fetchone()
            return dict(zip(COLUMNS, row))
