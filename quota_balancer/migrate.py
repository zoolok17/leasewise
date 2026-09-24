"""Schema migration for the quota inventory store.

Card 1 is migration version 1 and card 2 is migration version 2. Each DDL
block is normative: version 1 is copied byte for byte from docs/DESIGN.md
section 8 ("Card 1"); version 2 adds the card-2 tables (readings, windows)
and the gateway report / account coverage columns of section 7. A store at
the current version is a no-op; a store at a newer, unknown version is
rejected before anything is written or switched (left byte-for-byte
unchanged).

Migration protocol (fix round): the preliminary version read takes no lock.
A store below the current version is switched to WAL first (WAL is a
file-mode switch and cannot run inside a transaction) and the switch is
checked; the switch takes a brief exclusive lock that does not wait through
the busy handler, so under a concurrent init it is retried on the busy
condition until a bounded deadline. Then the write lock is taken
(BEGIN IMMEDIATE) and the version re-read inside it, so concurrent inits
serialize and a latecomer observes the current version as a no-op. Every
pending step's DDL and the version stamp commit as one transaction; any
failure rolls back, so a stop between statements leaves no half-built
store. A store at a newer version is rejected before anything is written or
switched.
"""

import sqlite3
import time

from .errors import (
    CODE_SCHEMA_UNSUPPORTED,
    CODE_STORE_ERROR,
    MSG_SCHEMA_UNSUPPORTED,
    MSG_STORE_FAILED,
    QuotaError,
)

SCHEMA_VERSION = 2

# The WAL switch (set_wal) takes a brief exclusive lock on the database file
# and, unlike ordinary statements, does not wait through the busy handler:
# under a concurrent init it can raise "database is locked" while a sibling
# is mid-DDL. Retrying on the busy condition until a bounded deadline makes
# parallel init idempotent; a persistent lock then surfaces as STORE_ERROR.
WAL_RETRY_DEADLINE_SECONDS = 5.0
WAL_RETRY_SLEEP_SECONDS = 0.02

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

# Card 2 (migration version 2): the reading-reducer tables and the gateway
# report / account coverage columns (docs/DESIGN.md section 7).
#
# readings.account_id is nullable in this card so an ambiguous-source reading
# (account attribution unknown) can still be recorded for dedup/audit without
# being reduced into any account; it is NOT NULL in every attributed reading.
# readings.seat_id carries no foreign key here because the seats table is
# introduced by a later card; the reference is deferred to that card.
_DDL_V2 = (
    "CREATE TABLE readings(\n"
    " event_id TEXT PRIMARY KEY,\n"
    " machine_id TEXT NOT NULL REFERENCES machines(id),\n"
    " account_id TEXT REFERENCES accounts(id),\n"
    " seat_id TEXT,\n"
    " kind TEXT NOT NULL CHECK(kind IN ('cli_window','gateway_snapshot')),\n"
    " observed_at INTEGER NOT NULL,\n"
    " received_at INTEGER NOT NULL,\n"
    " payload TEXT NOT NULL) STRICT;",
    "CREATE TABLE windows(\n"
    " account_id TEXT NOT NULL REFERENCES accounts(id),\n"
    " window_key TEXT NOT NULL,\n"
    " used_bps INTEGER,\n"
    " resets_at INTEGER,\n"
    " observed_at INTEGER NOT NULL,\n"
    " confidence TEXT NOT NULL CHECK(confidence IN ('observed','unknown')),\n"
    " source_stale INTEGER NOT NULL DEFAULT 0,\n"
    " covered_seq INTEGER,\n"
    " source_event_id TEXT,\n"
    " PRIMARY KEY(account_id, window_key)) STRICT;",
    # gateways: gateway report columns (reporter-owned, not operator-owned).
    "ALTER TABLE gateways ADD COLUMN ledger_epoch INTEGER;",
    "ALTER TABLE gateways ADD COLUMN last_seq INTEGER;",
    "ALTER TABLE gateways ADD COLUMN period_start INTEGER;",
    "ALTER TABLE gateways ADD COLUMN period_end INTEGER;",
    "ALTER TABLE gateways ADD COLUMN policy_hashes TEXT;",
    "ALTER TABLE gateways ADD COLUMN last_report_at INTEGER;",
    "ALTER TABLE gateways ADD COLUMN state TEXT;",
    "ALTER TABLE gateways ADD COLUMN sized_ready INTEGER NOT NULL DEFAULT 0;",
    # accounts: coverage columns. coverage_complete is operator-only
    # (default false, never inferred from traffic); the rest are the
    # coverage/idle-proof bookkeeping of the design.
    "ALTER TABLE accounts ADD COLUMN window_keys TEXT;",
    "ALTER TABLE accounts ADD COLUMN activity_seq INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE accounts ADD COLUMN coverage_complete INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE accounts ADD COLUMN required_reporters TEXT;",
    "ALTER TABLE accounts ADD COLUMN outage_local_concurrency_cap INTEGER NOT NULL DEFAULT 2;",
)

