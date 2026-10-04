# Step record - Card R1-3-tidy: three small clean-ups after R1-3d and R1-3e

Work item `card-r1-3-tidy`. Branch `card-r1-3-tidy` from main `c163535d6387d548bea5cac25ec5432f845f5d09`. Standard library only, synthetic values only.

## In plain words

Three small things Codex asked for. Nothing the operator sees changes.

1. `status` no longer reads the whole receipt history just to throw it away. Today the answer is "not known" whatever the receipts say, so the read was wasted work that grew with the number of receipts.
2. A guard test that quietly skipped a file that no longer exists now fails instead, so a rename cannot leave the guard checking nothing.
3. The comment on the race test now says exactly what that test shows and what it does not.

## What changed

1. `quota_balancer/evidence_view.py`: a named switch `RECEIPTS_CARRY_GENERATION = False`. While it is False, `coverage` hands the comparison an empty receipt list and does not query the `receipts` table. The two figures stay as they were: `unreceipted_micro_eur` is null and `receipts_exceed_settled` is false. The schema test that fails when a generation column appears is unchanged, so the switch cannot be forgotten.
2. `tests/test_cardr13e_coverage.py`: the no-reference guard fails when a named file is missing. The explicit module list is kept. Its docstring says it is a guard for those modules, not a proof about every dependency.
3. `tests/test_cardr13d_fix1.py`: the race test's docstring says the probe meets A's lock and B's call overlaps A's hold, and that it does not observe the instant B waits inside SQLite.

## Tests

- New: `status` with 60 receipts issues no query on `receipts` (a trace callback on the connection). The same test turns the switch on to show the check really would see such a query.
- Changed: the earlier R1-3e test that expected one receipt row to reach `compare` now expects none while the switch is False and the old row shape when it is True.
- Red first: on the previous `evidence_view.py` the new test fails (1 failure).
