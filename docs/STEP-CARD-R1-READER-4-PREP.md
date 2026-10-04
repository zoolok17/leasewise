# Step record - Card R1-READER-4-prep: the live-trial runbook, rehearsed end to end

Work item `card-r1-reader-4-prep`. Branch `card-r1-reader-4-prep` from main `1e6c66570a46fa19401f57e95a89e807becc1132`. Plan: lead decision D-221 (and D-217 for the
reader's `account` and `interval_seconds`). **No product code changed; no schema change.** Nothing real was touched: no gateway, no agenttalk program, no ledger, no
provider, no key.

## In plain words

Step 4 of the money reader is the live trial on the operator's desktop. Before asking for the operator's go, the runbook for it was written and **run, command for
command, on this laptop against a pretend gateway**. The rehearsal installs Leasewise from a source archive (checked by its SHA-256), sets up the store, starts the two
windows, approves and adopts a report, checks "passed", stops, and rolls back. It also runs one drill: an adopt attempted after the newest report went stale. It
passed twice from a clean start, and after the rollback nothing was left behind.

## What was built

1. `docs/TRIAL-R1-READER-4.md`: the operator's runbook (how-to, plain words first, exact PowerShell, what each command prints, a table of the reader's closed words, and the corrected
   advice for `send_refused`: `resume` only when Leasewise's side was wrong and is fixed; `reset` when the reader's own configuration was wrong). The commit to install is the
   placeholder `<COMMIT>` until the lead fills it at the merge.
2. `tests/trial/` (file names do not start with `test_`, so `discover` does not collect them: the suite still counts 2572 tests, as on main):
   - `pretend_gateway.py`: a pretend gateway program (a Python script run by `python.exe`). It answers `gateway report --json` and `gateway receipts --after N --limit M --json` in agenttalk's documented
     shapes (report version 1, receipt envelope 1, rows with `charge_period`), from a made-up ledger file; `advance` adds one receipt of 450 so a second report differs; every call is noted
     in a log so the rehearsal can count them. Made-up values only; it is not named like agenttalk.
   - `rehearse.ps1`: the rehearsal. It **reads the runbook and runs the PowerShell blocks that carry a `<!-- rehearse: ... -->` tag, exactly as written**, in one session. It differs from
     the operator in three places only: the block where the operator types the names of their machine (`replace`: pretend gateway, scratch folder), the two blocks run in separate windows
     (`window`: started as hidden background processes, ended by exact process id), and the pretend gateway is moved forward between steps.
3. This record.

## Result

