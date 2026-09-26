# Step record — Card 2b-2: billing evaluation

The original turn records below are historical. The **Fix round** section
at the end supersedes their conflicting judgment calls and test claims.

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

## Fix round — combined independent reads (2026-09-26)

Base: `e03d325`; request `tk-f05c1e2c770c`. Both independent reads were
reproduced through the public evaluator before production changes.
No source repository changes, provider traffic, push or enforcement.

Regression commit `72f27e4` adds 32 tests: the initial targeted run had
36 failing assertions/subtests and two errors. Contract fix `0469940`
passed its seven targeted validation cases. Monetary/decision fix
`4510ad6` passed all 32 regressions. Fixture correction `0a6b9c0` passed
all 72 existing evaluator acceptance tests and both demo outcomes.
Follow-up regression commit `d76b396` adds six cases; three exposed
remaining edge failures. Fix `c49734c` passes all 38 regression cases.
Final inspection reproduced raw provider conflicts disappearing under
basis rounding: regression `ccc60bc` was RED, then `0afc1aa` passed all
39 regressions and the 72 acceptance tests.

Finding IDs C1-C14 refer to the numbered Codex read; S1-S7 refer to the
numbered Sonnet findings. M2/M4 are its previously surviving mutants.
Test names below are methods in `tests/test_billing_eval_fix.py` unless
explicitly stated otherwise; each has the `test_` prefix.

| Finding | Fix | Regression tests | Fix commit |
|---|---|---|---|
| C1 / S3: basis and scope | Convert provider/ledger/share/boundary amounts to cap basis before comparison; unknown basis and mismatched owner/scope defer with unavailable headroom. | c1_convert_ledger_before_fallback; s3_convert_provider_before_gross; c1_basis_unknown_precedes_scope_and_gateway; missing_vat_defers_comparison; c1_scope_owner_revision_gates | 4510ad6 |
| C2 / S2: stale gateway admits | Latest selected report age gates both headroom formulas, inclusive 120000ms boundary. | c2_freshness_gates_both_headrooms | 4510ad6 |
| C3 / S6: ambiguous chain | Validate raw same-seq amounts and epoch/hash/machine identity; unresolved store transitions remain authoritative; identical replays count once. | c3_contradictory_same_seq_unusable_both_orders; c3_epoch_hash_machine_continuity; raw_same_seq_conflict_cannot_round_away; identical_replays_count_once; selected_legacy_report_does_not_disappear | 4510ad6, c49734c |
| S6: duplicate gateways | Reject duplicate gateway records and invalid chain references before aggregation. | s6_duplicate_gateway_rejected; chain_reference_wrong_gateway_or_kind; duplicate_event_collision_rejected | 0469940 |
| C4 / S5: wrong historical bracket | Use upper_at, inclusive upper/lower bounds; BOTH settled and open exposure come from UPPER. Complete_at remains the lower-overlap proof boundary. | c4_upper_bracket_not_lower_or_complete; c4_exact_at_upper_time_counts; s5_upper_only_certificate_holds | 4510ad6 |
| C5: event-order precedence | Test each higher-priority predicate against every event before any covered result. | c5_precedence_all_events_both_orders | 4510ad6 |
| C6: invented receipt-gap limit | Keep only receipt+30s lower bound and report age<=120s; no maximum distance after receipt. | c6_no_maximum_receipt_gap | 4510ad6 |
| C7 / S4: heartbeat resets idle | Derive change time from amount changes, including reserves/uncertainty; unchanged heartbeats do not reset it. | c7_unchanged_heartbeats_do_not_reset_idle; reserve_change_resets_idle_but_repeated_reserve_does_not | 4510ad6 |
| C8 / S1: missing table rows | Implement matched, lag_expected and missing_old_spend plus first-row basis/scope reasons; stale uses state=stale. Old settled lower bounds need no nearby upper-cutoff snapshot. | c8_matched_and_lag_expected; c8_missing_old_spend_during_activity; missing_old_spend_needs_no_nearby_cutoff_snapshot; C1 tests | 4510ad6, c49734c |
| C9: overlap bounds and priority | Validate each explicit floor against its own P_e; a valid explicit proof wins even when B_e>P_e. Reject explicit evidence lacking completeness/provenance even with an unusable ledger. | c9_each_overlap_uses_its_own_provider_amount; c9_explicit_proof_beats_derived_overlap; explicit_overlap_without_proof_rejected_on_unusable_ledger | 4510ad6, c49734c |
| C10: provider tie order | Latest selection uses source time then UUID; conflict folding reuses the store reducer on raw values before rounding, not receipt order. | c10_uuid_tie_not_receipt_order; raw_provider_conflict_cannot_round_away | 4510ad6, 0afc1aa |
| C11 / S7: overflow and floats | Checked int64 money results/sums; integer rational ceil only; no binary floats. | c11_estimate_overflow_rejected; c11_component_sum_overflow_rejected; c11_tolerance_large_integer_exact | 4510ad6 |
| C12 / S7: closed API drift | Exact three-key gateway input and ten-key Evaluation output; store's unresolved references supply reconcile state. | c12_exact_spec_input_output | 0469940 |
| C13: lost provider floor | Diagnostic floor is max of known operands, including retained provider high-water when ledger is unavailable. | c13_known_provider_floor_survives_unusable_ledger; existing BillingConflictedLatestGross tests | 4510ad6 |
| C14: validation holes | Exact integer version; typed payload rejection; known unresolved references. | c14_boolean_version; c14_null_payload_typed_error; c14_unknown_unresolved_reference | 0469940 |
| M2: receipt margin mutant | Pin 29999ms versus 30000ms after receipt. | mutant_receipt_margin_exact_boundary | 4510ad6 |
| M4: tolerance-floor mutant | Pin fractional tolerance equality/+1 boundaries. | mutant_tolerance_rounds_up | 4510ad6 |

