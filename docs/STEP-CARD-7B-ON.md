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


## Fix round 2 (D-315)

Codex's delta read of fix round 1 (tk-539b98590527) still found two things: a link *inside* the checked source folder (a linked `quota_balancer` package) was imported by Python, and the baseline
could be passed by a figure that was observed before step 12 but delivered after it. The lead fixed the rules (D-315, order tk-e9725fd73ff0); this section builds exactly those. Commits on top of `314eae7`:
the scripts and runbook, the tests, and this record.

### In plain words

1. **One rule for links.** At the start of every step the scripts now look at *everything* inside the live folder, without ever entering a link. If any entry is a link, the step stops, says which one, and
   changes nothing. This happens before any file is read and before Python starts, so a linked source package can no longer be imported.
2. **The baseline remembers the time.** Step 12 now writes the machine's clock at that moment as well as the newest delivered figure per account.
3. **The done check refuses unless everything is proven.** A figure counts only if it was observed after the clock time of step 12 *and* after the newest figure delivered at that time. The reporter's
   status must be a proper JSON object with all the parts the check needs; its last scan must lie between three minutes ago and 30 seconds ahead of the clock.

### Rule by rule

1. **No link anywhere under the live folder.** `switchon-guard.ps1` has a new `Assert-NoLinkUnder`: a stack walk with `DirectoryInfo.GetFileSystemInfos()` (no `Get-ChildItem`), the root refused
   before it is queued, every entry's reparse-point flag looked at before a folder is pushed, at most 50,000 entries (more is a stop with a plain message). It runs right after the existing ancestor and bus
   checks and before `names.json` is read, so it precedes every Python command of every step. The rollback's own scan stays as a second look right before the delete. The runbook says it once ("How to run each step",
   "No link anywhere in the live folder") with the remaining limit: the live folder is private, so a link could appear between the check and its use only if the operator, SYSTEM or an Administrator made it.
2. **A time cut.** `baseline.json` is now `{"schema": 1, "cut_ms": <UTC milliseconds when step 12 ran>, "delivered": {"<alias>": <newest delivered observation or null>, ...}}`. The clock is read *before* the
   store is asked. A second run refuses; `switchon-12-baseline.ps1 -Again` replaces the file (the runbook names it only for the clock case).
3. **A fail-closed done check.** `switchon-13-done.ps1` passes only if: the baseline has exactly these fields (schema 1, whole numbers, exactly the two aliases, no extra field); each alias has an accepted
   observation later than both `cut_ms` and its delivered maximum; the status command exits 0 and returns a JSON **object** (the exact type is checked, because PowerShell calls a list a "custom object"
   too, which is how the first draft let `[]` through) with `readings` configured, `operator_actions` a list, `rows` and `capture_fault` with whole numbers, exactly the two expected sources and a last scan
   that parses; the last scan lies between 180 s ago and 30 s ahead; every other command exits 0 and its answer parses; and there is no operator line, failed reading or capture fault. A clock more than 30 s
   earlier than `cut_ms` stops with "The clock moved back since step 12 ... run step 12 again with -Again, then ask the lead for the two turns again".
4. **Tests.** `tests/test_switchon_behaviour.py`: `Containment` (a linked package below a plain source folder, refused by four steps with a tracer proving Python never started and the evil package never
   imported; a link at the reporter's database and at a store file; a link at depth; the 50,000 bound via a copy set to 5), `DoneCheck` (16: old rows; a figure observed before the cut but newer than the
   delivered maximum, at the cut and one millisecond before it; both bounds; a scan 60 s and an hour ahead fail, 15 s ahead and 0 pass; unreadable scan times; `null`, lists, a list holding a good object,
   text, nothing, and a dozen missing or mistyped parts; a malformed baseline in thirty-odd shapes, including the first version's file; the clock message and its 30 s tolerance; the baseline step; a plain rerun
   refuses and leaves the file byte for byte, `-Again` replaces). `tests/test_switchon_scripts.py`: the guard walk and its order, the new done-check conditions, the runbook's new sentences (32 tests).
5. **The default-path test** (minor) no longer opens `C:\lw\live`: it runs only the guard's final `else` branch (plain assignments, checked to contain no file-system command) and a second test shows a stray
   variable stops the start before any folder is opened.
6. **The pseudo-console helper** (minor): every handle, the pseudo console and the attribute list are released in a `finally`; the child is waited for by its own handle (`WaitForSingleObject`) and stopped by that
   handle if it outlives the wait. A test counts the process's handles over four runs, one of which has to be stopped.
7. **Wording.** The runbook no longer says that a minute of waiting proves anything (step 12 says waiting does not prove the delivery queue is empty and why the time cut makes that safe); "scanned within
   three minutes" is explained as the reporter having run recently, not as still running; the stale sentence about unchanged privacy helpers is replaced.

