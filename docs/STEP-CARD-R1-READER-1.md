# Step record - Card R1-READER, step 1: the reader's pure half, the runner's report mode, the month rows of the receipt page

Work item `card-r1-reader-1`. Branch `card-r1-reader-1` from main `a690e93a393467f0bbbb2d83e7734208e5b7b6fb`. Design: `C:\lw\records\DESIGN-R1-READER.md`
(revision 3; the "Lead decision D-195" block wins where they differ). Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only. Synthetic
fixtures, and agenttalk's own documented output (merged head `921c82d4d5b94d133dd954f223c55e663c132cc1`, agenttalk PR #316). No real gateway, key or
provider is touched.

## In plain words

The reader is the small program that will read a gateway's books and send Leasewise a numbered report. This step builds the parts that **read and
compute**, with no state, no sending and no live gateway. **Nothing reads a real gateway, and nothing here changes any decision of Leasewise.**

The names the reader expects now come from agenttalk's merged code and guide, not from guesses. Live use still waits: the operator's maintenance
window (the live gateway runs an older agenttalk on purpose), R1-3d adoption, steps 2 to 4, and Codex's read of this step, which includes the check
against the merged head.

## What was built

1. **The record was folded** (`DESIGN-R1-READER.md`, revision 3): the D-195 rule (the saved bytes are deleted only when Leasewise accepts them)
   replaces every rule that drops or rebuilds a pending report; the two progress numbers `minted_seq` and `acked_seq` are in the state tables;
   `resume` and `reset` are the operator commands; agenttalk's answers (D-194) are written in; Codex's wording points are applied; every guessed
   name is replaced by the merged one with its source; the unowned figure keeps the qualification "not exact monthly lease attribution". The D-195
   block stays as the decision log, with a line saying it is folded.
2. **The runner's `report` mode** (`runner.py`): a closed mode that appends nothing. `--json` is required by `agenttalk gateway report`, so it is part
   of the mode's fixed command, `runner.REPORT_TAIL` (`gateway report --json`): the configured command must end with it, otherwise nothing is started.
   `receipts` is the default and unchanged. An unknown mode, or a receipts run without whole numbers, starts nothing. Containment, the deadline, the
   bounded read and the closed words are the same. The command's own failure words (`bad_request`, `report_unavailable`, exit code 2) arrive as the
   runner's existing `exit_code`: standard error is the NUL device and is never read or kept.
3. **The merged contract in one place** (`gwcontract.py`): every key name, both versions, the four hold words, with their source in agenttalk
   `921c82d4` (file and line) in the header.
4. **The report projection** (`gwreport.parse_snapshot`): strict JSON, `gateway_report_version` exactly 1, `child_receipt_report_version` exactly 2,
   an exact check of every field used (types, bounds: money 0 to 10^12, receipt number 0 to 2^63-1; hex lengths; months `YYYY-MM` in ASCII digits; no
   month twice), every other key ignored, nothing named kept, and an object that prints nothing of itself. `child_cap_ready` is checked first: false,
   a null `child_receipt_report_version` or a null `child_receipts_through_seq` is `child_cap_not_ready`; every other fault is `status_shape`. A hold
   needs one of the four words and no hold needs none.
5. **The receipt page with month rows** (`receipts.py`): the envelope stays version 1. `parse_page` and `read_page` take `month_rows`: the existing rows
   (default, unchanged) or month rows (`charge_period` required, a month or null, all other keys closed as before). Month rows are a `MonthReceipt`;
   existing rows stay plain `Receipt`s with the same fields as before.
6. **The aggregation** (`gwreport.fold_page`, `Totals`): receipts are added only up to the snapshot's own `N`, in its generation, even when a page
   returns more; a receipt with no month is in no month; a gap or a repeat against the saved position is damage; a snapshot with an `N` below the saved
   position is `receipts_rolled_back`; a page read in the existing row shape is refused; the input totals are never changed.
7. **The computation** (`gwreport.compute`), exactly as the worked example: `unattributed = committed(P) - covered(P) - U(P)`, refused below 0 and never
   clamped; `settled` is 0 only when every period row is earlier, and a later row is `period_mismatch`; `reserved`, `uncertain` and
   `held_over_reservation` as the example; `quiet` derived from the report version (true for version 1); `sized_ready` always false.

## Compatibility finding: who reads receipt pages on main

