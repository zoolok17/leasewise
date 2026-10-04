# Step record - Card R1-M follow-up (R1-M-FU): mismatched cap bases refused at import, unknown kept through saturate, one listing per scope

Work item `card-r1-m-fu`. Branch `card-r1-m-fu` from main `c48ee96baf88f72426db4f81bb1b3295f5f6d0f2`. Interpreter: CPython 3.13
(`C:\lw\venv`), standard library only. Audience: the lead and the reviewers. Synthetic data only. The three follow-ups Codex recorded when
R1-M part 2 merged (M-018; `docs/STEP-CARD-R1-M2.md`, "Stated limits").

## In plain words

- **Tell the operator earlier.** When accounts that share a spending limit declare different tax bases, a lease was refused
  (`CAP_BASIS_CONFLICT`), but only when someone asked for one. The configuration import and the billing-policy import now refuse such a
  set-up straight away, with the same word and nothing written. The lease check stays: it protects stores made before.
- **"Unknown" survives one more step.** `money.saturate` turned an unknown total into an error. It now returns unknown.
- **Fewer queries.** A lease request listed a scope's accounts, and read each account's policy, again for every use. It now does each
  once per request. No answer changes.

Nothing changes for any lease decision, except that the first item refuses earlier.

## What was built

1. **Refusal at import** (`store._refuse_mixed_cap_bases`). It runs **inside** each import's transaction, after the import has been applied and
   before the commit, so it sees the state the import would leave (the policies and the membership as they will be) and a refusal rolls
   everything back: **nothing is written**. Both paths: `import_config` (a membership or limit change; also run after a version-1 import)
   and `billing_policy_import` (a policy change).
   - **The word:** `CAP_BASIS_CONFLICT` is reused. Both imports raise a `QuotaError` that the command line prints with its fixed message
     (a code, never a value), exactly as they already do for `INVALID_CONFIG` and `SCHEMA_UNSUPPORTED`, so they can carry it, and it means
     the same and asks the same of the operator (give every account that shares the cap the same cap basis). The message is fixed and names
     no account. The import error contract (a static word, never a store error) is kept.
   - **Which accounts take part:** for every money limit, the accounts that belong to its scope under the configuration being imported: a
     **project** limit, the accounts linked to the project; a **team** or a **seat** limit, the accounts of the team's project (a team and
     a seat belong to a project; this is the set of accounts a lease for them can use, a superset of the accounts that happen to have a
     lease today); the **platform**, every account. Seat limits are included for parity with the lease check, which guards every scope.
     Only accounts with a billing policy whose cap basis is known (`ex_vat` or `invoice_total`) take part; an account with no policy or an
     `unknown` basis does not, as at lease time.
   - **Re-importing an already mismatched store is refused until the bases are aligned.** A store made before this check can hold a mismatch.
     Every import (even an unrelated re-import of the configuration) is then refused. The way out is a billing-policy import that aligns the
     bases, which is accepted because the check sees the aligned result.
   - The lease-time check is unchanged.
2. **`money.saturate(UNKNOWN)` returns `UNKNOWN`.** Callers checked: `lease._in_cap` (a plain number), `lease._scope_total` (a sum of numbers
   from `_in_cap`) and `money.convert_total` (which already returns UNKNOWN before it saturates). None passes UNKNOWN today, so nothing changes;
   none can now turn an unknown into a number, and a future caller that passes one gets UNKNOWN back and must decide what it means.
3. **One listing per scope per request** (`lease._RequestFacts`). A request creates one object that reads the accounts of each scope and each
   account's policy the first time and keeps them for the request, which holds one write lock, so what was read cannot change under it. The
   cap-basis guard and both totals (receipts and holds), for every scope and every group, use it. No answer changes. **Measured on a grant for
   a team with two accounts that have leases:** statements that list a scope's accounts, 7 before and 4 after (one per scope of the chain);
   statements that read a policy, 8 before and 2 after (one per account). **The wording is corrected:** the number of queries grows with the
   accounts that have leases in a scope (one policy read per such account, once per request), not "one query per account" for every use.

## Tests (`tests/test_cardr1_money_fu.py`, 12)

- `Refusal`: `test_the_policy_import_refuses_a_mismatch_for_every_scope_and_writes_nothing` (team, project, seat, platform),
  `test_the_config_import_refuses_a_membership_change_that_creates_a_mismatch_and_writes_nothing`,
  `test_the_config_import_refuses_a_new_money_cap_that_shares_mismatched_accounts`,
  `test_the_platform_cap_spans_every_account_even_outside_the_project`, `test_an_aligned_set_up_is_accepted_by_both_imports`,
  `test_an_account_with_no_policy_or_an_unknown_basis_never_triggers_the_refusal`,
  `test_a_cap_on_another_metric_never_triggers_the_refusal`,
  `test_a_store_that_already_holds_a_mismatch_is_refused_on_every_import_until_the_bases_are_aligned`. "Writes nothing" compares every table the
  imports can write before and after, and the fixed message is checked.
- `Saturate`: `test_saturate_keeps_unknown_and_changes_nothing_else` (including `saturate(add(1, UNKNOWN))`),
  `test_a_converted_total_that_cannot_be_converted_stays_unknown_through_both_helpers`.
- `OneListing`: `test_a_grant_lists_each_scope_once_and_reads_each_policy_once_and_answers_as_before` (counts the statements of one grant),
  `test_the_answers_are_the_same_with_the_shared_listing`.

**Red first:** against the previous code, 8 of the 12 methods fail (14 failures and 2 errors, counting subtests): all five refusal tests that
describe a refusal, both `Saturate` tests, and the query count (7 listings against 4). The other four describe behaviour that does not change
and pass before and after (an aligned set-up, no policy or an unknown basis, another metric, and the answers).

## Existing tests

**One file moved, for a stated reason.** `tests/test_cardr1_money_capbasis.py` built its mixed set-up through the public imports, which now
refuse it. It now builds the set-up in two steps: the public imports with ONE cap basis (including the configuration import "after the
policies exist"), then B's declaration changed directly in the store, which is exactly a store made before the check. Every assertion in it
is unchanged. All other existing tests pass unchanged.

## Honest notes

- Team and seat limits use the accounts of the project, a superset of the accounts that lease today, so the import can refuse a set-up in
  which the mismatched account has not leased yet. That is the point of refusing earlier.
- The check reads the whole `limits` table once per import; a store with no mixed known bases returns immediately.
