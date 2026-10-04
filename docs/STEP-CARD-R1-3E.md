# Step record - Card R1-3e: the pure C3 comparison and its `status` figures, and three notes carried from R1-3d

Work item `card-r1-3e`. Branch `card-r1-3e` from main `07df6dc172e4f0cfdce27ec31b902e3b24efb05f`. Design: `C:\lw\records\DESIGN-R1-3.md` (revision 2, SHA-256
`71c07b9c...3ab9`) sections 5, 8.3, 9 and 10.1. Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only. Synthetic values only.

## In plain words

A gateway report's `settled` covers all settled spend: the receipts Leasewise holds for that gateway and month, plus spend that has no receipt yet. This card
adds a small pure comparison of the two (`quota_balancer/receipt_coverage.py`) and shows two figures in the `evidence` block of `status`:

- `unreceipted_micro_eur`: the spend in the gateway's books without a receipt yet. **Null when coverage cannot be proven**, and never shown as 0 for unknown.
- `receipts_exceed_settled`: true when the receipts counted as covered claim more than the gateway's own month total. That should be impossible.

**Both are information only:** never an error, never a transition, no effect on usability, `adopt_blockers`, the state or any decision. **Today everything is
"mode C"**, because Leasewise's receipts carry no ledger generation: the first figure is null and the second false. A later card gives the receipts a
generation; until then the figures stay null.

## What was built

1. **The pure comparison** (`receipt_coverage.compare`): a report (gateway, generation, `receipts_seq`, month, `settled`) against the final receipts.
   - **Covered** = this gateway, this ledger generation, `seq <= receipts_seq`, in the report's month. A receipt that spans two months (month `None`) is never covered.
   - **Mode C** (`unreceipted` null, exceed false) when `receipts_seq` is null, when a receipt of this gateway that could be covered carries no generation, when a
     covered receipt has no known cost, or when the SOURCE of the receipts records no generation at all (`receipts_carry_generation=False`, below).
   - Otherwise `unreceipted = settled - covered_sum` when `covered_sum <= settled`; when `covered_sum > settled`, `unreceipted` is null and `receipts_exceed_settled`
     is true. Exact whole numbers within the money bounds; a value out of bounds raises `money.MoneyError`, nothing is clamped.
2. **The two keys** in the `status` evidence block (`evidence_view.coverage`, called from `project`), with the block (absent for a gateway with no version-4 report).
3. **The three notes from Codex's R1-3d delta:**
   - **N1 (c), race evidence.** `tests/test_cardr13d_fix1.py` `TwoOperators` no longer sleeps. While A holds the write lock, a probe connection with no busy timeout
     must get "database is locked" (a real database signal), B must not yet have returned from `BEGIN IMMEDIATE`, and B's own call, timed on a monotonic clock,
     began before A let go and returned only after. Three runs in a row were stable.
   - **The F2 sweep wording** in `docs/STEP-CARD-R1-3D.md`: `reading_import`'s early clock serves the future-time refusal and is the one stamp of everything the import
     writes (`received_at`, reader and transition rows, the automatic month roll and its audit row). Stamps, not decisions.
   - **R-018** is unchanged (see the limits).

## Choices made (each tested)

- **Which report and month.** The figures describe the report the block already describes (the newest report, `latest`), and its own month (its period start). The
  design says the covered receipts are those "in R's month", and the block shows `latest`; there was no ambiguity to ask about, but this is the choice.
- **`receipts_carry_generation`.** The lead's brief says everything is mode C today because the receipts carry no generation. With no receipt at all, "a receipt has no
  generation" is vacuous and the comparison would read as covered with nothing receipted, showing the whole of `settled` as unreceipted. So the source is asked: the
  caller (`evidence_view.coverage`) passes False because the `receipts` table has no generation column, and a test fails the day one is added, forcing that line to
  be revisited. The first run of the existing "no amount echoed" test showed the problem (it found 5000 in the block).
- **Receipts without a month.** The `receipts` table records no month either; every row is given month `None` (never covered). Unknown costs make mode C.
- **A receipt that cannot be covered does not force mode C:** another gateway's receipt, or one of another month, is ignored even without a generation.

## Tests (`tests/test_cardr13e_coverage.py`, 23)

`Covered` (6): exactly this gateway, generation, watermark and month (every other kind of receipt is ignored); the watermark is inclusive; a cross-month receipt is
never covered; the difference is information (a real zero is a zero); covered above settled raises only the diagnostic; exact at the largest amount and refused out of bounds.
`ModeC` (7): no watermark; a receipt with no generation; a receipt that could never be covered does not force it; an unknown cost; null is not zero; **a source with no
generation is mode C even with no receipt**; **the over-count of mode C is stated (160 against a true 100)**. `Pure` (2). `StatusFigures` (8): the keys with the block and
absent without a version-4 report; null on the wire with receipts present; null with no receipt at all; **the rest of the block is identical with and without the figures,
even when a diagnostic fires**; the comparison is made for the newest report and its month; no sentence carries a value; the receipts table still has no generation;
**no grant, admission, reporter or store path mentions the comparison or its keys**.

**Red first.** The module and keys are new, so on main the new file fails at import (23 tests). In the existing tests one changed: the R1-3d test that said the two keys
are not there yet now expects them.

## Limits

- Mode C until the receipts carry a generation (a later card): `unreceipted_micro_eur` is null and the diagnostic never fires in practice.
- **R-018:** `evidence_view.newest_report` (the report `status` describes, and so the one these figures describe) orders by row number, which SQLite may renumber on
  `VACUUM`. It chooses what `status` shows and decides no trust. Unchanged by this card.
- Promoting `receipts_exceed_settled` to a transition waits for an integration test of the gateway's receipts against Leasewise's (design 5, Q2).