`receipts.parse_page` and `receipts.read_page` have **one caller outside `receipts.py`: the reporter cycle, `reporter/run.py` `_take_pages`** (card L6a-1,
the receipts that settle seat leases). It reads **existing rows**. Against an agenttalk at `921c82d4` or later every row has the extra key
`charge_period`, so that caller would refuse every row (`bad_row`, counted as `refused:bad_row`). It is safe (nothing wrong is stored or sent, and
sending of other uploads is not affected) but that path would go blind the day the live gateway is upgraded. **So the answer to "could any caller meet
such a gateway" is yes, once the maintenance window upgrades it.** This step does not change that caller (the brief says to change it only if the
answer is yes, and the right change is a design choice, not a side effect of a pure-reader step). It is reported to the lead as a decision: before the
upgrade, run.py must read month rows (and ignore or keep the month). A test pins that run.py still reads existing rows, so the choice cannot be made by accident.

## Choices made (each tested)

- **A new closed word, `amount_invalid`,** for a sum that is not an amount (an overflow). The design says such a sum is refused and never clamped but
  names no word. No real report can reach it (each amount is at most 10^12); the guard is proven on a snapshot built by hand.
- **The hold reason** is checked to be one of the four words (and to be present exactly when there is a hold), then dropped.
- **A missing key is damage; an explicit null is "not installed".** A report without `child_receipt_report_version` or `child_receipts_through_seq` is
  `status_shape`; the same keys null are `child_cap_not_ready`.
- **Months are matched with `[0-9]`, not `\d`.** The first run of the tests showed that `\d` accepts digits of other scripts (agenttalk's own checker
  refuses them); both month patterns are ASCII only.

## Tests (`tests/test_cardr1reader1_report.py`, 58)

`Projection`: the good snapshot; nothing named kept; ignored keys at every level (including `period` on unresolved rows); every required field
missing; every field mistyped or out of range; the 10^12 boundary; an unknown version in both directions; no child caps or no lease binding (five
shapes, one word); the four hold words and the hold/word pairing; strict JSON; an empty report. `AgenttalkOwnOutput`: **agenttalk's documented
report and page, copied as printed, are read and give October's unowned money 2630 - 1290 - 670 = 670**; the existing row reader refuses that page; every documented key
is present; the report without binding gives the not-ready word. `ReceiptPage`: month rows; envelope version 1 only; existing rows unchanged;
a row of the other shape refused; closed keys and the month (including other-script digits); order and gaps; run.py still the only caller.
`WorkedExample`: the baseline, **the five cases with their exact figures** (agenttalk's own ledger tests have the same five), and the balances.
`ReceiptBoundary`: a receipt above `N` ignored, another generation refused, a null month and a zero-charge receipt, other months, accumulation
across pages, a gap or a repeat, a watermark of 0, a rollback, an incomplete read, another generation's totals, an existing-shape page refused.
`Figures`, `ReportMode` (Windows): nothing appended after the fixed tail, a command without the tail starts nothing, an exit code is
`exit_code`, the receipt mode unchanged by name and by default, an unknown mode or a receipt run without numbers starts nothing, the same containment
and byte limit.

**Red first.** The pure modules and their table are new, so on main the whole file fails at import. The tests were written against the draft of the
first session; on the first run against the rebuilt code three failed and found real defects, fixed in the same step: months written with other
scripts' digits were accepted, a missing `service_hold_reason` was accepted, and the old overflow test no longer matched the 10^12 bound.

**Counts.** 58 tests in the new file, all green. The whole suite: 2233 tests (`discover -s tests`, no `-t`) in four chunks that cover every one of the 93
test files exactly once (897 + 326 + 440 + 570 = 2233), all green. Ruff (E4, E7, E9, F, B): 74 findings, all in files this step did not change
(none in the new or changed files; 4 in the new test file were fixed). Bandit: 24 findings (5 low, 18 medium, 1 high), none in the new or changed files;
no `nosec` added (4 existing).

## Existing tests

None changed. The runner's and the receipt reader's existing tests pass unchanged (the defaults are the old behaviour). `Receipt`'s fields are
unchanged; the month lives on a subclass for month rows only.

## Honest notes

- Nothing reads a live gateway and nothing is sent. No state exists yet (steps 2 and 3).
- The names are agenttalk's merged ones, but Codex's read of this step must still do the merged-head check before any live use.
- The cycle (step 3) must always read month rows when it uses a report with `child_receipt_report_version` 2; `fold_page` refuses existing-shape rows.
- The unowned figure is **not exact monthly lease attribution** (agenttalk's documented limit): a cross-month receipt's shares stay in it. Wherever
  Leasewise shows it, it keeps that qualification.
