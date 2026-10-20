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
| `12% (fresh, 3 min old); resets Thu 2026-10-15 14:57 UTC; from Laptop B store` | A reading at most 10 minutes old whose allowance has not reset. Trust it as "about now". |
| `at least 45% (2 h 10 min old); resets Thu 2026-10-15 14:57 UTC; from desktop copy` | An older reading whose allowance has not reset. Use may have grown since, so read it as a floor: the real figure is this or higher. |
| `unknown: reset since Thu 2026-10-15 11:10 UTC, no new reading` | The last reading's allowance has reset since, and nothing newer came. The old percentage no longer applies, so none is shown. |
| `unknown: no reading recorded` | No reading was ever kept for this label and window. It never shows as `0%`. |

Every reset time, past or future, is shown in UTC with its weekday, date and hour:minute. A reset exactly at the moment of the table counts as passed.
Percentages are cut to one decimal and never rounded up, so a floor never reads higher than the figure kept (12.36 shows as 12.3, 99.99 as 99.9).
The place a figure came from is one of three fixed names: `Laptop B store`, `desktop copy`, or `another machine` (a name found in the history file is never printed).

Under the rows you may also see:

- **The desktop copy line.** `desktop copy 4 min old` is the age of the last desktop copy the collector accepted. `desktop copy late (N min)` appears when it is more
  than 15 minutes old (exactly 15 minutes is not late); B's rows are then more likely to be old. `desktop copy time unknown (its receipt is missing or unreadable)`
  appears when the receipts file is missing, damaged or has no entry for the desktop; B's rows keep their own wording, which comes from each reading's own age.
  `desktop copy time is ahead of this machine's clock` appears when the copy's time is more than 60 seconds after the table's time.
- **A possible switch.** `possible switch on <date>: ...` appears under a label when the existing switch detector flagged its weekly history in the last 7 days. It
  is only a possibility. The table never relabels, merges, hides or drops a figure because of a flag, and a normal weekly reset is not flagged.
- **Coverage.** `coverage incomplete: a machine that does not report also uses this subscription` appears under each label named with `--incomplete-coverage`. It names
  no machine and no team.
- **Skipped lines.** `(N unreadable history line(s) skipped)`: lines of the history file that could not be read, for example a last line the collector was still writing.
- **A footer** of four fixed sentences: a 5-hour figure needs a recent reading from a seat on that subscription (it is fresh for up to 10 minutes, then "at least" until its reset, and unknown after the reset until a new reading); "at least" is a floor; use outside agenttalk cannot be separated from
  agent work; and the table is advice only.

## The lead's command

Run in `C:\lw\repo`:

```
C:\lw\venv\Scripts\python.exe -I -B tools\three_accounts\measure.py now --db C:\lw\live\store\inventory.db --labels C:\lw\measure\three-accounts\aliases.json --history C:\lw\measure\three-accounts\history --incomplete-coverage A
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
- **The 5-hour window needs recent readings.** A reading counts as fresh for 10 minutes, then as 'at least' until its reset; after the reset, the row is unknown until a seat on that subscription reports again.
- **A has a second user that does not report.** Since 2026-10-18 another machine also uses A. The real use of A may be higher than any figure; `--incomplete-coverage A`
  says so in the table.
- **The desktop copy's age bounds B.** B's figures are only as new as the last accepted copy; check the desktop copy line first.
- **Reset times carry their date,** so a reset one day ago and one eight days ago read differently.
- Two subscriptions with the same reset time cannot be told apart by it (the switch detector's known limit from step 0).

## Tests

`tests/test_three_accounts_now.py` (49). Fixtures are a store and a history folder the test builds, with made-up aliases; nothing live is read. They cover: six rows
always, C included, never an empty cell or `0%`; each of the four wordings and the 600-second edge; the newest reading across both sources; the copy age, the edge at
exactly 15 minutes, and missing or damaged receipts; a half-written last line, skipped and counted; a switch flag 6 days old marking the label, one 8 days old not, a
normal weekly reset marking nothing, and the figure staying visible; the coverage line; the footer; nothing written (file names and bytes of the history and store
folders unchanged, and no lock file created); no alias, path or user name in the output; and `from-store`'s output file and counts unchanged by the refactor. The first read's fixes are covered
by their own class: an unknown machine name never printed, the real command line leaving no cache file in a fresh copy of the tool, an undecodable history line
costing one line, the date sentence only for `report`, missing receipts next to a fresh B row, past resets with dates, percentages never rounded up, a receipts
version of `true` refused, and a copy time ahead of the clock.

## Choices made in the build

The first eight were made while building and are listed here with this round's changes; the last two come from the first read.

1. **Extras on a fresh or floor row.** After the exact wording, the row adds `; resets <weekday date time UTC>; from <source>`. The source is now one of three fixed names
   (`Laptop B store`, `desktop copy`, `another machine`); before this round it printed the machine name from the file. Reset-passed and nothing-recorded rows use exactly the brief's words.
2. **The desktop entry.** The desktop copy line needs an entry for `desktop` in `receipts.json`. A receipts file that exists but has no such entry gets the same
   "time unknown" line as a missing or damaged one (the wording changed this round, see 9).
3. **A reading dated after the table's time is ignored.** It matters only for tests and clock drift.
4. **Several switch flags in the last 7 days.** One note per distinct date.
5. **Ages over 24 hours** are written `N d M h`, because a weekly floor can be days old.
6. **`--incomplete-coverage` with anything but A, B or C** stops with `not done: --incomplete-coverage must be A, B or C` (exit 3; the value is not repeated).
7. **Reset times.** The build showed weekday and time only, as the brief asked. This round every reset carries its date (the read showed two different days gave the same table).
8. **`now` does not take the collector's lock.** It only reads, so it is safe while the collector appends; a half-written last line is skipped and counted.
9. **Missing receipts (first read, item 4).** The line no longer says anything about B's figures, which are judged by each reading's own age: `desktop copy time unknown (its receipt is missing or unreadable)`.
10. **Rounding (first read, item 6).** Cut to one decimal, never rounded up, for every state; a whole number shows without a decimal (`12%`).
