# Step record - Card 7b-on: switching usage readings on for Laptop B

Work item `card-7b-on`. Branch `card-7b-on`, from main `9a1e2b0` (card 7b version 2 merged). Audience: the lead and the reviewer.
Interpreter for the scripts: PowerShell 7 (rehearsed with 7.6.6); Leasewise: CPython 3.13, standard library only.

## In plain words

Card 7b lets the Leasewise reporter read agenttalk's usage files and send each account's 5-hour and weekly figures to Leasewise. It is switched on nowhere yet.
claude-agenttalk-lead approved switching it on (2026-10-09, answer to q-9ddb21b0c31c): first on Laptop B, for our own team's two accounts only (our Claude account and our
Codex account), with the figures staying on this machine, reading only our own agenttalk folder inside `C:\lw`.

This card writes **what the operator needs to do that safely**:

- a runbook in plain words, `docs/SWITCHON-7B-LAPTOP-B.md`: 14 numbered steps with a check after each of the two steps that need the operator's hand;
- one ready script per step in `docs/switchon-7b/` (17 files with the shared start and the entry guard), in the style of the money-reader trial;
- a drift test that keeps every runbook block identical to its script, `tests/test_switchon_scripts.py` (31 tests);
- a **rehearsal** of the whole script form on a throwaway copy, in the lead's scratch folder, with made-up data. The operator steps were **not** run for real: no `C:\lw\live` exists.

What the operator will do, in order: copy the scripts folder, edit three lines (Python path, archive path, checksum), then run the scripts one at a time. About 45 minutes of the
operator's time up to the point where the reporter runs, then a wait for one real turn of a Claude seat and of the Codex seat, then the done check.

## What the steps are

| Step | Script | Who | What it does |
|---|---|---|---|
| 0 | `switchon-00-check.ps1` | operator | prints the PowerShell version; changes nothing |
| 1 | `switchon-01-setup.ps1` | operator (edits 3 lines) | makes the private folder `C:\lw\live`, an ownership marker and `names.json` |
| 2 | `switchon-02-unpack.ps1` | operator | checks the archive's SHA-256, unpacks it into `C:\lw\live\src` |
| 3 | `switchon-03-check.ps1` | operator | checks Python 3.11+; counts agenttalk's usage files (a number only) |
| 4 | `switchon-04-store.ps1` | operator | creates Leasewise's empty store |
| 5 | `switchon-05-accounts.ps1` | **operator step** | the two accounts, the project, this machine and one placeholder seat that makes the machine an allowed reporter |
| 6 | `switchon-06-check-accounts.ps1` | check | opens the store read-only and asks Leasewise's own question: is this machine an allowed reporter for each account |
| 7 | `switchon-07-settings.ps1` | operator | private settings file, identity value (never printed) |
| 8 | `switchon-08-table.ps1` | **operator step** | asks for the two account texts, checks their shape, writes the two `[[readings.sources]]` rows |
| 9 | `switchon-09-check-table.ps1` | check | `reporter status` lists both rows by provider and alias only |
| 10 | `switchon-10-server.ps1` | operator (window 1) | Leasewise on `127.0.0.1` only |
| 11 | `switchon-11-reporter.ps1` | operator (window 2) | the reporter |
| 12 | `switchon-12-done.ps1` | check | the done check: accepted figures for both accounts, no warning |
| 13 | `switchon-13-stopped.ps1` | operator | no process left |
| 14 | `switchon-14-rollback.ps1` | operator | deletes `C:\lw\live` after three checks |
| - | `switchon-help-status.ps1` | operator | the readings status, aliases and closed words only |

## Decisions

- **Scripts are the one form.** The money-reader trial offered pasted blocks too, because a script was refused on a machine earlier. Here the runbook says: scripts, PowerShell 7, and if the machine refuses
  them, stop and tell the lead (the policy is never changed). Each runbook block is still printed, and a test keeps it identical to its script, so a reader sees exactly what runs.
  No "named adaptations" are needed: the values the operator edits are the same angle-bracket placeholders in the block and in the script.
