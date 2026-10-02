# Step record - Card L1c: honest unknown usage for shadow leases, and an id lookup

Work item `card-l1c`, work order tk-4e9ba0dda3d9. Branch `card-l1c`, built on the
card-l1b head `3596ea4` (both cards change settle). A small, Leasewise-only card
and a prerequisite of card 7a (docs: `C:\lw\records\DESIGN-7A.md`, Part 6 Q1 and
Q3). Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only.

## In plain words

1. **An honest "unknown".** The observe-only reporter of card 7a often does not
   know how many tokens an attempt used. Leasewise forced it to send whole
   numbers (the database columns could not be empty), so "unknown" would have
   been stored as 0, which is a false fact. Now a report about a **shadow**
   lease may say "unknown" (`null`) for each of its three token counts. Every
   other kind of lease still must report whole numbers, because their usage
   feeds limits and money.
2. **Shadow usage never counts toward a limit.** A shadow lease only observes.
   Its reports, known or unknown, are now explicitly left out of every usage
   sum that admission uses. A missing number can never be added up as 0 and can
   never turn a total into "nothing" that disables a limit.
3. **An id lookup.** A new read-only command, `policy-ids --json`, lists the
   seat, model and task-class names together with the ids a lease request has
   to name. It shows nothing else.

No new route, no change to `status`.

## Files

- `quota_balancer/migrate.py` - migration version 9 (`_DDL_V9`, `MIGRATION_V9`),
  `SCHEMA_VERSION` 9.
- `quota_balancer/lease.py` - the static receipt check admits null tokens; settle
  decides who may use them; the two usage sums skip shadow leases.
- `quota_balancer/store.py`, `quota_balancer/cli.py` - `policy_ids` and the verb.
- `tests/test_cardl1c.py` - 31 tests. Version-number updates in five older test
  files (below).
- `docs/STEP-CARD-L1C.md` (this record) and a README section.

## Design as implemented

**Migration 9.** `receipts` is rebuilt exactly like `readings` was in version 4:
a new table `receipts_new` with `input_tokens`, `cached_input_tokens` and
`output_tokens` nullable (each keeps `>= 0` for a non-null value), every row
copied column by column, `DROP TABLE receipts`, `RENAME`, and the
`receipt_final` partial index recreated. Everything else is the version-5
definition: `STRICT`, the foreign keys to `leases` and `gateways`, `calls NOT
NULL`, the `final` check, the money check. All five statements and the version
stamp run in the single `BEGIN IMMEDIATE` transaction of `migrate`, so an
interruption after any statement leaves the version-8 store whole. A newer,
unknown version (10) is refused exactly as before; a version-8 store upgrades.

**Who may report unknown.** The static validator accepts `null` for each token
field (the key must still be present; `calls` stays a required integer; a
non-null value is still a checked non-negative integer, a bool or a string is
still invalid). In `_settle`, right after the lease is read in the same
transaction, a lease whose stored enforcement is not `shadow` with any null token
is `INVALID_REQUEST`, before any write. A table CHECK cannot see the lease, so
the rule lives in code.

**Replay.** The stored values are compared as stored: unknown equals unknown, and
unknown is not zero, so the same event id with `0` instead of `null` is an
`IDEMPOTENCY_CONFLICT`.

