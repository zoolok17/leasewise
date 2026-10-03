# Step record - Card R1-M: one money basis with checked arithmetic (part 1)

Work item `card-r1-m`. Branch `card-r1-m` from main `a434394979e10040a768837d10cb367cd315e3f3`. Design:
`C:\lw\records\DESIGN-R1-MONEY.md` (SHA-256 `febf94b02ad4a08d2ef70ed8bd54c4dc55d4c28d750aa467633a2a2d4510b500`), sections 2 to 6, with
the lead's decisions Q1 to Q9 (register D-155). Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only. Audience: the lead and
the reviewers. This card is steps R1-M1 to R1-M3; R1-M4 and R1-M5 wait for agenttalk's 6a-1 confirmations (C1, C3, C4 of the design).

## In plain words

Before Leasewise lets one account's spending decide anything, every amount it adds or compares must be exact and mean the same thing.
Two problems were found in the code and are fixed here:

- The database's own adding (`SUM`) stops with an error at very big numbers, and that error was reported as "the store failed"
  (residual risk R-013) instead of "the budget is used up". Admission and the gateway shares now add exactly, so an absurd total is
  the closed refusal it means.
- Money arithmetic had no single home. It now has one small module with the rules written down and tested.

What does not change: at normal sizes every answer is the same as before. What is not built yet: conversions between tax bases on
the lease side (R1-M4) and the "unknown" terms of the gateway evidence (R1-M5).

## Step 1: R1-M1 (the module) and R1-M2 (admission sums)

### What was built

- `quota_balancer/money.py` (new, pure):
  - `check` (a whole non-negative number, at most a given maximum; floats, booleans, negatives and text refused), `saturate`, the interface
    constants (`INT64_MAX`, `SATURATION`, `WIRE_MAX` 10^12 for the reporter's receipt wire, `ADMIT_WIRE_MAX` 10^12 - 1 for the admission answer);
  - `UNKNOWN`, one object that is never 0 and cannot be used as a number or a truth value; `add`, `lower_bound`, `exceeds`, `reaches`
    (stop at the first point that decides; a known part that already passes the limit decides even if other terms are unknown),
    `headroom`;
  - `convert(amount, source, target, vat_bps, role)`: the existing exact tax-basis formula, rounded up for an **exposure** and down
    for a **limit**; `UNKNOWN` when a basis is unknown, the rate is missing or out of range, or the result would not fit;
  - `exact_sums` and `exact_sum`: the split sum (below), with a row-count guard and a guard for negative amounts.
- `lease.py`: `_money_sums` and `_hold_exposure` replace the four `SUM` queries of `_admit_group` (S1 scope receipts, S2 scope open
  holds, S3 account receipts, S4 account open holds). The same query text served money and input tokens, so tokens are fixed
  with it. The totals reach `policy.check_admission` saturated at `policy.USAGE_MAX` = 2^63. `_advisory_budget_check` uses `money.reaches`.
- `policy.py`: `USAGE_MAX`, and the two usage checks of `check_admission` accept it (a cap is still at most 2^63 - 1).

### The split sum, and why it is exact

For a non-negative number x, x = (x >> 32) * 2^32 + (x & 4294967295). The database adds the two halves separately and Python
puts them together. Each high half is below 2^31 and each low half below 2^32, so up to 2^31 - 1 rows cannot reach 2^63. The
query also counts the rows and refuses more than 2^31 - 1 (a closed store fault; it cannot happen on this store, and the guard makes the
claim checked). For the hold exposure the two columns (`reserved`, `COALESCE(settled_actual, 0)`) are added separately and
subtracted in Python, and a guard counts holds settled above what they reserved (a broken invariant, refused as a store fault instead of
a silent negative). Nothing adds money with `+`, `-` or `*` inside SQL (those turn into decimals at overflow).

### Tests (`tests/test_cardr1_money.py`, 42)

| What | Where |
|---|---|
| range: 0 and 2^63 - 1 accepted, 2^63, 2^64, -1, booleans, floats, text refused; both interface bounds pinned and tested (the reporter wire takes 10^12 and refuses 10^12 + 1, storage takes 2^63 - 1); saturation | `Range` |
| exact sums beyond the stored range; `exceeds`/`reaches` stop at the first point that decides (a generator is not read further); a total above every bound is answered, not raised | `Sums` |
| unknown is never zero: a sum with an unknown term, the object cannot be used as a number or truth value, a known part that passes the limit still decides, an unknown limit is unknown | `Unknown` |
| each conversion and each role (hand-computed table at 0, 1, 2000 and 10000 bps; a limit is never larger and an exposure never smaller than the exact value; the exposure rule equals the existing `reduce.convert_basis` on 500 random cases; unknown basis, bad rate, a result that does not fit are UNKNOWN; non-amounts refused; euro only at the door) | `Conversion` |
| the split sum equals a Python sum on 3,000 adversarial rows (edge values 0, 1, 2^31, 2^32, 2^62, 2^63 - 1, many rows at the largest amount) where the database's own `SUM` raises; no rows is 0; NULL adds nothing; a total of exactly 2^63; the row-count guard; a negative amount; the violation guard | `Table` |
| S1 to S4, each with a total above the largest number (before: a raw database error, in a real grant `STORE_ERROR`; now `MONEY_LIMIT` or `INPUT_LIMIT`), just below the bound (exact, the old decision), and normal sizes; the hold exposure subtracts settled amounts exactly; a hold settled above what it reserved and too many rows are `STORE_ERROR`; the real schema's CHECKs forbid negative amounts in every summed column | `AdmissionSums` |
| a whole enforced grant after overflowing receipts is `MONEY_LIMIT`; at normal sizes it is admitted with its hold as before; the advisory check stops at the cap and reads lazily | `WholeGrant` |

Red first: against the previous `lease.py` and `policy.py`, 9 of the 16 admission tests fail. The older tests, including the R1-2 test
that pins the advisory check at 2^63 - 1 (`test_cardr1_expiry`), pass unchanged.

Mixed signs: the schema forbids negative amounts by `CHECK(... >= 0)` on `receipts.actual_micro_eur`, `receipts.input_tokens`, `holds.reserved`
and `holds.settled_actual`, and a test shows the database refusing each. `exact_sums` still counts a negative if a store ever has one (a
table without the CHECK in the tests) and refuses the sum.

## Step 2: R1-M3 (the gateway shares)

### What was built

- `store._allocated` replaces the three `SUM(share_micro_eur)` queries: S5 (importing a billing policy), S6 (ingesting a provider
  reading) and S7 (`status`). The total is exact; the allocation check "shares + allowance <= cap" then refuses a huge one as
  `INVALID_CONFIG` with the existing sentence (before: `STORE_ERROR`), and `status` shows it exactly. This holds **when the tax basis
  is known**. When the account's `ledger_basis` is `unknown`, the allocation check is postponed as before (`allocation_within_cap`
  defers: accepting the evidence is not approving the allocation), so a huge allocation is accepted there. That was kept on purpose.
