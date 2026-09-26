# Step record — Card 2b-2b: opening planner

Work order: tk-59437f7fd6c5. Branch `card-2b-2b` off `945e380` (card 2b-2
merged). Date: 2026-09-26. Interpreter: CPython 3.14, standard library only.

The card is docs/DESIGN.md section 8, row "2b-2b: opening planner":
`plan_opening(context, request) -> Proposal`, a pure function with a closed
input and a deterministic proposal. **Out of scope** (design, section 11):
applying a proposal, drain execution, financial writes, operator resolution,
revalidation/fencing, 6b activation. No CLI verb, no store, no file, clock
or network access. Time is injected (`now_ms`).

## Files

- `quota_balancer/billing_open.py` — the planner (new).
- `quota_balancer/billing_eval.py` — one behaviour-neutral extraction:
  the coverage-array validation moved out of `_validate_request` into
  `_validate_coverage(coverage, ctx, now_ms)` so both entry points share it
  instead of forking it. Nothing else in the evaluator changed.
- `tests/test_card2b2b.py` — 90 tests (below).
- `demo_card2b2b.py` — the card-row demo (both outcomes).

Reused, not forked, from 2b-2: `_validate_context` (closed context and
chain membership), `_validate_coverage`, `_normalize_money` (basis gate),
`_scope_matches`, `_gateway_usable` (chain continuity, unresolved
transitions, same-seq contradictions, 120 s freshness),
`_validate_known_overlap_bounds` and `_overlaps` (explicit-floor rules),
`_conflict_latched`, `_latest_provider`, `_fresh`, `_sum`/`_checked`
(int64), and the validators of `validate_readings.py`.

## Design as implemented

