# Step record - Card 7a-2: the reporter core

Work item `card-7a-2`, work order tk-64a33e70e710. Branch `card-7a-2`, stacked on
card L1d (`bedec6b`). Contract: `C:\lw\records\DESIGN-7A.md` (frozen copy
`DESIGN-7A-draft4.md`); **Part 12 and its addendum win over Parts 3-4**.
Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only.

## In plain words

agenttalk will write a small journal of what each agent's turns did (card 7a-1,
not built yet). The **reporter** reads that journal and tells Leasewise about
every message: it opens one observe-only (shadow) lease, reports each run of the
agent, and sends one final receipt with the totals - or with "unknown" where the
journal cannot prove the numbers.

This card builds the reporter's **core**: everything that *decides* what to
send, driven by hand-made journal files. It sends nothing itself. The real
HTTP client, the `run` / `drain` / `status` commands, the configuration file and
the end-to-end runs are card 7a-3.

What the core does:

1. **Reads the journal safely.** One module mirrors the journal's closed format
   and refuses an unknown version. A round lists every file at one instant and
   reads each only up to the length it had then, complete lines only.
2. **Stores before it moves on.** Everything read goes into the reporter's own
   database first; only then do the read positions move, so agenttalk may delete
   old journal files afterwards.
3. **Says honestly when it may have missed something.** For each message it
   decides "complete" or "incomplete" with a closed reason. "Complete" means
   "no loss the journal can see"; it is not a guarantee.
4. **Never invents a final.** A message that ended without a known result sends
   no final. An incomplete message still gets a final, but its token numbers are
   "unknown", never 0. A final is computed once, stored, and sent unchanged even
   after a restart.
5. **Retries safely.** A request that was refused as "stale" (card L1d) may be
   rebuilt with a fresh time at most three times, keeping the original; any
   other bad answer is never rebuilt.
6. **One owner.** A second reporter on the same state file fails at once.

## Files

- `quota_balancer/reporter/events.py` - schema mirror, discovery, snapshot read.
- `quota_balancer/reporter/state.py` - SQLite state, exclusive ownership, the
  outbox (`next_item`, `apply`), immutable finals.
- `quota_balancer/reporter/rounds.py` - rounds, message assembly, completeness.
- `quota_balancer/reporter/mapper.py` - stable ids, request bodies, totals (pure).
- `quota_balancer/reporter/decisions.py` - the error table (pure).
- `tests/journal_fixture.py` (hand-made journals), `tests/test_card7a2.py`.
- `docs/STEP-CARD-7A-2.md` (this record) and a README section.

## Part 12 rules -> tests (`tests/test_card7a2.py`)

