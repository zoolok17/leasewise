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

## Fix round (turns A/B/C — the two reads)

Two independent reads of the card-4 store: READ A
(`card4-read-sonnet.md`) and READ B (`card4-read-opus.md`). Each
finding is handled finding -> fix -> pinning test. Turn A is
`7d78204` (settlement accounting), turn B is `41c2c5a` (enforcement
and binding), turn C is this commit (gateway on settle + the minors +
this section).

### Turn A (`7d78204`) — `tests/test_card4_settle_fix.py` (4)

- READ A BLOCKER 1 / READ B B1 — settled hold headroom was not
  returned. Fix: the final receipt's holds loop settles
  `min(actual, reserved)` per holds row; the unspent remainder of the
  reservation comes back to the caps. Pin:
  `test_settled_lease_releases_hold_headroom` (a budget that fits
  exactly 3 holds; after three small settles, a 4th grant is
  admitted).
- READ A MAJOR 3 / READ B B2 — day caps settled against the month
  start. Fix: a (metric, day) hold takes the grant-day bucket. Pin:
  `test_day_limit_counts_settled_spend`.
- READ A BLOCKER 2 / READ B M1 — an overrun wrote one adjustments
  row per money hold. Fix: exactly one row for the max overrun, in
  the original period. Pin:
  `test_overrun_two_money_holds_one_adjustment` (two money holds,
  exactly one adjustments row).