### How the red-first order was shown

The new and changed behaviour tests were run, unchanged, in a detached worktree at `314eae7` (since removed): 18 of 50 fail there (the linked package, the database and store links, the deep link, the bound,
the late figure, the future scan, the null and list statuses, the malformed baselines, the clock message, `-Again`, and the handle count; some of the `DoneCheck` failures are only because the baseline
file changed shape). Saved list: `red-fix2-behaviour.txt` in the developer's scratch.

### Rehearsal in strict mode (scratch only; `rehearsal3-part1.txt`, `rehearsal3-part2.txt`)

Steps 0 to 15 as before. Step 12 printed the baseline time; a plain second run refused. Step 13 failed before any new figure. Then a figure was written whose observation time was 2 seconds *before* step 12 and
delivered after it: the store held it (newest accepted figure after the baseline's delivered one, older than the cut) and step 13 still failed. After real-shaped new figures it passed (`PASSED`, rows sent 12,
none waiting or failed); with the reporter stopped and three minutes gone it failed again. Then the adversarial cases: a linked package below the plain source folder was refused by steps 3 and 13 with the link
named; a baseline from the future stopped step 13 with the clock message and `-Again` replaced it; the older cases (a stray variable, a partial set, overlapping roots, a saved bus that differs, a link in an
ancestor, a linked source folder, a linked archive parent) each stopped with "Nothing was changed"; step 15 then printed `False` and the fake agenttalk folder was intact. The transcripts hold no user name,
no placeholder account text and no identity value.


## Card 7b-on2 (liaison cold read)

The liaison (claude-agenttalk-lead) read the merged runbook cold, on the desktop, and returned FIX (commit `af33f1a`). The safety design passed: no policy change, no install, no writes outside the live folder, loopback only,
no other team's files, no secret printed. Three things blocked it and six more were to be fixed before the run. The lead decided each one (order tk-e36437a82cac); this section builds exactly those, plus Codex's one MINOR
from the last read. Two commits on top of `af33f1a`: the scripts with the rebuilt runbook, and the tests.

### In plain words

1. **Step 8 now asks for the parts, not for text the screen never shows.** `agenttalk capacity` prints a sentence, not the `claude:<name>:home-xxxxxxxx` text. The script asks for the name and the 8 characters (four masked
   questions), puts the text together itself and then compares it with our own usage files, so a typo is found before anything is written.
2. **The runbook names the agenttalk version** (0.99.0 or newer, every seat restarted, one turn done) and step 3 refuses unless both providers have a file the reporter can use.
3. **Port 8443 is checked.** Step 10 will not start while anything listens on the port. Step 11 will not start the reporter (which carries the identity value) unless the only listener is window 1's own server.
4. **Step 1 and step 15 can always undo each other**, step 15 will not delete while a window is running, and it deletes the marker last so a failed delete can be repeated.
5. Smaller stops and wording (windows that fail, step 1's questions, step 5 twice, step 12's wait, the commit sentence, step 14's text), **Python without outside influence**, **privacy per Windows account**, and the clock
   is read again when step 13 decides.

### Finding by finding

1. **Step 8 (blocking).** `switchon-08-table.ps1`: four masked questions (the name after "OS user" and the 8 characters of `home-xxxxxxxx`, for each provider; `home-` in front is optional; exactly 8 of 0-9 a-f; a name is 1 to 256
   characters with no colon, no control character and no space at either end). The messages say which part is wrong and never show it. The assembled text must equal the `account` of a usable usage file of that provider; only a
   count is printed ("Claude: matches 1 usage file(s)"). The reading of the usage files is one function, `Get-UsageAccounts`, in `switchon-common.ps1`, shared with step 3, with the reporter's limits (first 64 files by name, 16 KiB,
   links skipped). A file counts as a match only if the reporter could use it (see 2). Rehearsal input now sets four parts (`LW_SWITCHON_CLAUDE_NAME`, `_CLAUDE_HOME`, `_CODEX_NAME`, `_CODEX_HOME`); all are in the guard's list, so
   any one of them stops an operator's run. The troubleshooting line for "N seat file(s) name an agenttalk account that is not in readings.sources" now says that step 8 proved a match, so the line means a seat runs under another
   Windows user or home folder.
2. **agenttalk version (blocking).** "Before you start" names agenttalk 0.99.0 or newer, every seat restarted on it, one turn since. Step 3 counts, per provider, the files with `schema_version` exactly the whole number 2, `scope`
   "account" and a non-empty `account` starting with `claude:` or `codex:` (as `capacityreader.py` accepts schema 2 only) and refuses if either provider has none; the runbook says that if a later agenttalk writes another format,
   step 3 stops and the operator tells the lead.