- **One folder, `C:\lw\live`**, never `%LOCALAPPDATA%`, the user profile or another team's folder. The entry guard refuses any live or bus folder that is not a plain path inside `C:\lw`
  (dots, forward slashes, a trailing backslash, another drive), and any link (the live folder, `names.json`, the bus folder, the bus `state` folder).
- **The rehearsal needs another root, so the guard reads three environment variables** (`LW_SWITCHON_ROOT`, `_BUS`, `_PORT`, plus two for the account texts in step 8). They default to the real values, the runbook tells the
  operator never to set them, and the guard still refuses anything outside `C:\lw`. A seat of ours can therefore only rehearse inside `C:\lw`, never elsewhere.
- **Where the account texts go.** Step 8 asks for them with `Read-Host` (or the rehearsal environment variables), checks the shape (`claude:` or `codex:`, a user part, `:home-` and eight characters 0-9 a-f) without
  printing them, and appends them to the private settings file only. No script prints, logs or stores them elsewhere; the drift test checks that no script writes a secret variable anywhere but its private file.
- **The allowed-reporter list is the inventory.** Leasewise accepts a machine's readings for an account only if the machine has a seat in a project that has the account (`http_api._authorized_reporter_for_account`).
  Step 5 therefore imports a small inventory: two subscription accounts (`claude-main`, `codex-main`, no money budget), a project, a team, this machine and one placeholder seat `readings` in shadow mode (it grants and
  refuses nothing). Step 6 asks the same question of the store, read-only.
- **The done check reads two things.** Leasewise's store (a window row per account is what "accepted" means; a read-only query) and the reporter's status (no operator line, no failed reading, no capture fault).
  "Accepted" per account is not in the reporter's status (it counts rows in total), so the store is the proof.
- **Only the readings lane.** The settings have no `limit_events`, gateway or money-reader table, so none of those lanes runs.
- **The runbook is built from a template, the test is the guard.** The runbook's blocks were inserted from the scripts, and `check_drift` fails if either changes alone.

## Rehearsal (scratch only; `C:\lw\scratch\claude-lw-developer-1\switchon\`)

The scripts were copied; only step 1's three values (Python, a test archive of this branch, its checksum) were filled in. Root `...\rehearsal\live`, a fake bus folder with two synthetic seat files made from card 7b's
fixtures (placeholder texts `claude:<os-user>:home-...`), port 18443. The full transcript is `rehearsal-final.txt` there. In short:

| Step | Printed |
|---|---|
| 0 | `PowerShell 7.6.6`, `scripts run here` |
| 1 | `True` |
| 2 | the source folders (`docs`, `quota_balancer`, `tests` and files) |
| 3 | `3.13.7`, `2 usage file(s) found` |
| 4 | `True` |
| 5 | `claude-main`, `codex-main` |
| 6 | `claude-main: allowed reporter`, `codex-main: allowed reporter`, `both accounts: allowed` |
| 7 | `private: 3 access entries, all of them you, SYSTEM or Administrators` |
| 8 | `two rows written` |
| 9 | `claude -> claude-main`, `codex -> codex-main`, `table: ok` |
| 10, 11 | server and reporter started in the background; the reporter's line: `{"blocked": null, "journal": "ok", "new_events": 0, "sent": 4}` |
| 12 | `claude-main: 2 window(s) accepted by Leasewise`, `codex-main: 2 window(s) accepted by Leasewise`, `reporter: rows sent=4 waiting=0 failed=0; capture faults=0`, **`PASSED`** |
| help-status | the readings object: both sources by provider and alias, `rows.sent` 4, every scan counter 0 |
| 13 | nothing (after stopping both processes by exact PID) |
| 14 | `False`: the folder is gone; the fake bus folder and its files are untouched |

Also shown:

