# Step record - Card R1-M, part 2: the declared basis on the lease side (R1-M4) and unknown through the sums (R1-M5)

Work item `card-r1-m2`. Branch `card-r1-m2` from `card-r1-m` at `9b0838ff2ab3cf8efa2c1b97a9c819dffdb91c15`. Design:
`C:\lw\records\DESIGN-R1-MONEY.md` (SHA-256 `9619a4de39b2bc1a34a49742d2fc03e482aa497980ae588adf6d03fb7b14f96a`), sections 2 to 6 and 9
(agenttalk's answers C1, C3 and C4). Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only. Audience: the lead and the
reviewers. Synthetic amounts and policies only.

## In plain words

Part 1 made all money adding exact. Part 2 makes Leasewise compare money in one declared unit, and keeps "unknown" from ever turning
into zero.

- **One unit on the lease side (R1-M4).** An account that has a billing policy has said which tax basis its budget is in (the cap
  basis) and which basis the gateway's money is in (the ledger basis). Before admission compares anything, the receipts, the open
  holds and the new hold are converted into the cap basis and **rounded up**, so money that is spent never counts for less than it
  is. A gateway share used as its own ceiling is rounded **down**, so a limit never allows more than it is.
- **Unknown stays unknown (R1-M5).** A total with an unknown term is unknown, not the known part and not 0. The evaluator then shows
  no headroom. The terms of the admission inequality are now built as known or unknown, and when a gateway's report does not say
  which receipts it already includes, **all** final receipts of the month are added: an upper bound that counts a settled lease
  twice, which is safe and stated.

What does not change: with equal bases (every account today: `ex_vat` and `ex_vat`), and with no billing policy, every answer is the
answer it always was. No existing test expectation changed (see "Existing tests").

## What was built

### R1-M4

- `reduce.convert_basis(amount, source, target, vat_bps, role="up")`: a rounding role. `"up"` is the default, so every earlier caller
  (`validate_policies`, the evaluator) is unchanged; `"down"` floors. A role that is neither is `INVALID_CONFIG`.
- `money.convert_total`: a conversion for an **exact total**, which can be above every stored bound (a sum of many amounts). The answer
  is exact up to 2^63 and saturated at 2^63 above it, so a huge total stays above every cap in both directions of conversion. It
  answers UNKNOWN exactly as `money.convert` does when a basis is `unknown` or the rate is missing. `money.convert` keeps its
  stricter bound and is now built on the same internal step.
- `lease._lease_basis(conn, account_id)` reads the account's policy and returns `(ledger_basis, cap_basis, vat_bps)`, or None.
  `lease._in_cap(amount, basis)` converts a lease-book amount (rounded up, saturated). `lease._admit_group` takes the basis and
  converts, **for money only**, the scope receipts, the scope holds, the account receipts, the account holds and the new hold; the
  advisory budget check converts each receipt. `_hold_exposure` now returns the exact exposure (it was saturated inside); the caller
  converts and saturates.
- One new hold that is already above every cap after conversion is refused as `MONEY_LIMIT` at once.
- `billing_eval._normalize_money` keeps `share_micro_eur` (the allocation against the cap, rounded up, exactly as before) and adds
  `share_limit_micro_eur` (the ceiling of that gateway, rounded down). `billing_open._allocations_exact` reads the ceiling, because
  "opening within its share" compares against a limit.

### R1-M5

- `billing_eval._sum` and `_checked` are `money`'s: UNKNOWN passes through, an overflow or a value that is not an amount is still
  `INVALID_CONFIG`. `billing_open` uses the same two functions. The evaluator's headroom is `money.headroom` of the total, so an
  unknown term gives "unavailable" and no headroom, never a manufactured room.
- `money.gateway_exposure`, `account_exposure`, `unreported_receipts` (with `MODE_C` and `MODE_COVERED`) and
  `coordinator_headroom`: the terms of DESIGN-R1 3.3 and 3.4. Nothing calls them yet: the reducer that does is R1-3. They are built
  and tested now so that R1-3 does no arithmetic of its own.

### The rule for R1-3 (agenttalk's answer C3)

No code path compares a gateway report with receipts today, so no code was added. The rule R1-3 must follow: a report's `settled`
covers **all** settled spend, that is, the receipts up to its watermark **plus spend that is not (yet) receipted** (turns with no
lease reference, and bound turns whose receipt is still pending). So compare **per receipt** up to the watermark, and count the
remainder as unreceipted spend. A remainder is never an error by itself. The earlier assumption that the two are equal is replaced by
this rule. A reserved or uncertain attempt is charged at the reserve rates, and its receipt carries the charged amount.

## Decisions the code makes, and what to know

1. **A policy that cannot convert leaves the lease books as they were.** If the policy's ledger or cap basis is `unknown`, or the
   bases differ and no VAT rate is given, `_lease_basis` is None and admission compares the books as it always did. The design said an
   unknown term should make enforced admission refuse. I did not do that: it would change the answers of accounts that exist today
   and it was not in the lead's decisions. It is stated in the README, pinned by a test, and is a question for the lead. (Only enforced
   metered leases are affected, and those are off: `METERED_ENFORCEMENT_ACTIVATED` is False.)