3. **Port 8443 (blocking).** `Get-PortListeners` (Get-NetTCPConnection, IPv4 and IPv6, any address, joined with the process list): step 10 stops with the process number and program name if anything listens, and also if the list
   cannot be read (only the one error "nothing listens there" means none; a first draft treated every "not found" error as none, which a test with a missing cmdlet showed to be fail-open). Step 11 requires exactly one listener, on
   `127.0.0.1`, whose command line has `quota_balancer`, `serve` and `--db` followed by exactly the store (case-insensitive), run by step 1's Python, directly or as the child of a launcher with the same command line; and it refuses a
   second reporter for the same settings file. No server code changed.
4. **Step 1 (before the run).** Order: ask and check; create the folder; write the marker and `names.json` at once; make the folder private and check it; make the subfolders. A privacy failure says "was made but could not be made
   private. Nothing secret was written. Run step 15 to remove it, then tell the lead." Step 15 without `names.json` deletes only a folder whose marker names it and which holds nothing but the marker.
5. **Step 15 (before the run).** It runs step 14's process check first (one shared function, leaving out its own process) and refuses while window 1 or 2 runs. Delete order: everything except the marker and `names.json`, then
   `names.json`, then the marker, then the empty folder; a failure part way keeps the first two.
6. **Windows that fail.** Steps 10 and 11 say: if a window prints an error or closes by itself, stop, press Ctrl-C in the other, start nothing again, tell the lead which window and its last line (without any path that holds your
   user name); never run step 10 or 11 while their windows are open (the scripts refuse that now).
7. **Wording and small stops.** (a) Step 1 asks three plain questions (not secret, so shown; quote marks are removed); the aliases stay fixed; rehearsal variables `LW_SWITCHON_PYTHON`, `_ARCHIVE`, `_SHA` are in the guard's list
   (all set or none). (b) Step 12 waits two minutes and for a line with `"sent"` above 0; the reporter sends nothing when every usage file is older than ten minutes (a figure counts for ten minutes), so the text says: if every
   line still shows 0 after five minutes, go on. (c) Step 5 refuses if the store already has an account or a machine. (d) The commit sentences read correctly both with the placeholder and in the hand-over copy. (e) Step 14's text
   says it reads all processes and shows only those that name the live folder.
8. **Python without outside influence.** One list, `$PyFlags = -E -s -B`, in `switchon-common.ps1`, used by every Python call (a drift test checks every call). `-I` was not used because the scripts run from the source folder.
   `PYTHONDONTWRITEBYTECODE` is gone (`-E` ignores it; `-B` replaces it).