Four runs, each from a clean start, all passed (`REHEARSAL PASSED`), under Windows PowerShell 5.1 (the desktop's default) with Python 3.13 from `C:\lw\venv`: two on the
tree before `main` moved (225 s and 228 s), and two again after `main` took in the 3b-tidy card (`d5078b5`), on the merged tree (228 s and 227 s). The tested trees are identical to this
commit's tree except for this record. Logs are kept in the seat's scratch folder (`rehearsal-final1`, `-final2`, `-merged1`, `-merged2`). The timings below are from the second run.

| Runbook step | What was shown | Time (second run) |
|---|---|---|
| 1 names, derived names, `names.ps1` | `True` | under 1 s |
| 2 archive checked with `Get-FileHash`, unpacked | names of the unpacked items | 4.1 s |
| 3 Python and gateway checks | `3.13.7`, then `1 True 2` | 0.2 s |
| 4 store: init, config-import, policy-ids | the gateway alias | 0.8 s |
| 5 identity value, private file, access list | one line, the user and `(F)` | 0.3 s |
| 6 start the server and the reader | two process ids | 8 s (4 s each, a pause for start-up) |
| 7 wait for the first report | `state=unadopted latest report number=1` | 5.4 s |
| 8 compare the fingerprints, approve | `True`, `True`, `{"approved":true}` | 0.6 s |
| 9 blockers, helper, adopt | `blockers=[]`, `{"adoption_id":1}`, exit 0 | 1.5 s in all |
| 10 passed | `usable`, settled 1330 = 1330, `PASSED` | 0.8 s |
| the gateway moves forward, then the passed check again | a new report; `usable`, settled 1780 = 1780 | about 60 s to the next report, 0.4 s |
| drill: reader stopped, report goes stale (waited 135 s), the stale view, the adopt | `state=stale blockers=[not_fresh]`; the adopt printed its fixed sentence `ADOPTION_REFUSED` and exit code 2 | 135 s wait, 1.7 s of commands |
| drill: reader restarted, next report, adopt again, passed | report number 3 adopted (`{"adoption_id":2}`), `usable`, settled 1780 = 1780 | about 60 s to the next report |
| reader status (closed words) | all counts 0, `halt` null | 0.4 s |
| 11 stop, 12 roll back | nothing left running; `False` | under 1 s |

Each command took under 10 seconds; the longest waits are the two waits for a new report (about a minute each, set by `interval_seconds`) and the 135-second wait of the drill.

## Evidence that nothing is left behind

The rehearsal gives the run its own application-data and temp folders under the scratch root (so a stray write cannot hide among other programs' files), and lists, before and after the
whole run: the top level of that fake `LOCALAPPDATA`, `APPDATA` and `TEMP`; the top level of the **real** `LOCALAPPDATA` (50 entries) and `APPDATA` (15); the real temp folder, filtered to
names Leasewise's test programs use (`quota-*`, `leasewise*`); and `git status` of the repository. Reason for each: those are the places Windows programs normally write to, the
repository is where a careless run would leave files, and the trial folder is the one place that is supposed to change. Result, both runs: **no new and no missing entry in any of them**
after the rollback (only the deleted trial folder differs), and the repository status stayed empty. Leasewise's temp file for each report is removed by Leasewise itself.

**The identity value:** it is made in memory, never printed. The rehearsal reads it back from the private file just before the rollback, only into memory, to look for it: it lives in
exactly two files, `reporter.toml` and the store `inventory.db` (where `identity-set` records it), and in **no file outside the trial folder** (the log, the window outputs, the
gateway's folder): 0 files, both runs. The log was searched for the value itself, not for a pattern.

## What had to change from D-221, and what the rehearsal found

- **Found: the reader asked the gateway for receipts every 5 seconds, not once per report.** The older seat-lease receipt pass runs once per reporter cycle, and the default cycle is
  5 seconds (measured: 13 receipt calls in one minute). The calls are read-only, but the plan said once per report. **Fix, in the runbook only:** `poll_seconds = 60` in the reporter
  configuration. Measured afterwards: at most 2 receipt calls in any minute, and the report command about once a minute (the runs' counts also include the runbook's own hand checks).
  Whether a code change is wanted (the older pass need not read at all when no seat is gateway-bound) is the lead's decision; none was made here.
- **Found: `status` shows neither a report's event id nor its settled total**, and `gateway-adopt` needs the event id. **Fix, in the runbook only:** a small read-only script the runbook writes
  (`newest-report.py`; it opens the store read-only) prints the newest report's id, number, settled total and age. A `status` field would remove the need; no code was changed.
- **Confirmed as planned:** one folder under `%LOCALAPPDATA%`; no `pip` (Leasewise runs from the extracted source; `PYTHONDONTWRITEBYTECODE` keeps the folder clean); a `git archive` zip checked
  with `Get-FileHash` before extracting; `serve` on `127.0.0.1` only; the fingerprints compare against `status`'s `report.policy_hashes` (what Leasewise holds); adopt within 120 seconds; the env
  table needs the user's application-data variable, named exactly **`LOCALAPPDATA`** (agenttalk's own path code reads it); the reader never opens the front-token file.
- **Added by earlier decisions, now in the runbook:** `account` in `[gateway_report]` (D-217: Leasewise only accepts a gateway's report from the owning machine and account), the inventory
  document that binds the gateway to that machine and account, `interval_seconds` left at its default of 60.
- **Windows PowerShell 5.1 care:** the blocks avoid `>` redirection (it writes UTF-16 in 5.1) and use `Out-File`/`Set-Content -Encoding ascii`; the configuration is built with literal TOML strings
  so Windows paths need no escaping; the identity value is made with the .NET random generator (works in 5.1 and 7).
- **A drill finding for the operator:** a refused adopt shows in red in PowerShell with extra lines about the command; the sentence inside is the message. The runbook says so.

## What was not rehearsed (so the live trial still has to show it)

The real agenttalk program and its two commands (only their documented shapes were rehearsed, from agenttalk's own sample, `921c82d4`), a real ledger with real receipts and open turns, the
user's front-token file, the agenttalk version check after the window, **Ctrl-C** (the rehearsal ended the two background processes by process id, which is harsher; the reader restarted cleanly after it),
and a gateway whose settled money moves while the checks run (the runbook says to compare when it has been quiet for a minute).

## Gates

Full suite in four chunks, two at a time: 952 + 390 + 616 + 598 = 2556, plus 16 for `test_card4_ops` through `discover -p` = 2572 = `discover`, all green; the kit is not collected.
Ruff (the operator's command) 74 findings, bandit 5 low, 18 medium, 1 high, same as main; no `nosec` added.