| Rule | What it says | Tests |
|---|---|---|
| D1 | closed dispositions, `outcome_unknown` replaces `consumed_undriven` | `SchemaMirror.test_the_disposition_set_is_closed_and_renamed`, `FinalsD5.test_outcome_unknown_sends_no_final_in_either_column` |
| D3 | an unclean predecessor that lost a whole dispatch makes a message incomplete; one that arrived after the successor started stays complete; the disposition's own stream needs no closure | `CompletenessD3`: `test_an_unclean_predecessor_that_lost_a_whole_dispatch`, `test_a_message_that_arrived_after_the_successor_stays_complete`, `test_a_cleanly_closed_predecessor_costs_nothing`, `test_the_disposition_stream_needs_no_closure` |
| D1b | `message_disposed` carries five raw gate facts (`consumed`, `landed`, `compliance_success`, `dead_lettered`, `terminal_failure`), each true, false or null; finals still use `disposition` only | `SchemaMirror.test_the_five_raw_gate_fields_are_true_false_or_null`, `SchemaMirror.test_raw_gate_fields_do_not_change_a_final` |
| D3c, L1 | `message_at` is the sender's clock: every comparison with a local time allows `CLOCK_SKEW_ALLOWANCE_SECONDS` (600): overlap tests widen toward "possibly overlapping" (unclean predecessor, missing predecessor, a loss in another stream); consistency tests tolerate up to the margin (later than the disposition, later than a dispatch start, two copies differing) | `CompletenessD3`: `test_the_clock_skew_margin_has_two_sides`, `test_a_message_exactly_at_the_margin_is_outside_it`, `test_a_message_just_after_the_successor_start_is_still_suspect`, `test_a_message_that_arrived_long_after_the_successor_stays_complete`; `FixRound1Skew` (six tests, each on both sides of the margin) |
| D3a | a missing, unreadable or inconsistent time makes a message incomplete | `CompletenessD3`: `test_a_missing_message_time_is_incomplete`, `test_an_unreadable_message_time_is_unknown_too`, `test_a_message_time_later_than_its_disposition_is_inconsistent`, `test_a_message_time_after_its_only_event_is_inconsistent`, `test_two_different_message_times_are_inconsistent`, `test_a_stream_that_starts_before_its_predecessor_is_inconsistent`, `test_a_clock_set_back_inside_a_stream_is_inconsistent`; missing predecessors: `test_a_missing_predecessor_makes_an_old_message_incomplete`, `test_a_registered_stream_whose_files_are_all_gone_is_missing` |
| D4 (as read) | stream records carry no `seq`; continuity over event `seq` across segments with `after_seq`; a deleted segment; a missing tail shown by `stream_closed.last_seq`; live and crash torn tails; drops | `SchemaMirror.test_stream_records_carry_no_seq`; `ContinuityD4` (continuity, deleted segment, missing tail, lost event, drop, rising drop counter, live tear, crash tail, half-written line, lost cursor, second round, reverse discovery, vanishing segment) |
| D5 | the truth table; stored before sending; same bytes after a restart; a replayed duplicate disposition makes no second final; late observations counted, never sent | `FinalsD5` (every table cell, persist-before-send, duplicate, late, immutability, ordering), `Crashes.test_a_crash_after_the_finals_are_stored`, `ContinuityD4.test_a_lost_cursor_re_reads_without_double_counting` |
| D7 | rebuild only on `ESTIMATE_STALE`, at most 3, original kept, budget durable, the 4th answer fails, a generic `INVALID_REQUEST` never rebuilds | `RebuildD7`, `ErrorTable` |
| 4.6 totals | Claude cache mapping, null propagation, incomplete -> null, overflow -> null, calls = launched only | `Totals`, `Identity` |
| 4.4 ids | stable ids; per-dispatch identity = the journal's turn id | `Identity`, `MapperBodies` (the bodies pass Leasewise's own request validators) |
| crashes | after the data, after the cursor, after the finals, inside a step | `Crashes` |
| ownership | a second reporter fails at once (in process and as a real second process) | `OwnershipOutbox` |
| requeue | a requeued message is a new message with its own lease | `Identity.test_requeue_is_a_new_message_with_its_own_lease` |
| privacy | damaged lines and unknown keys never reach the state; errors are static | `SchemaMirror.test_a_damaged_line_is_counted_and_skipped_never_stored`, `PrivacyCore` |

## Where the contract was unclear, and what I chose (marked for the lead)

1. **Unknown version.** "Refuse an unknown version" is implemented as: the whole
   round raises `UnsupportedVersion` before anything is written, so nothing is
   half-stored; the operator must upgrade the reporter.
2. **Damaged lines.** A complete line that is not valid is counted
   (`damaged_lines`) and skipped. I added no new reason: the event it carried is
   missing, so the sequence-gap rule already marks overlapping messages.
3. **Time model.** A "gap" is a time span between two observed events (or the
   stream start or close). A message is affected when its window (arrival to
   disposition; open-ended while it has no disposition) overlaps the gap; the gap
   must start strictly before the window ends, so a loss after the disposition
   does not taint it.
4. **Torn tails** are only recorded as a fact once the writer cannot still be
   writing them (a later segment, a closed stream, or a later stream). Until then
   the read position stays before the partial line.
5. **Time checks I added** inside D3a: two different `message_at` values for one
   message, a message time later than any of its dispatch starts, a clock that
   goes backwards inside a stream. All give `clock_inconsistent`.
6. **Unreadable `message_at`** (not a time) counts as missing, so `unknown_arrival`.
7. **Several dispositions for one message:** the first by stream order and `seq`
   wins; later ones never make a second final (counted as late once a final
   exists).
8. **Error table gaps:** `LEASE_CLOSED` on a non-final report is dropped like
   `LEASE_EXPIRED`; any answer not in the table (an unknown 4xx) fails that item
   for good; a 409 or 410 on a *final* fails it loudly (a final is never retried
   blindly). A configuration fault stops only that agent.
9. **`dispatch_ended.failure_class`** may be absent or null (the design says
   "failed or not launched only").
10. **Amendment (D1b, D3c).** The five raw fields are REQUIRED keys of
    `message_disposed` (closed schema; each `true`, `false` or `null`). The skew
    margin first applied to the unclean-predecessor rule only; fix round 1 (L1)
    widened it to every comparison of `message_at` with a local time.
