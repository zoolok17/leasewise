# Step record — Card 2: reading reducer

Work order: tk-64f63e3a29c7 (build: quota balancer card 2 + one card-1
carry-over). Seat: qwen-quota-dev-1. Branch: `card-2` (built on `main` @
de597a5). Date: 2026-09-24. Interpreter: CPython 3.14.4 (standard library
only: `sqlite3`, `argparse`, `json`, `os`, `re`, `sys`, `time`).

The card is the reading reducer from docs/DESIGN.md section 8, row 2,
plus section 5's reading rules and one small card-1 carry-over. No live
provider calls, no HTTP, no agenttalk — fixture CLI only.

## Card-1 carry-over (committed first, on its own)

`fix(card-1): make concurrent init idempotent (WAL switch retry)` —
9b80518.

Parallel `init` on a fresh store could exit 3: the WAL journal-mode
switch takes a brief exclusive lock on the database file and, unlike
ordinary statements, does not wait through the busy handler, so a
sibling mid-DDL could make it raise "database is locked". `migrate.py`
now retries the `PRAGMA journal_mode=WAL` switch on the busy condition
until a bounded deadline (5 seconds, 20 ms sleep); a persistent lock then
surfaces as a store error instead of a half-switched store. A new test
(`Card1CarryOverTests.test_concurrent_init_all_exit_zero`) runs several
`init` processes in parallel over repeated rounds and requires every
process to exit 0.

## What was built

- **Schema, migration version 2** (`migrate.py`). Card 1's version-1 DDL
  is kept byte for byte; version 2 adds the card-2 tables and the
  gateway-report / account-coverage columns of section 7, all inside the
  same transactional discipline as card 1 (preliminary version read takes
  no lock; a store below the current version is switched to WAL first and
  the switch is checked; `BEGIN IMMEDIATE`; the version is re-read inside
  the lock so concurrent inits serialize; every pending step's DDL plus
  the version stamp commit as one transaction). A store at a newer,
  unknown version is rejected before anything is written or switched and
  left byte-for-byte unchanged. The new objects:
  - `readings(event_id PK, machine_id FK, account_id nullable FK,
    seat_id, kind, observed_at, received_at, payload)`. `account_id` is
    nullable in this card so an ambiguous-source reading (attribution
    unknown) can still be recorded for dedup/audit without being reduced
    into any account; `seat_id` carries no foreign key here because the
    seats table is a later card's.
  - `windows(account_id, window_key, used_bps, resets_at, observed_at,
    confidence IN ('observed','unknown'), source_stale, covered_seq,
    source_event_id; PK (account_id, window_key))`.
  - `gateways` gains the reporter-owned report columns
    (`ledger_epoch, last_seq, period_start, period_end, policy_hashes,
    last_report_at, state, sized_ready`).
  - `accounts` gains the coverage/idle-proof columns
    (`window_keys, activity_seq, coverage_complete (operator-only,
    default false), required_reporters, outage_local_concurrency_cap`).
  - `PRAGMA user_version=2`.
- **`reduce.py`** — the pure reducer (no I/O, clock injected). It
  implements section 5's reading rules:
  - **Dedup by source id, source time preserved.** The window stores the
    source `observed_at`; the ingest time is recorded separately in
    `readings.received_at` and never refreshes the observation.
  - **High-water per reset epoch, never lower.** Within the same reset
    epoch the utilization is the running maximum. An observation from an
    older (out-of-order) epoch is a no-op. A *later* epoch is restarted
    by its first post-reset observation; time passage alone proves no
    recovery.
  - **Explicit confidence vocabulary**: the input states are exactly
    `observed` and `unknown` (the card-2 input contract), plus the separate
    `source_stale` boolean. A legacy `stale` sample is mapped *by the
    producer* (the reporter, card 7b) to `observed` with
    `source_stale=true`; card 2 stores the flag and preserves the value and
    the source timestamp. The reducer does not accept `stale` as a
    confidence value.
  - **Gateway report.** Ledger `seq` is monotonic — a non-increasing seq
    is a no-op. A change of ledger epoch or policy hashes marks the
    report `reconciliation_required`. `last_report_at` is the source
    time; at read time a report older than the 120-second fail-closed age
    resolves to `stale` (METER_STALE), and a gateway with no report
    resolves to `none`.
  - **Freshness at read time.** Every window's age is computed at status
    time. With coverage incomplete the 600-second wall cap always
    applies: a window whose source observation is older than 600 seconds
    is `current=false` with `freshness_reason=wall_cap`.
