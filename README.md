# agenttalk-quota

A quota and token balancer that agenttalk consumes: accounts, subscriptions and pay-per-token endpoints, limits, per-turn leases, meter readings and routing policy.

Design (v3, accepted for build 2026-09-24): docs/DESIGN.md. Built card by
card (section 8); card 1 = standalone inventory, card 2 = the reading
reducer over it.

## Card 1 — standalone inventory, exactly three tables

A standalone CLI over one SQLite database. Standard library only
(`sqlite3`, `argparse`, `json`, `uuid`). No agenttalk, no models, no
scopes, no leases, no HTTP — those belong to later cards.

Requirements: Python 3.11+ (stdlib only). From the repository root:

```console
python -m quota_balancer --db quota.db init
python -m quota_balancer --db quota.db config-import --file docs/fixtures/card1-inventory.json
python -m quota_balancer --db quota.db status --json
```

(An installed copy exposes the same entry point as `quota`.)

Note: `--db` is a global option and must precede the verb
(`--db X init`, not `init --db X`). The store's SQL also requires
SQLite >= 3.37 (STRICT tables); `init` reports a store error on an
older SQLite instead of creating a half-specified schema.

- `init` — create the store at migration version 1 (idempotent; WAL,
  foreign keys on). Re-running it is a no-op.
- `config-import --file DOC` — validate a version-1 document (closed
  key shapes, alias regex `[a-z][a-z0-9_-]{0,63}`, duplicate-alias,
  reference, exact integer micro-EUR in the signed 64-bit range,
  metered-only shares, share SUM per account <= its budget) and replace
  the inventory atomically, retaining UUIDs for retained aliases.
- `status --json` — print the inventory projected to aliases only,
  sorted by alias:

  ```json
  {"schema_version":1,"accounts":[{"alias":"meter","kind":"metered","budget_micro_eur":100000000,"allocated_micro_eur":100000000}],"machines":["desktop","worker"],"gateways":[{"alias":"g1","account":"meter","machine":"desktop","share_micro_eur":60000000},{"alias":"g2","account":"meter","machine":"worker","share_micro_eur":40000000}]}
  ```

Exit codes: 0 success; 2 invalid config or unsupported schema version;
3 store I/O failure. On failure the tool prints one JSON object to
stderr — `{"error":{"code","message"}}` with code `INVALID_CONFIG`,
`SCHEMA_UNSUPPORTED` or `STORE_ERROR` — and no raw input or OS error
text. `status` success output is the only thing written to stdout.

Demo (import, restart, identical status): run `init`, then
`config-import --file docs/fixtures/card1-inventory.json`, then
`status --json`. Each invocation is a fresh process against the same
database file, so a second `status --json` after the import is the
restart case; the output is byte-identical and shows the account
budget and allocated amount of 100000000 micro-EUR. Fixtures:
`docs/fixtures/card1-inventory.json` (valid) and
`docs/fixtures/card1-inventory-invalid.json` (g2 share 40000001, which
pushes the share sum above the account budget).

Tests: `python -m unittest discover -s tests -v` (46 tests across both
cards — the card-1 suite, including the carry-over, plus the card-2 named
checks, reducer rules, and validation/connectivity checks; see
docs/STEP-CARD-1.md and docs/STEP-CARD-2.md).

## Card 2 — reading reducer

The same standalone CLI, extended. It ingests reporter fixtures and
reduces them into per-account windows and a per-gateway report, projected
to aliases only. Standard library only. No agenttalk, no models, no
scopes, no leases, no HTTP — still fixture only.

The database migrates from version 1 to version 2 (a v1 store is brought
up by `init`; a v0 store goes straight to 2). New tables `readings` and
`windows`; `gateways` gains the report columns and `accounts` the
coverage/idle-proof columns.

```console
python -m quota_balancer --db quota.db init
python -m quota_balancer --db quota.db config-import --file docs/fixtures/card1-inventory.json
python -m quota_balancer --db quota.db reading-import --file readings.json
python -m quota_balancer --db quota.db set-coverage --account meter --complete true
python -m quota_balancer --db quota.db reconcile --gateway g1
python -m quota_balancer --db quota.db status --json
```

- `reading-import --file DOC` — validate a version-2 document (closed key
  shapes, machine/account/window-key local aliases, `event_id` as a local
  UUID, exact integer `used_bps` in 0..10000, every integer bounded to the
  signed-64 range, policy hashes as fixed-length hex digests, kind
  `cli_window` or `gateway_snapshot`, no observations more than 30 seconds
  in the future) and, in one transaction, reduce it: dedup by `event_id`
  (a replay with matching content is a no-op, the same id with different
  content is `IDEMPOTENCY_CONFLICT`), keep the source observation time,
  hold the high-water utilization per reset epoch (a later epoch restarts
  from its first post-reset observation; an older epoch is a no-op), keep
  the explicit confidence states (`observed` requires a value and a reset;
  `unknown` carries nulls and never refreshes a window that already has a
  known value; a legacy `stale` sample arrives pre-mapped by the reporter
  as `observed` + `source_stale=true`, with its value and source time
  preserved), and advance the gateway report (the snapshot must bind to
  the gateway's own machine and account; monotonic `seq`; an epoch or
  policy-hash change needs reconciliation, which stays set until the
  operator's `reconcile` verb). An ambiguous source (no account) is
  recorded but reduced into no account.
- `set-coverage --account A --complete {true,false}` — operator-only; the
  only thing that ever writes `coverage_complete`. Telemetry never sets
  it.
- `reconcile --gateway G` — operator-only; clears a gateway's
  `reconciliation_required` state. Telemetry never clears it (an epoch or
  policy-hash change re-raises it on the next report).
- `status --json` — the card-1 inventory plus, per account,
  `coverage_complete` and its windows (window key, `used_bps`,
  `resets_at`, `confidence`, `source_stale`, `observed_at`, `age_ms`,
  `current`, `freshness_reason`), and, per gateway, the report
  (`state`, `last_report_at`, `ledger_epoch`, `last_seq`,
  `period_start`, `period_end`, `policy_hashes`, `sized_ready`).
  Freshness is fail-closed in this card: with coverage incomplete the
  600-second wall cap always applies (a sample older than 600 seconds is
  `current=false`, `freshness_reason=wall_cap`), and a gateway report
  older than 120 seconds reads `state=stale` (METER_STALE); the
  idle-current proof that would lift the cap is a later card's.

Exit codes: 0 success; 2 invalid config/schema or an idempotency
conflict; 3 store I/O failure. On failure the tool prints one JSON object
to stderr — `{"error":{"code","message"}}` with code `INVALID_CONFIG`,
`SCHEMA_UNSUPPORTED`, `STORE_ERROR` or `IDEMPOTENCY_CONFLICT` — with
static messages only.

Demo (reordered and idle samples): run `init`, `config-import`, then
`reading-import` a document where the later observation arrives before
the earlier one (the window takes the high-water usage and the latest
source time regardless of arrival order), and a second document with a
~700-second-old sample (the window reads `wall_cap`). A second `status
--json` after a process restart re-reads the same reduced state — only the
read-time `age_ms` advances. See docs/STEP-CARD-2.md for the transcript.

Tests: the five named checks (`reading_replay`, `stale_mapping`,
`coverage_fallback`, `account_isolation`, `privacy_2`) plus the reducer
rules; the named tests are shown red at the migration-only commit and
green with the reducer — see docs/STEP-CARD-2.md.
