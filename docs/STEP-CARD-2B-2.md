# Step record — Card 2b-2: billing evaluation

Work order: tk-bc9cd374d5cd (short turn 4 of 4; turns 1-3 on the same
branch). Seat: qwen-quota-dev-1. Branch: `card-2b-2` (built on
`main` via ec9b1ab, the card-4 merge @ schema v5). Date: 2026-09-26.
Interpreter: CPython 3.14 (standard library only: `uuid`, `datetime`,
`math`, `json`, `copy`, `unittest`).

The card is docs/DESIGN.md section 8, row "2b-2: billing evaluation":
`evaluate_billing(context, request)` over an explicit immutable
context/request - historical and gross-excess reconciliation, a
read-only lag timer and conditional advisory headroom. No opening
planner (card 2b-2b), no writes, leases, holds or resolutions.
Fixtures only: no live provider calls, no HTTP, no network. Time is
injected (now_ms); nothing reads the wall clock.

## Commits (this card, in order)

Turn 1 (carried fixes, 3):

1. `bb02853` test(2b-2 turn 1): red tests for carried R2 + R1(a)
2. `ad5230d` fix(2b-2 turn 1 R2): pin the v3 gateway snapshot period
   to one UTC month
3. `8fdb2c8` fix(2b-2 turn 1 R1a): refuse a config-import that breaks
   a billing policy

Turn 2 (input/output contract, 2):

4. `cf48d32` test(2b-2 turn 2): red tests for the evaluator
   input/output contract
5. `4d84aa1` feat(2b-2 turn 2): pure evaluate_billing input/output
   contract

Turn 3 (reconciliation rows, 2):

6. `b61ca00` test(2b-2 turn 3): red tests for the reconciliation rows
7. `0b78c2e` feat(2b-2 turn 3): reconciliation rows in evaluate_billing

Turn 4 (this turn, 4):

8. `9ebf3b2` feat(2b-2 turn 4): advisory headroom and admission basis
9. `bfa2e8e` test(2b-2 turn 4): billing_output_errors and privacy_2b
10. `fe5fe9b` demo(2b-2 turn 4): the card row demo (both outcomes)
11. (this file) docs(2b-2 turn 4): step record

## What was built

- **Turn 1.** The two carried review fixes landed first: a v3 gateway
  snapshot period is pinned to exactly one UTC month, and a
  config-import that would break an existing billing policy's
  invariants is refused (R2, R1a).
- **Turn 2.** The pure evaluator skeleton: `evaluate_billing(context,
  request) -> Evaluation` with a closed input surface (exact context
  and request keys, bounded ints, known references, certificate
  ordering, explicit overlap floors range-checked against
  min(P, L_now, B_e)), the complete closed output shape, the ordered
  12-row decision table (first match wins) and the non-AI overlay
  (never downgrades a hold, never replaces an earlier alert reason).
  Until the headroom landed, the output failed closed:
  headroom=null, admission_basis=unavailable, request_fits null (false
  on a hold).
- **Turn 3.** The reconciliation rows: the uncertified gross-excess
  hold (fresh latest P beyond L_now + R_now + B + allowance +
  tau(L_now); an unknown boundary disables the check, never B=0),
  conflict-latest ordering (the fresh gross check runs BEFORE the
  latched-conflict alert, which is preserved, never cleared),
  latest-seq ledger selection (seq order, never observed_at; an empty
  or decreased chain is unusable), gateway usability (reconciliation
  required, nonmonotonic chains, listed transitions), the midnight
  boundary exposure, the as-of bracket at the certified time (skew /
  covered / excess, both sides within 120 s of T), the read-only lag
  timer (a material negative gap alerts once the stored activity age
  reaches lag_seconds; equality never starts the timer) and the
  tolerance tau(L) = max(abs, ceil(L * bps / 10000)) as a comparison
  width, never a budget credit.
- **Turn 4.** The advisory headroom and admission basis, exactly as
  section 11 "Advisory headroom and opening plan" (the 2b-2 parts;
  the opening planner stays with 2b-2b):
  - Comparable (admission_basis=billing_estimate) only when EVERY
    retained provider event is proven (a coverage entry with a
    certified time, matched provenance, a usable B_e and the
    derived O_e=B_e under B_e<=P_e - never a hand-set explicit
    floor), the latest sample is fresh and no conflict is latched:
    headroom = cap - E - R_now. The proof-bearing fixtures hold:
    P=14.30, O=14.30, L=18.43, R=0.50, cap=100 => headroom 81.07.
  - Otherwise, with usable local ledger inputs, the fallback:
    headroom = cap - allowance - L_now - R_now (admission_basis=
    ledger_fallback) - the separate non-AI allowance stays out of
    the requested H (C1, accepted), and the diagnostic E is kept for
    diagnostics, not enforced: P=45, L=50, R=0, cap=100, allowance=0
    => E=95 diagnostic, headroom 50; allowance=10 => 40.
  - A hold recommendation denies H regardless of headroom
    (request_fits=false). request_fits is otherwise three-valued:
    false on a hold, unknown (None) with no known headroom - never a
    false-fit, never an admission - else H <= headroom.
  - A cap-exceeding estimate is rejected and fails closed (null,
    unavailable), never reported negative; a missing P keeps the
    fallback (estimate=null, never P=0); a missing/unusable ledger
    leaves headroom null / admission_basis unavailable.
