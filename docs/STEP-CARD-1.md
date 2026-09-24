# Step record — Card 1: standalone inventory, exactly three tables

Work order: tk-5b7abae06db5 (build: quota balancer card 1).
Seat: qwen-agenttalk-dev-1. Branch: `card-1` (built on `main` @ 705810c).
Date: 2026-09-24. Interpreter: CPython 3.14.6 (standard library only:
`sqlite3`, `argparse`, `json`, `uuid`, `re`, `os`, `sys`).

## What was built

- Package `quota_balancer` runnable as `python -m quota_balancer --db PATH`
  (installed alias `quota` declared in `pyproject.toml`), with the three
  verbs of the card: `init`, `config-import --file`, `status --json`.
  No agenttalk, no models, no scopes, no leases, no HTTP.
- `migrate.py` — migration version 1 executes the DDL of docs/DESIGN.md
  section 8 byte for byte (three STRICT tables: accounts, machines,
  gateways; `PRAGMA user_version=1`). Connections run with
  `foreign_keys=ON`; the database is opened in WAL. Version 0 migrates,
  version 1 is a no-op (idempotent `init`), any other version is rejected
  with `SCHEMA_UNSUPPORTED`.
- `validate.py` — strict validation of version-1 documents: closed key
  shapes at every level (all keys required, no extras), exact integer
  `version == 1` (booleans rejected), alias regex
  `[a-z][a-z0-9_-]{0,63}` (design section 7), duplicate-alias, reference
  (gateway -> account/machine) checks, exact integer micro-EUR in the
  signed 64-bit range (rejects floats, booleans, zero, negatives and
  overflow), vendor enum ovh/openai/anthropic, metered-only shares,
  fixed `utc_month` period, and share SUM per account <= its budget.
  A subscription account carries the required `budget_micro_eur` key with
  JSON null (the DDL requires NULL there).