11. **`now`** (the estimate time of a new acquisition) is an input of the round,
    so the core has no clock of its own.

## Evidence

- Tests are NOT red-first on this card: code and tests were written together.
  Instead 20 single-point mutations were applied one at a time, and every one is
  caught by at least one test: unclean rule off, successor start ignored, missing
  time accepted, time-after-disposal accepted, gap detection off, closing record
  ignored, torn tails confirmed at once, `outcome_unknown` finals, incomplete
  keeps tokens, final mutable, dedupe off, four rebuilds, rebuild on generic
  invalid, rebuild budget not stored, exclusive lock off, calls not counted,
  cache mapping off. (Two of the first run survived and led to the tests
  `test_a_message_time_after_its_only_event_is_inconsistent` and
  `test_a_lost_cursor_re_reads_without_double_counting`.)
- `python -m unittest tests.test_card7a2`: 91 tests OK before the amendment; 96 after
  it (5 new, one test replaced by four); 119 after fix round 1. Three more mutations (margin 0, margin
  inclusive, raw type unchecked) are caught.

## Fix round 1 (tk-15619ce77471): codex F1-F5, lead L1-L2

Codex read the card at `aee4aa0` (FIX) and reproduced each finding. All seven are
fixed. In plain words:

- **F1 - a rotation header is not an event we read.** A file header says "the
  writer had written up to number N before this file". Before, that number was
  counted as if we had read it, so a damaged line just before a rotation vanished
  from the loss check. Now the header is only a watermark; every number up to it
  that we did not read is a loss. (`test_f1_*`)
- **F2 - order inside one stream is the sequence number, not the clock.** Whether
  a loss came before a message's ending is decided by the numbers in the same
  stream; across streams, where only times can order things, a tie counts as
  overlapping. (`test_f2_*`)
- **F3 - a final no longer depends on a crash.** Chosen: **(b) finish an
  unfinished round before reading any new input.** A round is marked evaluated in
  the same transaction that decides its finals; at the start of every round the
  reporter first finishes a round that was stored but not evaluated, from what
  that round stored. Later input then counts as late. I picked (b) over tagging
  every fact with its round because it needs no change to the stored facts and
  makes the crashed run identical to the uncrashed one. (`FixRound1Crash`)
- **F4 - a wrongly typed `kind` is a damaged line.** The parser checks the type
  before looking the kind up, and any unreadable line (not only the known error
  type) is counted and skipped, so one bad line can never stop a round. A deeply
  nested line is also a closed parser error. (`test_f4_*`)
- **F5 - stopping an agent drops its queued reports.** In the same transaction the
  queued non-final reports become `dropped` and are counted in `reports_dropped`;
  reports that arrive later for a stopped agent are dropped and counted at once.
  Finals are never dropped and other agents are untouched. (`FixRound1Agents`)
- **L1 - the clock margin everywhere** (see the D3c row above).
- **L2 - a 401 is retried.** It is a reporter-wide credential problem, so it is a
  retry with back-off and the code stays in `last_error_code`; nothing becomes
  failed for good. (`FixRound1Unauthenticated`)

Old tests that depended on the narrower margin were moved to arrival times beyond
it (message-time-after-disposition, missing predecessor, two differing times,
deleted segment); no assertion about a rule was weakened.

Evidence: the new tests were committed first (`eb573c9`): 22 new tests, 17 red
before the fixes (the five that already passed are guards: the deep-nesting
parser check, a loss after a disposition, no-crash equality, finishing a crashed
round, and one parser check). One more parser test was added after that
commit. After the fixes `tests.test_card7a2` runs 119 tests (96 before this round
plus 23), all OK; the full suite runs 956 tests, all OK. Eleven single-point mutations (seq gaps
ignored, watermark tail ignored, same-stream order by time, ties not overlapping,
unfinished round not finished, kind type unchecked, agent stop keeps reports, late
report not dropped, margin zero, unclean without margin, 401 permanent) are each
caught. Codex's probe file, copied to scratch and run against this head: 7 of 7 OK.

## Judged not testable here

Concurrent writers on one journal file (one writer per stream by design); the real
HTTP transport and the verbs (card 7a-3).

## Residual risk

- A process whose journal never started, or a clock set back without leaving a
  trace, can still hide a loss; totals are observations and never feed admission
  (register R-007).
- The reporter keeps every event row it has read (small, but unbounded); pruning
  is a 7a-3 concern.