- **`validate_readings.py`** — closed-shape validation of version-2
  reading documents (every key required, none extra, at every level),
  the alias regex for machine/account, an exact-integer `used_bps` in the
  0..10_000 range, a kind enum `cli_window` / `gateway_snapshot`, and a
  rejection of observations more than 30 seconds in the future.
- **`store.py`** — `reading_import` (one `BEGIN IMMEDIATE` transaction:
  resolve every reference against the inventory, then for each reading
  dedup by `event_id` and reduce) and `set_coverage` (operator-only).
  Dedup: a replay whose content matches the stored reading is a no-op;
  the same `event_id` with different content is
  `IDEMPOTENCY_CONFLICT` (exit 2) and leaves the store untouched. A
  dangling machine/account reference is `INVALID_CONFIG`. An
  ambiguous-source reading (no account) is recorded but reduces into no
  account. `status` now projects, per account, `coverage_complete` and
  the per-window rows (window key, used_bps, resets_at, confidence,
  source_stale, observed_at, age_ms, current, freshness_reason) and, per
  gateway, the report (state, last_report_at, ledger_epoch, last_seq,
  period_start, period_end, policy_hashes, sized_ready). All projections
  still use aliases only.
- **`errors.py`** — adds `IDEMPOTENCY_CONFLICT` (exit 2) and its static
  message; the other card-1 codes and the static-message discipline are
  unchanged.
- **`cli.py`** — three new verbs: `reading-import --file`,
  `set-coverage --account --complete`, and (fix round) `reconcile
  --gateway`.
- **README** "Card 2" section with the demo commands.
- **`tests/test_card2.py`** — the five named checks plus the reducer
  rules (see Tests).

Interpretation notes (where the card leaves a choice):
- **The wall cap is always applied in card 2.** The design's
  idle-current proof (F07) that would lift the cap needs
  `activity_seq`/`covered_seq` turn tracking (a grant increments the
  sequence, a post-turn reading certifies it), which belongs to the lease
  cards onward. Card 2 stores and reports `coverage_complete` but never
  infers it and does not yet use it to lift the cap; whenever the proof
  is missing — always, in this card — the 600-second wall cap applies.
  "Missing proof uses the wall cap," exactly as the design says.
