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
