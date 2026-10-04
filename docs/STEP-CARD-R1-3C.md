# Step record - Card R1-3c: the pure evidence rules and the transitions written at ingest

Work item `card-r1-3c`. Branch `card-r1-3c` from main `a549a14be5f48bb914048de99cd2246fc5e9adf1`. Design: `C:\lw\records\DESIGN-R1-3.md` (SHA-256
`71c07b9c...3ab9`), sections 3, 4 and 9 and the row R1-3c of 10.1. Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only. Synthetic reports only.

## In plain words

R1-3b kept every version-4 report. This step makes Leasewise **decide what each report means for trust**, and write down a problem once, when it is
first seen. The deciding is done by pure functions that touch no database; ingest gives them the facts and writes what they return. **Only `status`
will see the result, and that block is card R1-3d's.** No seat, lease or admission decision changes, and there is still no operator command.

## What was built

1. **`quota_balancer/evidence.py` (pure).** `assess(report, state)` applies the rules in the design's order: a retired reader is only counted;
   identity before order (`sequence_conflict`, `epoch_generation_mismatch`); then order (duplicate, old chain, old period, late); then what an
   in-order report of the current chain shows (`reported_by_gateway` first, `epoch_changed`, the two fingerprints, `figure_fell` for `settled` on
   every report and for `unattributed` between quiet reports, `unattributed_rose` above the adopted report on quiet reports); then the month roll.
   An unknown figure is never read as zero and never proves a fall. The module imports no database, clock or file.
2. **Ingest writes the result once** (`store._record_report_v4`, `_evidence_state`, `_apply_assessment`): the reader row, the transitions, the
   counters, the last quiet figure and the roll. Nothing is recomputed from history and nothing is resolved or overwritten.
3. **Reader bookkeeping.** The first incarnation seen with no adoption is `pending` and raises nothing. A new incarnation after an adoption raises one
   `reader_changed` (its own unique key). `store._promote_reader` is the one step that writes `current`: it **retires the old current and every pending
   reader first**, then promotes (the unique index is immediate), and refuses to promote a retired reader. The month roll uses it; adoption (R1-3d) will.
4. **Retirement covers every writer.** `store._reader_state` is the one retired-reader classification. The accepted-report path (which had no check:
   `epoch_generation_mismatch`) now goes through it, so a retired incarnation's report is kept, counted (`retired_reader_reports`) and creates no
   transition on any path; the two refused-collision paths use the same helper.
5. **The month roll** (`store._roll_period`): in the same transaction, a `period_roll` adoption (operator null, previous adoption linked), the same
   reader stays current (its deciding adoption moves to the roll), the chain state points at it and starts its quiet figure at that report, and one
   audit row (the adoption id only, no amounts) is written.
6. **`billing_eval._gateway_usable`** stays the evaluator's second line of defence. For a version-4 report its identity key now also holds the reader,
   generation and month, so a chain that mixes them is refused. A chain of the adoption report alone is whole and usable (B3), ready for R1-3d's context
   builder. The evaluator's own report validation does not yet accept version-4 payloads: that is R1-3d's context builder.
7. **Carried from Codex:** N1 (`tests/test_cardr13b_ingest.py`: a malformed field now keeps the id derived from the report's own parts, so only the field
   check can refuse it; added the 2^63-1 accepted and 2^63 refused boundaries for `receipts_seq` and `held_over_reservation`) and N2 (README and the R1-3b
   record now say that no evidence consumer uses the rows for status or admission yet, while ingest does read them).
8. **The `status` block** is placed by the design in R1-3d (its section 8.3), not here. Left to R1-3d.

## Choices the design left open (each stated, each tested)

- **A state kind is latched while open:** `reported_by_gateway`, `epoch_changed`, `price_hash_changed`, `child_cap_hash_changed` and `unattributed_rose`
  write one row while one of the same kind is open (otherwise a gateway on hold would write a row every report). An event kind (`figure_fell`,
  `sequence_conflict`, `epoch_generation_mismatch`, `reader_changed`) is written for each report that shows it. The design says "one open" only for
  `unattributed_rose`; the others are this card's choice.
