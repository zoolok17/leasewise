# Step record - Card 3acc-1: the "now" table

Work item `card-3acc-1`. Branch `card-3acc-1`, from main `64994c5`. Audience: the lead, who runs the command, and the reviewer.
Python 3.11 or newer, standard library only. One command added to `tools/three_accounts/measure.py`; nothing in the running server, the reporter or the collector changed.

## In plain words

The operator shares three Claude subscriptions, which we call A, B and C. Until now the lead pieced together "how much is used, and when does it reset?" by hand
from several files and a report. The new command `measure.py now` prints that answer as one small table: for each subscription, the 5-hour allowance and the
weekly allowance. It gives advice only. Nothing is reserved, refused or enforced, and the command writes nothing anywhere.

The design is `docs/DESIGN-THREE-ACCOUNTS.md` (an unchanged copy, SHA-256 `785a9a84f2134cba32a5f90f5200fb44b25c02665de462d6532901973ec83eb5`; it now includes
section 6). This card is narrower than the design: where the two differ, the card's brief decided.

## How to read the table

The table always has six rows: A, B and C, each with `five_hour` and then `seven_day`. Each row uses the newest reading of that subscription and window, from either
source. A's readings come from the Leasewise store (Laptop B's own seats). B's readings come from the desktop's kept history. C is used by no machine, so it has no
readings.

Every row says one of four things:

| The row says | It means |
| --- | --- |
| `12% (fresh, 3 min old); resets Thu 14:57 UTC; from laptop-b` | A reading at most 10 minutes old whose allowance has not reset. Trust it as "about now". |
| `at least 45% (2 h 10 min old); resets Thu 14:57 UTC; from desktop` | An older reading whose allowance has not reset. Use may have grown since, so read it as a floor: the real figure is this or higher. |
| `unknown: reset since Thu 11:10 UTC, no new reading` | The last reading's allowance has reset since, and nothing newer came. The old percentage no longer applies, so none is shown. |
| `unknown: no reading recorded` | No reading was ever kept for this label and window. It never shows as `0%`. |

The time of a reset is shown in UTC with the weekday and hour:minute. A reset exactly at the moment of the table counts as passed.

Under the rows you may also see:

- **The desktop copy line.** `desktop copy 4 min old` is the age of the last desktop copy the collector accepted. `desktop copy late (N min)` appears when it is more
  than 15 minutes old (exactly 15 minutes is not late); B's rows are then more likely to be old. `desktop history unreadable: B can only be old or unknown` appears
  when the receipts file is missing, damaged or has no entry for the desktop; the table is still printed.
- **A possible switch.** `possible switch on <date>: ...` appears under a label when the existing switch detector flagged its weekly history in the last 7 days. It
  is only a possibility. The table never relabels, merges, hides or drops a figure because of a flag, and a normal weekly reset is not flagged.
- **Coverage.** `coverage incomplete: a machine that does not report also uses this subscription` appears under each label named with `--incomplete-coverage`. It names
  no machine and no team.
- **Skipped lines.** `(N unreadable history line(s) skipped)`: lines of the history file that could not be read, for example a last line the collector was still writing.
- **A footer** of four fixed sentences: the 5-hour figure exists only while a seat is working; "at least" is a floor; use outside agenttalk cannot be separated from
  agent work; and the table is advice only.

## The lead's command

Run in `C:\lw\repo`:

```
C:\lw\venv\Scripts\python.exe -I tools\three_accounts\measure.py now --db C:\lw\live\store\inventory.db --labels C:\lw\measure\three-accounts\aliases.json --history C:\lw\measure\three-accounts\history --incomplete-coverage A
```

| Option | Meaning |
| --- | --- |
| `--db` | The Leasewise store. Opened read-only (`file:...?mode=ro`), as `from-store` does. |
| `--labels` | The alias table, `{store alias: "A" or "B" or "C"}`, kept outside the repository. |
| `--history` | The folder the collector writes. Only `observations.jsonl` and `receipts.json` are read. |
| `--incomplete-coverage LABEL` | Optional, may be repeated. Adds the coverage line under that label. |
| `--now` | Optional, epoch seconds, for tests. The default is the clock. |

Exit codes: 0 printed; 3 a problem with the arguments, the alias table or the store. A failure prints one plain sentence, never a path or a value.

## What it reads and writes

- It reads the store (read-only), `observations.jsonl` and `receipts.json`. It reads nothing else: not a seat's capacity file, not the inbox copy, not the live state folder.
  Seat files are not used because a seat drops its percentages once its figure is more than 10 minutes old; the kept histories do not.
- It writes nothing, and it does not take the collector's lock, so it is safe to run while the collector appends. A half-written last line is skipped and counted.
- `STORE_PROMISE` (the read-only promise for the store, decision D-348) applies unchanged: the database file stays byte-identical and nothing is written through the
  connection; on a live WAL-mode store SQLite itself may leave its own `-wal` and `-shm` files.
- The store reading is now one function, `read_store`, used by both `now` and `from-store`. `from-store` prints and writes exactly what it did before; a test fixes the
  SHA-256 of its output file and its counts.
- The thresholds are the report's: a figure is fresh for 600 seconds and the same `classify` and `switch_flags` code decides the wording. The only new numbers are named
  constants: `DESKTOP_LATE_S` (15 minutes, design section 2: three missed 5-minute copies) and `SWITCH_LOOKBACK_S` (7 days).

## Not in this card, and why each part waits

- **The people's line (design 6.1).** It needs a decision on who is told what and how; the table first has to prove it reads right.
- **Any number for use outside agenttalk (6.2).** The readings cannot separate it from agent work, so a number would be a guess. The footer says so.
- **"Unknown subscription" or relabelling by reset-time matching (6.4).** Two subscriptions can share a reset time; an automatic label could be wrong. The switch note
  only flags and leaves the decision to the lead.
- **A JSON output.** Nothing consumes it yet.
- **Any service, timer or new collector.** The liaison approved a measured step; the lead runs the command by hand.

## Known limits

- **A has figures only when Laptop B's seats run a turn.** Between turns A's row grows old, then becomes a floor, then unknown.
- **The 5-hour window needs a working seat.** With no working seat on a subscription its 5-hour figure is old or unknown, by design.
- **A has a second user that does not report.** Since 2026-10-18 another machine also uses A. The real use of A may be higher than any figure; `--incomplete-coverage A`
  says so in the table.
- **The desktop copy's age bounds B.** B's figures are only as new as the last accepted copy; check the desktop copy line first.
- **The weekday is not a date.** A reset shown as `Thu 11:10 UTC` could be today or a week ago; the age or the reset context in the same row resolves it.
- Two subscriptions with the same reset time cannot be told apart by it (the switch detector's known limit from step 0).

## Tests

`tests/test_three_accounts_now.py` (36). Fixtures are a store and a history folder the test builds, with made-up aliases; nothing live is read. They cover: six rows
always, C included, never an empty cell or `0%`; each of the four wordings and the 600-second edge; the newest reading across both sources; the copy age, the edge at
exactly 15 minutes, and missing or damaged receipts; a half-written last line, skipped and counted; a switch flag 6 days old marking the label, one 8 days old not, a
normal weekly reset marking nothing, and the figure staying visible; the coverage line; the footer; nothing written (file names and bytes of the history and store
folders unchanged, and no lock file created); no alias, path or user name in the output; and `from-store`'s output file and counts unchanged by the refactor.