- **A wrong account table is refused.** A mistyped Claude text (`...home-ZZZZ`) stops step 8 with "The Claude text is not in the form ...", nothing written; the same Claude text typed for Codex stops it with the Codex
  sentence; a table whose Codex row names an alias that Leasewise does not know is listed wrongly by step 9 and refused by `reporter drain` with `CONFIG_UNRESOLVED: readings: an account is not in the policy-ids snapshot`; one text
  under two aliases is refused with `CONFIG_INVALID: readings: two sources name the same folder and agenttalk account`. No sentence showed a text.
- **An unmapped account file raises its operator line.** With the Codex seat file naming another account, `scan.unmapped` is 1 and the status says `1 seat file(s) name an agenttalk account that is not in readings.sources. Run
  "agenttalk capacity" ...`; no account text appears anywhere in the status.
- **A link stops the guard before anything is opened.** A junction used as the live folder, as the bus folder, and as the bus `state` folder each stop steps 1 and 3 with "... is a link. Nothing was changed. Tell the lead."
  A bus folder on another drive and a root with dots are refused with "is not a plain folder inside C:\lw". The junctions were removed without recursion; their targets were intact.
- The transcript holds no OS user name, no placeholder account text and no identity value.

## Tests

- `tests/test_switchon_scripts.py` (31): the folder holds exactly the listed files (ASCII, LF); every step loads the shared start first; every runbook block equals its script (and the check notices a changed block, a missing block, an
  extra block, a changed script, CRLF); the shared start and the guard are pinned by SHA-256 and hold the checks they promise; safety rules over all scripts (no profile or temp folder, no network, no policy change, only
  loopback addresses, only paths inside `C:\lw`, only the rollback deletes and only the live folder after the link and marker checks, the unpack goes only into `src`, no secret is printed or written outside its private file,
  the settings are made private before a secret is written, the server listens on `127.0.0.1` only, no other lane in the settings); the runbook's sections, its placeholders-only rule and that it names every script; and, when `pwsh`
  is on the path, a parse of every script, step 0, the guard against folders outside `C:\lw`, dotted and trailing-backslash paths, links (junctions) and another live folder in `names.json`, and a plain folder letting step 3 count a
  usage file without showing it. The link tests need `AGENTTALK_SCRATCH` inside `C:\lw`; otherwise they skip.
- **Found by that test:** the first guard let `C:\lw\live\` (a trailing backslash) through, because `GetFullPath` keeps it. The guard now also refuses a path ending in a backslash. (Writing the fix, a lost backslash briefly made the
  check refuse everything; the same tests showed it at once.)

## What the operator or the lead must provide

- **The lead:** the commit and `leasewise-<COMMIT>.zip` with its SHA-256 (`git archive` of `main`); the `docs/switchon-7b` folder; the `<COMMIT>` in the runbook; and, when the operator is ready, one small task for a Claude seat and one for the
  Codex seat (a real turn is what makes agenttalk write a fresh figure).
- **The operator:** Python 3.11+ path; PowerShell 7; port 8443 free; and the two account texts copied from their own screen in step 8.

## Limits and open points

- **Not run for real.** The rehearsal used made-up files and a throwaway root; the first real run may show a surprise (for example an agenttalk file that breaks the documented format: the reporter then counts it and shows its
  operator line).
- **The real account texts are unknown to this card.** The shape check (`claude:`/`codex:`, `:home-`, eight characters 0-9 a-f) follows the design (DESIGN-7B2.md 2.3); if agenttalk's real text differs, step 8 refuses it and the lead decides.
- **Port 8443** is shared with the money-reader trial's default. If a trial server runs, stop it first (or the lead sets another port for the rehearsal-only variable, which the operator never sets).
- **A reboot** needs only steps 10 and 11 again.
- **Not covered:** the gateway, the money reader, limit events, any other machine, and how long Leasewise keeps readings (spec question 12.3, for the liaison).
