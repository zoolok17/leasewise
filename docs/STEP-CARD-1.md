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

## Fix round (2026-09-24, task tk-35104a3beea0)

Cold reads of 350478c: frontend-dev NOT READY (2 MAJOR), qwen reviewer
READY; the stricter verdict governed. All fixes landed in `32e2af5`
(code + tests, +417/-108 across 4 files), on top of 350478c.

### Findings -> fixes -> tests

| # | Finding (reader) | Fix | Test evidence |
|---|---|---|---|
| 1 | MAJOR: `migrate.py` ran the DDL via `executescript`, which is not one transaction; a stop mid-migration leaves a half-built store (frontend-dev) | `migrate()`: preliminary version read (no lock); a version-0 store is switched to WAL first and the switch is checked (WAL cannot run inside a transaction); then `BEGIN IMMEDIATE`, version re-read inside the lock, the three DDLs + `user_version=1` commit as one transaction; any failure rolls back | `test_mid_migration_failure_leaves_no_half_store` — **red before the fix** (after an injected mid-migration failure the store still contained `accounts` and `machines`; now only the foreign table remains, `user_version` still 0, exit 3) |
| 2 | MAJOR: `store.py` read the old alias->UUID maps outside the import transaction, so a concurrent import could commit new UUIDs between the read and the replacement (frontend-dev) | The map reads moved inside the `BEGIN IMMEDIATE` replacement transaction (version re-check first, then reads, then DELETE/INSERT, COMMIT) | Retention behavior pinned by `test_uuids_retained_for_retained_aliases`; the lock-before-read ordering is structural (the SELECTs sit textually inside the `BEGIN IMMEDIATE`...`COMMIT` block). A deterministic interleaving test would need driver-level hooks or timing-dependent races — see "Not fixed / not testable" below |
| 3 | MINOR: concurrent `init` on a fresh file could race (frontend-dev) | Same discipline as #1: the version is re-read inside `BEGIN IMMEDIATE`; the latecomer observes version 1 and commits a no-op | No dedicated test — see "Not fixed / not testable" |
| 4 | MINOR: `status` ran the version check and three projections as separate autocommit reads, not one snapshot (frontend-dev) | Version check + the three projections now run in a single read transaction (`BEGIN`...`COMMIT`) | Output shape pinned by `test_status_shape_is_exact`; snapshot semantics is structural |
| 5 | MINOR: the WAL pragma ran before the version check — a store whose version is rejected was still modified (the journal-mode switch rewrites the file header) (frontend-dev) | Read paths (`status`, `import_config`) no longer touch journal mode at all; WAL is set only by the migration, after the preliminary read confirms version 0, and the returned mode is checked | `test_rejected_schema_store_left_unchanged` — **red before the fix** (every verb on a `user_version=2` store rewrote the header, bytes 01 01 -> 02 02, i.e. switched it to WAL; now the file is byte-identical and no `-wal`/`-shm` side files appear) |
| 6 | MINOR: test gaps — M2 (gateway on a subscription account), M5/M6 (duplicate gateway/account aliases), the untested `kind='subscription'` branch (both readers) | New tests in `Card1FixRoundTests` | `test_gateway_on_subscription_account_rejected` (**red under M2**), `test_duplicate_gateway_alias_rejected` (**red under M5**), `test_duplicate_account_alias_rejected` (**red under M6**, account and machine cases), `test_subscription_account_import`, `test_validation_edges` (vendor enum, missing key, zero money) |

### Red-before-fix evidence (code at 350478c, new tests added)

Command: `python -m unittest discover -s tests` -> `Ran 16 tests ...
FAILED (failures=2)`. The two failures were exactly the two code-defect
tests:

- `test_mid_migration_failure_leaves_no_half_store`: tables assertion
  failed — `accounts` and `machines` survived the failed migration.
- `test_rejected_schema_store_left_unchanged`: byte assertion failed —
  the store file changed under a rejected verb (WAL header rewrite).

Note: this test initially passed on unfixed code by accident (the store
was already WAL from `init`, so the old code's `journal_mode=WAL` was a
no-op); the seed now downgrades the rejected store to `DELETE` journal
mode first, which makes the old code's header rewrite observable.

### Mutation verification (code at 32e2af5)