- READ A MAJOR 7 (the blocking side) — an overrun must park the
  account. Fix: an overrun final inserts the parked block; the next
  grant is refused ACCOUNT_PARKED. Pin: `test_overrun_blocks_account`.
  (The reconciliation reading of `resolved_at` is turn 3's scope.)

### Turn B (`41c2c5a`) — `tests/test_card4_enforce_fix.py` (6)

- READ B B3 — platform limits were never enforced. Fix: the platform
  singleton (the null UUID `_PLATFORM_ID`) rides in every grant's
  chain and in the limits query. Pin:
  `test_platform_limit_enforced`.
- READ A MAJOR 5 (grant side) — the gateway/account were not tied to
  the seat's project. Fix: the account must be in the project's
  `project_accounts` (POLICY_DENIED), and the grant's gateway must
  be one of the project's gateways. Pins:
  `test_foreign_account_rejected`, `test_foreign_gateway_rejected`.
- READ B M2 — `request_hash` was client-provided. Fix: the
  server-side content hash over the canonical request JSON (every
  field except `request_hash`); same id + changed content is
  IDEMPOTENCY_CONFLICT, unchanged content replays. Pin:
  `test_replay_changed_content_conflicts`.
- READ A MAJOR 4 — a subscription/advisory lease that reserved no
  money could not settle a null amount. Fix: a final receipt with a
  null `actual_micro_eur` is accepted when the lease reserved no
  money. Pin: `test_advisory_settle_null_money`.
- READ A MAJOR 6 / READ B m3 — `ttl_ms` (the lifetime bound) was
  unbounded. Fix:
  `_check_int(ttl_ms, 1, _MAX_TTL_MS)` with `_MAX_TTL_MS` = 24 h,
  plus the int64 overflow guard on `expires_at`. Pin:
  `test_ttl_ms_bounded` (2_073_600_000 accepted; 2_073_600_001,
  2**62, 2**63-1 refused).
- Inherited-draft corrections found while pinning: the platform
  singleton was the string `"platform"` (card-3 requires UUID chain
  ids; 32 reds) and the ttl test used a 1-day bound where the max is
  24 h.

### Turn C (this commit) — `tests/test_card4_turn_c_fix.py` (6)

- READ A MAJOR 5 (settle side) — the receipt's `gateway_id` must
  match the lease's gateway. Fix: `_settle` refuses a foreign gateway
  with a static RECEIPT_CONFLICT (a NULL names no other gateway);
  the lease stays untouched. Pin:
  `test_settle_foreign_gateway_conflicts`.
- READ A MINOR 8 / READ B m1 — the settle response summed over the
  holds rows (every money cap reserves the same hold size, so the
  lease was double-billed). Fix: released/retained is per lease,
  from `lease["money_hold"]`; the overrun stays in the adjustments
  row. Pins: `test_settle_release_retained_per_lease` and the
  turn-A assertion in `test_overrun_two_money_holds_one_adjustment`
  updated from `2 * HOLD` to `HOLD`.
- READ A MINOR 11 / READ B m2 — a parked block recorded after a
  grant blocked the legitimate replay. Fix: the idempotency replay
  now comes before the parked check. Pin:
  `test_parked_replay_returns_stored_lease`.
- READ A MINOR 10 — estimate freshness. Fix:
  `observed_at` must be at most 120 s old at the grant
  (`_EST_FRESH_MS`), else INVALID_REQUEST. Pin:
  `test_stale_estimate_refused`. Fixture note: the two modules that
  grant at a later injected clock (the day-cap test in
  `test_card4_settle_fix`, the October grant in
  `test_card4_expiry`) now derive `observed_at` from that clock so
  their grants stay fresh — they test bucketing/reconciliation, not
  estimate age.
- READ A MINOR 12 (concurrency) — a shadow lease consumed a slot.
  Fix: the live-slot count excludes `enforcement = 'shadow'`. Pin:
  `test_shadow_consumes_no_concurrency_slot`.
- READ B m6 — a non-final receipt past the TTL was accepted on an
  active lease. Fix: a non-final receipt is refused once the lease
  is uncertain or its TTL has elapsed; a late FINAL still settles.
  Pin: `test_nonfinal_past_ttl_refused`.

Mutation verification (turn C): each of the six fixes was reverted
in isolation and its pinning test went red (settle-foreign-gateway,
settle-nonfinal-past-ttl, settle-retained-per-hold,
grant-replay-after-parked, grant-estimate-freshness,
concurrency-shadow-counted); the source was restored byte-exact
after each.

### Deferred (with reasons)

- READ A NIT 13 / READ B n1 — "card 4 added a trailing `;` to
  card-3's v3 index": verified `_DDL_V1/_V2/_V3` are byte-identical
  to the card-3 base `db39746`; the `;` is in the original. No
  change. NIT 13's second half (no card-4 exact-fit-at-cap test):
  the equality boundary is pinned at store level by card 3's
  `test_admission_at_cap_ok_over_cap_denied` and by the third of the
  three exact-fit grants in
  `test_settled_lease_releases_hold_headroom`.
- READ B n2 — `adjustments.unattributed_micro_eur` has no CHECK >=
  0: the value is server-computed as actual - reserved under
  `actual > reserved` (structurally non-negative), and adding the
  constraint needs a v6 table rebuild; the read's READY list
  excludes the NITs.
- READ A MINOR 9 — the store's `final` is int 0/1 (schema CHECK);
  the wire's `true` converts in card 5's HTTP layer.
- The clamp sub-item of READ A MAJOR 6 / READ B m3 — `expires_at` is
  not clamped at a period boundary (section 4 requires it); needs a
  design ruling on which boundary.
- READ A m4 — the enforcement x account-kind matrix needs the full
  section-7 ruling plus a refusal-code choice.
- READ A MINOR 12 (remainder) — task_class has no design taxonomy
  in card-4 scope (the column stores the effort alias);
  daily_totals' derivation point is unspecified; token_hash is card
  5 per the read. Its platform-scope bullet is covered by the turn-B
  B3 fix, and its last bullet (settled non-final interim receipts)
  is marked "as designed" in the read itself.

### Commands and counts (fix round)

```console
$ python -m unittest discover -s tests
Ran 171 tests in 79.935s
OK
```

155 (original card 4) + 4 (turn A) + 6 (turn B) + 6 (turn C) = 171.
Card-4 modules in isolation: `python -m unittest tests.test_card4
tests.test_card4_ops tests.test_card4_expiry
tests.test_card4_settle_fix tests.test_card4_enforce_fix
tests.test_card4_turn_c_fix` -> `Ran 49 tests ... OK` (6 + 16 + 11
+ 4 + 6 + 6).

### Turn D (this commit) — `tests/test_card4_turn_d_fix.py` (5)

Final pre-merge read (head 0144e2e): NOT READY on one finding, N1.
This turn implements the work-ordered follow-ups (N1, N2, the lead
ruling on A-MAJOR 7 part 2), plus N4 (left to my call):

- N1 (MAJOR) — the turn-B ttl fix set `_MAX_TTL_MS = 24 * _DAY_MS`
  (24 DAYS); the bound is 24 HOURS (DESIGN section 6). Fix:
  `_MAX_TTL_MS = _DAY_MS`. `test_ttl_ms_bounded` corrected
  (86_400_000 accepted; 86_400_001, 2**62, 2**63-1 refused); the
  exact boundary is pinned by `test_ttl_24h_boundary` (24 h
  admitted with `expires_at = granted_at + 24 h`; 24 h + 1 ms and
  the old 24-day value refused). The profile TTLs (30m/2h/6h)
  remain unapplied: they need task_class, covered by the
  task_class deferral below.
- N2 (MINOR) — the estimate freshness check was one-sided: a
  future-dated `observed_at` was admitted. Fix: also refuse
  `observed_at > now + FUTURE_SLOP_MS` (the readings' 30 s clock
  skew, imported from `reduce`); exactly +30 s is still admitted.
  Pin: `test_future_estimate_refused`.
- A-MAJOR 7 part 2 (lead ruling: "receipt conflict blocks") — a
  CONFLICTING FINAL receipt (a 2nd different final on a settled
  lease, or a final naming a foreign gateway) now blocks further
  work on the account: a live NULL-model `parked` block with
  reason `receipt_conflict`, keyed to the conflicting event
  (`source_event_id`), recorded like the overrun block (INSERT OR
  IGNORE under the `blocks_live` index) plus a
  `receipt_conflict_block` audit row; the park commits before the
  RECEIPT_CONFLICT raise so it survives the settle rollback. A
  conflicting NON-final is refused without parking. Pins:
  `test_conflicting_final_blocks_account`,
  `test_foreign_gateway_final_blocks`.
- N4 (my call: implemented) — the overrun adjustment's period took
  a hold's bucket and depended on holds row order (mutation F20
  survived). Fix: the adjustment is stamped with the lease's own
  grant-time period (`lease.period_start`, always the grant month:
  "in the original period"), independent of the holds. Pin:
  `test_overrun_adjustment_lease_period` (a single day-bucket hold
  via a subscription account, whose NULL budget takes the
  account-month hold out of the set).