2. **Gateway receipts are taken as `ex_vat`** (agenttalk's answer C1). Leasewise has no field to check it; the policy's
   `ledger_basis` is what the operator declares. This is residual risk R-016, a convention, not a verified fact.
3. **The conversion is applied to the sum of a group, not to each row,** except in the advisory check, which folds receipt by
   receipt and stops early. Rounding the sum up never counts less than the exact value and counts at most one micro-EUR less than
   rounding each row up.
4. **Holds** keep their stored raw amounts (the stored evidence is never rewritten); the conversion happens at the comparison.
5. **Caps** (`limits.hard`) are read as being in the cap basis, like the budget.

## Tests

Red first: against the previous code, all 37 new tests fail (4 failures and 305 errors, counting the subtests). The tests that pin
unchanged answers (`Unchanged`) describe behaviour that already existed; they pass before and after, and are there so that they keep
passing.

`tests/test_cardr1_money_basis.py` (17):
- `Different`: `test_the_account_receipts_are_converted_up_before_the_budget_is_compared`,
  `test_a_fraction_of_a_micro_euro_is_counted_up_never_dropped`, `test_the_open_holds_and_the_new_hold_are_converted_up_too`,
  `test_a_scope_cap_compares_its_receipts_and_holds_in_the_cap_basis`, `test_the_other_direction_is_rounded_up_as_well`,
  `test_one_new_hold_that_is_above_every_cap_after_conversion_is_refused_as_the_limit_it_is`,
  `test_a_huge_total_stays_above_every_cap_whichever_way_it_is_converted`,
  `test_the_advisory_check_of_a_whole_grant_converts_each_receipt_up`, `test_tokens_are_never_converted`,
  `test_the_conversion_the_lease_books_use_is_read_from_the_policy`.
- `Unchanged`: `test_no_policy_compares_the_books_as_they_are`, `test_equal_bases_change_nothing` (both `ex_vat` and both
  `invoice_total`), `test_a_policy_that_cannot_convert_leaves_the_books_as_they_were` (four policies),
  `test_a_whole_advisory_grant_with_equal_bases_is_unchanged`.
- `Helpers`: `test_convert_basis_rounds_up_by_default_and_down_for_a_limit`,
  `test_a_limit_is_never_larger_than_the_exact_value_and_an_exposure_never_smaller`,
  `test_convert_total_takes_an_exact_total_above_every_stored_bound`.

`tests/test_cardr1_money_unknown.py` (20):
- `EvaluatorSums`: `test_an_unknown_term_makes_the_sum_unknown_and_never_the_known_part`,
  `test_an_overflow_is_still_invalid_config_and_so_is_a_value_that_is_not_an_amount`,
  `test_checked_lets_unknown_through_and_refuses_what_the_store_could_not_keep`,
  `test_the_headroom_of_an_unknown_total_is_unknown_not_a_number`,
  `test_a_known_part_that_passes_the_limit_still_decides_through_the_module`.
- `SharesAsLimits`: `test_a_share_rounds_up_as_an_allocation_and_down_as_its_own_ceiling`,
  `test_the_ceiling_is_never_above_the_exact_value_and_the_allocation_never_below`,
  `test_equal_bases_give_the_same_figure_for_both_uses`, `test_an_unknown_basis_converts_nothing`,
  `test_the_opening_planner_reads_a_share_as_its_ceiling`.
- `Terms`: `test_a_gateway_exposure_is_settled_plus_reserved_plus_uncertain`,
  `test_no_usable_report_or_an_unknown_part_is_unknown_never_zero`, `test_one_gateway_without_evidence_makes_the_account_unknown`,
  `test_missing_coverage_is_mode_c_an_upper_bound_never_unknown_and_never_zero`,
  `test_with_coverage_only_the_newer_receipts_are_added`, `test_mode_c_is_never_below_the_covered_figure`,
  `test_the_coordinator_headroom_is_unknown_when_any_term_is`.
- `ConservativeInequality` (DESIGN-R1 12.5): `test_the_components_agree_and_the_coordinator_is_never_larger` (a grid of six basis
  pairs, five reports, `Q` of 0, 1 and 777, `H` of 0, 5 and 1000; the components agree and the coordinator's headroom is the
  evaluator's minus exactly `Q + H`), `test_a_test_that_passed_with_equal_headroom_would_be_wrong`,
  `test_an_unknown_gateway_makes_both_unknown_so_neither_admits`.

## Existing tests

No existing expectation changed. The `test_billing_eval_fix*` suites, the evaluator and opening suites, and every `ex_vat` and
`ex_vat` test pass untouched; the rounding of a share as a limit shows only when bases differ and a share is read as a ceiling, and
no existing test does both.

## Honest notes

- The lease-side conversion is built and tested, but enforced metered leases are still off (`METERED_ENFORCEMENT_ACTIVATED`); the
  advisory budget check is the path a live account meets today.
- The terms of R1-M5 are not called by any code yet (R1-3 does). The inequality tests exercise them with the evaluator's real
  normalisation, so the agreement is checked against real code, not against a copy.
- The unknown-basis choice (decision 1 above) deserves the lead's eye.
- Not done, by decision: no change to `validate_policies.py`; no per-gateway comparison of reports with receipts (no such path yet).