# The canonical version-2 migration script, kept for byte-for-byte comparison.
MIGRATION_V2 = "\n".join(_DDL_V2) + "\nPRAGMA user_version=2;\n"

# Step N upgrades a store from version N-1 to version N.
_STEPS = {1: _DDL_V1, 2: _DDL_V2}


def schema_version(conn):
    """Return the store's PRAGMA user_version (reads the file header)."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def require_supported(conn):
    """Reject a store whose user_version is not the current one.

    A store at a newer, unknown version is rejected here and left
    byte-for-byte unchanged (the read paths never switch journal mode).
    A store at an older version is rejected too: the operator re-runs
    `init` to bring it up to date before reading or importing.
    """
    if schema_version(conn) != SCHEMA_VERSION:
        raise QuotaError(CODE_SCHEMA_UNSUPPORTED, MSG_SCHEMA_UNSUPPORTED)


def _is_busy_error(exc):
    """True for the SQLite "database is locked" busy conditions."""
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def set_wal(conn):
    """Switch the database to WAL mode and verify the switch happened.

    The switch takes a brief exclusive lock and does not wait through the
    busy handler, so a concurrent init's write lock can make it raise
    "database is locked". Retry on the busy condition until a bounded
    deadline; a real, persistent lock then surfaces as STORE_ERROR (exit 3)
    rather than a half-switched store.
    """
    deadline = time.monotonic() + WAL_RETRY_DEADLINE_SECONDS
    while True:
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        except sqlite3.OperationalError as exc:
            if not _is_busy_error(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(WAL_RETRY_SLEEP_SECONDS)
            continue
        if mode.lower() != "wal":
            raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED)
        return


def rollback_quietly(conn):
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def migrate(conn):
    """Bring the database to the current schema version. Idempotent,
    transactional, and ordered.

    A store at the current version is a no-op. A store at a newer, unknown
    version is rejected before anything is written or switched (left
    byte-for-byte unchanged). A store below the current version (0 = fresh
    or empty file, 1 = card 1) is brought up step by step: the pending
    steps' DDL and the version stamp commit as one transaction, so a
    failure (or a stop) between statements leaves no half-built store.
    """
    version = schema_version(conn)
    if version == SCHEMA_VERSION:
        return
    if version > SCHEMA_VERSION:
        # Newer, unknown version: reject before the WAL switch so the store
        # is left byte-for-byte unchanged.
        raise QuotaError(CODE_SCHEMA_UNSUPPORTED, MSG_SCHEMA_UNSUPPORTED)
    set_wal(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = schema_version(conn)  # re-read inside the lock
        if version == SCHEMA_VERSION:
            conn.execute("COMMIT")
            return
        if version > SCHEMA_VERSION:
            conn.execute("ROLLBACK")
            raise QuotaError(CODE_SCHEMA_UNSUPPORTED, MSG_SCHEMA_UNSUPPORTED)
        for step in range(version + 1, SCHEMA_VERSION + 1):
            for statement in _STEPS[step]:
                conn.execute(statement)
        conn.execute("PRAGMA user_version=" + str(SCHEMA_VERSION))
        conn.execute("COMMIT")
    except BaseException:
        rollback_quietly(conn)
        raise