- **Output and privacy pins (turn 4).** billing_output_errors: every
  result branch (fallback, comparable certified, hold, unusable
  ledger, no sample, latched conflict, non-AI overlay, provisional)
  serializes to the closed shape - exact keys, closed enums for
  state/reason/action/quality/admission_basis, bounded non-negative
  ints or null for the money fields, proposal always null,
  request_fits bool-or-null - and repeated calls are identical with
  the inputs untouched (before/after snapshots, zero financial side
  effects). Error branches are static: INVALID_CONFIG, the static
  message, no retry_at, and the closed exit mapping (exit 2, not a
  store fault). privacy_2b: inputs carry a distinctive machine alias
  and distinctive event ids; no output value is a container (no key,
  id, path, signed header, URL or raw input can ride along) and
  every error message is marker-free.

## Commands and counts

- Full suite: `python3 -m unittest discover -s tests` ->
  **251 OK** (236 at the start of this turn; +10 headroom, +5
  output/privacy tests). This card: `python3 -m unittest discover -s
  tests -p 'test_card2b2.py'` -> 72 OK (was 57).
- Demo: `python3 demo_card2b2.py` (below).
- Turn 4 red-first: the 10 headroom tests were shown RED before the
  implementation; the output/privacy tests pin the already-closed
  contract (they pass, and keep it closed).

## The demo (section-8 card row)

`python3 demo_card2b2.py` prints both demo outcomes:

    1) uncertified 30 EUR excess (P=80, L=50) => HOLD:
      action: hold   quality: comparable   reason: provider_excess
      admission_basis: ledger_fallback   headroom_micro_eur: 50000000
      request_fits: False
    2) normal P=45 / L=50 => fallback headroom 50:
      action: none   quality: provisional
      admission_basis: ledger_fallback   estimate_micro_eur: 95000000
      headroom_micro_eur: 50000000   request_fits: True

## Judgment calls

Turn 3 (carried forward, pinned by tests):

- **known_floor = the ledger's L_now, or null when unusable.** The
  floor diagnostic is the current settled total (unattributed is a
  subset of settled and is never added again); an unusable ledger
  has no known floor (null, never zero-filled).
- **A known boundary requires store proof.** A null boundary
  exposure is UNKNOWN, never 0; known requires a boundary event id
  (or an explicit 0 with such proof). The excess checks are disabled
  on an unknown boundary, not run with B=0.
- **Conflict latching is derived from the provider totals.** The
  evaluator replays the store's source-time reduce over the
  current-period provider totals (a newer total below the retained
  high-water, or an equal-time unequal total, latches it) and
  preserves the flag, never clearing it.

Turn 4 (this turn):

- **Comparable headroom uses derived proof only.** An explicit
  overlap floor is range-checked and used for the estimate, but it
  never counts as proof for the comparable basis: a hand-set floor
  does not certify the estimate. Completeness is per-event over ALL
  retained provider events, and the derived O_e=B_e requires
  B_e<=P_e.
- **Freshness and conflict gate comparability, not the fallback.** A
  stale sample or a latched conflict blocks the comparable basis but
  the usable-ledger fallback still reports (the row's action -
  billing_stale alert, source_conflict alert - is unchanged).
- **A rejected estimate fails closed, silently.** A cap-exceeding
  certified headroom is not reported (negative money is not money in
  this system); the fields read null/unavailable and nothing is
  admitted on them.
- **request_fits is three-valued.** Only a hold is a known
  false-fit; a missing basis yields None (unknown), not False - a
  false "does not fit" on an unknown headroom would be an admission
  decision the card does not make.
- **A >120 s snapshot is unusable for brackets and the gross receipt
  guard, but not for L_now.** The latest-seq settled has no
  freshness gate, so a stale-report context still yields the
  fallback headroom (test_stale_snapshot_is_unusable was re-pinned
  accordingly).
- **A latent turn-3 fixture bug surfaced when the stub was removed.**
  test_listed_transition_keeps_gateway_unusable listed
  _eid("transition"), which was never a chain row - the gateway was
  always usable and only LOOKED unusable under the unconditional
  stub. The test now lists the actual chain row id.

## Judged not testable (with the reason)

- **The opening planner.** Card 2b-2b by the card row; the evaluator
  never proposes (proposal is always null) and never writes.
- **Live provider calls / HTTP / network.** The card is fixtures
  only; no live calls by design.
- **Real store writes and financial side effects.** The evaluator is
  pure; zero side effects are pinned with before/after snapshots.
  Application, revalidation/fencing, partial-write recovery,
  accept-correction audit and operator resolutions need a
  separately sized integration work order (6b activation).
- **The wall clock.** Freshness, lag and receipt margins are all
  injected (now_ms, observed_at/received_at); the real clock is not
  exercised, by the pure-function contract.
