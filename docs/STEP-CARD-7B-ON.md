# Step record - Card 7b-on: switching usage readings on for Laptop B

Work item `card-7b-on`. Branch `card-7b-on`, from main `9a1e2b0` (card 7b version 2 merged). Audience: the lead and the reviewer.
Interpreter for the scripts: PowerShell 7 (rehearsed with 7.6.6); Leasewise: CPython 3.13, standard library only.

## In plain words

Card 7b lets the Leasewise reporter read agenttalk's usage files and send each account's 5-hour and weekly figures to Leasewise. It is switched on nowhere yet.
claude-agenttalk-lead approved switching it on (2026-10-09, answer to q-9ddb21b0c31c): first on Laptop B, for our own team's two accounts only (our Claude account and our
Codex account), with the figures staying on this machine, reading only our own agenttalk folder inside `C:\lw`.

This card writes **what the operator needs to do that safely**:

- a runbook in plain words, `docs/SWITCHON-7B-LAPTOP-B.md`: 15 numbered steps (14 before fix round 1) with a check after each of the two steps that need the operator's hand;
- one ready script per step in `docs/switchon-7b/` (17 files with the shared start and the entry guard), in the style of the money-reader trial;
- a drift test that keeps every runbook block identical to its script, `tests/test_switchon_scripts.py` (first version: 31 tests; see "Fix round 1" for the current ones);
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
| 12 | `switchon-12-baseline.ps1` | operator | (fix round 1) records the newest accepted figure per account before the real turns |
| 13 | `switchon-13-done.ps1` | check | the done check: figures newer than the baseline for both accounts, a recent reporter scan, no warning |
| 14 | `switchon-14-stopped.ps1` | operator | lists what is still running (stops nothing) |
| 15 | `switchon-15-rollback.ps1` | operator | deletes `C:\lw\live` after its checks |
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


## Fix round 1 (of 2)

Codex read the first version (tk-8c58ba937b11) and returned FIX: five major points and one minor, each reproduced with synthetic data in its scratch. The lead's order (tk-4173d57d025f) is
followed point by point. Commits on top of `43ef67d`: the `identity-set` helper (`a5709bb`), the scripts and runbook (`45464e4`), the tests (`d6372ea`), this record.

### In plain words

The first version was careful about four named folders and trusted too much around them. Now **every folder on the way** to every file a step uses is checked for links, only a short list of names is read
back from `names.json`, the rehearsal switch is all-or-nothing, the account prompts show nothing, privacy is checked before anything private is written, the done check needs figures that arrived **after**
the operator started waiting, and the identity value never appears on a command line.

### Finding by finding

1. **Contain every file access (major).** `switchon-guard.ps1` now walks from `C:\lw` down to each path it uses (the live folder, `names.json`, the agenttalk folder, its `state` folder; the shared start adds the
   source folder, store, settings, state, journal, policy snapshot, marker and baseline; the archive and the marker are checked where they are used) and refuses a link at any level. It reads only an allow-list of
   fields from `names.json`, checks their form (hex token and checksum, aliases, an existing Python file), insists that the saved live folder, agenttalk folder and port equal the checked ones, and **recomputes** the
   source, store, settings and loopback address instead of reading them back. Step 1 and step 2 require the archive to be a plain `.zip` inside `C:\lw` with no link on its way. The rollback checks the marker (`.owner.json`)
   for a link before it reads it. Tests: `test_switchon_behaviour.Containment` (a linked ancestor, a saved bus / live folder / port that differs, a linked source folder, a linked archive parent, an archive outside `C:\lw`
   or not a zip, a linked marker with nothing deleted, injected derived names ignored, a foreign marker, a link inside the folder).
2. **A complete rehearsal mode (major).** Rehearsal needs `LW_SWITCHON_REHEARSAL` to be exactly `yes` (case sensitive: the first version of the check accepted `YES`) and **all** of `LW_SWITCHON_ROOT`, `_BUS`, `_PORT` (not 8443) and
   `_INPUT` (`env` with both test texts, or `prompt` with neither); both roots must lie inside `C:\lw\scratch`; the live and agenttalk folders may not be equal or nested. In operator mode any `LW_SWITCHON_...` variable stops
   the step with a plain message. The rollback checks the separation again right before it deletes. The runbook no longer counts the variables. Tests: `RehearsalMode` (a partial set, every variable in operator mode, the defaults,
   nested / equal / enclosing roots, a nested bus stopping the rollback with the sentinel intact, roots outside the scratch folder, the input setting, the port).
