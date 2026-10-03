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
   and it was not in the lead's decisions (accepted as D-168). It is stated in the README and pinned by a test. **It already affects
   advisory admission today**: the advisory budget check and the scope caps apply to advisory leases, so a missing VAT rate can admit
   a lease where a known rate would refuse it. It also remains a gate item before enforcement (`METERED_ENFORCEMENT_ACTIVATED`)
   is switched on. In a shared cap the fallback applies to the unconvertible account's part only.
2. **Gateway receipts are taken as `ex_vat`** (agenttalk's answer C1). Leasewise has no field to check it; the policy's
   `ledger_basis` is what the operator declares. This is residual risk R-016, a convention, not a verified fact.
3. **The conversion is applied to the sum of a group, not to each row,** except in the advisory check, which folds receipt by
   receipt and stops early. Rounding a sum up and summing individually rounded rows differ by up to n - 1 units for n rows (ten rows
   of 1 at 20% give 12 against 20), and **both are at least the exact exposure**, so neither counts less than what was spent.
4. **Holds** keep their stored raw amounts (the stored evidence is never rewritten); the conversion happens at the comparison.
5. **Caps** (`limits.hard`) are read as being in the cap basis, like the budget.
6. **A shared cap is summed per account** (fix round 1, D-172). A team or project cap holds the spending of several accounts. Each
   account's receipts and holds are read separately, converted with that account's own policy (rounded up) and then added; the
   asking account's policy is never applied to another account's spending, and no account is left out. The cap is read in each
   account's cap basis, so accounts that share a cap should declare the same one. (Enforced in fix round 2, decision 7.)
7. **A shared money cap whose accounts declare different cap bases is refused** (fix round 2, D-177). See the section below.

## Tests

**Red first, as measured.** The two first-build files (37 test methods) were run against the code of `9b0838f` (before the first
build). Outcomes, with test methods and subtests kept apart: **309 failed outcomes** (4 failures and 305 errors; of these, 28 methods
errored and 2 failed as a whole, and 279 are failed subtests of the grid tests), **3 methods passed** and **3 subtests passed**.
Many of the failures are only because the new helpers did not exist yet. The three methods that pass against the old code describe
unchanged behaviour: `EvaluatorSums.test_a_known_part_that_passes_the_limit_still_decides_through_the_module`,
`SharesAsLimits.test_an_unknown_basis_converts_nothing` and `Unchanged.test_a_whole_advisory_grant_with_equal_bases_is_unchanged`. The
other tests that pin unchanged answers (the rest of `Unchanged`, and the rounding and overflow tests) also describe behaviour that
already existed, but they cannot run against the old code because they call the new helpers. (An earlier version of this record said
"all 37 failed" and listed more tests as passing before; the lead's note quoted 7 passed methods, which I could not reproduce: I
count 3 passed methods plus 3 passed subtests.)

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

## Fix round 1 (Codex's read of part 2; D-172)

In plain words: a team cap is shared by several accounts, and the first build converted everyone's past spending with the policy of
whichever account was asking. So a second account with another VAT rate could make spending that was already over the cap look
below it, and its lease was admitted. Codex reproduced it through the public advisory grant: a team cap of 1,150 in the invoice
basis; account A (20% VAT) has spent 1,000 excluding VAT, which is 1,200; A was refused and B (10% VAT, no spending) was admitted,
because A's spending was recalculated at B's rate as 1,100.

- **Fix.** `lease._scope_total` reads a shared scope once per contributing account, converts each account's exact total with that
  account's own policy (an exposure, rounded up), and adds with `money.add`. It is used for the scope receipts and the scope holds.
  The account budget is one account's own and is unchanged. An account whose policy cannot convert contributes its raw amount, for its
  own part only (the accepted fallback).
- **Tests** (`tests/test_cardr1_money_shared.py`, 8). Red first: against the previous `lease.py`, 7 of the 8 fail (2 failures, 5
  errors); the one that passes is the cap the spending does not reach. Codex's reproduction through the public grant
  (`test_account_a_is_refused_and_so_is_account_b`), the same for holds
  (`test_open_holds_of_another_account_are_converted_with_that_accounts_policy`), a cap the spending does not reach admits both
  (`test_a_cap_that_the_spending_does_not_reach_still_admits_both`), changing the requester never changes the value of historical
  spending (`test_changing_the_requester_never_changes_the_value_of_historical_spending`), equal bases are the plain sum
  (`test_equal_bases_are_the_plain_sum_whoever_asks`), an account with no policy contributes its raw amount
  (`test_an_account_with_no_policy_contributes_its_raw_amount`), one unconvertible account falls back for its own part only
  (`test_an_unconvertible_account_keeps_its_raw_amount_for_its_own_part_only`), and a huge account stays above every cap
  (`test_the_total_of_a_huge_account_stays_above_every_cap`).
- **Docs.** The D-168 fallback is now stated to affect advisory admission today (decision 1); the rounding statement is corrected
  (decision 3); the README no longer says "every answer unchanged" except for equal bases and no policy; the tests that describe
  unchanged behaviour are named above.

## Fix round 2 (Codex's delta read; D-177): a shared money cap whose accounts declare different cap bases is refused

In plain words: each account declares the tax basis its caps are counted in (with VAT or without). A team, project or platform cap
has no basis of its own. If two accounts that share a cap declare different bases, one account's spending counted with VAT was added to
another's counted without VAT, and the total meant nothing. Codex reproduced it through the public imports and the public grant: a
shared cap of 1,150; A declares `invoice_total` and B declares `ex_vat` (both ledgers `ex_vat`, both at 20% VAT); B settles 1,000 and
A's next advisory grant was admitted; with B switched to `invoice_total` the same spending is 1,200 and A was refused `MONEY_LIMIT`.

- **The rule.** For each scope that has an applicable money limit, the accounts that take part are the requester (even with no
  spending yet) and every account that has a lease in that scope. Only accounts with a billing policy whose `cap_basis` is known
  (`ex_vat` or `invoice_total`) count. If more than one known cap basis remains, the grant is refused with the new closed code
  **`CAP_BASIS_CONFLICT`**. It is checked in one place (`lease._require_one_cap_basis`), before any scope total is compared, so the
  receipt and the hold paths cannot disagree. It applies in advisory mode too, and never to a token limit. The requester's basis is never
  picked and no account's spending is dropped. An account with no policy, or with `cap_basis = unknown`, does not take part: it stays
  the accepted D-168 fallback, for that account's part only.
- **The code.** `CAP_BASIS_CONFLICT`: wire status 409 (a conflict of configuration), exit code 2 (a request condition), one fixed
  message: "accounts that share a spending cap declare different cap bases; give them all the same cap basis". `POLICY_MISMATCH` was
  not reused (it means the request names a revision that is not current), nor `MONEY_LIMIT` (nothing has been spent past the cap).
  The existing code-list tests pass unchanged: they read the registry, so the new word is covered by them (an exit code, a fixed
  message, no two codes sharing a message).
- **What the operator does:** give every account that shares the cap the same `cap_basis` in its billing policy.
- **A consequence worth knowing.** An account that is not asking and has no lease in the scope does not take part, so the account that
  has spent is not refused when it asks: only an account asking *alongside* different declared bases is. Once both have leases, both are
  refused.
- **Tests** (`tests/test_cardr1_money_capbasis.py`, 12; Codex's case through `import_config`, `billing_policy_import`, `grant` and
  `settle`). `Conflict`: `test_mixed_cap_bases_refuse_the_requester_with_the_new_code_for_every_scope` (team, project, platform),
  `test_the_refusal_is_the_same_wherever_the_spending_is` (the other way round), `test_with_one_cap_basis_the_scope_is_evaluated_normally`
  (refused `MONEY_LIMIT` at 1,200 against 1,150, admitted against 1,200), `test_the_conflict_is_found_again_when_it_is_introduced_later`,
  `test_once_both_accounts_have_a_lease_in_the_scope_both_are_refused`, `test_nothing_is_written_by_a_refused_grant`. `DoesNotTakePart`: no policy, `cap_basis = unknown`, a requester with no policy, a cap
  used by one account only, and token limits. `TheCode`: `test_the_new_code_is_defined_everywhere_a_code_must_be`. **Red first:** against
  the previous `lease.py` (with the code defined), 5 of the 12 methods fail (9 failed outcomes, counting subtests) and 7 pass; the 7
  describe behaviour that does not change. Against the previous code as a whole, 10 of 12 fail (the code does not exist).
- **Stated limits, recorded as follow-ups and not built (the stopping rule):**
  1. the conflicting configuration is not refused when it is imported (`validate_policies`, `import_config`,
     `billing_policy_import`); it shows only when a lease is asked for;
  2. `money.saturate(UNKNOWN)` raises `TypeError` (Codex L1, latent: no current caller passes UNKNOWN);
  3. a shared scope costs one query per contributing account (Codex's cost note).
