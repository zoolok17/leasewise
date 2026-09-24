"""Schema migration for the quota inventory store.

Card 1 is migration version 1. The DDL is normative: it is copied byte for
byte from docs/DESIGN.md section 8 ("Card 1") and creates exactly the three
tables of this card.

Migration protocol (fix round): the preliminary version read takes no lock.
A version-0 store is switched to WAL first (WAL is a file-mode switch and
cannot run inside a transaction) and the switch is checked; then the write
lock is taken (BEGIN IMMEDIATE) and the version re-read inside it, so
concurrent inits serialize and a latecomer observes version 1 as a no-op.
The DDL and the version stamp commit as one transaction; any failure rolls
back, so a stop between statements leaves no half-built store. A store at
any other version is rejected before anything is written or switched.
"""

import sqlite3

from .errors import (
    CODE_SCHEMA_UNSUPPORTED,
    CODE_STORE_ERROR,
    MSG_SCHEMA_UNSUPPORTED,
    MSG_STORE_FAILED,
    QuotaError,
)

SCHEMA_VERSION = 1

# The three DDL statements, byte for byte from docs/DESIGN.md section 8.
_DDL_V1 = (
    "CREATE TABLE accounts(id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE, vendor TEXT NOT NULL,\n"
    " kind TEXT NOT NULL CHECK(kind IN ('metered','subscription')), budget_micro_eur INTEGER,\n"
    " CHECK((kind='metered' AND typeof(budget_micro_eur)='integer' AND budget_micro_eur>0)\n"
    " OR (kind='subscription' AND budget_micro_eur IS NULL))) STRICT;",
    "CREATE TABLE machines(id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE) STRICT;",
    "CREATE TABLE gateways(id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE,\n"
    " account_id TEXT NOT NULL REFERENCES accounts(id), machine_id TEXT NOT NULL REFERENCES machines(id),\n"
    " share_micro_eur INTEGER NOT NULL CHECK(share_micro_eur>0), period TEXT NOT NULL CHECK(period='utc_month')) STRICT;",
)

# The canonical migration script (DDL plus the version stamp), kept for
# byte-for-byte comparison against the design block.
MIGRATION_V1 = "\n".join(_DDL_V1) + "\nPRAGMA user_version=1;\n"


def schema_version(conn):
    """Return the store's PRAGMA user_version (reads the file header)."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def require_supported(conn):
    """Reject a store whose user_version is not 1, without modifying it."""
    if schema_version(conn) != SCHEMA_VERSION:
        raise QuotaError(CODE_SCHEMA_UNSUPPORTED, MSG_SCHEMA_UNSUPPORTED)


def set_wal(conn):
    """Switch the database to WAL mode and verify the switch happened."""
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if mode.lower() != "wal":
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED)


def rollback_quietly(conn):
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def migrate(conn):
    """Bring the database to schema version 1. Idempotent. Transactional.

    Version 0 means "not initialized" (fresh or empty file) and runs the
    migration. Version 1 is a no-op. Any other version is unknown or newer
    and is rejected. The DDL and the version stamp are one transaction, so
    a failure (or a stop) between statements leaves no half-built store.
    """
    version = schema_version(conn)
    if version == SCHEMA_VERSION:
        return
    if version != 0:
        raise QuotaError(CODE_SCHEMA_UNSUPPORTED, MSG_SCHEMA_UNSUPPORTED)
    set_wal(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = schema_version(conn)  # re-read inside the lock
        if version == SCHEMA_VERSION:
            conn.execute("COMMIT")
            return
        if version != 0:
            conn.execute("ROLLBACK")
            raise QuotaError(CODE_SCHEMA_UNSUPPORTED, MSG_SCHEMA_UNSUPPORTED)
        for statement in _DDL_V1:
            conn.execute(statement)
        conn.execute("PRAGMA user_version=1")
        conn.execute("COMMIT")
    except BaseException:
        rollback_quietly(conn)
        raise