3. **A masked prompt (major).** Step 8 uses `Read-Host -MaskInput`. The test runs the **real interactive branch** in a Windows pseudo console (`tests/switchon_console.py`, ConPTY) with loud markers: they appear
   nowhere on the console, in no transcript and in no file except the private settings file. Real account texts are never moved into environment variables (the `env` input exists only in rehearsal mode, with synthetic texts).
4. **Privacy before writing (major).** Step 8 checks that the live folder and the settings file are private **before** it asks; step 7 checks the live folder, the store's folder and the store **before** the identity value exists,
   and the settings file before the enrollment. The after-write checks stay (`Assert-PrivateAfter`). The messages differ: "nothing secret has been written, and nothing was changed" before a write, "A secret HAS ALREADY BEEN
   WRITTEN there. Stop here and tell the lead" after one. Tests: `PrivacyBeforeWriting` (the file's SHA-256 is unchanged after a refusal; no settings file is even created by step 7; the two messages are different). The old test that asserted
   the late order was replaced.
5. **PASSED must prove new readings (major).** New step 12 records `baseline.json` (the newest accepted observation per account, in the store). Step 13 passes only if: every command it runs succeeded; the reporter scanned
   in the last 180 seconds; the settings list exactly the two expected sources; each account has an accepted observation **newer than the baseline**; and there is no operator line, failed reading or capture fault.
   Tests: `DoneCheck` (old rows only, equal to the baseline, none for one account, a reporter that never ran or stopped, a failed status command, a failed store query, wrong sources, a warning / failure / fault, no baseline, a good case).
6. **No secret on a command line (minor).** `identity-set` has a new `--identity-stdin` option (`quota_balancer/cli.py`; `--identity-ref` still works; exactly one is required); step 7 pipes the value to it. Tests: `tests/test_switchon_cli.py`
   and `NoSecretOnACommandLine` (a Python shim records every argument and the standard input of steps 4, 5 and 7: the 48-character value is on no command line and was read from standard input).
7. **Wording.** The placeholder seat `readings` "is never used to request a lease"; step 14 (was 13) says it only **lists** what is still running and that the operator stops it with Ctrl-C.

### Also changed

- Steps 12 to 14 are now 13 to 15 (the baseline is step 12). The done check reads a `$WaitMinutes` line (10); the tests run a copy with 0.
- The status time is read whether PowerShell turns it into a date or leaves it as text (the first draft of the check mis-read it).
- `tests/test_switchon_scripts.py` now has 30 tests (text and drift) and `tests/test_switchon_behaviour.py` 36 (they need PowerShell 7 and `AGENTTALK_SCRATCH` inside `C:\lw\scratch`; otherwise they skip).

### How the red-first order was shown

The new behaviour tests were run against a copy of the first version's scripts (`43ef67d`): 32 of the 36 fail there (some only because the step numbers moved; the genuine ones include the linked ancestor, the saved bus,
the privacy order, the echoing prompt and the done check passing on old rows). They pass on the new scripts.

### Rehearsal in the new mode (scratch only; transcripts `rehearsal2-run.log` and `rehearsal2-part2.txt` in the lead's scratch folder)

Steps 0 to 7 as before; step 8 on a pseudo console: two rows of asterisks, `two rows written`, no typed text visible; step 9 `table: ok`; windows 10 and 11; step 12 recorded both baselines;
**step 13 before any new figure failed** ("no accepted figure newer than the baseline yet" for both accounts); after fresh synthetic figures in both seat files it **passed** (`PASSED`, newest accepted figures newer than the baseline,
`rows sent=8 waiting=0 failed=0`); with the reporter stopped and three minutes gone it failed again ("has not scanned in the last three minutes"). The adversarial cases printed: a stray variable in operator mode ("A rehearsal setting
... is present in this window"), a missing port ("needs all of its settings and LW_SWITCHON_PORT is missing"), a nested / enclosing / equal bus ("the same, or one lies inside the other"), a rollback with a nested bus (refused; the folder
stayed), a saved bus that differs ("names.json was made for another ... port than this run uses"), a link in an ancestor, a linked source folder and a linked archive parent ("... is a link. Nothing was changed"), then the rollback
(`False`; the fake agenttalk folder intact). No transcript holds a user name, a placeholder account text or an identity value.