### Reversed judgments and clarified limits

- Stale gateway reports disable **both** headrooms. Stale provider
  billing alone still permits the fresh-ledger fallback with an ALERT.
- Valid explicit overlap evidence can establish comparable admission;
  it is not excluded merely because it is explicit or B_e exceeds P_e.
- known_floor includes known provider evidence, preserving the retained
  high-water; it is not only L_now and is never enforced in fallback.
- Remove the unspecced maximum receipt gap, heartbeat-based idle timer,
  lower-snapshot historical threshold and per-event short-circuit order.
- The extra gateway reconciliation flag and output conflict flag are
  removed. The closed contract wins over the author's test definitions.
- Existing fixtures are corrected, not relaxed: distinct reports have
  distinct seq; tests of provider staleness keep local reports fresh;
  idle tests include fresh unchanged heartbeats. Freshness, same-seq
  conflict and receipt-gap assertions that contradicted the spec were
  reversed. Missing rows now have behavioral tests, not just a name list.
- Sonnet S5's sample P=13 and UPPER settled=14 cannot prove excess under
  the written formula. Its mechanism finding is accepted; the positive
  HOLD fixture uses UPPER settled=10, with later spend raising L_now to
  14. The companion P=13/UPPER=13 fixture explicitly forbids a false HOLD.
- Ambiguity: a same-seq report with different observation times is treated
  as ambiguous/unusable, even with equal amounts. Identical replays count
  once. Store-selected chain IDs remain a trusted store assertion, not a
  telemetry-selected identity. Raw differences cannot disappear through
  monetary rounding. No monetary timestamp is silently made current.
- Boundary exposure is treated as ledger-basis money; explicit overlap
  evidence is cap-basis money. Cap and allowance already use cap basis.
  Rational operations use Python's exact wide intermediate numerator and
  check the final int64 amount, matching shared convert_basis; aggregates
  and estimates are checked before any output or admission comparison.