- `store.py` — `init`, atomic inventory replacement (single transaction;
  validation precedes every write, so a rejected document leaves the
  store untouched), UUID retention for retained aliases, and the status
  projection (aliases only, sorted by alias, `allocated_micro_eur` = SUM
  of the account's gateway shares, 0 when none).
- `errors.py` / `cli.py` — exit 0 success, 2 invalid config/schema,
  3 store I/O; on failure one JSON object
  `{"error":{"code","message"}}` with codes `INVALID_CONFIG`,
  `SCHEMA_UNSUPPORTED`, `STORE_ERROR` and static messages only (no raw
  input or OS error text, including a last-resort guard around driver
  errors). The error object goes to stderr; `status --json` success
  output is the only thing written to stdout.
- Fixtures: `docs/fixtures/card1-inventory.json` (the design's valid
  example, byte-identical) and `docs/fixtures/card1-inventory-invalid.json`
  (same document, g2 share 40000001).
- README "Card 1" section with the demo commands.

Interpretation notes (where the card leaves a choice):
- Error JSON is printed on stderr; success output on stdout.
- Aliases must be unique within their collection (accounts, machines,
  gateways respectively).
- A missing or malformed import document (including undecodable text or
  pathologically deep nesting) is `INVALID_CONFIG`, exit 2; a missing or
  unopenable database file is `STORE_ERROR`, exit 3.
- `status`/`config-import` on an uninitialized store: no file -> exit 3
  (store), zero-byte/unknown file -> exit 2 (`SCHEMA_UNSUPPORTED`).
- `init` requires the parent directory to exist; a missing one is a
  store I/O failure (exit 3).

Corrections made while completing the card (the working tree started as
an uncommitted earlier draft of this same work order):
- `_is_int` used `isinstance(value, int)`, which accepts booleans
  (bool subclasses int); `budget_micro_eur: true` was silently stored as
  1. Now an exact `type(value) is int` check, per the design's
  "reject booleans-as-integers".
- The share-sum check compared against twice the budget, so an over-budget
  import was accepted. Now share SUM per account <= its budget, exactly
  as the card states.
- Test methods renamed to the idiomatic `test_`-prefixed form and a
  `load_tests` shim removed, so plain discovery finds them.
- `config-import` now also maps `RecursionError` (deeply nested JSON) to
  `INVALID_CONFIG` instead of an uncaught traceback.
- `privacy_1` extended with a byte-exact status assertion and a
  store-IO sentinel case (sentinel in the DB path must not be echoed).

## Tests

Command (from the repository root):

```
python -m unittest discover -s tests -v
```

All 9 tests passed (`Ran 9 tests ... OK`), run in the foreground with
CPython 3.14.6. The five card-named checks appear with the `test_`
prefix; the bracketed names are the card's names:

| Test (tests/test_card1.py) | Card check | Result |
|---|---|---|
| `test_init_is_idempotent` (Card1Tests) | init_is_idempotent | ok |
| `test_shares_cannot_exceed_account` (Card1Tests) | shares_cannot_exceed_account | ok |
| `test_invalid_config_rolls_back` (Card1Tests) | invalid_config_rolls_back | ok |
| `test_money_is_integer` (Card1Tests) | money_is_integer | ok |
| `test_privacy_1` (Card1Tests) | privacy_1 | ok |
| `test_status_shape_is_exact` (Card1ContractTests) | (contract) exact status JSON | ok |
| `test_schema_version_rejected` (Card1ContractTests) | (contract) unknown/newer version | ok |
| `test_uuids_retained_for_retained_aliases` (Card1ContractTests) | (contract) UUID retention | ok |
| `test_uninit_store_errors` (Card1ContractTests) | (contract) uninitialized store | ok |

Both corrections above were mutation-verified: before the fix, the
invalid fixture imported with exit 0 (test red) and
`budget_micro_eur: true` with a small share stored as 1 (probe exit 0);
after the fix, both are rejected with exit 2 and the suite is green.

## Demo transcript (import, restart, identical status)

Run from the repository root; the database lives in the seat scratch
directory. Every step is a separate process against the same database
file, so step 4 is the restart case. `$PY` = the CPython 3.14.6
interpreter; `$DB` = the scratch demo path `…/tk-5b7abae06db5/demo/quota.db`.

```console
$ mkdir -p <scratch>/demo && "$PY" -m quota_balancer --db "$DB" init
exit=0                       # silent; idempotent (re-running is a no-op)

$ "$PY" -m quota_balancer --db "$DB" config-import --file docs/fixtures/card1-inventory.json
exit=0                       # silent

$ "$PY" -m quota_balancer --db "$DB" status --json        # first read (process 3)
exit=0
{"schema_version":1,"accounts":[{"alias":"meter","kind":"metered","budget_micro_eur":100000000,"allocated_micro_eur":100000000}],"machines":["desktop","worker"],"gateways":[{"alias":"g1","account":"meter","machine":"desktop","share_micro_eur":60000000},{"alias":"g2","account":"meter","machine":"worker","share_micro_eur":40000000}]}

$ "$PY" -m quota_balancer --db "$DB" status --json        # restart: fresh process (process 4)
exit=0
$ cmp status-1.json status-2.json
status-1.json and status-2.json are byte-identical

$ "$PY" -m quota_balancer --db "$DB" config-import --file docs/fixtures/card1-inventory-invalid.json
{"error":{"code":"INVALID_CONFIG","message":"config document is invalid"}}
exit=2
```

The status shows the account budget and the allocated amount of
100000000 micro-EUR, identical before and after the restart. The invalid
fixture (g2 share 40000001, sum above the budget) is rejected with exit
2 and the prior data stays intact (asserted by
`test_shares_cannot_exceed_account`).

## Commits on `card-1`

- `28a9df0` feat(card-1): standalone inventory store with three tables
- `6ca2cc5` docs(readme): Card 1 usage, contract and demo commands
- (this step record)
