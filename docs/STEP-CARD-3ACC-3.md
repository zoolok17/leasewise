# Step record - Card 3acc-3: plain explanations under the usage table (`--explain`)

Work item `card-3acc-3`. Branch `card-3acc-3`, from main `bd3332b`. Audience: the lead, who runs the command, and the reviewer.
This record adds to `docs/STEP-CARD-3ACC-1.md` (the table itself); it is a new file so that the table's record stays as it was merged.
Python 3.11 or newer, standard library only. The specification is `docs/DESIGN-STEP3-EXPLANATIONS.md`, an unchanged copy of the design file (SHA-256
`9f0e9fde0e9baf6bc95eea4b20093e2ea55445c25c8362f3bc88b0efa2e9a4f8`).

## In plain words

`measure.py now` prints a six-row table of how much of each subscription's allowance has been used. With the new flag `--explain` it prints a few plain sentences
under the table: what the readings allow us to say, which label's readings are in doubt, and what to check next. Without the flag, the output is exactly what it
was (a test compares it with the output of the code before this change).

The sentences are advice about the evidence. They never say which subscription has room, how long anything will last, or what to choose.

## The lead's command

Run in `C:\lw\repo`:

```
C:\lw\venv\Scripts\python.exe -I -B tools\three_accounts\measure.py now --db C:\lw\live\store\inventory.db --labels C:\lw\measure\three-accounts\aliases.json --history C:\lw\measure\three-accounts\history --incomplete-coverage A --explain
```

The command reads the store (read-only, `mode=ro`) and the two history files, as before, and writes nothing.

## The five sentences

Every printed line is one of these, word for word apart from the values. Times are written in full (weekday, date, hour:minute, UTC) so that no time can be read as
another day's.

1. **T1, the most that can be left.** Printed for a row whose figure may be relied on and whose reset is still ahead.
   `For A: If this reading and its label are right, at most 59% of the weekly allowance is left until Sat 2026-10-24 01:00 UTC (reading of Wed 2026-10-21 10:35 UTC).`
   The percentage is 100 minus the stored figure, never below 0, rounded toward plus infinity to one decimal, worked from the stored value (the table shows a cut
   figure, so it can differ by a tenth). A bound on what is left is never shown smaller than 100 minus the stored figure; a stored figure at or above 100 (the reader
   accepts up to 1000%) gives `at most 0%`, never a negative number.
2. **T2, the observed change.** Added to the same line when it applies: `Its readings rose from 87% (Wed 2026-10-21 06:08 UTC) to 91% (Wed 2026-10-21 12:31 UTC).`
   It uses the oldest and the newest readings that may be relied on, of the same label, window and reset period, in the last 12 hours before the table's time; the times
   are the readings' own times. It is left out when the rise is under one point or fewer than two readings qualify. It gives no rate and no forecast.
3. **T3, a label in doubt.**
   `B: a possible switch was flagged on 2026-10-21: its weekly reset moved from Fri 2026-10-23 21:00 UTC to Sat 2026-10-24 01:00 UTC and its figure went from 92% to 60%. Its newest reading has the same reset times as A's, so it may be A's reading under B's label. B's readings since then are not used here until one shows B's own reset times again.`
   The "may be" sentence is a hint, and it is printed only when all of these hold; if any fails, it is left out:
   - take the label's newest weekly reading and its newest 5-hour reading, of any kind: both must exist, and both must be in doubt;
   - take the other label's newest weekly reading and newest 5-hour reading, of any kind: both must exist, and both must be readings that may be relied on;
   - the weekly reset times must agree, and the 5-hour reset times must agree, within the same five minutes as the series rule;
   - if several readings share the newest time within one window, all of them count, and their reset times must all agree (otherwise no hint).
   Older readings are never used to fill the hint. Nothing is relabelled.
   A flag that was a drop of the percentage inside one reset period reads: `... its weekly figure fell from 80% to 30% within one reset period (it resets <time>).`
4. **T4, cannot tell.** `C weekly: no reading has been recorded here. A turn by a seat logged into C may supply one; check that a usable reading arrives under C before relying on it.`
   For a label that has readings, but none newer that can be used: `A 5-hour: no newer reading has been recorded here. ...`. Printed for each row that has no figure to rely
   on, except the rows of a label in doubt that do have readings (T3 speaks for them).
5. **T5.** `Nothing to add to the table.` It stands alone only when nothing else applies. In practice every row gets a T1, T3 or T4, so it is a safety net and is not
   expected to appear.

**Order:** by label (A, B, C). For each label: its T3 first, then the 5-hour row, then the weekly row (T1 with T2 after it, or T4).

## The doubt rule

- **A trusted series, always.** Each label keeps one trusted weekly series: the reset time of its earliest reading, and that plus a whole number of weeks (within five
  minutes). This holds at all times, not only while a doubt is open.
- **It starts** when a weekly reading does not fit the trusted series, whether or not the possible-switch detector flagged it (the detector says nothing once the earlier
  reset has passed), and at any possible-switch flag, however old the flag is.
- **It covers** that reading and every later one of the label until it ends. None of them gets a T1 or a T2.
- **It ends** only when a later weekly reading fits the trusted series. From then on readings on that series are used again; readings that do not fit stay out, and a new
  one that does not fit starts a new doubt.