- A sparse continuous settled lower bound remains valid for the old-spend
  ALERT without a nearby bracket. Its tolerance uses that lower bound;
  this diagnostic cannot produce a HOLD or refund. Upper-exposure and
  overlap calculations retain their separate 120s bracket requirements.
- This remains an advisory pure function. It does not establish provider
  completeness, authenticate the context builder, clear stored holds or
  enforce ceilings against unmediated spend.

### Fix-round verification

- Targeted: 39 new regressions and 72 existing evaluator tests pass.
- Both demo outcomes pass: gross excess HOLD; P=45/L=50 fallback=50.
- Mutation checks executed in memory, source unchanged: receipt margin
  30000->0 and tolerance ceil->floor each fail their targeted test.
- Initial full run: 289 tests passed in 93.119s. It was already running
  when the final inspection found the provider-rounding conflict; a
  final full run is therefore necessary to cover the delivered fix.
- Final full run: `python -m unittest discover -s tests` passed **290
  tests in 83.546s**, foreground, after the last production change.
  Test temporary files stayed under task scratch. `git diff --check`
  passed. The isolated clone and red/green/mutation logs are retained
  for the lead to fetch and review; no push was performed.

## Fix round 2

Contributor/reviewer reference for the delta after `aa17e21`. The four
follow-up counterexamples are fixed in the pure advisory evaluator; no
lease enforcement, provider I/O or stored reconciliation state changes.
All test names below are in `tests/test_billing_eval_fix_round2.py` and
have the `test_` prefix.

| Finding | Fix | Regression evidence |
|---|---|---|
| N1 (major): inverted historical bracket | At an equal observation time the upper uses the highest sequence. Both sides must exist within the window and upper seq must be at least lower seq; inversion supplies no historical certificate. Current-chain latest selection remains highest-seq. | n1_timestamp_ties_use_later_sequence; n1_clock_rollback_never_inverts_historical_bracket (both input orders, exact T and between rolled-back times). |
| N2 (C7 remainder): raw activity rounded away | Derive last financial change before basis conversion. Raw reserves 5 to 6 break idle even though both convert to 5; unchanged raw reserves retain the timer. | n2_raw_reserve_change_breaks_idle_despite_rounding; also asserts converted headroom and input immutability. |
| N3 (C8 remainder): matched without overlap proof | Matched requires proved, fresh, conflict-free overlap in addition to complete certificates and tolerance. P=49.9/L=50 with an unproved derived overlap stays temporally_unresolved; a valid explicit floor can establish matched. | n3_matched_requires_proved_overlap. |
| N4 (C9 remainder): stale input skips numeric validation | Reject explicit floors exceeding converted provider money before usability short-circuits. Continuous chains also supply known current/historical bounds even when stale; missing/ambiguous chains do not invent bounds. | n4_stale_ledger_cannot_hide_invalid_explicit_floor; n4_known_provider_and_historical_bounds_always_validate; n4_stale_ledger_valid_floor_stays_unavailable. |

The initial six-test run on unchanged production code failed with eight
subtest failures covering every finding. After the fixes, those six tests
passed. One further boundary test covers absent ledger evidence and a
historical bound below the current ledger. The combined first/second fix
regression selection passes 46 tests. Valid stale evidence still returns
unavailable headroom; early validation cannot authorize stale money.

The historical-bracket clarification rejects inversion rather than sorting
the current ledger by wall clock. A tie is resolved using sequence; a
rollback that leaves the upper older than the lower remains unresolved.
The earlier closure of C7/C8/C9 applied to their initial examples; the
narrower combinations reported in the second read are covered here.

Final verification: `python -m unittest discover -s tests` ran once after
all production/test edits: **297 tests passed in 78.261s**, foreground.
`git diff --check` passed. The clone and red/green/full-suite logs remain
in task scratch for the lead's fetch and evidence review; no push.