Each mutation was applied, the named test run (expected red), the
mutation reverted, and the full suite re-run green. Targeted runs used
`python -m unittest test_card1.Card1FixRoundTests.<name> -v` from
`tests/`:

- **M2** (drop the metered-only gateway check):
  `test_gateway_on_subscription_account_rejected` FAILED at
  `assertEqual(bad.returncode, 2)` with actual **1** — the CLI crashed
  with an unhandled `TypeError` (`int > NoneType`) in the share-SUM
  check, because the gateway referenced a subscription account whose
  budget is null. Side effect noted: the SUM check relies on the
  metered-only guard; no separate `None` guard is added (a subscription
  account has no budget to allocate — the guard is the contract). If a
  later card relaxes metered-only shares, the SUM check must be
  revisited then.
- **M5** (drop the duplicate gateway alias check):
  `test_duplicate_gateway_alias_rejected` FAILED at the same assertion
  with actual **3** — the duplicate reached the DB and the `UNIQUE`
  constraint failed as `STORE_ERROR` instead of `INVALID_CONFIG`.
- **M6** (drop the duplicate account alias check):
  `test_duplicate_account_alias_rejected` FAILED with actual **3** for
  the account case (DB `UNIQUE` on `accounts.alias`); the machine case
  in the same test still exits 2 (its check was not mutated).

Full suite after every revert: `python -m unittest discover -s tests`
-> `Ran 16 tests ... OK` (16/16; environment CPython 3.14.6,
SQLite 3.50.4).

Also verified: the canonical `MIGRATION_V1` string is byte-identical to
the 350478c value (723 chars, compared via `ast.literal_eval` of the
old source), so the DDL the reviewers compared is unchanged.

### NIT dispositions (both readers)

Fixed in 32e2af5:

- Version checked before the closed-shape check (`validate.py`): a
  document at an unknown version is a schema problem even if its shape
  drifted with it.
- Duplicate JSON keys rejected at every level (`object_pairs_hook`).
- BOM accepted (`utf-8-sig`) — the bytes after the BOM are exactly what
  `utf-8` would have parsed.
- Connection no longer leaks if the FK pragma fails.
- WAL switch result checked in the migration.
- `init` requires SQLite >= 3.37 (STRICT tables) before touching the
  file, and says so in the README.

Not fixed (reasons):

- **argparse usage errors** (unknown verb, missing `--db`): standard
  argparse behavior kept (usage to stderr, exit 2). The card contract
  defines exactly three error codes for domain/store failures and their
  static messages; it does not define a usage-error path, and there is
  no code that fits — exit 3 would misreport a store I/O failure and a
  fourth code is a contract change beyond this card. The privacy
  invariant covers store/document content; argparse echoes the
  operator's own command line, which is the operator's own text.
- **Blanket `except Exception` in `cli.main`**: the three verbs already
  wrap their boundaries (`QuotaError` for the domain, `sqlite3.Error`
  for the driver). A catch-all would convert real bugs (e.g. the
  `TypeError` the M2 probe exposed) into exit 3 "store operation
  failed", hiding regressions behind a contract code.

### Not testable deterministically (discipline pinned structurally)

- Concurrent init / concurrent import interleavings: a deterministic
  test would need driver-level hooks (not available on stdlib sqlite3
  in this card) or timing-dependent races, which are flaky by
  construction. The discipline is pinned structurally instead: the
  version re-read and the alias->UUID reads sit textually inside the
  `BEGIN IMMEDIATE`...`COMMIT` block, and the externally observable
  properties (no half-built store, rejected store byte-unchanged, UUID
  retention) are pinned by the tests above.

### Interpretation confirmed

A missing or unreadable `--file` is `INVALID_CONFIG`, exit 2 — the
document is the input under validation, and a file that cannot be read
is a document that cannot be validated (reconfirming the card-1 call).

## Commits on `card-1`

- `28a9df0` feat(card-1): standalone inventory store with three tables
- `6ca2cc5` docs(readme): Card 1 usage, contract and demo commands
- `350478c` docs(card-1): step record with test results and demo transcript
- `32e2af5` fix(card-1): transactional migration, lock-first import, single-snapshot status
- (this fix-round update, with the README `--db` ordering note)
