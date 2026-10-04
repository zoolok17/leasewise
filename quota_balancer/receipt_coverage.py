"""The C3 comparison: a gateway report against the receipts Leasewise holds (card R1-3e, DESIGN-R1-3.md section 5).

A report's `settled` covers ALL settled spend: the receipts up to its watermark, plus spend that has no receipt yet. So the two are not equal and
the difference is not an error. This pure function (no database, no clock, no file) says two things about one report, for `status` only:

* `unreceipted_micro_eur`: `settled - covered_sum`, the spend in the gateway's books that has no receipt yet. `None` when coverage cannot be proven
  (mode C) or when the covered receipts claim more than `settled`. **Never 0 for "unknown".**
* `receipts_exceed_settled`: True when the covered receipts claim more than the gateway's own month total. That should be impossible; it is a
  diagnostic and nothing more.

Both are INFORMATION ONLY: never an error, never a transition, no effect on usability or on any decision. Nothing in the grant path reads them.

Which receipts a report COVERS: this gateway, this ledger generation, a number at most the report's `receipts_seq`, in the report's month. A receipt
that spans two months (its month is None) is never covered: it is counted from the books, the safe side.

MODE C: when the report says nothing about coverage (`receipts_seq` is None), or a receipt that could matter has no generation (**today every
receipt: Leasewise's `receipts` table has no generation column**), nothing is covered. Then the answer is "unknown" (`None`, `False`), and the
admission arithmetic adds every final receipt of the month (`money.unreported_receipts(receipts, None)`), which counts a settled lease twice. That
over-count is by design and safe; it is stated here and tested, not hidden.
"""

from collections import namedtuple

from . import money

# One report as the comparison sees it. `month` is any comparable value that names the report's month (the stored period start).
Report = namedtuple("Report", "gateway_id generation receipts_seq month settled")
# One FINAL receipt. `generation` is None when the receipt does not carry one (today: always); `month` is None when it spans two months or
# the month is not known; `amount` is None when its cost is not known.
Receipt = namedtuple("Receipt", "gateway_id generation seq month amount")
# `covered_sum` is None in mode C.
Comparison = namedtuple("Comparison", "mode covered_sum unreceipted_micro_eur receipts_exceed_settled")

MODE_COVERED = money.MODE_COVERED
MODE_C = money.MODE_C


def _mode_c():
    return Comparison(MODE_C, None, None, False)


def compare(report, receipts, receipts_carry_generation=True):
    """-> `Comparison`. Exact whole numbers within the existing money bounds; a figure out of bounds raises `money.MoneyError` (never clamped).

    `receipts_carry_generation` says whether the SOURCE of the receipts records a ledger generation at all. Leasewise's `receipts` table does not
    (today), so its caller passes False: coverage cannot be proven even when no receipt is stored, and the answer is mode C. Without it, an empty
    list of receipts would read as "covered, nothing receipted" and show the whole of `settled` as unreceipted, which nothing proves."""
    settled = money.check(report.settled)
    if report.receipts_seq is None or not receipts_carry_generation:
        return _mode_c()                                           # no coverage claim, or no generation to prove one with
    mine = [r for r in receipts if r.gateway_id == report.gateway_id]
    # A receipt of this gateway that could be covered (its month is the report's, or unknown) but carries no generation cannot be placed in a
    # ledger: nothing can be proven covered.
    if any(r.generation is None for r in mine if r.month is None or r.month == report.month):
        return _mode_c()
    covered = [r for r in mine if r.generation == report.generation and r.month == report.month
               and type(r.seq) is int and r.seq <= report.receipts_seq]
    if any(r.amount is None for r in covered):
        return _mode_c()                                           # a covered receipt without a known cost: the sum cannot be proven
    total = money.add(*(money.check(r.amount) for r in covered))
    if total <= settled:
        return Comparison(MODE_COVERED, total, settled - total, False)
    return Comparison(MODE_COVERED, total, None, True)             # the receipts claim more than the books: a diagnostic, never a clamp
