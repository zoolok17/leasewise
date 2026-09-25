# Step record — Card 4: lease store

Work order: tk-4c900a55cb28 (short turns 1-3; this is the final turn,
turn 3). Seat: qwen-agenttalk-dev-1. Branch: `card-4`, built on
`db39746` (the card-2b-1 merge, schema v4). Interpreter: CPython 3.14.2
(standard library only: `sqlite3` in the store, `multiprocessing` in the
grant_race test, `argparse`/`json` in the CLI).

The card is the lease store from docs/DESIGN.md section 8, row 4, plus
section 4's lifecycle (active -> settled on the gateway's final receipt;
Expiry -> uncertain, never a refund; late receipts retain their original
period; terminal rows cannot be reopened) and section 7's 409/410/429
contract. Store-level only: there is no HTTP layer until card 5, so the
acceptance tests call `lease.grant/check/settle/expire` directly against
a schema-v5 fixture store.

## What was built, by turn

### Turn 1 (ce82bac, c84e97d) — schema v5 + null_uniqueness

- **Schema, migration version 5** (`migrate.py`, ce82bac). Card 1-2b-1
  versions 1-4 are kept byte for byte; version 5 adds the lease-store
  tables — `leases`, `holds`, `receipts`, `blocks`, `adjustments`,
  `audit`, `daily_totals` — plus the exact partial indexes the card's
  row requires (the `receipt_final` uniqueness index, the `lease_live`
  live-slot index, the `receipts.event_id` idempotency index, and the
  null-window / null-model uniqueness partial indexes). Same
  discipline: preliminary version read takes no lock, `BEGIN
  IMMEDIATE`, version re-read inside the lock, every step's DDL plus
  the version stamp commit as one transaction, and a newer unknown
  store is rejected before anything is written and left
  byte-for-byte unchanged.
- **`null_uniqueness`** (`tests/test_card4.py`, 6 methods, c84e97d):
  one case per partial index — null-window limit duplicate, null-model
  block duplicate, final-receipt duplicate, live-lease (seat, turn)
  duplicate — all rejected; plus the v4 -> v5 clean upgrade and the
  newer-version store left byte-untouched.

### Turn 2 (1447355) — transactional grant/check/settle, 409/410/429

- **`quota_balancer/lease.py`** — the three store-level operations,
  each one explicit `BEGIN IMMEDIATE` transaction over an
  autocommit connection:
  - **`grant`** derives the seat chain (seat -> team -> project), the
    account from the model, and the caps through the card-3 arithmetic
    (`policy.estimate`); enforces every applicable limit through
    `policy.check_admission`, mapping a denial to the section-7 429
    code of the failing metric (`INPUT_LIMIT` / `MONEY_LIMIT` /
    `WINDOW_LIMIT` / `CONCURRENCY_LIMIT`); the account budget
    (`accounts.budget_micro_eur`) is the one mandatory
    (money, month) cap under the synthetic id `account:<account_id>`.
    A parked/probing block refuses with `ACCOUNT_PARKED`. Idempotency
    is by (machine_id, request_id): same hash replays the stored lease
    (no new hold), a different hash is `IDEMPOTENCY_CONFLICT` (409).
    One live slot per (seat, turn) is the `lease_live` index. A
    granted lease gets one holds row per applicable input/money
    limit; only a sized metered lease records holds.
  - **`check`** re-evaluates on every call (no cache, no TTL
    extension): settled is `LEASE_CLOSED` (410), expired is
    `LEASE_EXPIRED` (410), blocked is `ACCOUNT_PARKED` (429), missing
    is `NOT_FOUND` (404).
  - **`settle`** records a receipt; a duplicate event_id with
    identical content replays as a no-op, different content is
    `IDEMPOTENCY_CONFLICT`. A final receipt (exactly one per lease)
    moves the lease to settled and releases every hold into
    `settled_actual = min(actual, reserved)` — never below zero, never
    above the hold; an observed overrun is recorded as liability
    (money: an `adjustments` row with `resolved_at` NULL plus an audit
    row; input: an audit row).
- **`tests/test_card4_ops.py`** (16 methods): the 409/410/429
  contract rows (replay, same-id-different-content conflict,
  live-slot conflict, money/input/window limits, parked, advisory
  reserves nothing, check lifecycle, settled-closed), the settle
  release/retain/overrun/zero floors, and **`grant_race`** —
  concurrent grants (real `multiprocessing` workers) against one
  budget, no overspend: the sum of unresolved holds never exceeds the
  cap.

### Turn 3 (this turn) — expiry, late_receipt, privacy_4