9. **Privacy per Windows account.** Said plainly in the runbook: only your Windows account, SYSTEM and Administrators can open the folder; any program running under your Windows account can read it, including another team's seats
   on this machine if they run under the same account (when the lead checked on 2026-10-09, only this team's seat wrappers were running on Laptop B, all under one account; the limit applies if another team's seats ever run here under it); what that exposes is the identity value (it only lets a program send readings to this machine's own Leasewise) and the account texts.
10. **Clock (Codex's MINOR).** Step 13 reads the clock through one function, `Get-Now`, at the start and again, fresh, when it decides; the "clock moved back" check and the scan age use that fresh reading.

### Stated limits (also in the runbook's "Limits that remain")

- **Link race:** the live folder is private, so a link could appear between the check and its use only if the operator, SYSTEM or an Administrator made it.
- **Port take-over:** the checks of steps 10 and 11 happen once, when the step starts. A program that took the port over after step 11's check would have to run under the operator's own Windows account (Windows refuses a second
  binding of a port in use from another account; Microsoft's page "Using SO_REUSEADDR and SO_EXCLUSIVEADDRUSE" describes this), and such a program can read the settings file anyway. On this machine, with two sockets on a scratch
  port in the same account: when both set `SO_REUSEADDR` (which Python's `ThreadingHTTPServer`, the Leasewise server, does), the second bind succeeds and the connection I made reached the first; when only the second set it, or
  only the first, or neither, the second bind was refused; with `SO_EXCLUSIVEADDRUSE` on the first the second was refused. So the server's own socket does not stop a same-account second listener; the start checks do. Nothing more is claimed.
- **Look-alike:** a program of the operator's own account that imitates window 1's command line would pass step 11's check; it could read the settings file anyway.
- Hard links are not detected (the scripts only write files they create).

### What I saw with the venv launcher

With `C:\lw\venv\Scripts\python.exe` as step 1's Python, window 1's socket belongs to a child process whose program is the base interpreter and whose parent is the launcher; both have the same command line. Step 11 accepted it
(rehearsal and `test_our_own_server_is_accepted_including_through_the_venv_launcher`); the check accepts a child only when its parent is step 1's Python with the same command line. Step 14 lists both processes.

### Not built

The liaison's separate request to switch the agenttalk turn journal on for one seat (for the clock-skip measurement) is not part of this order; nothing here touches it.

### How the red-first order was shown

The new test file was run in a detached worktree at `af33f1a` (since removed): 41 of its 82 tests fail there (step 1's questions and privacy order, the clock seam, the whole port group, step 8's parts and typo, step 3's formats, step
5 twice, step 15's cases, Python isolation, the rehearsal variables). Saved list: `red-on2-behaviour.txt` in the developer's scratch. The clock tests fail there on the missing `Get-Now` seam rather than on behaviour.

### Rehearsal (scratch only, strict mode; `rehearsal4-part1.txt`, `rehearsal4-part2.txt`)

Steps 0 to 15 with made-up data and the venv python. Step 1 on a pseudo console (three questions, answers shown), step 8 on a pseudo console (four masked questions, nothing typed visible, two `matches` lines). A stranger on the
rehearsal port: step 10 and step 11 both refused, nothing started. Window 1 started; a second step 10 refused; window 2 started; a second step 11 refused. Idle seats (usage files an hour old): window 2 printed `"sent": 0`
(so step 12's text allows going on); after fresh files one line showed `"sent": 4`. Baseline, the done check failing before new figures and for a figure observed before step 12 and delivered after it, passing after new figures.
Step 14 listed the four processes; step 15 refused while they ran. Then the stray-variable, partial-answers, linked-package, clock-moved-back (`-Again`) and step-15 cases on a marker-only folder (removed) and a marker with
something else (refused). Step 15 then printed `False`; the fake agenttalk folder was intact.


### Fix round 1 (Codex delta tk-982c65ffad44)

Codex read card 7b-on2 at `9914603` and returned FIX: one MAJOR and two MINOR points, all reproduced with synthetic data. The lead fixed the rules (order tk-0b8c734a7c0c); this section builds exactly those. Two commits on top
of `9914603`: the scripts with the rebuilt runbook, and the tests.

**In plain words.** Step 11 used to accept any program whose command line contained the right words, so an unrelated script given those words as arguments would have passed, and the identity value could have gone to it. It
now accepts only the exact command that step 10 launches. Steps 3 and 8 used to copy the reporter's rules for reading the usage files in PowerShell, and the copy differed from the reporter (it counted links before dropping them,
and checked the size before a separate full read). They now ask the reporter's own scan, so they cannot differ.

1. **Step 11 checks the exact command (MAJOR).** `Assert-OurServerListens` in `switchon-common.ps1`. The listener's command line is split into words (an odd number of quote marks, or a process whose command line or program cannot
   be read, is refused). The words after the program must equal, in order and with nothing before, between or after, `$PyFlags` followed by `Get-ServeArgs`: the one function step 10 also uses
   (`-E -s -B -m quota_balancer --db <store> serve --host 127.0.0.1 --port <port>`); the store is compared as a Windows path ignoring case, every other word exactly. The program is one of two cases: (a) step 1's Python itself,
   with its first word naming the same file; (b) when step 1's Python is a venv launcher (a `pyvenv.cfg` in the folder above its folder, inside `C:\lw`, not a link), the listener's program equals that file's `executable =`
   value (or `home =` plus `python.exe`), its parent is step 1's Python, and the parent's command line is identical to the child's. No other child is accepted. The runbook and this record say the remaining limit once: a program
   under the operator's own Windows account could copy this exact command line, for example from another folder; it could read the settings file anyway.
2. **Steps 3 and 8 use the reporter's own scan (MINOR 2 and 3).** `Get-UsageAccounts` is gone. One small Python helper (a here-string in `switchon-common.ps1`, run with `$PyFlags` from the source folder like the other Leasewise
   commands) calls `quota_balancer.reporter.capacityreader.scan(<agenttalk folder>, <now in ms>)`, which already applies `strictfile.walk` (entries bounded before collecting, links and folders dropped, names checked, then 64
   files) and `strictfile.load_json` (reads at most 16,385 bytes). Step 3 gets, per provider, the number of accepted files whose account starts with `claude:` or `codex:`. Step 8 sends the two assembled texts on **standard input**
   (JSON, non-ASCII escaped) and gets, per provider, how many accepted files have exactly that account. The answer is numbers only; on any problem the helper prints one fixed sentence and exits non-zero (it catches everything, so no
   exception text can carry an account), and the step stops with a plain message. A folder that cannot be listed, or more entries or files than the reporter looks at, stops the step and says so. `scan` could be called cleanly this
   way, so nothing is mirrored in PowerShell.
3. **Record and runbook.** The runbook says step 11's exact check and its remaining limit, and that steps 3 and 8 use the reporter's own scan ("the very code the reporter runs"). The lead's rewording of the shared-account sentence
   (`9914603`) is kept.

**Tests** (`tests/test_switchon_behaviour.py`, `tests/test_switchon_scripts.py`). `ExactServerCommand`: a fake `quota_balancer` package in the working folder listens on the test port whatever it is given, so any command line can be
tried; the exact command is accepted whatever the case of the store; refused: an unrelated script given the words and the exact store, `-c` mode, a trailing extra word, an extra word in the middle, a different flag order, a missing
flag, another port number, another store, `serve` before `--db`; a copied venv launcher with a matching `pyvenv.cfg` is accepted (with `executable`, and with `home` only) and refused when the file names another interpreter; a
child whose parent's command line differs is refused; a non-venv Python with a child is refused. `RealServerAcceptance` and the earlier real-server test cover the genuine direct and the genuine venv-launcher cases. `UsageFormat`:
64 links that sort first plus two plain files are both counted; a file over 16 KiB and a valid small document followed by 20,000 blanks are not counted (and the same document without the blanks is); more than 64 files stops the
step with the plain message; a broken scan stops the step with no account text and no traceback; the texts reach the scan on standard input only. The usage files in the tests are now complete synthetic fixtures of the reporter's
format, because the reporter's own scan checks every field. The tests also pick a free port instead of a fixed one (another seat's test run was found holding the old one).

**Red first.** The new test file was run on `9914603` (detached worktree, since removed): 6 of 92 tests fail there: the unrelated-script case, the venv child whose program is not the file's interpreter, the 64 earlier links, the
more-than-64-files stop, the broken scan, and the texts-on-standard-input test. The others pass there too: the old code already refused a child whose parent's command line differs, a non-venv Python with a child, and any file over
16 KiB (it checked the size). The new tests still pin those.

**Rehearsal** (`rehearsal5-part1.txt`, `rehearsal5-part2.txt` in the developer's scratch): steps 0 to 15 in strict mode with the venv Python as before, and it adds an unrelated script holding the port with the exact store (refused; no reporter running afterwards).


### Fix round 2 (Codex delta 2 tk-2d28eb122f76, fallback D-321)

Codex read the card at `862f0a7` and returned FIX again, on step 11's listener check only (the two usage-scan findings were closed). Its MAJOR points: the check split the listener's command line into words in its own way, and Python
splits it differently (`-m "quota_balancer"--db "<store>"` passes the word check, but Python runs the module `quota_balancer--db`); the store word was resolved in the checker's folder, so `..\store\inventory.db` named another
database from another folder; and a drive-relative `executable = C:..\..\base\python.exe` in `pyvenv.cfg` counted as rooted. The lesson: reading another process's command line can never prove what that process runs. So the
lead's pre-declared fallback (D-321) applies: **step 11 identifies the server by the process step 10 started and recorded, never by the text of a command line.** Two commits on top of `862f0a7`.

**In plain words.** Step 10 now starts the server itself and writes down which program it started (its number and its start time) before it waits. Step 11 accepts only that program, or the one program it started in turn
(a virtual environment's `python.exe` starts the real interpreter as a second process that owns the port). No command line is read to decide this, so no clever command line can pass.

1. **Step 10 starts and records.** `switchon-10-server.ps1`: the port check stays first; then `System.Diagnostics.ProcessStartInfo` with `FileName` = step 1's Python, `ArgumentList` filled one word at a time from `$PyFlags` and
   `Get-ServeArgs` (so .NET does the Windows quoting, never string joining), `UseShellExecute = $false`, no redirection (the server shares window 1 as before), working folder = the source folder. Before it waits it writes
   `server.json` with exactly `schema` 1, `pid`, `started_utc_ticks` (from the process's `StartTime`) and `port`. It then waits in slices of 500 milliseconds (so Ctrl-C works); a `finally` gives the server five seconds to follow
   Ctrl-C and then stops the process it started. When the server ends it says "Window 1's server has stopped." (exit code 0) or "Window 1's server stopped with an error (code N). Tell the lead." `server.json` is in the guard's
   list of derived paths checked for links.
2. **Step 11 checks only the record.** `Assert-OurServerListens` reads `server.json` strictly like the baseline (exactly the four fields, whole numbers, schema 1, this run's port); a missing or malformed file says "Window 1 was
   not started by step 10 of this runbook. Start step 10 first. Nothing was started." The recorded process must still run with the recorded start time (a reused process number does not match) and its program must be step 1's Python
   (a Windows path, ignoring case). Exactly one listener on the port, on `127.0.0.1`, owned by (a) the recorded process or (b) its direct child (its parent number is the recorded one and it started at or after the recorded
   time); the child's program and command line are not looked at. Anything else gives the existing "not window 1's Leasewise server ... the identity value was not sent" stop; an unreadable process or port list stops the step.
   All command-line parsing and the `pyvenv.cfg` logic are gone from the function. `Get-ServeArgs` stays as the single source of step 10's arguments. `Assert-NoReporterRunning` still reads command-line words because it can only
   refuse (and a second reporter is also stopped by the reporter's own exclusive state lock); a comment says so. The strict-reading helpers (`Test-Whole`, `Test-Object`, `Test-HasAll`, `Test-HasOnly`) moved from step 13 into the
   shared start.
3. **Runbook.** Steps 10 and 11 say it in plain words; every sentence about the exact command-line check and `pyvenv.cfg` is gone.

**Remaining limit, stated once:** a program under the operator's own Windows account could stop the server and take the port after step 11's check, or edit `server.json`. Such a program can read the settings file anyway: the
shared-account limit already stated.

**Tests** (`RecordedServer`, `Step10Starts` in `tests/test_switchon_behaviour.py`; the drift test). Accepted: the real server started by step 10 itself, with the base Python and through the venv launcher (the child owns the socket); a
recorded launcher with a listening child; a recorded base interpreter that listens itself. Refused, each also proving that the real step 11 stops before it would start the reporter (a copy of step 11 whose last line only says it
would start it): Codex's quoted-boundary command started by a test; the relative-store command; a stranger that is neither the recorded process nor its child; a grandchild; a recorded process that has ended; an edited or earlier
start time; a recorded program that is not step 1's Python; two listeners (one on `::1`); a listener on every address; a missing record and fifteen malformed ones (not JSON, a list, `null`, an empty object, an extra field, the wrong
port, schema 2, a text or negative or zero or fractional number, a missing field), all with the real listener present; a link in place of `server.json`. Step 10: it passes exactly the flags and the serve words through
`ArgumentList` with a store path containing a space (the fake server reports its own arguments and flags), writes the record while the server still runs (checked with the server paused), the recorded start time equals the
process's own; a clean exit, an exit with code 3 and a missing module (code 1) give the right messages; Ctrl-C through the pseudo console ends the script and the server, the port is free and step 14 lists nothing. The drift test
pins that step 10 has no call operator, writes the record before `WaitForExit(500)`, shares `Get-ServeArgs` with step 11, and that no command-line text is left in `Assert-OurServerListens`.

**Red first.** The new test file was run on `862f0a7` (detached worktree, since removed): 7 of 18 tests of the two new classes fail there (the quoted-boundary command, the missing and malformed records, the launcher and direct
acceptances through a record, step 10's arguments and record, its messages, and Ctrl-C). The rest pass there too: the old check already refused the relative-store command (it resolved it in another folder), strangers and a
grandchild.

**Rehearsal** (`rehearsal6-part1.txt`, `rehearsal6-part2.txt`, strict mode, venv Python): a stranger on the port before any record (step 10 and step 11 refused; no reporter ran); an unrelated script holding the port (refused); window 1
accepted through the venv launcher and its child, a second step 10 refused, window 2 up, a second step 11 refused; the baseline, the done check and step 14/15 as before; then, with the record of the stopped window 1 still in place,
Codex's quoted-boundary stranger holding the port (step 11 refused, 0 reporter processes afterwards); then Ctrl-C in two pseudo consoles ended window 1 (the server's process was gone, the port free) and window 2, and step 14 listed
nothing; the adversarial cases and step 15 as before.

### Close-out (Codex delta 3 tk-505934ca5f9b)

Codex's third read closed every listener-identity finding and found one new MAJOR in step 10: the script started the server and then built and wrote `server.json` before its `try`/`finally`, so if the write failed the cleanup
never ran and an unrecorded server kept running (step 11 refused it, so nothing unsafe followed, but the operator could not simply retry). Step 10 now refuses first if `server.json` exists and is not a plain file ("... is not a
file. Nothing was started. Tell the lead."); the start and the record are both inside the `try`; and if the record is not saved, the `finally` stops the server and everything it started at once (`Kill($true)`, then waits for it to be
gone) and the operator reads "The server was started but its record could not be saved, so it was stopped again. Nothing else was changed. Tell the lead." The normal path keeps its five seconds' grace after Ctrl-C. The record is
written by a one-line function, `Save-ServerRecord`, which the test replaces with a failing one in a copy of the script (a test hook; no production check is weakened). Tests (`Step10Starts`): a folder at `server.json` starts nothing;
a failing record write, run directly and through the venv launcher, leaves no server process (the process the script was handed is gone, the port is free, step 14 lists nothing) and gives the plain message; the normal path and Ctrl-C
tests pass as before. Both new tests fail on `b914256` (the first with the raw .NET error after the server had started; the second at the missing seam).

## Card 7b-on3 (liaison re-read N1-N6)

The liaison's desktop re-read of the merged runbook (card 7b-on2, `main` `6b3c77e`) said GO and listed six points; the lead took all six (order tk-c976f811aea5). Three commits on top of `6b3c77e`: the scripts and the rebuilt
runbook, the tests, and the runbook's status line with this record.

**In plain words.** A step that asks "does anything listen on this port?" now treats only one exact answer as "no". The runbook says what the scripts read and prints, asks for PowerShell 7.3 or newer (step 0 refuses an older
one), step 1 no longer writes the file that holds the path of Python until the folder is private, and step 15's refusal names the program it found. The final version was rehearsed on this machine and the logs are kept.

1. **N1, the listener list.** `Get-PortListeners` reads the error of `Get-NetTCPConnection -State Listen -LocalPort <port>` as "nothing listens" only when BOTH hold: its identifier matches `CmdletizationQuery_NotFound*,Get-NetTCPConnection`
   (a prefix, because the same query without `-State Listen` says `..._NotFound_LocalPort,...`) and its exception is a `Microsoft.PowerShell.Cmdletization.Cim.CimJobException`. A missing command (same category, other identifier and
   exception), access denied, a CIM error with another identifier and a binding error all stop the step (fail closed). Tests (`PortListNothing`): both identifier forms mean empty (the second is produced by the real cmdlet without
   `-State Listen` in a copy of the shared start); a missing cmdlet, a CIM error with another identifier (`CimJob_BrokenCimSession`, produced with a made-up `-CimSession`) and a binding error each stop the step; the real server tests
   still start step 10 on a real free port.
2. **N3, what the scripts read.** One plain bullet in "What this does": steps 10, 11, 14 and 15 look at the list of all programs running on this machine (to find who holds the port, a second reporter, or a program using the live
   folder); they print only process numbers and program names, never a command line; a program name shown by step 10's refusal may be passed to the lead. A runbook test checks the sentences.
3. **N4, PowerShell version.** "Before you start" says PowerShell 7.3 or newer. Step 0 prints the version, then refuses an older one: "This needs PowerShell 7.3 or newer; this window runs <version>. Nothing was changed." The version
   comes from one line (`$Version = $PSVersionTable.PSVersion`) that the test replaces in a copy; step 0 still reads nothing else. Test (`Step0Version`): 7.2.9, 7.0.0, 5.1 and 6.2 refused; 7.3.0, 7.6.6 and 8.0.0 pass; the real
   version passes.
4. **N5, `names.json` after privacy.** Step 1 now: checks the answers; creates the folder; writes only `.owner.json` (a random token and the folder path); makes the folder private and reads the access list back; only then writes
   `names.json` (it holds the path of Python, which can contain the user name); then the subfolders. A privacy failure says "`C:\lw\live` was made and holds only the ownership marker (a random token and the folder path), but it could
   not be made private. Nothing secret and none of your answers were written. Run step 15 to remove it, then tell the lead." Tests: a privacy failure, both ways it can fail (the access list cannot be set; it reads back wrong), leaves a
   folder holding exactly the marker (no `names.json`, no subfolder, no path of Python in the marker), and the real step 15 then removes it by its marker-only path; the drift test pins that the privacy check comes before the
   `names.json` write and that step 1 writes exactly two files.
5. **N6, step 15's wording.** "A program that uses the live folder is still running (process N, program X). If it is window 1 or 2, press Ctrl-C there; otherwise close that program. Then run step 15 again. Nothing was deleted."
   The runbook text and the tests (the process number and `python.exe` are checked) follow.
6. **N2, a recorded rehearsal of this version.** `C:\lw\scratch\claude-lw-developer-1\switchon\rehearsal7\` holds one log per case and `INDEX.md` (head, PowerShell 7.6.6, result, log file): (a) a free port, step 10 starts and a
   second step 10 is refused; (b) a busy port, step 10 refuses and step 11 refuses a stranger; (c) the venv Python, the child owns the socket and step 11 accepts it, and its reporter then runs; (d) step 1 stopping at the privacy
   check (simulated) and step 15 removing the folder; (e) step 15 refusing while a program runs and then succeeding; (f) the whole run, steps 0 to 15, through `PASSED` and the final rollback. All six passed. The logs were scrubbed
   in two ways, and `INDEX.md` says so: the driver searched them for the Windows user name (from the environment), the typed name and home parts, a user-profile path, and the identity value (read from the settings file just
   before the rollback), and a separate script searched all seven files again for the user name from both the environment and the operating system: no finding. The runbook's status line now says exactly this was rehearsed, on
   made-up data, on this machine's PowerShell 7.6, and that it has not been run for real.

**Red first.** The new behaviour tests were run on `6b3c77e` (detached worktree, since removed): the PowerShell-version test, the identifier-forms test, the privacy-failure test and the step 15 wording test fail there; the test that
a missing command, another CIM error and a binding error stop the step passes there too (the old code compared the whole identifier, so it already stopped on them).

## Card 7b-on4 (liaison re-read 2)

The liaison's second desktop re-read of the merged runbook (card 7b-on3, `main` `2b6263d`) said GO and asked for five small points before the operator gets it (order tk-76d1b2959f56, continued as tk-9d7cd02b544a).
Documents only: `git diff --name-only 2b6263d HEAD` lists `docs/STEP-CARD-7B-ON.md` and `docs/SWITCHON-7B-LAPTOP-B.md` and nothing else; no script or test changed.

**The five points.**
1. **A rehearsal in a normal window.** The earlier rehearsals ran in our seats' elevated sessions (window title "Administrator"), where a new folder is owned by the Administrators group; the operator will use a normal window.
   `runas /trustlevel:0x20000` was tried first: it made Administrators "deny only" but left the integrity level at High, so it was not a normal window (kept as case f2, labelled as such). Case f2m then ran the whole
   run through the desktop shell (`explorer.exe` handed a `.cmd` file to the running, non-elevated shell): its log opens with `IsInRole('Administrators')` = False and `whoami /groups` showing
   `Mandatory Label\Medium Mandatory Level`.
2. **Step 11's identity refusal with a valid `server.json`.** Cases g1, g2 and g3 (below).
3. **One self-contained index** of every rehearsal: `C:\lw\scratch\claude-lw-developer-1\switchon\rehearsal8\INDEX.md` (commit, window kind, PowerShell version, result, file), with the notes that (d) simulates the privacy
   failure with a copy of the shared script whose access-list function throws (it proves the recovery, not a real access-list error) and that (f) begins with a refused step 10 and 11 because the stranger of case (b) still held the port.
4. **The runbook names the window kind** ("a normal PowerShell 7 window, not 'Run as administrator'") in "How to run each step" and steps 10 and 11, and its status line tells the two rehearsed versions and the window kinds apart (this record and the rehearsal index name the commits). The normal-window run was started by a script through
   the desktop shell, not opened by hand from the Start menu, and steps 1 and 8 took their answers from the rehearsal's test input; the status line says so (liaison, 2026-10-10).
5. **One sentence in step 11:** if another program also listens on the port, step 11 refuses even a working window 1 until that program is gone (see the g1 finding).

**Every rehearsal case** (strict rehearsal mode, made-up data, a system-chosen loopback port, the venv launcher as step 1's Python, PowerShell 7.6.6, all PASS):
- a free port; a busy port; the venv child accepted; step 1 stopping at the privacy check with step 15 removing the folder; step 15 refusing then succeeding; the whole run (cases a-f, elevated, on `5a29dd9`);
- f2: the whole run with administrator rights switched off but High integrity (on `2b6263d`);
- **f2m: the whole run in a normal, medium-integrity window (on `2b6263d`)**;
- g1: window 1 running and recorded, a stranger also binds the port with `SO_REUSEADDR` on both: refused; g2: window 1 stopped, `server.json` valid but its process ended, a stranger on the port: refused;
  g3: `server.json` names a live, non-listening process of step 1's Python with the right start time, a stranger listens: refused. Each log shows `server.json` passing the strict field check before step 11 runs
  and that no reporter was running afterwards.
`5a29dd9` and `2b6263d` carry the same scripts (only the two documentation files differ between them); the installed commit changes only documents too.

**What differed without administrator rights (f2 and f2m against the elevated runs).** The owner of the folder step 1 makes and of the settings file step 7 writes is **the user** instead of the Administrators group;
the access-list check passed in both (it allows you, SYSTEM and Administrators by identifier). The process-table reads (steps 10, 11, 14, 15) and the port-owner lookup answered without error. Nothing else differed.

**The g1 finding.** When a stranger binds the same port after window 1 with `SO_REUSEADDR` on both sockets, the machine's listening-port table shows ONE row, and it is the later binder (the stranger), while a test
connection is answered by the first binder (window 1). Step 11 therefore refuses (the single row is neither the recorded process nor its child). That is the safe direction; it also means step 11 refuses a working
window 1 until the other program is gone, which the runbook now says.

**Logs.** All eleven logs were searched in full, case-insensitively, for the Windows user name (from the environment and from the operating system, both non-empty), the typed name and home parts, a user-profile path,
any 48-character hexadecimal value (the shape of the identity value, which no log prints) and, in the new logs, the real scratch path (written as `<scratch>`): no finding.
