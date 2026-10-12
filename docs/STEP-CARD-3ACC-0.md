# Step record - Card 3acc-0: the desktop export and the measuring tool

Work item `card-3acc-0`. Branch `card-3acc-0`, from main `4d9f6d5`. Audience: the liaison and the lead, who run the two programs, and the reviewer.
Python 3.11 or newer, standard library only. New files only: nothing in the running server, the reporter or any existing code changed.

## In plain words

The operator moves machines between three Claude subscriptions, which we call A, B and C (never by e-mail or account name). The accepted design
(`docs/DESIGN-THREE-ACCOUNTS.md`, SHA-256 `65585f20bd46b33cf130ee6618179f7b54ab4ae84d4f300a6cb5ad4032722312`, an unchanged copy) asks for one view of how much of each
subscription's 5-hour and weekly allowance is used. The liaison approved **step 0 only**: measure for a week before anything goes live.

This card adds two small programs:

- **`export_capacity.py`** runs on the desktop. It reads that machine's usage files (read-only), looks each account up in a small local table of labels, and
  writes **one cleaned file** with only: the machine, the export time, and for each label and window the percentage used, when it resets, when it was
  observed, where it came from and how many seat files carried it. The usage files hold the Windows user name; the cleaned file never does.
- **`measure.py`** runs on Laptop B. `collect` keeps each desktop copy; `from-store` turns Laptop B's own readings in the Leasewise store into the same kind of
  record; `report` counts, for each label and window, how often a figure was fresh, only an older "at least X%" floor, or unknown, and flags a **possible switch**
  where a subscription's weekly reset time moved when no reset had passed.

Nothing here runs by itself: no timer, no service, no network. The people named below start each command.

## What each program does

### `tools/three_accounts/export_capacity.py`

1. Lists the state folder: at most 4,096 entries looked at, at most 64 files named `<name>.capacity.json` of at most 16 KiB each, first by name (the reporter's own
   bounds). Links and folders are skipped.