- **No adoption, no chain transitions.** Without an adoption there is nothing to differ from, so only the identity kinds are written (they need no chain).
  `reported_by_gateway` is not written either: adoption item 8 already refuses a report that says "on hold".
- **"Old chain" means an epoch below the adoption's;** an older month means a period below the adoption's. A newer epoch is `epoch_changed`.
- **Same identity, different event id.** Because the event id is derived from incarnation, generation and number, the same (incarnation, epoch, number)
  under another event id always has another generation: it is both an `epoch_generation_mismatch` and, if the figures differ, a `sequence_conflict`;
  with the same figures it is a duplicate (counted) and still a mismatch.
- **The roll** needs the new month to be later than the adoption's, from the same reader, epoch, generation and the adopted pair, with that pair
  approved and nothing open, after the report's own transitions were written.

## Fix round 1: `settled` is cumulative in every month, also before that month's adoption

Codex found a gap: in a new month, before its first quiet report, a fall of `settled` went unnoticed, and the automatic month roll then adopted the
lower figure as the baseline (busy 100, then quiet 50). Now **`settled` is checked on every report, in every month**:

- `store._evidence_state` supplies, for the incoming report, the **highest `settled` already stored** for the same gateway, reader, epoch, generation
  and month (from `gateway_totals`). In the adopted chain the adopted report is the floor. The first report of a month has nothing to compare with
  (the month reset), and the first report of a new month is never compared with the old month's figure.
- `evidence.assess` raises `figure_fell` whenever `settled` is below that figure, for a busy and a quiet report alike, and no longer limits the check to
  the adopted month. An open `figure_fell` blocks the roll like every open transition, so the operator must look.
- The highest figure (not the previous report) also catches a fall past a missing or late report. Because the baseline is the highest figure, a report
  after a fall that is still below the highest is a fall too (100, then 50, then 60 gives two rows). A late, lower-numbered report is still counted and
  ignored before any fall is checked. `unattributed` is unchanged: quiet reports only, against the adopted report.
- Limit: a stored report that is itself wrong (a conflicting one) can raise the highest figure, which only makes a fall more likely (the safe side).

## Tests (`tests/test_cardr13c_evidence.py`, 68)

Pure rules as a table (`Purity`, `OrderOfTheChecks`, `Monotonic`, `MonthRoll`, `Readers`): retired first; identity before order in both arrival orders;
duplicate, late, old chain and old period; `settled` on every report; `unattributed` on quiet reports only; unknown never zero; each condition of the roll;
`reported_by_gateway` before any roll; the reader states. At ingest (`ReaderBookkeeping`, `RetiredReaders`, `TransitionsAtIngest`, `IdentityBeforeOrder`,
`OrderAtIngest`, `MonthRollAtIngest`): pending first reader; one `reader_changed` per incarnation; retire before promote; a retired incarnation creates no
transition on the accepted epoch-mismatch path, the hold/fingerprint/figure path, the content-collision path and the refused same-id path; each change once and
never resolved; the quiet rules; a resolved conflict replayed never re-opens. `GatewayUsable`: the evaluator over a hand-made context.

**Red first.** The pure module is new, so its tests cannot run on main. Against main's `store.py` and `billing_eval.py` (with the one helper the tests'
adoption fixture needs added, so the failures are about behaviour), **21 of the 62 methods fail (24 failures counting subtests)**. They are the ingest, reader
and evaluator tests that describe new behaviour; several `RetiredReaders` tests are red partly because the old store never creates the reader row their set-up
retires. The tests that describe behaviour that does not change pass on both (the refused same-id paths that already counted, the resolved-conflict replay,
the no-roll cases, the pure tests that only use the new module).

## Existing tests

One expectation moved: `tests/test_cardr13b_ingest.py` seeded a `retired` reader row after the first ingest; ingest now creates a `pending` row first, so
the test deletes it before the seed. N1 and N2 changed that file's wording and one test. Nothing else changed.

## Honest notes

- Nothing reads the transitions for `status` or admission until R1-3d; no reader sends version 4 live until R1-READER closes D1.
- A transition is detected only when a report arrives. A gateway that goes silent is `stale`, not a transition.
- The month roll is not the operator's approval of the month's opening unmanaged total; further quiet rises latch after it.