- `status` gains **`allocated_above_int64: true`** on an account whose allocated total is above 2^63 - 1, and no new key otherwise (every
  ordinary account has exactly the old keys). The design called this flag `allocated_at_least`; it is renamed because the figure is exact,
  not a lower bound.

### Tests (`tests/test_cardr1_money_store.py`, 11)

S5: shares that add up above the largest number (and a total of exactly 2^63 - 1 above the cap) are `INVALID_CONFIG` and write nothing; normal
shares import as before; the boundary at the cap is unchanged. S6: a reading after the shares grew past the largest number is
`INVALID_CONFIG`; a normal reading is accepted. S7: an allocation above the bound is exact and flagged; exactly 2^63 is flagged and one less is
not; an ordinary account has exactly the old keys and the old figure; an account with no gateway allocates 0. The helper refuses a damaged store
with a closed error. Red first: against the previous `store.py`, 5 of the 11 fail.

## Fix round 1 (Codex's read)

In plain words: the seven sums and the admission decisions were right. Three small things were put right.

- **F1.** `money.reaches(terms, 0)` answered False for nothing to add, or only unknown amounts. A total of non-negative amounts has always
  reached 0, so the answer is now True, decided before any term is read. The strict `exceeds` is unchanged (`exceeds([], 0)` is False,
  `exceeds([UNKNOWN], 0)` is UNKNOWN). No grant was wrong, because the only caller passes a positive budget; later steps will use it.
- **L1.** The text above claimed an overflowing allocation is always refused. It is refused when the tax basis is known; with an
  unknown ledger basis the check is postponed, and that is kept (decision of the lead). Four new tests pin both cases at policy import
  and at reading ingest (two shares of 2^63 - 1: accepted with `unknown`, `INVALID_CONFIG` with a known basis).
- **N1.** The README now names the two wire bounds separately: 10^12 for receipt money, 10^12 - 1 for admission-answer money.

Tests: one new in `test_cardr1_money.py` (red against the old helper), four new in `test_cardr1_money_store.py` (pin existing behaviour).

## Gates

See the final report of the card (suite totals, scanner counts, the `nosec` list).

## Honest notes

- The conversion helper is built and tested but **no admission code calls it yet**: the behaviour change (rounding by role, the declared
  basis on the lease side) is R1-M4, which waits for agenttalk's confirmation of the basis of receipts and reports.
- The README sentence that the model rates are in the ledger basis is written as "meant" and becomes a rule with R1-M4.
- Line numbers in the design were re-checked against `a434394` before the change; they matched.
