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

# The layout (DESIGN-6A 8.6). One row per admitted message: the request (saved before anything is sent), its state
# (`requested` until the grant is saved, then `granted`), the grant, and when the wrapper acknowledged the close.
DDL = (
    "CREATE TABLE admission(id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " agent TEXT NOT NULL, message_id TEXT NOT NULL, request_id TEXT NOT NULL, body_current TEXT NOT NULL,"
    " rebuilds INTEGER NOT NULL DEFAULT 0,"
    " state TEXT NOT NULL DEFAULT 'requested' CHECK(state IN ('requested','granted')),"
    " last_error_code TEXT, not_before_ms INTEGER NOT NULL DEFAULT 0, last_pending_ms INTEGER,"
    " lease_id TEXT, gateway_id TEXT, reference TEXT, ref_sha256 TEXT, max_calls INTEGER, max_micro_eur INTEGER,"
    " ttl_seconds INTEGER, expires_ms INTEGER, closed_ms INTEGER,"
    " CHECK(state='requested' OR (lease_id IS NOT NULL AND gateway_id IS NOT NULL AND reference IS NOT NULL"
    " AND ref_sha256 IS NOT NULL AND ttl_seconds IS NOT NULL AND expires_ms IS NOT NULL)),"
    " UNIQUE(agent, message_id), UNIQUE(ref_sha256)) STRICT",
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
            "SELECT ref_sha256, lease_id, gateway_id, agent, message_id, expires_ms FROM admission"
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
        return LATER, STORE_UNREADABLE
    found = []
    for ref, lease_id, gateway_id, agent, message_id, expires_ms in rows:
        if not (type(ref) is str and _HEX64.fullmatch(ref) and type(lease_id) is str and _UUID.fullmatch(lease_id)
                and type(gateway_id) is str and _UUID.fullmatch(gateway_id) and type(agent) is str
                and type(message_id) is str and type(expires_ms) is int):
            return LATER, STORE_UNREADABLE              # a damaged row is never half used
        one = Binding()
        one.ref_sha256, one.lease_id, one.gateway_id = ref, lease_id, gateway_id
        one.agent, one.message_id, one.expires_ms = agent, message_id, expires_ms
        found.append(one)
    return OK, found
