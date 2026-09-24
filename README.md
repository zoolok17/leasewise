# agenttalk-quota

A quota and token balancer that agenttalk consumes: accounts, subscriptions and pay-per-token endpoints, limits, per-turn leases, meter readings and routing policy.

Design (v3, accepted for build 2026-09-24): docs/DESIGN.md. Built card by card (section 8); card 1 = standalone inventory.

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

Tests: `python -m unittest discover -s tests -v` (9 tests, including
the five card-named checks).