- **It does not end** because time passes (the table's own 7-day note ends, the doubt does not), because a later reading came on another series, or because of anything
  outside the readings (a message, a changed label table).
- **A flag raised while the doubt is open** (including the flag the returning reading raises against the wrong readings before it) does not start a new doubt.
- **Readings with the same time** are judged together, and the same way in any list order: if any of them does not fit, all of them are in doubt and that instant does not
  end a doubt.
- **A 5-hour reading** has no weekly series of its own, so it is judged by the label's latest weekly reading at its time (all of them, when several share that time).
- It is the function `doubt_walk` (with `fits_series` and `reading_eligible`), tested on its own.
- **Known limit:** the trusted series is the label's earliest reading in the history. If a label's machine really moves to another subscription for good, the label stays
  in doubt until its readings fit its old series again. The cure is the label table (map the folder to the label of the subscription it now uses); until then the block
  says T3. If the earliest reading kept were itself misfiled, the label's good readings would be the ones in doubt.

## What the block never says

Which subscription to use or has room; that anything "is enough", "will last" or "can finish"; a rate per hour or a forecast; a ranking; any account name or identifier; or
anything outside the five sentences. Every bound begins "If this reading and its label are right", because a reading cannot prove which subscription wrote it. The tests
check every output against the five sentence shapes and against a list of forbidden words.

## Tests

`tests/test_three_accounts_explain.py` (57 tests). Fixtures are a store and a history folder the test builds, with the labels and percentages of 21 October.

**What is checked on every output.** One shared check, `check_wording`, is called by every test of the file that prints an explanation (the direct, command-line and
read-only tests included). It checks that the table comes first and unchanged, that every line of the block is one of the five sentence shapes, and that none of the
forbidden words appears anywhere in the output **except in the four fixed footer sentences of the table**. Those four sentences were approved before this card and
must stay byte-for-byte (the default output must not change), and one of them says "available". `WordingChecksReachEveryOutput` proves the wiring: it puts a forbidden
word, and a sentence outside the five shapes, into the printed text in memory and asserts that the checks fail on the paths that print an explanation.

The binding tests, by name:

- the bound: `test_the_bound_is_never_below_one_hundred_minus_the_stored_value_and_never_negative_for_every_hundredth_to_1000` (every hundredth from 0 to 1000, each
  also matched against the T1 grammar), the over-use test, and the stored-value test;
- the 21 October misfiled reading: `test_the_doubt_outlives_the_seven_day_note`, `test_a_later_b_reading_on_the_other_subscriptions_reset_times_does_not_end_it`,
  `test_b_is_eligible_again_only_on_its_own_friday_series`; the trusted series (`TrustedSeries`: Friday, Saturday, Friday, then Saturday again after the Friday reset;
  tied readings in both orders) and the separate `DoubtRuleItself` tests;
- the hint (`IdentityHint`): both windows equal gives the clause; a different 5-hour reset, a missing 5-hour reading, or a missing reading of the other label gives none;
  both sequences of the second read (an older doubtful reading, or an older trusted pair of the other label, that matched while the newest readings do not); the
  five-minute edge (300 s agrees, 301 s does not); ties at the newest time, with agreeing and with different reset times;
- speed (`Speed`): three labels with a week of readings every five minutes in both windows (2,016 per window) must explain in under 3 seconds. After the fix this takes
  about 0.2 s on the build machine, so 3 s leaves room for a slow shared machine; before the fix the same week took about 160 s. The index used for the 5-hour readings
  is also compared with the plain rule for every reading;
- `test_the_default_output_is_byte_identical_to_the_code_before_this_change` (SHA-256 of the default output for a fixture, recorded from the code at `bd3332b`);
- `test_nothing_is_written_to_the_history_or_the_store_folder` and `test_the_store_is_opened_read_only`;
- `test_times_are_the_observations_own_times_to_the_minute`; the T4 tests (never suggests using a label or starting work).

## Choices the design did not settle, and what the read changed

1. **`For <label>:` in front of T1.** The template has no label; the design's example does.
2. **Full time stamps** (weekday, date, hour:minute UTC) everywhere, where the design's example shows only the hour.
3. **T2 only with a T1,** using that figure's reset period.
4. **The "may be" label.** First version: compared on the weekly reset time only. Changed after the first read: both windows; changed after the second read: the newest
   readings of any kind of both labels (not the newest of those that passed a filter), within five minutes, with ties handled conservatively (see the rule under T3). With
   two matches, the first label in order is named.
5. **5-hour readings and the doubt:** judged by the label's latest weekly reading at their time.
6. **A flag raised while the doubt is open does not start a new doubt.** (The returning reading raises a flag against the wrong readings before it.) Since the read, the
   trusted series itself decides what ends a doubt, not the flags.
7. **No T4 for a label in doubt that has readings:** T3 speaks for those rows.
8. **The forbidden-word check skips the four fixed footer sentences of the table** (one of them says "available"); every other character of every output is checked.
   Changing that footer would be a separate wording card.
9. **T5 is a safety net.** Every row gets a T1, T3 or T4, so "Nothing to add to the table." does not appear in practice; it is implemented and its text is tested.
10. **The trusted series is the label's earliest reading** (added after the read; see the known limit above), and readings sharing a time are judged together, conservatively.
11. **An over-use figure** (stored 100 or more) gives `at most 0%` (the lead's decision after the read).
12. **The hint's tolerance is the series rule's five minutes** (lead's decision, second read), so "the same reset times" means "within five minutes"; two newest readings
    that share a time and disagree give no hint.
13. **The 5-hour readings are judged through a once-built index** of the label's weekly instants (a lookup per reading instead of a scan), with the tie meaning unchanged:
    when several weekly readings share the latest time, the 5-hour reading is in doubt if any of them is.
