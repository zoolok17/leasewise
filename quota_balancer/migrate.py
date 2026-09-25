"""Schema migration for the quota inventory store.

Card 1 is migration version 1, card 2 is version 2, card 3 is version 3,
and card 2b-1 is version 4. Each DDL block is normative: version 1 is
copied byte for byte from docs/DESIGN.md section 8 ("Card 1"); version 2
adds the card-2 tables (readings, windows) and the gateway report /
account coverage columns of section 7; version 3 adds the card-3 policy
tables (models, profiles, projects, teams, seats, project_accounts,
limits) of section 7; version 4 (card 2b-1, section 11) rebuilds
readings' kind CHECK to admit provider_billing and adds the billing
policy/totals tables. Versions 1-3 are kept byte for byte: a card adds
only its own DDL. A store at the current version is a no-op; a store at
a newer, unknown version is rejected before anything is written or
switched (left byte-for-byte unchanged).

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

SCHEMA_VERSION = 4

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

# Card 3 (migration version 3): the policy-arithmetic tables of section 7
# (models, profiles, projects, teams, seats, project_accounts, limits) plus
# the exact limits_scope uniqueness index. These are config-owned and stay
# EMPTY in this card (no policy config-import exists yet; a later card
# loads them); the policy arithmetic itself is pure and reads plain fixture
# rows, not the store.
_DDL_V3 = (
    # projects: the top-level org unit.
    "CREATE TABLE projects(\n"
    " id TEXT PRIMARY KEY,\n"
    " alias TEXT NOT NULL UNIQUE) STRICT;",
    # teams: a team belongs to a project.
    "CREATE TABLE teams(\n"
    " id TEXT PRIMARY KEY,\n"
    " project_id TEXT NOT NULL REFERENCES projects(id),\n"
    " alias TEXT NOT NULL,\n"
    " UNIQUE(project_id, alias)) STRICT;",
    # project_accounts: the project<->account membership (composite PK).
    "CREATE TABLE project_accounts(\n"
    " project_id TEXT NOT NULL REFERENCES projects(id),\n"
    " account_id TEXT NOT NULL REFERENCES accounts(id),\n"
    " PRIMARY KEY(project_id, account_id)) STRICT;",
    # seats: a seat binds a team to a machine.
    "CREATE TABLE seats(\n"
    " id TEXT PRIMARY KEY,\n"
    " team_id TEXT NOT NULL REFERENCES teams(id),\n"
    " machine_id TEXT NOT NULL REFERENCES machines(id),\n"
    " alias TEXT NOT NULL,\n"
    " role TEXT NOT NULL,\n"
    " enforcement TEXT NOT NULL CHECK(enforcement IN ("
    "'metered','subscription_admission','subscription_warn','shadow',"
    "'metered_advisory')),\n"
    " last_seen_at INTEGER,\n"
    " UNIQUE(machine_id, alias)) STRICT;",
    # models: per-account model rows. The reserve rates/envelopes bind to the
    # installed price policy_hash (section 3); the tariff rates are the
    # settlement rates (section 4). context_ceiling is the input (context)
    # ceiling; output_ceiling the total output ceiling.
    "CREATE TABLE models(\n"
    " id TEXT PRIMARY KEY,\n"
    " account_id TEXT NOT NULL REFERENCES accounts(id),\n"
    " alias TEXT NOT NULL,\n"
    " context_ceiling INTEGER NOT NULL CHECK(context_ceiling>0),\n"
    " output_ceiling INTEGER NOT NULL CHECK(output_ceiling>0),\n"
    " tariff_input_rate INTEGER NOT NULL CHECK(tariff_input_rate>=0),\n"
    " tariff_output_rate INTEGER NOT NULL CHECK(tariff_output_rate>=0),\n"
    " reserve_input_rate INTEGER NOT NULL CHECK(reserve_input_rate>=0),\n"
    " reserve_output_rate INTEGER NOT NULL CHECK(reserve_output_rate>=0),\n"
    " reservation_mode TEXT NOT NULL CHECK(reservation_mode IN ('sized','fixed')),\n"
    " policy_hash TEXT NOT NULL,\n"
    " effort_aliases TEXT NOT NULL,\n"
    " UNIQUE(account_id, alias)) STRICT;",
    # profiles: the per-task-class policy (section 6). candidates is the
    # ordered tier list (last tier is 'park'); the numeric caps are the
    # admission-plan defaults the arithmetic applies.
    "CREATE TABLE profiles(\n"
    " id TEXT PRIMARY KEY,\n"
    " task_class TEXT NOT NULL UNIQUE,\n"
    " candidates TEXT NOT NULL,\n"
    " context_rules TEXT NOT NULL,\n"
    " calibration TEXT NOT NULL,\n"
    " expected_calls INTEGER NOT NULL CHECK(expected_calls>0 AND expected_calls<=100),\n"
    " growth_tokens INTEGER NOT NULL CHECK(growth_tokens>=0),\n"
    " margin_bps INTEGER NOT NULL CHECK(margin_bps>=10000 AND margin_bps<=20000),\n"
    " max_calls INTEGER NOT NULL CHECK(max_calls>0 AND max_calls<=100),\n"
    " output_cap INTEGER NOT NULL CHECK(output_cap>=0),\n"
    " per_call_output_cap INTEGER NOT NULL CHECK(per_call_output_cap>=0),\n"
    " ttl_ms INTEGER NOT NULL CHECK(ttl_ms>0),\n"
    " policy_revision INTEGER NOT NULL CHECK(policy_revision>=0)) STRICT;",
    # limits: a scope/metric/period cap. soft is an optional warning bound
    # that must not exceed hard; the effective cap is the tightest (min) hard
    # across the applicable scopes (section 4).
    "CREATE TABLE limits(\n"
    " id TEXT PRIMARY KEY,\n"
    " scope TEXT NOT NULL CHECK(scope IN ('platform','project','team','seat')),\n"
    " scope_id TEXT NOT NULL,\n"
    " metric TEXT NOT NULL CHECK(metric IN ("
    "'input_tokens','money_micro_eur','window_bps','concurrency')),\n"
    " period TEXT NOT NULL CHECK(period IN ('turn','day','month','window')),\n"
    " window_key TEXT,\n"
    " soft INTEGER,\n"
    " hard INTEGER NOT NULL CHECK(hard>=0),\n"
    " CHECK(soft IS NULL OR soft>=0),\n"
    " CHECK(soft IS NULL OR soft<=hard)) STRICT;",
    # The exact uniqueness index of section 7 (empty string is not a valid
    # window key; COALESCE makes a NULL window_key distinct from '').
    "CREATE UNIQUE INDEX limits_scope ON limits("
    "scope,scope_id,metric,period,COALESCE(window_key,''))",
)

# Card 2b-1 (migration version 4): provider billing ingestion (section 11).
#
# The readings table is REBUILT, not altered: SQLite has no in-place CHECK
# modification, and the kind CHECK must gain provider_billing. The rebuild
# (new STRICT table, copy of every row, drop, rename) preserves old rows,
# IDs, payloads, and the windows table byte for byte. It runs BEFORE
# billing_totals is created, because billing_totals carries source FKs to
# readings(event_id). The future usage kind is deliberately NOT admitted:
# it ships with its own closed validator/consumer.
#
# billing_policies is operator-owned config (one policy per metered
# account, closed enums; unknown basis is permitted configuration, not
# permission to compare). billing_totals is the source-time reduced view:
# latest (with its own source time and event), a retained high-water (with
# its own event), and a conflict latch that only a later audited resolution
# integration can clear. Currency and scope live in the policy.
_DDL_V4 = (
    "CREATE TABLE readings_new(\n"
    " event_id TEXT PRIMARY KEY,\n"
    " machine_id TEXT NOT NULL REFERENCES machines(id),\n"
    " account_id TEXT REFERENCES accounts(id),\n"
    " seat_id TEXT,\n"
    " kind TEXT NOT NULL CHECK(kind IN ('cli_window','gateway_snapshot','provider_billing')),\n"
    " observed_at INTEGER NOT NULL,\n"
    " received_at INTEGER NOT NULL,\n"
    " payload TEXT NOT NULL) STRICT;",
    "INSERT INTO readings_new(event_id, machine_id, account_id, seat_id, kind,\n"
    " observed_at, received_at, payload)\n"
    " SELECT event_id, machine_id, account_id, seat_id, kind, observed_at,\n"
    " received_at, payload FROM readings;",
    "DROP TABLE readings;",
    "ALTER TABLE readings_new RENAME TO readings;",
    "CREATE TABLE billing_policies(\n"
    " account_id TEXT PRIMARY KEY REFERENCES accounts(id),\n"
    " reporter_machine_id TEXT NOT NULL REFERENCES machines(id),\n"
    " scope_alias TEXT NOT NULL,\n"
    " scope_revision INTEGER NOT NULL CHECK(scope_revision>0),\n"
    " dedicated_ai INTEGER NOT NULL CHECK(dedicated_ai IN (0,1)),\n"
    " non_ai_allowance_micro_eur INTEGER NOT NULL CHECK(non_ai_allowance_micro_eur>=0),\n"
    " cap_basis TEXT NOT NULL CHECK(cap_basis IN ('unknown','ex_vat','invoice_total')),\n"
    " provider_basis TEXT NOT NULL CHECK(provider_basis IN ('unknown','ex_vat','invoice_total')),\n"
    " ledger_basis TEXT NOT NULL CHECK(ledger_basis IN ('unknown','ex_vat','invoice_total')),\n"
    " vat_bps INTEGER CHECK(vat_bps IS NULL OR (vat_bps>=0 AND vat_bps<=10000)),\n"
    " poll_seconds INTEGER NOT NULL CHECK(poll_seconds>0),\n"
    " max_age_seconds INTEGER NOT NULL CHECK(max_age_seconds>0),\n"
    " lag_seconds INTEGER NOT NULL CHECK(lag_seconds>0),\n"
    " abs_tolerance_micro_eur INTEGER NOT NULL CHECK(abs_tolerance_micro_eur>=0),\n"
    " relative_tolerance_bps INTEGER NOT NULL CHECK(relative_tolerance_bps>=0 AND relative_tolerance_bps<=10000),\n"
    " mode TEXT NOT NULL CHECK(mode IN ('advisory')),\n"
    " stale_action TEXT NOT NULL CHECK(stale_action IN ('alert')),\n"
    " divergence_action TEXT NOT NULL CHECK(divergence_action IN ('hold'))) STRICT;",
    "CREATE TABLE billing_totals(\n"
    " account_id TEXT NOT NULL REFERENCES accounts(id),\n"
    " scope_revision INTEGER NOT NULL CHECK(scope_revision>0),\n"
    " period_start INTEGER NOT NULL,\n"
    " period_end INTEGER NOT NULL,\n"
    " latest_micro_eur INTEGER NOT NULL,\n"
    " observed_at INTEGER NOT NULL,\n"
    " latest_event_id TEXT NOT NULL REFERENCES readings(event_id),\n"
    " high_water_micro_eur INTEGER NOT NULL,\n"
    " high_water_event_id TEXT NOT NULL REFERENCES readings(event_id),\n"
    " conflict INTEGER NOT NULL DEFAULT 0 CHECK(conflict IN (0,1)),\n"
    " PRIMARY KEY(account_id, scope_revision, period_start)) STRICT;",
)

# The canonical version-3 migration script, kept for byte-for-byte comparison.
MIGRATION_V3 = "\n".join(_DDL_V3) + "\nPRAGMA user_version=3;\n"

# The canonical version-4 migration script, kept for byte-for-byte comparison.
MIGRATION_V4 = "\n".join(_DDL_V4) + "\nPRAGMA user_version=4;\n"

# Step N upgrades a store from version N-1 to version N.
_STEPS = {1: _DDL_V1, 2: _DDL_V2, 3: _DDL_V3, 4: _DDL_V4}


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
    or empty file, 1 = card 1, 2 = card 2, 3 = card 3) is brought up step
    by step to version 4 (card 2b-1): the
    pending steps' DDL and the version stamp commit as one transaction, so
    a failure (or a stop) between statements leaves no half-built store.
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
