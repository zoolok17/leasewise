"""Schema migration for the quota inventory store.

Card 1 is migration version 1, card 2 is version 2, card 3 is version 3,
card 2b-1 is version 4, and card 4 is version 5. Each DDL block is
normative: version 1 is copied byte for byte from docs/DESIGN.md section
8 ("Card 1"); version 2 adds the card-2 tables (readings, windows) and
the gateway report / account coverage columns of section 7; version 3
adds the card-3 policy tables (models, profiles, projects, teams, seats,
project_accounts, limits) of section 7; version 4 (card 2b-1, section
11) rebuilds readings' kind CHECK to admit provider_billing and adds
the billing policy/totals tables; version 5 (card 4, section 7) adds
the lease-store tables (blocks, leases, holds, receipts, adjustments,
audit, daily_totals) and the exact partial indexes (blocks_live,
receipt_final, lease_live); version 6 (card 5, section 6) adds the
identity allowlist columns to machines; version 7 (card 5 fix round 2)
adds limit_events (full-content idempotency for POST /limit-events) and
http_request_identity (the HTTP transport's own Idempotency-Key ledger
for wire-only fields the store never receives); version 8 (card 5 fix
round 3) adds limit_events.legacy and backfills it from pre-v7 stores'
own blocks.source_event_id rows (creator identities only - a joiner's
identity was never recorded before v7 at all). Versions 1-3 are kept
byte for byte: a card adds only its own DDL. A store at the current
version is a no-op; a store at a newer, unknown version is rejected
before anything is written or switched (left byte-for-byte unchanged).

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

SCHEMA_VERSION = 10

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
    # ordered list of model aliases (JSON; the terminal 'park' tier is
    # implicit - every candidate blocked parks - and is never stored);
    # the numeric caps are the per-class ceilings grant enforces.
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
    "scope,scope_id,metric,period,COALESCE(window_key,''));",
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

# Card 4 (migration version 5): the lease store (section 7). The seven
# tables - blocks, leases, holds, receipts, adjustments, audit,
# daily_totals - and the exact partial indexes of section 7
# (blocks_live, receipt_final, lease_live). They are created EMPTY in
# this turn: the transactional grant/check/settle API lands in a later
# turn, so no writer of these tables exists yet in this commit.
#
# holds.limit_id carries no foreign key: an account hold reserves a
# synthetic account-limit identifier, not a duplicate limits row
# (section 7). The partial indexes keep one parked/probing block per
# (account, model-or-null), one active/uncertain lease per (seat, turn)
# and one final receipt per lease; cleared/settled rows keep no slot.
_DDL_V5 = (
    "CREATE TABLE blocks(\n"
    " id TEXT PRIMARY KEY,\n"
    " account_id TEXT NOT NULL REFERENCES accounts(id),\n"
    " model_id TEXT REFERENCES models(id),\n"
    " reason TEXT NOT NULL,\n"
    " retry_at INTEGER,\n"
    " state TEXT NOT NULL CHECK(state IN ('parked','probing','cleared')),\n"
    " source_event_id TEXT,\n"
    " updated_at INTEGER NOT NULL) STRICT;",
    "CREATE UNIQUE INDEX blocks_live ON blocks("
    "account_id,COALESCE(model_id,'')) WHERE state IN ('parked','probing');",
    "CREATE TABLE leases(\n"
    " id TEXT PRIMARY KEY,\n"
    " machine_id TEXT NOT NULL REFERENCES machines(id),\n"
    " seat_id TEXT NOT NULL REFERENCES seats(id),\n"
    " account_id TEXT NOT NULL REFERENCES accounts(id),\n"
    " model_id TEXT NOT NULL REFERENCES models(id),\n"
    " gateway_id TEXT REFERENCES gateways(id),\n"
    " parent_lease_id TEXT REFERENCES leases(id),\n"
    " turn_id TEXT NOT NULL,\n"
    " request_id TEXT NOT NULL,\n"
    " request_hash TEXT NOT NULL,\n"
    " token_hash TEXT,\n"
    " task_class TEXT NOT NULL,\n"
    " context_mode TEXT NOT NULL,\n"
    " enforcement TEXT NOT NULL,\n"
    " estimate TEXT NOT NULL,\n"
    " policy_snapshot TEXT NOT NULL,\n"
    " input_cap INTEGER NOT NULL CHECK(input_cap>=0),\n"
    " output_cap INTEGER NOT NULL CHECK(output_cap>=0),\n"
    " max_calls INTEGER NOT NULL CHECK(max_calls>0),\n"
    " per_call_output_cap INTEGER NOT NULL CHECK(per_call_output_cap>=0),\n"
    " money_hold INTEGER CHECK(money_hold IS NULL OR money_hold>=0),\n"
    " granted_at INTEGER NOT NULL,\n"
    " expires_at INTEGER NOT NULL,\n"
    " activity_seq INTEGER NOT NULL DEFAULT 0,\n"
    " period_start INTEGER NOT NULL,\n"
    " state TEXT NOT NULL CHECK(state IN ('active','uncertain','settled')),\n"
    " outcome TEXT,\n"
    " UNIQUE(machine_id, request_id)) STRICT;",
    "CREATE UNIQUE INDEX lease_live ON leases(seat_id,turn_id) "
    "WHERE state IN ('active','uncertain');",
    "CREATE TABLE holds(\n"
    " lease_id TEXT NOT NULL REFERENCES leases(id),\n"
    " limit_id TEXT NOT NULL,\n"
    " period_start INTEGER NOT NULL,\n"
    " reserved INTEGER NOT NULL CHECK(reserved>=0),\n"
    " settled_actual INTEGER "
    "CHECK(settled_actual IS NULL OR settled_actual>=0),\n"
    " PRIMARY KEY(lease_id, limit_id, period_start)) STRICT;",
    "CREATE TABLE receipts(\n"
    " event_id TEXT PRIMARY KEY,\n"
    " lease_id TEXT NOT NULL REFERENCES leases(id),\n"
    " gateway_id TEXT REFERENCES gateways(id),\n"
    " seq INTEGER,\n"
    " calls INTEGER NOT NULL CHECK(calls>=0),\n"
    " input_tokens INTEGER NOT NULL CHECK(input_tokens>=0),\n"
    " cached_input_tokens INTEGER NOT NULL CHECK(cached_input_tokens>=0),\n"
    " output_tokens INTEGER NOT NULL CHECK(output_tokens>=0),\n"
    " actual_micro_eur INTEGER "
    "CHECK(actual_micro_eur IS NULL OR actual_micro_eur>=0),\n"
    " outcome TEXT,\n"
    " final INTEGER NOT NULL DEFAULT 0 CHECK(final IN (0,1)),\n"
    " observed_at INTEGER NOT NULL,\n"
    " received_at INTEGER NOT NULL) STRICT;",
    "CREATE UNIQUE INDEX receipt_final ON receipts(lease_id) WHERE final=1;",
    "CREATE TABLE adjustments(\n"
    " id TEXT PRIMARY KEY,\n"
    " account_id TEXT NOT NULL REFERENCES accounts(id),\n"
    " gateway_id TEXT NOT NULL REFERENCES gateways(id),\n"
    " source_event_id TEXT NOT NULL UNIQUE,\n"
    " period_start INTEGER NOT NULL,\n"
    " unattributed_micro_eur INTEGER NOT NULL,\n"
    " reason TEXT NOT NULL,\n"
    " resolved_at INTEGER) STRICT;",
    "CREATE TABLE audit(\n"
    " actor_alias TEXT NOT NULL,\n"
    " action TEXT NOT NULL,\n"
    " object_id TEXT,\n"
    " at INTEGER NOT NULL,\n"
    " redacted_details TEXT NOT NULL) STRICT;",
    "CREATE TABLE daily_totals(\n"
    " scope TEXT NOT NULL,\n"
    " scope_id TEXT NOT NULL,\n"
    " utc_day TEXT NOT NULL,\n"
    " tokens INTEGER NOT NULL CHECK(tokens>=0),\n"
    " calls INTEGER NOT NULL CHECK(calls>=0),\n"
    " money_micro_eur INTEGER NOT NULL CHECK(money_micro_eur>=0),\n"
    " unmanaged_turns INTEGER NOT NULL CHECK(unmanaged_turns>=0),\n"
    " PRIMARY KEY(scope, scope_id, utc_day)) STRICT;",
)

# The canonical version-5 migration script, kept for byte-for-byte comparison.
MIGRATION_V5 = "\n".join(_DDL_V5) + "\nPRAGMA user_version=5;\n"

# Card 5 (migration version 6): the private-HTTP identity allowlist
# (section 6: "Default loopback; optional private authenticated TLS
# tailnet ingress with machine allowlist"). machines add identity_ref
# (unique among non-null values - a machine need not be enrolled),
# enabled and last_seen_at (section 7's promised "later" columns). No
# CHECK on `enabled` (STRICT already enforces INTEGER; the HTTP layer
# treats anything but 1 as disabled, fail closed).
_DDL_V6 = (
    "ALTER TABLE machines ADD COLUMN identity_ref TEXT;",
    "ALTER TABLE machines ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1;",
    "ALTER TABLE machines ADD COLUMN last_seen_at INTEGER;",
    "CREATE UNIQUE INDEX machines_identity_ref ON machines(identity_ref) "
    "WHERE identity_ref IS NOT NULL;",
)

# The canonical version-6 migration script, kept for byte-for-byte comparison.
MIGRATION_V6 = "\n".join(_DDL_V6) + "\nPRAGMA user_version=6;\n"

# Card 5 fix round 2 (migration version 7): two card-5-owned tables no
# earlier card added.
#
# limit_events retains every ACCEPTED POST /limit-events report by its own
# event_id - not only the one that first created a block (fix round 2,
# tk-dae9aab24ef8 findings N3/N4): full-content idempotency (a replay
# compares every field; a changed field is IDEMPOTENCY_CONFLICT) and
# tracking which block an event joined, so a later report can still be
# recognized as a pure replay after the block itself is cleared or
# updated by other events.
#
# http_request_identity is the HTTP transport's own Idempotency-Key
# ledger for the two routes (grant, readings) whose wire body carries
# fields (task_class, seat_id) the store never receives at all -
# "idempotency identity covers the FULL canonical wire body, including
# fields the store does not keep" (finding N2). Scoped by route: an
# Idempotency-Key is a client-chosen UUID, unique in practice (it must
# equal the body's own request_id/event_id) but the route is included
# for defense in depth, not because a real collision is expected.
_DDL_V7 = (
    "CREATE TABLE limit_events(\n"
    " event_id TEXT PRIMARY KEY,\n"
    " account_id TEXT NOT NULL REFERENCES accounts(id),\n"
    " model_id TEXT REFERENCES models(id),\n"
    " turn_id TEXT NOT NULL,\n"
    " observed_at INTEGER NOT NULL,\n"
    " evidence_kind TEXT NOT NULL,\n"
    " resets_at INTEGER,\n"
    " exhausted_windows TEXT NOT NULL,\n"
    " block_id TEXT NOT NULL REFERENCES blocks(id),\n"
    " received_at INTEGER NOT NULL) STRICT;",
    "CREATE TABLE http_request_identity(\n"
    " route TEXT NOT NULL,\n"
    " idempotency_key TEXT NOT NULL,\n"
    " wire_hash TEXT NOT NULL,\n"
    " created_at INTEGER NOT NULL,\n"
    " PRIMARY KEY(route, idempotency_key)) STRICT;",
)

# The canonical version-7 migration script, kept for byte-for-byte comparison.
MIGRATION_V7 = "\n".join(_DDL_V7) + "\nPRAGMA user_version=7;\n"

# Card 5 fix round 3 (migration version 8, finding F2): v7 created
# limit_events EMPTY, forgetting every limit-event identity a pre-v7
# store already had. blocks.source_event_id is the only surviving trace
# of those pre-v7 CREATOR events (a joiner's own event_id was never
# recorded anywhere before v7 at all - that identity is unrecoverable,
# a documented residual limitation, not something this migration can
# fix). `legacy` flags a backfilled row's content as SYNTHESIZED, not
# real evidence: turn_id/exhausted_windows have no v6 equivalent at all
# (a fixed sentinel/empty placeholder), and observed_at/received_at fall
# back to the block's own updated_at (a real timestamp, just not the
# original report instant). record_limit_event never compares OR merges
# a legacy row's content - see its own docstring. The WHERE clause
# excludes receipt/settle-conflict blocks (reason IN
# ('receipt_conflict','overrun')): those also set source_event_id, but
# to a SETTLE event_id, never a limit-event one.
_DDL_V8 = (
    "ALTER TABLE limit_events ADD COLUMN legacy INTEGER NOT NULL DEFAULT 0;",
    "INSERT INTO limit_events(event_id, account_id, model_id, turn_id, "
    "observed_at, evidence_kind, resets_at, exhausted_windows, block_id, "
    "received_at, legacy) "
    "SELECT source_event_id, account_id, model_id, "
    "'00000000-0000-4000-8000-000000000000', updated_at, reason, "
    "retry_at, '[]', id, updated_at, 1 FROM blocks WHERE "
    "source_event_id IS NOT NULL AND reason IN "
    "('codex_exhausted','claude_exhausted','provider_usage_429') "
    "AND source_event_id NOT IN (SELECT event_id FROM limit_events);",
)

# The canonical version-8 migration script, kept for byte-for-byte comparison.
MIGRATION_V8 = "\n".join(_DDL_V8) + "\nPRAGMA user_version=8;\n"

# Card L1c (migration version 9): a receipt on a SHADOW lease may say
# "unknown" for its token counts, so the three token columns of receipts
# allow NULL (their >= 0 check stays for a non-null value). Every other
# column, check, foreign key and the receipt_final partial index are the
# version-5 ones. SQLite cannot relax NOT NULL in place, so the table is
# rebuilt exactly like readings was in version 4: new table, copy every row
# (same values, same order of columns), drop, rename, recreate the index.
# Which leases may report NULL (shadow only) is enforced by settle, in the
# transaction that reads the lease; a CHECK cannot see the lease.
_DDL_V9 = (
    "CREATE TABLE receipts_new(\n"
    " event_id TEXT PRIMARY KEY,\n"
    " lease_id TEXT NOT NULL REFERENCES leases(id),\n"
    " gateway_id TEXT REFERENCES gateways(id),\n"
    " seq INTEGER,\n"
    " calls INTEGER NOT NULL CHECK(calls>=0),\n"
    " input_tokens INTEGER CHECK(input_tokens IS NULL OR input_tokens>=0),\n"
    " cached_input_tokens INTEGER "
    "CHECK(cached_input_tokens IS NULL OR cached_input_tokens>=0),\n"
    " output_tokens INTEGER "
    "CHECK(output_tokens IS NULL OR output_tokens>=0),\n"
    " actual_micro_eur INTEGER "
    "CHECK(actual_micro_eur IS NULL OR actual_micro_eur>=0),\n"
    " outcome TEXT,\n"
    " final INTEGER NOT NULL DEFAULT 0 CHECK(final IN (0,1)),\n"
    " observed_at INTEGER NOT NULL,\n"
    " received_at INTEGER NOT NULL) STRICT;",
    "INSERT INTO receipts_new(event_id, lease_id, gateway_id, seq, calls,\n"
    " input_tokens, cached_input_tokens, output_tokens, actual_micro_eur,\n"
    " outcome, final, observed_at, received_at)\n"
    " SELECT event_id, lease_id, gateway_id, seq, calls, input_tokens,\n"
    " cached_input_tokens, output_tokens, actual_micro_eur, outcome, final,\n"
    " observed_at, received_at FROM receipts;",
    "DROP TABLE receipts;",
    "ALTER TABLE receipts_new RENAME TO receipts;",
    "CREATE UNIQUE INDEX receipt_final ON receipts(lease_id) WHERE final=1;",
)

# The canonical version-9 migration script, kept for comparison.
MIGRATION_V9 = "\n".join(_DDL_V9) + "\nPRAGMA user_version=9;\n"


# Card R1-3a (migration version 10): the seven tables that keep a gateway's evidence (DESIGN-R1-3.md section 3, revision 2). TABLES ONLY: no
# ALTER, no rebuild, and no row of any kind (no approval, adoption, transition or chain state, and no backfill from old readings).
# No report ingest or admission code used them in this card (the import effect and the removal guard aside); R1-3b fills two of them. Money columns are whole numbers >= 0; a hash is 64 lowercase hex
# characters; a generation is 32. The definitions live here, in one place.
_HEX64 = "length({c})=64 AND {c} NOT GLOB '*[^0-9a-f]*'"
_HEX32 = "length({c})=32 AND {c} NOT GLOB '*[^0-9a-f]*'"

_DDL_V10 = (
    "CREATE TABLE gateway_epochs(\n"
    " gateway_id TEXT NOT NULL REFERENCES gateways(id),\n"
    " reader_incarnation TEXT NOT NULL CHECK(" + _HEX32.format(c="reader_incarnation") + "),\n"
    " ledger_epoch INTEGER NOT NULL CHECK(ledger_epoch>=0),\n"
    " generation TEXT NOT NULL CHECK(" + _HEX32.format(c="generation") + "),\n"
    " first_event_id TEXT NOT NULL REFERENCES readings(event_id),\n"
    " PRIMARY KEY(gateway_id, reader_incarnation, ledger_epoch)) STRICT;",
    "CREATE TABLE gateway_totals(\n"
    " event_id TEXT PRIMARY KEY REFERENCES readings(event_id),\n"
    " gateway_id TEXT NOT NULL REFERENCES gateways(id),\n"
    " reader_incarnation TEXT NOT NULL CHECK(" + _HEX32.format(c="reader_incarnation") + "),\n"
    " ledger_epoch INTEGER NOT NULL CHECK(ledger_epoch>=0),\n"
    " generation TEXT NOT NULL CHECK(" + _HEX32.format(c="generation") + "),\n"
    " seq INTEGER NOT NULL CHECK(seq>=0),\n"
    " period_start INTEGER NOT NULL CHECK(period_start>=0),\n"
    " period_end INTEGER NOT NULL CHECK(period_end>period_start),\n"
    " settled_micro_eur INTEGER NOT NULL CHECK(settled_micro_eur>=0),\n"
    " reserved_micro_eur INTEGER NOT NULL CHECK(reserved_micro_eur>=0),\n"
    " uncertain_micro_eur INTEGER NOT NULL CHECK(uncertain_micro_eur>=0),\n"
    " unattributed_micro_eur INTEGER NOT NULL CHECK(unattributed_micro_eur>=0),\n"
    " receipts_seq INTEGER CHECK(receipts_seq IS NULL OR receipts_seq>=0),\n"
    " quiet INTEGER NOT NULL CHECK(quiet IN (0,1)),\n"
    " held_over_reservation INTEGER NOT NULL CHECK(held_over_reservation>=0),\n"
    " price_hash TEXT NOT NULL CHECK(" + _HEX64.format(c="price_hash") + "),\n"
    " child_cap_hash TEXT NOT NULL CHECK(" + _HEX64.format(c="child_cap_hash") + "),\n"
    " gateway_flag INTEGER NOT NULL CHECK(gateway_flag IN (0,1)),\n"
    " observed_at INTEGER NOT NULL CHECK(observed_at>=0),\n"
    " received_at INTEGER NOT NULL CHECK(received_at>=0),\n"
    " CHECK(unattributed_micro_eur<=settled_micro_eur)) STRICT;",
    "CREATE INDEX gateway_totals_chain ON gateway_totals(gateway_id, reader_incarnation, ledger_epoch, seq);",
    "CREATE TABLE gateway_approvals(\n"
    " gateway_id TEXT NOT NULL REFERENCES gateways(id),\n"
    " price_hash TEXT NOT NULL CHECK(" + _HEX64.format(c="price_hash") + "),\n"
    " child_cap_hash TEXT NOT NULL CHECK(" + _HEX64.format(c="child_cap_hash") + "),\n"
    " approved_at INTEGER NOT NULL CHECK(approved_at>=0),\n"
    " operator TEXT NOT NULL,\n"
    " PRIMARY KEY(gateway_id, price_hash, child_cap_hash)) STRICT;",
    "CREATE TABLE gateway_adoptions(\n"
    " id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    " gateway_id TEXT NOT NULL REFERENCES gateways(id),\n"
    " event_id TEXT NOT NULL UNIQUE REFERENCES readings(event_id),\n"
    " reader_incarnation TEXT NOT NULL CHECK(" + _HEX32.format(c="reader_incarnation") + "),\n"
    " ledger_epoch INTEGER NOT NULL CHECK(ledger_epoch>=0),\n"
    " generation TEXT NOT NULL CHECK(" + _HEX32.format(c="generation") + "),\n"
    " period_start INTEGER NOT NULL CHECK(period_start>=0),\n"
    " seq INTEGER NOT NULL CHECK(seq>=0),\n"
    " settled_micro_eur INTEGER NOT NULL CHECK(settled_micro_eur>=0),\n"
    " reserved_micro_eur INTEGER NOT NULL CHECK(reserved_micro_eur>=0),\n"
    " uncertain_micro_eur INTEGER NOT NULL CHECK(uncertain_micro_eur>=0),\n"
    " unattributed_micro_eur INTEGER NOT NULL CHECK(unattributed_micro_eur>=0),\n"
    " price_hash TEXT NOT NULL CHECK(" + _HEX64.format(c="price_hash") + "),\n"
    " child_cap_hash TEXT NOT NULL CHECK(" + _HEX64.format(c="child_cap_hash") + "),\n"
    " adopted_at INTEGER NOT NULL CHECK(adopted_at>=0),\n"
    " operator TEXT,\n"
    " kind TEXT NOT NULL CHECK(kind IN ('operator','period_roll')),\n"
    " prev_adoption_id INTEGER REFERENCES gateway_adoptions(id),\n"
    " CHECK((kind='operator') = (operator IS NOT NULL))) STRICT;",
    "CREATE INDEX gateway_adoptions_gateway ON gateway_adoptions(gateway_id, id);",
    "CREATE TABLE gateway_readers(\n"
    " gateway_id TEXT NOT NULL REFERENCES gateways(id),\n"
    " reader_incarnation TEXT NOT NULL CHECK(" + _HEX32.format(c="reader_incarnation") + "),\n"
    " first_event_id TEXT NOT NULL REFERENCES readings(event_id),\n"
    " first_seen_at INTEGER NOT NULL CHECK(first_seen_at>=0),\n"
    " state TEXT NOT NULL CHECK(state IN ('pending','current','retired')),\n"
    " decided_by_adoption INTEGER REFERENCES gateway_adoptions(id),\n"
    " retired_at INTEGER CHECK(retired_at IS NULL OR retired_at>=0),\n"
    " PRIMARY KEY(gateway_id, reader_incarnation),\n"
    " CHECK((state='pending') = (decided_by_adoption IS NULL)),\n"
    " CHECK((state='retired') = (retired_at IS NOT NULL))) STRICT;",
    "CREATE UNIQUE INDEX gateway_readers_current ON gateway_readers(gateway_id) WHERE state='current';",
    "CREATE TABLE gateway_transitions(\n"
    " id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    " gateway_id TEXT NOT NULL REFERENCES gateways(id),\n"
    " kind TEXT NOT NULL CHECK(kind IN ('reader_changed','epoch_changed','epoch_generation_mismatch','price_hash_changed',"
    "'child_cap_hash_changed','figure_fell','unattributed_rose','sequence_conflict','reported_by_gateway','owner_changed')),\n"
    " event_id TEXT REFERENCES readings(event_id),\n"
    " adoption_id INTEGER REFERENCES gateway_adoptions(id),\n"
    " target_incarnation TEXT CHECK(target_incarnation IS NULL OR (" + _HEX32.format(c="target_incarnation") + ")),\n"
    " conflict_incarnation TEXT CHECK(conflict_incarnation IS NULL OR (" + _HEX32.format(c="conflict_incarnation") + ")),\n"
    " conflict_epoch INTEGER CHECK(conflict_epoch IS NULL OR conflict_epoch>=0),\n"
    " conflict_seq INTEGER CHECK(conflict_seq IS NULL OR conflict_seq>=0),\n"
    " detected_at INTEGER NOT NULL CHECK(detected_at>=0),\n"
    " resolved_by_adoption INTEGER REFERENCES gateway_adoptions(id),\n"
    " CHECK((event_id IS NULL) = (kind='owner_changed')),\n"
    " CHECK((kind='reader_changed') = (target_incarnation IS NOT NULL)),\n"
    " CHECK((kind IN ('sequence_conflict','epoch_generation_mismatch')) = (conflict_incarnation IS NOT NULL)),\n"
    " CHECK((conflict_incarnation IS NULL) = (conflict_epoch IS NULL) AND (conflict_epoch IS NULL) = (conflict_seq IS NULL)),\n"
    " UNIQUE(gateway_id, kind, event_id)) STRICT;",
    "CREATE UNIQUE INDEX gateway_transitions_reader ON gateway_transitions(gateway_id, target_incarnation) "
    "WHERE kind='reader_changed';",
    "CREATE INDEX gateway_transitions_open ON gateway_transitions(gateway_id, resolved_by_adoption);",
    "CREATE TABLE gateway_chain_state(\n"
    " gateway_id TEXT PRIMARY KEY REFERENCES gateways(id),\n"
    " current_adoption_id INTEGER REFERENCES gateway_adoptions(id),\n"
    " last_quiet_event_id TEXT REFERENCES readings(event_id),\n"
    " last_quiet_unattributed INTEGER CHECK(last_quiet_unattributed IS NULL OR last_quiet_unattributed>=0),\n"
    " late_reports INTEGER NOT NULL DEFAULT 0 CHECK(late_reports>=0),\n"
    " old_chain_reports INTEGER NOT NULL DEFAULT 0 CHECK(old_chain_reports>=0),\n"
    " duplicate_reports INTEGER NOT NULL DEFAULT 0 CHECK(duplicate_reports>=0),\n"
    " retired_reader_reports INTEGER NOT NULL DEFAULT 0 CHECK(retired_reader_reports>=0),\n"
    " CHECK((last_quiet_event_id IS NULL) = (last_quiet_unattributed IS NULL))) STRICT;",
)

# The tables of the evidence history, in creation order (the removal guards and the tests read this list).
EVIDENCE_TABLES = ("gateway_epochs", "gateway_totals", "gateway_approvals", "gateway_adoptions", "gateway_readers",
                   "gateway_transitions", "gateway_chain_state")

# The canonical version-10 migration script, kept for comparison (the tests build stores from the scripts).
MIGRATION_V10 = "\n".join(_DDL_V10) + "\nPRAGMA user_version=10;\n"

# Step N upgrades a store from version N-1 to version N.
_STEPS = {1: _DDL_V1, 2: _DDL_V2, 3: _DDL_V3, 4: _DDL_V4, 5: _DDL_V5,
          6: _DDL_V6, 7: _DDL_V7, 8: _DDL_V8, 9: _DDL_V9, 10: _DDL_V10}


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
    or empty file, 1 = card 1, 2 = card 2, 3 = card 3, 4 = card 2b-1) is
    brought up step by step to version 5 (card 4): the
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