- **`required_reporters`, `activity_seq`, `window_keys`, and
  `outage_local_concurrency_cap` are schema-only in card 2.** The columns
  exist (per section 7's schema) so a later card can populate them
  without a further migration; card 2's CLI neither writes nor reads them
  except that `coverage_complete` is surfaced in `status`.
- **The `state` column stores the report's stored state
  (`ok`/`reconciliation_required`); the `stale` and `none` states are
  derived at read time** from `last_report_at` and the 120-second age, so
  a report that is fresh now reads as its stored state and one that is
  old reads as `stale` without rewriting the row.
- **A missing or unreadable `--file` is `INVALID_CONFIG` (exit 2)** and a
  missing or unopenable database is `STORE_ERROR` (exit 3), matching the
  card-1 calls.

## Tests

Command (from the repository root):

```
python -m unittest discover -s tests -v
```

All 28 tests passed (`Ran 28 tests ... OK`), run in the foreground with
CPython 3.14.4 / SQLite 3.46.1: the 17 card-1 tests (including the
carry-over) plus the 11 card-2 tests. The five card-named checks appear
with the `test_` prefix; the bracketed names are the card's names.

| Test (tests/test_card2.py) | Card check | Result |
|---|---|---|
| `test_reading_replay` (NamedChecks) | reading_replay | ok |
| `test_stale_mapping` (NamedChecks) | stale_mapping | ok |
| `test_coverage_fallback` (NamedChecks) | coverage_fallback | ok |
| `test_account_isolation` (NamedChecks) | account_isolation | ok |
| `test_privacy_2` (NamedChecks) | privacy_2 | ok |
| `test_high_water_same_epoch_never_decreases` (ReducerRules) | (rule) high-water | ok |
| `test_new_epoch_requires_post_reset_observation` (ReducerRules) | (rule) new epoch | ok |
| `test_reordered_samples` (ReducerRules) | (rule) reorder | ok |
| `test_gateway_report_meter_stale` (ReducerRules) | (rule) METER_STALE | ok |
| `test_rejects_future_observation` (ReducerValidation) | (contract) future reject | ok |
| `test_rejects_unknown_window_field` (ReducerValidation) | (contract) closed shape | ok |

What each named check pins:

- **reading_replay** — a replay of the same `event_id` with identical
  content is a no-op (exit 0, store unchanged); the same `event_id` with
  different content is `IDEMPOTENCY_CONFLICT` (exit 2, store unchanged).
- **stale_mapping** — a `source_stale` reading retains the value, the
  *source* `observed_at`, and the `source_stale` flag; the store keeps
  the source time and a distinct, later ingest time
  (`received_at > observed_at`).
- **coverage_fallback** — with incomplete coverage (never set) and a
  sample older than 600 seconds, the window is stale
  (`current=false`, `wall_cap`); a fresh sample within the cap is
  current. Telemetry cannot set `coverage_complete`: it stays false, and
  it stays whatever the operator last set it to after further imports.
- **account_isolation** — an ambiguous-source reading (no account)
  reduces into neither account: two accounts sharing a machine both have
  empty window lists, while the reading is still recorded for dedup.
- **privacy_2** — injected secret / provider-id / path sentinels never
  appear in status or error output (a sentinel used as a bad account or
  machine reference is a static `INVALID_CONFIG`; a sentinel in the DB
  path is a static `STORE_ERROR`); only local aliases survive in the
  status.

### Red-before evidence

The card-2 code was split so the named tests could be shown red before
the reducer existed. At the migration-only commit 891d50e the store has
the v2 tables but the `reading-import` / `set-coverage` verbs and the
reducer are absent. Copying `tests/test_card2.py` into a scratch worktree
checked out at 891d50e and running it:

```
python -m unittest discover -s tests -v -p test_card2.py
Ran 11 tests ... FAILED (failures=11)
```

All eleven fail — the CLI rejects `reading-import` (argparse "invalid
choice") and the v2 windows/report projections do not exist yet. At the
reducer commit c403190 (and here, with the tests at 3704571) the same
file runs green: `Ran 11 tests ... OK`. The five named checks and the
reducer rules each go red at 891d50e and green at c403190.

## Demo transcript (reordered and idle samples)

Run from the repository root; the database and the two generated fixtures
live in the seat scratch directory. The fixtures use now-relative source
times so the freshness is stable; the reset epoch is the same for every
sample. `$PY` = CPython 3.14.4; `$DB` = the scratch demo path
`…/tk-64f63e3a29c7/demo2/quota.db`.

```console
$ python -m quota_balancer --db "$DB" init
exit=0                       # silent; idempotent

$ python -m quota_balancer --db "$DB" config-import --file docs/fixtures/card1-inventory.json
exit=0                       # silent

# Reordered samples, one process/transaction: the LATER observation
# (r-later, used 6000, observed 10s ago) arrives first, the EARLIER one
# (r-earlier, used 4000, observed 40s ago) second.
$ python -m quota_balancer --db "$DB" reading-import --file <demo>/reorder.json
exit=0

# Idle sample: ~700 seconds old, past the 600-second wall cap.
$ python -m quota_balancer --db "$DB" reading-import --file <demo>/idle.json
exit=0

$ python -m quota_balancer --db "$DB" status --json        # process N
exit=0
$ python -m quota_balancer --db "$DB" status --json        # restart: fresh process N+1
exit=0
```

Process N's `status` (abridged to the reduced fields; the two gateways
read `"report":{"state":"none",...}` because no gateway report was
imported):

```json
{"schema_version":2,"accounts":[{"alias":"meter","kind":"metered","budget_micro_eur":100000000,
"allocated_micro_eur":100000000,"coverage_complete":false,
"windows":[
 {"window_key":"w","used_bps":6000,"resets_at":<epoch>,"confidence":"observed",
  "source_stale":false,"observed_at":<later_obs>,"age_ms":~10475,
  "current":true,"freshness_reason":"fresh"},
 {"window_key":"w-idle","used_bps":500,"resets_at":<epoch>,"confidence":"observed",
  "source_stale":false,"observed_at":<idle_obs>,"age_ms":~700475,
  "current":false,"freshness_reason":"wall_cap"}]}]}
```

The **reorder** case shows the reducer is arrival-order independent:
window `w` takes the high-water usage (6000) and the *latest* source
time (`observed_at` of the later observation), even though the later
observation arrived first. The **idle** case shows the fail-closed
freshness: window `w-idle`, sampled ~700 seconds ago under incomplete
coverage, reads `current=false` with `freshness_reason=wall_cap`.

The restart (fresh process N+1) re-reads the same reduced state: a
`diff` of the two status outputs shows only the read-time `age_ms` values
advancing by a few milliseconds (10475 → 10548, 700475 → 700548); every
reduced field — `used_bps`, `resets_at`, `confidence`, `source_stale`,
`observed_at`, `current`, `freshness_reason`, `coverage_complete`, and
the gateway reports — is identical. `age_ms` is a read-time computation,
so it is not byte-stable across processes, by design (the design reports
"age … separately" from the stored value).

## Not testable deterministically (deferred to later cards, with the reason)

- **Idle-current proof (F07).** Lifting the 600-second wall cap requires
  the operator to set `coverage_complete` *and* the coordinator to prove
  no turn ran since the sample: no subsequent usage/admission, no
  active/uncertain turn, uninterrupted coverage of every machine/team,
  with `activity_seq` incremented by a grant and `covered_seq` certified
  by a post-turn reading. That turn tracking is the lease cards'
  (card 4 onward) — there is no grant or turn to track in card 2, so the
  proof can never be satisfied here and the wall cap always applies.
  Card 2 pins the always-cap behavior (the `coverage_fallback` test) and
  the operator-only flag (set by `set-coverage`, never by telemetry), but
  the lift path is not exercisable and is not claimed.
- **`required_reporters` / per-machine-per-team heartbeats and
  `outage_local_concurrency_cap`.** These are the topology and
  outage-slot columns (section 7); populating them needs the reporter
  heartbeats and the local outage slots of the later cards. Card 2 adds
  the columns so no further migration is needed and leaves them
  default-valued; there is no behavior to pin yet.
- **Concurrency of `reading_import`.** A deterministic interleaving
  test would need driver-level hooks (not available on stdlib `sqlite3`
  in this card) or timing-dependent races, which are flaky by
  construction. The discipline is pinned structurally instead: every
  reference read and the dedup check sit inside the `BEGIN IMMEDIATE`
  transaction, and the externally observable properties (a collision
  leaves the store untouched, a replay is a no-op, an ambiguous source
  updates no account) are pinned by the tests above.

## Fix round (two independent cold reads; lead dispositions 2026-09-24)

Both cold reads (READ A, READ B) came back NOT READY. The lead verified the
findings; the dispositions below were implemented as new commits on top of
876458a. Every new test went red at the pre-fix tree and green with the fix
(foreground suite: `python3 -m unittest discover -s tests -v`).

| Finding | Fix | Test(s) |
|---|---|---|
| BLOCKER (A+B): config-import delete-and-reinsert fails once a reading exists (FK) and silently resets coverage/report | Upsert: retained aliases updated on config-owned columns only; a removed alias is deleted only if unreferenced, else INVALID_CONFIG | `ConfigImportUpsert` (4 tests) |
| B-M1: a new epoch was accepted without a post-reset observation; an older sample could lower the high-water | A later epoch restarts only when `observed_at >= stored resets_at`; drift falls through to the same-epoch high-water | `test_new_epoch_rejected_before_reset` |
| B-M2: reconciliation_required self-cleared on the next report; a changed hash became the baseline | Sticky until the operator's explicit `reconcile --gateway` verb (new: `store.reconcile`); re-epoch/re-hash re-raise it | `test_reconciliation_sticky_epoch`, `test_reconciliation_sticky_hash` |
| B-M3: an out-of-range integer crashed with a driver overflow traceback (path leak) | Every integer in the reading validator bounded to the non-negative signed-64 range; a last-resort handler in `cli.main` emits the static error contract for any unexpected fault | `test_integers_bounded_to_signed_64` (one case per field) |
| B-M4: an unknown reading refreshed a known value's `observed_at` and flipped its confidence | `reduce_window` returns the row unchanged for an unknown reading over a window that has a known value | `test_unknown_reading_does_not_refresh_known_window` (+ upgrade guard `test_unknown_then_known_upgrades`) |
| A-MAJOR: no explicit sqlite busy timeout | Explicit `timeout=30.0` in `store._connect`; a held write lock is waited out and the operation proceeds, never a raw driver fault | `StoreConnectivity` (2 tests) |
| B-m1: seq compared before epoch | Epoch compared before seq; a new ledger's lower first seq is accepted | `test_new_epoch_lower_seq_accepted` |
| B-m2 = A-MINOR: gateway reports not bound to the gateway's own machine/account | A snapshot must match the gateway's own machine AND account (a null account can never bind): else INVALID_CONFIG | `test_gateway_report_bound_to_owner` |
| B-m3: negative timestamps/sequences accepted; `observed` accepted with nulls | All integers non-negative (see M3); `observed` requires a value AND a reset in every window | `test_rejects_negative_timestamps_and_sequences`, `test_observed_confidence_requires_values` |
| B-m4 (privacy): window key and policy hashes were unbounded free text, echoed in status | A window key must be a local alias; a policy hash must be a 64-char lowercase hex digest; all test window keys and hashes conformed | `test_window_key_and_policy_hash_are_bounded` |
| B-n3 (NIT): `event_id` not validated as a UUID | `event_id` must be a canonical lowercase local UUID (section 7: IDs are local UUIDs); all test event IDs are deterministic local UUIDs | `test_rejects_non_uuid_event_id` |

- **m5 (NIT, doc fix)**: the step record and README claimed the reducer
  "maps" the legacy `stale` state. The card-2 input vocabulary is
  `observed`/`unknown` only; the legacy mapping is the producer's (card 7b)
  responsibility. Both documents corrected.
- **n1 (NIT, reason, not widened)**: `readings.kind` carries
  `CHECK(kind IN ('cli_window','gateway_snapshot'))`. The section-7
  `usage` kind (a later card) cannot be added with `ALTER TABLE` —
  SQLite has no in-place CHECK modification — so that card's migration
  must rebuild the table. Card 2 stays closed to its two kinds; the
  rebuild is documented for that card.
- **n2 (NIT)**: the card-1 carry-over docstring said a latecomer init
  "observes version 1"; corrected to "the current schema version". A
  pre-init v1 store read yields `SCHEMA_UNSUPPORTED` with the static
  message; the "run init" hint is deliberately not in the message (the
  static-message contract is frozen by card-1 tests) — the code and this
  document point at `init`.
- **READ A MAJOR (local repro note)**: the crashed-writer hot-journal stall
  did not reproduce in this seat's environment (first access after a
  SIGKILL'd mid-DDL migration succeeded in ~0.1 s). The fix is the
  explicit, bounded busy timeout plus the held-lock behavior test, which
  pins the wait-and-proceed path the recovery relies on.

Full suite after the fix round: 46 tests, all green (foreground).

## Commits on `card-2`

- de597a5 base (origin/main, card 1 merged)
- 9b80518 fix(card-1): make concurrent init idempotent (WAL switch retry)
- 891d50e feat(card-2): schema v2 — readings/windows tables, report/coverage columns
- c403190 feat(card-2): reading reducer, fixture CLI, and status projection
- 3704571 test(card-2): named checks and reducer rules
- 876458a docs(card-2): step record, README section, and demo transcript
- e8d3d91 fix(card-2): make config-import an upsert (BLOCKER)
- dc19558 fix(card-2): cold-read fix round — reducer, report, and validator contracts