Two outcomes for two kinds of bad input (design: "Unknown references, bad
ordering, invalid explicit overlap, account mismatch or unsupported version
=> INVALID_CONFIG. Validly shaped but unsafe opening evidence returns
unsafe/empty allocations"):

- **INVALID_CONFIG** (static message, exit 2): non-closed shapes, wrong
  types (bool is not int, floats, non-exact booleans), int64 bounds and
  overflow, negatives, unknown/duplicated references, wrong baseline
  references, duplicate proof consumption, account mismatch, unsupported
  version, `drain_completed_at > now_ms`, invalid explicit overlap.
  All of these checks, and all checked arithmetic, run **before** any safety
  verdict, so an unsafe answer can never hide an invalid input.
- **unsafe proposal** (`state=unsafe`, `reason=opening_unsafe`, empty
  allocations, `source_events=[]`, `non_ai_allocation_micro_eur=0`): every
  well-shaped input for which a required proof is missing.

A proposal is **ready** only when all of these hold:

1. `drain_verified` is true; basis known and one common basis; scope
   matches; every gateway chain usable and fresh (2b-2 predicates).
2. No unresolved transition anywhere, and behind every baseline snapshot:
   reserved = 0, uncertain = 0, unattributed = 0, no pending
   reconciliation.
3. The billing event is the latest provider sample, fresh, and not
   conflict-latched.
4. Catch-up: `lastUpdate (observed_at) >= drain_completed + lag_seconds`
   AND its coverage entry has `matched_provenance`, a `complete_at` and
   `complete_at >= drain_completed`. Nothing else (no elapsed time, no
   expiry) counts.
5. With `D = max(0, P - L)` (`P` = the billing event's total, `L` = sum of
   baselines): every proof is consumed exactly once by its owner and is
   `disjoint_from_managed`; each allocation equals the sum of its proofs;
   gateway externals plus the non-AI amount equal exactly `D`; the non-AI
   amount is within `non_ai_allowance_micro_eur`; each
   `baseline + external` is within that gateway's share.

Ready output: one allocation per configured gateway (sorted),
`opening = baseline + external`, baselines untouched (P < L keeps every
baseline; nothing is ever lowered to P), the non-AI amount reported once in
its own bucket and added to no gateway. `proposal_id` is a UUIDv5 over the
canonical JSON of the numeric inputs and references (sorted arrays and
keys, resolved allocations so a missing gateway equals an explicit zero,
state included, `now_ms` excluded).

## Acceptance rows -> tests (`tests/test_card2b2b.py`)

| Row | Tests |
|---|---|
| billing_opening_input | `BillingOpeningInput`: `test_extra_and_missing_request_keys`, `test_extra_and_missing_nested_keys`, `test_non_dict_and_bad_container_types`, `test_account_mismatch`, `test_unsupported_version`, `test_wrong_baseline_references` (9 sub-cases), `test_baseline_must_be_the_chain_tip`, `test_snapshot_outside_current_chain_is_wrong_reference`, `test_duplicate_proof_consumption`, `test_duplicate_proof_ids_and_gateway_allocations`, `test_unknown_references`, `test_bad_ordering`, `test_invalid_explicit_overlap`, `test_explicit_floor_needs_a_usable_bracket_bound`, `test_explicit_floor_beyond_known_bounds_is_invalid_even_when_stale`, `test_bool_is_not_int`, `test_flags_must_be_exact_booleans`, `test_floats_rejected`, `test_int64_bounds`, `test_int64_sum_overflow_is_invalid_config`, `test_bad_context_is_invalid_config`, `test_well_shaped_insufficient_proof_is_unsafe_without_io`, `test_design_case_ready` |
| billing_opening_uncertain_overlap | `BillingOpeningUncertainOverlap`: reserved, uncertain, 0.57-uncertain, unresolved (own snapshot and any other event), reconciliation_required, unattributed, proof not disjoint, missing matched provenance, epoch change, contradictory same-seq chain, conflicting provider totals, stale provider (boundary test with a fresh ledger), stale ledger, superseded provider sample, scope mismatch, unknown/mixed basis, no gateways |
| billing_opening_catchup | `BillingOpeningCatchup`: `test_18_43_19_0_57_unsafe_until_resolved`, `test_p_minus_l_is_not_evidence_of_an_external_charge`, `test_eventual_settled_19_produces_no_duplicate_057`, `test_early_drain_refuses`, `test_lag_boundary_is_inclusive`, `test_complete_at_must_cover_the_drain`, `test_missing_or_incomplete_coverage_refuses`, `test_unverified_drain_refuses`, `test_elapsed_wait_alone_never_proves_catch_up` |
| billing_opening_allocations | `BillingOpeningAllocations`: split across gateways, missing allocation = zero, non-AI once in its own bucket, wrong owner (both directions), wrong sums (±1, P-L larger/smaller), missing ownership, unconsumed proof, non-AI amount vs its proofs, allowance exceeded, share overflow (and exactly-at-share ready), shares+allowance > cap, P < L preserves baselines, asserted external with P < L unsafe, sorted `source_events` |
| billing_proposal_only | `BillingProposalOnly`: `test_negative_amounts_are_invalid_config`, `test_proposal_id_stable_across_repeated_calls`, `test_proposal_id_stable_across_key_orders` (10 shuffles), `test_input_order_independence` (all permutations of readings, baselines, allocations, proofs, coverage, gateways, evidence ids), `test_proposal_id_tracks_the_material_inputs`, `test_valid_plan_preserves_baselines`, `test_no_financial_writes_inputs_untouched`, `test_error_path_leaves_inputs_untouched`, `test_result_is_detached_from_inputs`, `test_evaluation_side_is_unaffected` |
| billing_output_errors | `BillingOutputErrors`: closed shape and enums on every branch (JSON-serialisable), INVALID_CONFIG -> exit 2 with one static message, OPENING_UNSAFE and INVALID_CONFIG exit codes, int64 overflow -> INVALID_CONFIG, only typed errors escape |
| privacy_2b | `Privacy2b`: sentinels (secret, Windows path, POSIX path, provider id) injected into every string slot never appear in any error text; success output holds only approved aliases/UUIDs/enums/ints; module imports nothing capable of I/O (AST) and calls no `open/print/eval/...`; runtime run with `open`, `print`, `time`, `socket`, `os.stat/listdir/urandom`, `random` patched to raise; before/after snapshots of cwd listing, environment and inputs |

## Evidence

- **Red first.** Against a stub `plan_opening` that raises
  `NotImplementedError`, the 85 tests written first gave
  `Ran 85 tests ... FAILED (errors=93)` (93 = tests plus sub-cases; the few
  that passed are the static and "an error must be raised" tests that a stub
  satisfies trivially). Output kept in the seat scratch as `red-run.txt`.
- **Green.** All 90 tests pass (85 original + 5 added after mutation
  testing, below).
- **Mutation verification.** 52 single-point mutations of
  `billing_open.py` (each safety and validation rule removed or weakened,
  sort/ID determinism, output shape): 51 killed, 0 survived, 1 pattern
  removed with the redundant code it targeted. The first sweep had
  9 survivors: the provider-freshness gate, "any unresolved transition",
  the non-AI amount vs its proofs, both explicit-overlap guards (each was
  masked by the other), unsorted `source_events`, and a redundant proof-sum
  overflow check. Five tests were added to isolate them and the redundant
  check was deleted. Sweep output kept as `mutation-run.txt`.
- **Regression.** `test_card2b2*.py` and `test_billing_eval*.py` (211
  tests) green after the `_validate_coverage` extraction.
- **Full suite**, once, foreground: `python -m unittest discover -s tests`
  -> `Ran 387 tests in 85.2s ... OK` (297 before this card + 90 new).
  `git diff --check` clean.
- `python demo_card2b2b.py` prints both card-row outcomes and `demo ok`.

## Decisions on ambiguities (design silent or ambiguous; fail-closed reading)

1. **Sum rule.** "Allocations ... sum to that amount [max(0,P-L)]": read as
   gateway externals **plus** the non-AI amount equal `D`, so the non-AI
   charge is counted once and in neither gateway ledger.
2. **Unproven vs invalid.** A well-shaped but unproven request (unverified
   drain, missing/early coverage, missing ownership, wrong owner, wrong sum,
   unconsumed proof, `disjoint_from_managed=false`, exceeded allowance or
   share) is *unsafe*, not INVALID_CONFIG. Double consumption of a proof and
   unknown references are INVALID_CONFIG (matrix row).
3. **Baseline reference.** A baseline must reference a settled snapshot in
   the gateway's *current chain* at its *latest sequence* with exactly that
   amount; anything else is INVALID_CONFIG ("wrong baseline references"). A
   correct reference to a stale or discontinuous chain is unsafe.
4. **`billing_event_id`** must be a current-period provider event
   (INVALID_CONFIG otherwise), and must also be the *latest* sample
   (otherwise unsafe): a superseded sample is not the present P.
5. **`lastUpdate`** is the provider reading's `observed_at`; the
   `>= drain + lag_seconds` test is inclusive.
6. **Unknown provenance** is read as: any nonzero `unattributed_micro_eur`
   or a pending `reconciliation_required` behind a baseline, or any entry in
   `unresolved_event_ids` (not only the gateway's own snapshots) => unsafe.
   The design lists "known provenance" without defining the ledger signal.
7. **Mixed bases.** The design does not say in which basis proven external
   amounts are stated, nor which basis the opening is written in. Unknown
   basis (`_normalize_money`) is unsafe; a convertible but *mixed*
   cap/provider/ledger basis is also unsafe rather than silently converted
   (ceil rounding would make P-L "exactly covered" ambiguous). The B06
   configuration (all ex_vat) is fully supported.
8. **Unsafe proposals** carry `source_events=[]` and
   `non_ai_allocation_micro_eur=0` (no inferred authority) but still a
   deterministic `proposal_id` (includes the `unsafe` state).
9. **Proposal ID inputs.** `now_ms` is excluded so re-planning at a later
   time over the same material inputs keeps the ID; drain time,
   verification flag, billing event and total, coverage, baselines,
   resolved allocations, non-AI, all proofs and the state are included.
10. **Explicit overlap floors** follow the evaluator exactly (violation or
    no usable `B_e` => INVALID_CONFIG, checked even when the ledger is
    stale); the planner reuses `_validate_known_overlap_bounds` and
    `_overlaps`. The planner itself does not use `O_e`: P-L is compared
    directly, as the design specifies. **Correction (fix round
    tk-dba39941b750, finding 2):** at first build, "checked even when
    stale" was aspirational, not actual - `_overlaps` ran only when the
    gateway chain was fresh (`usable`), so a stale chain hid a missing or
    exceeded `B_e`. Fixed: continuity (chain well-formedness) is now
    checked independently of freshness (`continuous`, `require_fresh=False`)
    and gates `_overlaps`; freshness alone still gates readiness
    (`usable = continuous and` the fresh check). `_validate_known_overlap_bounds`
    is still reused unconditionally as before, and remains the only guard
    left when there is no gateway at all (continuity itself fails, not
    merely staleness).
11. **OPENING_UNSAFE as an error.** `plan_opening` returns the unsafe
    Proposal as a value. The `OPENING_UNSAFE` error code (exit 2) is for the
    later wrapper that makes an explicit usable-plan request; it is not
    raised here.
12. **`source_events`** for a ready plan: the billing event, every baseline
    snapshot and every coverage event, sorted. Evidence ids are operator
    assertions, not events, and are not listed.
13. **Baseline must cover the drain (fix round tk-dba39941b750, finding 1).**
    The design says "fresh matching reports" without stating an inequality
    against `drain_completed_at`. Fail-closed reading, for the design
    owner: each selected baseline snapshot's `observed_at` must be
    `>= drain_completed_at`. A report from before the drain describes the
    pre-drain ledger and cannot attest the ledger's post-drain, quiet
    state, however fresh it still is relative to `now` (a report can sit
    within the 120s freshness bracket of `now` and still predate the
    drain by any amount, when `lag_seconds` is small). This is distinct
    from, and in addition to, the existing `lastUpdate >= drain +
    lag_seconds` provider-side catch-up test.

## Fix round tk-dba39941b750 (blind read: 1 MAJOR, 2 MINOR)

Branch `card-2b-2b`, still on top of the tests/feat/docs commits above.
Each finding got a failing-first regression test (built from the
reproduction quoted in the work order) in a new
`FixRoundTkDba39941b750` class in `tests/test_card2b2b.py`, plus the
positive control the finding named; two more isolating tests were added
after re-running the mutation tool (below) exposed that two existing
checks had become unreachable by any test once the fixes landed.

| Finding | Fix | Test(s) | Commit |
|---|---|---|---|
| 1 MAJOR - readiness didn't require the baseline report to cover the drain | new `_baselines_cover_drain`: every baseline snapshot's `observed_at >= drain_completed_at`, added to the safety checks in `_opening_safe` (decision 13) | `test_finding1_baseline_before_drain_is_unsafe` (repro: unsafe) + its positive control (snapshot 1.5s after drain: ready, opening 19.00) | (this fix round) |
| 2 MINOR - staleness hid a missing/exceeded explicit-overlap bound | split the old freshness-gated `usable` into `continuous` (chain well-formed, `require_fresh=False`) and `usable` (`continuous` plus freshness); `_overlaps` is now gated by `continuous`, so it still runs on a stale-but-continuous chain (decision 10 correction) | `test_finding2_explicit_floor_needs_a_bound_even_when_stale` (repro at both snapshot ages) + `test_finding2_valid_stale_floor_stays_unsafe_not_invalid` (positive control: a provable stale floor stays unsafe, not invalid) + `test_finding2_known_overlap_bound_enforced_without_a_usable_gateway` (isolates `_validate_known_overlap_bounds`, added after mutation re-run) | (this fix round) |
| 3 MINOR - per-gateway opening arithmetic (`baseline+external`) was unchecked before a verdict | added an unconditional `_checked(baseline+external)` loop over every configured gateway in `_validate_open_request`, right after the existing checked sums | `test_finding3_per_gateway_opening_overflow_is_invalid_before_verdict` (repro) + `test_finding3_isolated_from_the_declared_external_field` (isolates the checked proof-sum from the new per-gateway check, added after mutation re-run) | (this fix round) |

**Evidence.**
- Red: all three reproduction tests failed against f8d8843 (finding 1:
  wrong verdict `ready`/`verified` instead of `unsafe`; finding 2: no
  `QuotaError` raised instead of `INVALID_CONFIG`; finding 3: no
  `QuotaError` raised instead of `INVALID_CONFIG`) - kept as
  `red-fix-round.txt`. The finding-2 positive control already passed
  pre-fix (it was never broken).
- Green: all 4 new tests plus the full `test_card2b2b.py` module (96
  tests) pass after the fix.
- Regression: `test_card2b2*.py` + `test_billing_eval*.py` = 171 tests
  green.
- Mutation re-run on `billing_open.py`: 49 mutations applicable (3 of the
  original 52 patterns no longer match after the `continuous`/`usable`
  refactor - not a coverage loss, the code they targeted moved), all 49
  killed, 0 survivors, plus 4 new mutations targeting the three fixes
  (all killed). The first re-run surfaced 2 survivors -
  `_validate_known_overlap_bounds` and the checked proof-sum - both now
  redundant-*looking* once findings 1-3 landed but each still the ONLY
  guard in a distinct scenario (no gateway at all; a small declared
  external hiding an overflowing proof sum): isolated with the two extra
  tests above rather than deleted. Kept as `mutation-run.txt`.
- Full suite, once, foreground (PYTHONPATH=clone, TEMP/TMP under
  scratch): `Ran 393 tests in 85.0s ... OK` (387 before this fix round +
  6 new: 4 reproductions/controls + 2 mutation-driven isolating tests).
  `git diff --check` clean. `python demo_card2b2b.py` still prints `demo
  ok` (the demo fixture already had a fresh, post-drain snapshot, so it
  was never affected by finding 1).

**Record corrections.** The "Evidence" section above already listed
"51 killed, 0 survived" for the *first* build's mutation sweep, which was
accurate for that sweep; it is not reopened here. What the fix round
corrects is decision 10 (marked inline above) and the module docstring's
claim in `billing_open.py` ("every validation and arithmetic check that
can raise runs BEFORE any safety verdict") - true only after this fix
round's finding-2 and finding-3 fixes; before it, `_overlaps` could be
skipped by staleness (finding 2) and the per-gateway opening sum was
checked only inside the ready branch (finding 3).

## Residual risk

- Mixed-basis deployments cannot plan an opening until the design fixes the
  basis of external amounts (decision 7).
- The catch-up predicate relies on the caller's `complete_at` and
  `matched_provenance` attestation; the planner cannot verify them (by
  design: operator/fixture assertions, not provider telemetry).
- Overlap floors are validated but not used to reduce `D`; if a later card
  wants the planner to use `O_e`, that is a design change.
- Decisions 3, 4, 6 and 13 tighten the design's wording; a cold reader may
  prefer INVALID_CONFIG over unsafe for the stale-chain reference (3).
- No integration with a store, HTTP mapping or application of proposals
  exists or is tested (out of scope).