2. Reads each file as one flat JSON object (no repeated key, no `NaN`, no nested value). Counts what it cannot use, keeping no text:
   - `old_format`: no schema version 2 or no `account` (the desktop's June leftovers);
   - `unknown_confidence`: the file says it holds no reading;
   - `other_source`: not a Claude account, or a source other than `claude_stream` and `claude_statusline`;
   - `unmapped`: an account not in the label table;
   - `unreadable`: could not be opened, or half a file (look again next time);
   - `refused`: parsed but breaks a rule (a missing or extra key, a nested value, a number or time out of range, a file over 16 KiB);
   - `over_limit`: files or entries beyond the bounds.
3. For each label and window (`five_hour` from the `primary_*` fields, `seven_day` from `secondary_*`) keeps the **newest** observation across that label's seat
   files, using the window's own observed time and falling back to the file's. `seats` is the number of usable seat files with that label.
4. Checks the finished document (below) and writes it to a temporary file beside `--out`, then renames it into place: no half file ever has the final name.

Output shape (times are epoch seconds, UTC):
`{"export_version": 1, "machine", "exported_at", "rows": [{"label", "window", "used_percent", "resets_at", "observed_at", "source", "seats"}], "counts": {...}}`.

### `tools/three_accounts/measure.py`

- **`collect --in <desktop copy> --history <folder>`** checks the copy with the same rules, and when its export time is newer than the last one kept for that
  machine, appends its rows to `<folder>/observations.jsonl` as observation records (with `seen_at`, the copy's age at that moment). A bad or older copy appends
  nothing and says so.
- **`from-store --db <inventory.db> --labels <aliases.json> --out <file.jsonl>`** opens the store with `file:...?mode=ro` only, takes Laptop B's `cli_window`
  readings for the aliases in the table, converts basis points to percent and milliseconds to seconds, and writes the same records (machine `laptop-b`, source
  `leasewise-store`). It rewrites `--out` completely each time, so it can be run again.
- **`report --observations <file>... --from YYYY-MM-DD --to YYYY-MM-DD [--json]`** samples every half hour (:00 and :30 UTC) per label, window and day:
  - **fresh**: a figure observed within the 600 seconds before the boundary;
  - **floor**: an older figure whose reset time is still ahead (read "at least X%");
  - **unknown**: neither. A reset exactly at the boundary has passed, so that is unknown.

  A label with any observation in the period is "in use"; if more than half of its samples (both windows together) are unknown it gets **STOP SIGNAL**.
  The **switch detector** looks at the weekly window only, between two consecutive distinct observations of one label: the reset time changes by more than
  300 seconds while the earlier one had not passed, or the percentage falls by more than one point within one reset period. A normal reset passing is not flagged.
  Every threshold is a named constant at the top of the file with a one-line reason.

## The exact command lines

The label table and every account text stay on the machine that owns them (see below). Paths in angle brackets are the runner's own.

**Liaison, on the desktop** (every 5 minutes after its agenttalk upgrade; read-only):

```
python <folder>\export_capacity.py --state-dir <agenttalk state folder> --labels <local label table> --machine desktop --out C:\lw\inbox\three-accounts\desktop-capacity.json
```

It prints one line of counts, for example `written: 2 row(s); files=1, used=1, old_format=0, ...`, and exits 0; exit 2 means it refused and wrote nothing; exit 3 means a
problem with the arguments or the table.

**Lead, on Laptop B:**

```
python tools\three_accounts\measure.py collect --in C:\lw\inbox\three-accounts\desktop-capacity.json --history <history folder>
python tools\three_accounts\measure.py from-store --db C:\lw\live\store\inventory.db --labels <aliases.json> --out <history folder>\store.jsonl
python tools\three_accounts\measure.py report --observations <history folder>\observations.jsonl <history folder>\store.jsonl --from 2026-10-12 --to 2026-10-18
```

Add `--json` to `report` for a machine-readable result. `from-store` and `report` change nothing in Leasewise; `collect` writes only into the history folder.

## The label table

- **Desktop table** (`--labels` of the export): a JSON object `{"<exact account text>": "A"}` with the label exactly `A`, `B` or `C`. The account text is the whole
  `claude:<Windows user>:home-<8 hex>` string as it appears in the usage files (so the table itself holds the user name).
- **Laptop B table** (`--labels` of `from-store`): `{"<account alias in the Leasewise store>": "A"}`, for example the alias of the account Laptop B uses mapped to `A`.
- **Where it lives:** on the machine that uses it, outside the repository and outside any folder that is copied (not in `C:\lw\inbox\three-accounts\`). It is never
  committed, sent or pasted into a report. The programs never print its contents or a key from it.
- The label follows the config **folder**, not the subscription: if a folder is logged into another subscription the label silently stays. That is what the
  switch detector is for; it is not a guarantee.

## What the export refuses, and why

Before writing, the finished document must be exactly the allowed shape: only the known top-level keys, rows with exactly the seven fields, a label of exactly `A`,
`B` or `C`, a window and a source from their fixed sets, whole-number times and finite percentages, counts that are whole numbers. It is **refused** (nothing
written, exit 2, counts printed) if any string in it contains `@`, a backslash, `/`, `:\`, `home-` or the local user name (any case). Why: the usage files
contain the Windows user name and a hash of the config folder; none of that may reach Laptop B's folder or a report. A refusal prints counts only; it never
prints an account text, a path or a file's content. The program also never writes into the state folder: it writes `--out` and its temporary sibling only.

## Known limits

- **The desktop gives zero rows until its agenttalk upgrade.** It runs 0.98.0, whose capacity files are June leftovers without an `account` field and with one
  shared stale figure. The export counts them as `old_format` and writes an empty row list; it does not fail. `collect` then keeps nothing but still reports.
- A figure exists only after a seat runs a turn, and it is current for 10 minutes; an idle label shows floors and then unknown. That is what is being measured.
- Laptop B's store rows come from `cli_window` readings only (Claude), arriving about twice an hour; the 600-second "fresh" rule makes most half-hour samples
  floors unless a reading lands just before a boundary. Read the report with that in mind.
- The stop rule pools both windows of a label; a label that only ever reports one window can reach "stop signal" for that reason.
- All times are UTC; the sample grid is fixed at :00 and :30.
- Two subscriptions that happen to share the same weekly reset time cannot be told apart by it: a switch between them would not be flagged.
- This covers Claude only. Nothing here goes live: no timer, no copy job, no change to the server or the reporter.

## Choices the brief did not settle

- Times in the export and the records are whole epoch seconds, so the files carry no time zone text.
- A repeated key or `NaN` makes a file `refused`; text that is only half a JSON document is `unreadable` (look again). This matches the reporter's reader.
- A usable file with confidence `stale` is kept: its figure carries its own old observed time and the floor rule handles it.
- Records from the store carry `seats: null` (a store reading has no seat count) and `copy_age_s: 0`.
- `collect` decides "newer" per machine by the export time of the last copy kept.
- A percentage fall of one point or less inside one reset period is treated as rounding, not a switch.
- The export's exit codes: 0 written, 2 refused, 3 a problem with the arguments or the table.

## Tests

`tests/test_three_accounts_export.py` (21) and `tests/test_three_accounts_measure.py` (21). Fixtures are complete documents built from exactly the reader's 27 listed
keys plus `plan_type` and `limit_id`, checked equal to `quota_balancer.reporter.capacityreader` and accepted by its own `read_capacity`; every account text and user
name is made up. They cover: the export of mapped files; the newest figure across seats; per-window observed times and the fallback; old-format files counted, not
failed; each counted kind; the input bounds; each unsafe-value refusal writing nothing; an atomic write that leaves no partial or temporary file after a failure;
the state folder left unchanged; `collect` keeping only newer copies and rejecting bad ones; `from-store` on a SQLite file the test builds, opened read-only,
store unchanged; the report on a synthetic week, including a boundary exactly at a reset; the stop signal; and the switch detector (one flag, one normal reset,
one small drift under the tolerance, a percentage fall, and the weekly-only rule).
