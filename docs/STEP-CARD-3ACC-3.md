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
   The percentage is 100 minus the stored figure, rounded **up** to one decimal, worked from the stored value (the table shows a cut figure, so it can differ by a
   tenth). A bound on what is left is never shown smaller than it is.
2. **T2, the observed change.** Added to the same line when it applies: `Its readings rose from 87% (Wed 2026-10-21 06:08 UTC) to 91% (Wed 2026-10-21 12:31 UTC).`
   It uses the oldest and the newest readings that may be relied on, of the same label, window and reset period, in the last 12 hours before the table's time; the times
   are the readings' own times. It is left out when the rise is under one point or fewer than two readings qualify. It gives no rate and no forecast.
3. **T3, a label in doubt.**
   `B: a possible switch was flagged on 2026-10-21: its weekly reset moved from Fri 2026-10-23 21:00 UTC to Sat 2026-10-24 01:00 UTC and its figure went from 92% to 60%. Its newest reading has the same reset times as A's, so it may be A's reading under B's label. B's readings since then are not used here until one shows B's own reset times again.`
   The "may be" sentence appears only when the label's newest weekly reset time is exactly the same as another label's newest one. It is a hint; nothing is relabelled.
   A flag that was a drop of the percentage inside one reset period reads: `... its weekly figure fell from 80% to 30% within one reset period (it resets <time>).`
4. **T4, cannot tell.** `C weekly: no reading has been recorded here. A turn by a seat logged into C may supply one; check that a usable reading arrives under C before relying on it.`
   For a label that has readings, but none newer that can be used: `A 5-hour: no newer reading has been recorded here. ...`. Printed for each row that has no figure to rely
   on, except the rows of a label in doubt that do have readings (T3 speaks for them).
5. **T5.** `Nothing to add to the table.` It stands alone only when nothing else applies. In practice every row gets a T1, T3 or T4, so it is a safety net and is not
   expected to appear.

**Order:** by label (A, B, C). For each label: its T3 first, then the 5-hour row, then the weekly row (T1 with T2 after it, or T4).

## The doubt rule

- **It starts** at the time of any possible-switch flag on a label's weekly series, however old the flag is.
- **It covers** the flagged reading and every later reading of that label. None of them gets a T1 or a T2.
- **It ends** only when a later weekly reading of the label has a reset time that fits the label's own series from before the flag: the last known reset time before
  it, or that plus a whole number of weeks (within five minutes). From then on readings on that series are used again; readings that do not fit stay out.
- **It does not end** because time passes (the table's own 7-day note ends, the doubt does not), because a later reading came on another series (for example a second
  reading of the other subscription's reset times under the same label), or because of anything outside the readings (a message, a changed label table).
- **A flag raised while the doubt is open** (including the flag the returning reading raises against the wrong readings before it) does not start a new doubt and does
  not move the series. A new wrong reading after the return starts a new doubt on the recovered series.
- **A 5-hour reading** has no weekly series of its own, so it is judged by the label's latest weekly reading at its time: used when that one may be relied on, kept out when
  it is in doubt.
- It is the function `doubt_walk` (with `fits_series` and `reading_eligible`), tested on its own.
- **Known limit:** if a label's machine really moves to another subscription for good, the label stays in doubt until its readings fit its old series again. The cure is
  the label table (map the folder to the label of the subscription it now uses); until then the block says T3.

## What the block never says

Which subscription to use or has room; that anything "is enough", "will last" or "can finish"; a rate per hour or a forecast; a ranking; any account name or identifier; or
anything outside the five sentences. Every bound begins "If this reading and its label are right", because a reading cannot prove which subscription wrote it. The tests
check every output against the five sentence shapes and against a list of forbidden words.

## Tests

`tests/test_three_accounts_explain.py` (32 tests). Fixtures are a store and a history folder the test builds, with the labels and percentages of 21 October. The binding
tests, by name:

- every printed line matches T1 to T5, and no forbidden word appears: `run_both` runs both checks on every output the file produces;
- `test_the_bound_is_never_below_one_hundred_minus_the_stored_value_for_every_hundredth` (every hundredth from 0 to 100) and the stored-value test;
- the misfiled reading: `test_the_doubt_outlives_the_seven_day_note`, `test_a_later_b_reading_on_the_other_subscriptions_reset_times_does_not_end_it`,
  `test_b_is_eligible_again_only_on_its_own_friday_series`, and the separate `DoubtRuleItself` tests;
- `test_the_default_output_is_byte_identical_to_the_code_before_this_change` (SHA-256 of the default output for a fixture, recorded from the code at `bd3332b`);
- `test_nothing_is_written_to_the_history_or_the_store_folder` and `test_the_store_is_opened_read_only`;
- `test_times_are_the_observations_own_times_to_the_minute`; the T4 tests (never suggests using a label or starting work).

## Choices the design did not settle

1. **`For <label>:` in front of T1.** The template has no label; the design's example does, so each T1 line starts with it.
2. **Full time stamps** (weekday, date, hour:minute) in every sentence, where the design's example shows only the hour.
3. **T2 only with a T1.** It appears only for a row that also gets a bound (reset ahead, reading may be relied on), using that figure's reset period.
4. **The "may be" label.** Compared on the weekly reset time only; when two other labels match, the first in order is named.
5. **5-hour readings and the doubt** (see the rule above).
6. **The forbidden-word test skips the four fixed footer sentences of the table**, which were approved before this card and one of which says "available"; every other
   character of every output is checked.
7. **No T4 for a label in doubt that has readings.** T3 explains those rows; T4 would say "no newer reading" when there is a newer, disputed one.