- **`expire`** (the explicit expiry path of section 4: "Expiry ->
  uncertain, never a refund"). One `BEGIN IMMEDIATE` transaction: an
  active lease whose TTL has elapsed is moved to `uncertain`. The
  outcome cannot be proved, so **the holds stay reserved — the
  liability is retained and nothing is released or refunded**; the
  lease keeps its live slot (the `lease_live` index counts
  uncertain), so a late final receipt can still settle it. Idempotent
  on an already-uncertain lease (`replayed=True`); a not-yet-expired
  lease is `INVALID_REQUEST`; a settled lease is terminal
  (`RECEIPT_CONFLICT`); a missing lease is `NOT_FOUND` (404).
- **late_receipt** (section 4: "Late receipts retain their original
  period; scope accounting uses grant-day/month"). A **late FINAL**
  receipt on an expired (uncertain) lease settles it against the
  **ORIGINAL grant period, exactly once**: the holds keep their
  grant-time `period_start`, so the reconciliation lands in the
  original bucket even across a day or month boundary. The event_id
  makes a replay a no-op; a settled lease is terminal
  (`RECEIPT_CONFLICT`). A **non-final** receipt on an uncertain lease
  is still refused (`LEASE_EXPIRED`) — only the gateway's
  closed-child final receipt resolves an unprovable outcome. The
  `_settle` gate changed from "uncertain is always
  `LEASE_EXPIRED`" to "uncertain is `LEASE_EXPIRED` unless the receipt
  is final".
- **privacy_4** (status/error output never shows provider IDs,
  secrets, paths or raw request text — only the closed contract). The
  `effort` field was the one leak point: it was validated only by the
  open alias regex (`^[a-z][a-z0-9_-]{0,63}$`), which accepts an
  alias-shaped planted string, and `grant` persisted it verbatim as
  the lease `task_class`. The fix (in `_grant`, where the model row is
  available) refuses an effort that is not one of the model's closed
  `effort_aliases`. Every other grant/settle string field was already
  closed (UUID, 64-hex hash, or enum).
- **`tests/test_card4_expiry.py`** (11 methods): the section-4 expiry
  rules (expiry retains liability + budget, retains the live slot,
  not-yet-expired is invalid, uncertain is idempotent, settled is
  terminal, check-after-expire is 410) and the `late_receipt` rows
  (reconciles the original period once + replay no-op, across a day
  boundary, overrun reconciles the original period, non-final
  refused), plus `privacy_4`.

## Mutation verification (this turn's behavioral fix)

The privacy_4 fix is behavioral (an effort the model does not know is
now refused instead of persisted). Mutation check: with the
`request["effort"] not in model[11].split(",")` guard removed from
`_grant`, `test_privacy_4` went red (`AssertionError: QuotaError not
raised` — a sentinel effort was accepted and would have been persisted
as `task_class`); with the guard restored it is green. The late_receipt
gate change is pinned the same way: `test_late_receipt_nonfinal_refused`
requires a non-final receipt on an uncertain lease to be refused while
the final receipt still settles it, and `test_late_receipt_reconciles_
original_period` requires the hold to keep its September bucket after
an October receipt.

## Commands and counts

From the repository root:

```console
$ python -m unittest discover -s tests
Ran 155 tests in 75.7s
OK
```

Card-4 acceptance coverage (33 methods across three files):

- `tests/test_card4.py` — 6 (turn 1: null_uniqueness, upgrade,
  newer-version).
- `tests/test_card4_ops.py` — 16 (turn 2: grant/check/settle, the
  409/410/429 contract, grant_race).
- `tests/test_card4_expiry.py` — 11 (turn 3: expiry, late_receipt,
  privacy_4).

Full suite at this commit: **155 tests, all green**. Card-4 modules in
isolation: `python -m unittest tests.test_card4 tests.test_card4_ops
tests.test_card4_expiry` -> `Ran 33 tests ... OK`.

## Demo transcript (restart with an unresolved hold)

Fixture only. The driver (`demo_card4_restart.py`, kept in the task
scratch dir, run from the repository root) builds a schema-v5 store
with the test fixture chain and a budget of exactly one hold
(`budget_micro_eur == hold_sized`), then runs as **two separate
processes** against one store file so the restart is a genuine process
exit, not an in-memory reset:

```console
$ python demo_card4_restart.py grant <scratch>/restart.db
== phase 1: grant (process 22116) ==
fixture budget_micro_eur = 2253  (exactly one hold)
grant  lease_id=921ae828-0e02-4715-8dc4-b613c96d73e1 state=active money_hold=2253
  hold  limit=account:11111111-1111-4111-8111-111111111111 period_start=1788220800000 reserved=2253 settled_actual=None
process 1 exits WITHOUT settling (the hold is unresolved)
```

Process 1 exits. A fresh process opens the same store:

```console
$ python demo_card4_restart.py restart <scratch>/restart.db
== phase 2: restart (fresh process 21204) ==
lease  id=921ae828-0e02-4715-8dc4-b613c96d73e1 state=active
  hold  limit=account:11111111-1111-4111-8111-111111111111 period_start=1788220800000 reserved=2253 settled_actual=None
the hold is still unresolved; a second grant must be refused:
second grant refused: code=MONEY_LIMIT (429) message=the money limit is reached
the budget still counts the hold the first process left behind
```

The lease and its hold survived the process exit unresolved, and the
second grant is refused 429 because admission still counts the
unresolved hold against the account budget (section 4: "Admission
transaction: settled charges + unresolved holds + new hold <= each
applicable cap"). (Lease IDs are random per run; the hold/budget
behavior is what the card pins.)

## Judged not testable in this card (and why)

- **A real gateway final receipt.** Settlement is driven by an injected
  receipt dict in the section-7 shape; the real closed-child final
  receipt is produced by the gateway (card 6a) and gated by 6b, which
  does not exist yet. The store-level rule (a final receipt settles
  exactly once; a non-final receipt never resolves an uncertain
  lease) is fully tested against the contract shape.
- **A real network/HTTP 409/410/429.** The codes and their exit codes
  are exercised at the store level (`QuotaError.code`), but the HTTP
  transport that maps them to 409/410/429 responses is card 5. This
  card tests the codes, not the wire.
- **True clock expiry.** Expiry is driven by an injected `now_ms`
  (the tests pass `now_ms >= expires_at`). A wall-clock TTL elapsing on
  its own is not observable in a fixture test; the rule is the same
  comparison the injected path exercises.
- **Cross-host / multi-process grant_race at the HTTP layer.**
  `grant_race` uses real `multiprocessing` worker processes against one
  store file and proves no overspend at the store level. Concurrency
  across machines through the HTTP layer is card 5's concern.