**Admission.** The two sums over receipts (per-scope token or money usage, and the
account budget's settled money) gain `l.enforcement <> 'shadow'`. Before this card
a shadow receipt with whole numbers WAS counted (the join had no enforcement
filter); that was a latent miscount of an observe-only lease and is now excluded
explicitly. `SUM` skips NULLs and is wrapped in `COALESCE(...,0)`, so an
all-unknown set sums to 0, never to NULL.

**`policy-ids --json`.** One read transaction after the version check (an old or
newer store is `SCHEMA_UNSUPPORTED`, a missing file `STORE_ERROR`, both static).
Output on one line, sorted keys, compact: `seats` (project, team, machine, seat
aliases and the seat id; ordered by project, team, machine, seat), `models`
(account alias, model alias, id; ordered by account, model) and `task_classes`
(sorted). Nothing else is read from the tables: no identity reference, vendor,
budget, rate, hash, enforcement, role, path. `--json` is required, as for
`status`.

### Every place that reads a receipt token column, and how it treats NULL

| Where | What it does | NULL |
|---|---|---|
| `_validate_settle_receipt` (static) | checks the three fields | admits `null`, still range-checks any integer |
| `_settle`, after the lease is read | the shadow-only rule | any null on a non-shadow lease: `INVALID_REQUEST`, nothing written |
| `_settle`, the `INSERT INTO receipts` | stores the fields | a null is stored as NULL |
| `_settle`, replay comparison | compares stored and sent fields | null equals null; null differs from 0 (conflict) |
| `_settle`, replay response | recomputes released/retained from money and the hold | tokens are not read |
| `_settle`, the hold-settlement loop (`actual = receipt["input_tokens"]` for an input-token hold) | settles holds of an enforcing lease | only leases with holds reach it (never shadow), and those cannot send null, so never null here |
| admission, per-scope usage `SUM(r.input_tokens)` | settled token usage | shadow leases excluded; for the others the value is never null; `SUM` ignores NULL anyway and `COALESCE` gives 0 |
| admission, per-scope money `SUM(r.actual_micro_eur)` and the account budget `SUM` | settled money | shadow leases excluded (not a token column, but the same filter) |
| `status`, billing and evaluation code | none read receipts | not applicable |

## Decisions (the lead's five, then mine)

1. Null tokens for shadow leases only; the rule is enforced in code in the
   transaction that reads the lease.
2. Migration 9 as specified: transactional rebuild, rows byte for byte, the
   index, constraints and foreign keys, rollback tests at each step.
3. Admission never miscounts unknown; shadow leases are excluded explicitly and
   pinned by tests with a subscription limit enforced exactly.
4. `policy-ids --json`, closed, sorted, read-only.
5. Docs; the reporter takes the ids as configuration in v1 (copied by the
   operator); an HTTP lookup is deferred.

Mine:

6. **Shadow money is excluded from the account budget too**, not only tokens
   (same filter, one more `AND`): an observe-only lease must not move any limit.
7. **Sort orders** of `policy-ids`: seats by project, team, machine, seat;
   models by account, model; task classes alphabetically. The brief asked for a
   "sorted" projection without naming the keys.
8. **The shadow-only check runs before the gateway and replay checks**, so a
   null token on a non-shadow lease is refused even on a replay (a stored
   non-shadow receipt can never contain null, so a replay with null would have
   conflicted anyway).
9. **No new error code**: `INVALID_REQUEST` (400, exit 2), the code any other
   malformed receipt gets.
10. **`calls` stays a required integer** for shadow receipts; only the three token
    counts can be unknown.

## Rows -> tests (`tests/test_cardl1c.py`)

| Row | Tests |
|---|---|
| shadow_receipt_null_tokens | `ShadowReceiptNullTokens`: non-final and final receipts with all three null are accepted and stored as NULL; each field independently; zero is not null; a replay is a no-op, a changed replay (0 or a number instead of null) conflicts; `calls` and bad integers still invalid; the key must be present; a late final settles an expired lease; over HTTP (201, then 200, replay, final) |
| non_shadow_requires_tokens | `NonShadowRequiresTokens`: for an advisory, both subscription kinds and an enforcing metered lease, each of the three fields null (final and non-final) is `INVALID_REQUEST` with nothing written; integers still settle them; the refusal over the HTTP handler |
| migration_v9 | `MigrationV9`: version and script; a hand-built v8 store with leases and three receipts upgrades (rows, the `receipt_final` index text, foreign-key and integrity checks, idempotent); the new table accepts NULL and still rejects negatives, text, a missing calls, a bad `final`, unknown lease or gateway, and a second final per lease; an interruption after each of the five statements (and before the stamp) rolls back whole (version 8, rows, table and index text, no `receipts_new`); version 10 is refused |
| admission_ignores_unknown_shadow_usage | `AdmissionIgnoresUnknownShadowUsage`: with an input-token limit of 1000, a subscription lease at exactly 1000, a shadow receipt with unknown tokens and one with 5,000,000 known tokens: the next grant is still admitted; one more token and it is `INPUT_LIMIT`; only-unknown receipts leave usage at 0; a huge shadow money figure does not touch the account budget |
| policy_ids_projection | `PolicyIdsProjection`: closed shape and exact sorted content, byte-stable single line; read-only (tables unchanged); no identity reference, vendor, budget, hash, path or role; the ids really work in a lease request; an empty store; static errors, required flag, old store refused; `status` unchanged |
| privacy_l1c | `PrivacyL1c`: sentinels in the token, calls and outcome fields are never echoed or stored; the non-shadow refusal is static; `policy-ids` prints nothing but aliases and ids |

## Pre-existing tests changed, and why

Only version numbers, because the schema version is now 9. No assertion about
behaviour changed.
- `tests/test_card1.py`: `schema_version` 8 -> 9 in three expected status
  strings and two version checks; the "newer, unknown version" store is now
  `user_version=10` (nine is current).
- `tests/test_card2b1.py`, `tests/test_card4.py`: `user_version` expected 9 (was
  8); `test_card4`'s "newer version" store is 10.
- `tests/test_card5.py` and `tests/test_card4_seat_kind_fix.py`: their hand-built
  fixture stores run `MIGRATION_V9` after version 8 (otherwise the store would be
  refused as out of date); the v7-upgrade fixture is untouched.

## Evidence

- **Red first.** Commit `e8ad463` (tests only, on the card-l1b head):
  `python -m unittest tests.test_cardl1c` -> 31 tests, 22 red (null tokens
  rejected, no version 9, no `policy-ids`, shadow usage counted); the rest are
  guards that must keep holding (non-shadow refusal, status).
- **Green.** `tests.test_cardl1c`: 31 tests OK. Full suite: `Ran 809 tests in
  317.7s ... OK` (778 on the card-l1b head + 31), nothing left in an empty TMP.
  Before the version-number updates the full run had 30 failures and 2 errors,
  all in the older fixtures listed above.
- **Mutation spot-check.** 8 single-point mutations (the shadow-only rule, the
  static integer check, the token and money usage filters, the nullable column,
  a column check, the index, the sort order): all killed.

## Judged not testable

Concurrent migration by two processes (the existing `BEGIN IMMEDIATE` and the
version re-read inside the lock serialise them; unchanged).

## Residual risk

- Subscription (non-shadow) leases still must report whole numbers, so a
  reporter that cannot know a subscription lease's usage still cannot say
  "unknown" there. That is a separate, later decision (the lead's note).
- Shadow reports with whole numbers were counted before this card and are not
  now; nothing in the repository relied on that, but a deployment that had
  imported such receipts would see lower usage (Leasewise has not been deployed).
- The reporter must copy the ids by hand in v1 (`policy-ids` output); an HTTP
  lookup is deferred.