Deferred to the reconciliation card (lead ruling, one line each):
- m7 — advisory (unsized gateway) leases never run the account-budget group; needs the "advisory only" ruling before 6b.
- N3 — the overrun park's INSERT OR IGNORE silently skips a live NULL-model `probing` block, and concurrent overruns share one block keyed to the first event; clearing a block must scan every unresolved adjustments row of the account (not the block's source_event_id), and an overrun should turn a live block `parked` rather than skip it.
- N5 — the `project_accounts` check runs before the idempotent replay, so a replay after the project loses the account gets POLICY_DENIED instead of its stored lease (replay-first, as in m2).
- m3 clamp — `expires_at` is not clamped at a period boundary; still blocked on the design ruling on which boundary.
- m4 — a `subscription_admission` seat on a METERED account skips money admission (a seat setting is a money bypass); needs the section-7 ruling, and seat enforcement must be validated against the account kind before card 5 exposes grants.
- (The m3-clamp and m4 deferrals were already listed in turn B's Deferred section; restated here as the lead's reconciliation-card deferrals.)

Mutation verification (turn D): each fix was reverted in isolation
and its pinning test went red (N1 24-day bound, N2 one-sided
freshness, the receipt-conflict park removed at both conflict
sites, N4 hold-bucket period); the source was restored byte-exact
after each.

### Commands and counts (turn D)

```console
$ python -m unittest discover -s tests
Ran 176 tests in 81.683s
OK
```

171 (through turn C) + 5 (turn D) = 176.
Card-4 modules in isolation: `python -m unittest tests.test_card4
tests.test_card4_ops tests.test_card4_expiry
tests.test_card4_settle_fix tests.test_card4_enforce_fix
tests.test_card4_turn_c_fix tests.test_card4_turn_d_fix` ->
`Ran 54 tests ... OK` (49 + 5).
