# Step record - Card R1-READER-4-scripts: each runbook step also ships as a script

Work item `card-r1-reader-4-scripts`. Branch `card-r1-reader-4-scripts` from main `d33ed6d554ca3a1642ed7651231a4027fc302e1c`. **No product code changed; no schema change.** Nothing real was
touched in the rehearsal: a pretend gateway only, no agenttalk program, no ledger, no provider, no key.

## In plain words

The live trial of the money reader **passed** on the operator's desktop on 2026-10-07: all 12 steps, no closed word, and Leasewise's settled total equal to the gateway's on all four checks.

One thing went wrong on the way. Pasting a long, multi-line block into a PowerShell window was not reliable for the operator. The liaison therefore turned every block into a small script, one per
step, and the trial was run from those scripts. This card keeps them in the repository (`docs/trial-r1-reader-4/`), says so in the runbook, and adds a test so that a script and its runbook block
cannot drift apart without somebody noticing. It also adds the one sentence the trial showed was missing: pressing Ctrl-C in the server window prints a few lines ending in `KeyboardInterrupt`;
that is normal and means the server has stopped.

## What changed

1. **The scripts** (16 files, copied unchanged from the liaison's folder; ASCII, LF in the index): `trial-common.ps1` (the shared start: stop at the first error, the UTF-8 writer, the two privacy
   helpers copied from step 1, the saved names, and the source folder as the working folder), one script per step (`trial-01-setup.ps1` ... `trial-12-rollback.ps1`, two for step 6 and two for
   step 9), and `trial-help-status.ps1` for "If something goes wrong". `trial-01-setup.ps1` holds the runbook's angle-bracket placeholders, never a value.
2. **The runbook, text only, no block changed:** the status line says it passed against the real gateway on 2026-10-07; a new part "Two ways to run each step" (paste the block, or run the
   step's script with `powershell -NoProfile -File` or `pwsh -NoProfile -File`; fill in the step-1 script first; each script has its own process, so only `names.json` is shared; the two step-6
   scripts run in two windows; if the machine's script policy refuses a script, paste the block and do not change the policy); one "Script form:" line under every step heading and under the two
   help sections; and the Ctrl-C sentence in step 11.
3. **The drift test** `tests/test_trial_scripts.py` (unittest, collected by `discover`, text only, runs no PowerShell, line endings normalised).
4. **This record.**

## Differences found between a script and its runbook block (reported, not fixed)

The scripts were compared with the runbook at `d33ed6d`. Apart from the pasted-block wrapper (`. {` and `}` and the two `$ErrorActionPreference` lines, which a script file does not need because
the shared start sets the preference once), three differences exist. The test states each one on purpose, so any other difference fails it:

- **Step 1, the names block:** the script keeps every value and placeholder but drops the trailing explanation comment of six lines (`$GatewayPre`, `$MachineAlias`, `$AccountAlias`,
  `$GatewayAlias`, `$BudgetMicroEur`, `$ShareMicroEur`). No effect on what runs.
- **Step 2, the second block (`Set-Location $Src`):** it has no script of its own. The shared start does it (`Set-Location -LiteralPath $Src`) whenever the source folder exists, which is after step 2.
- **"If an adopt is refused as too old":** the section's block prints one `state=... blockers=[...]` line. Its script (`trial-09a-blockers.ps1`, also the step-9 first block) prints the same line
  followed by the number of open items.

## The drift test and what it guards

- every one of the runbook's 18 PowerShell blocks appears, line by line and in order, in the script(s) for its step (step 1's helper definitions in `trial-common.ps1`), apart from the three
  differences above;
- every script is pure ASCII with LF line endings and starts with the dot-source of `trial-common.ps1` (the two step-6 window scripts load `names.json` themselves instead, as the blocks do);
- `trial-01-setup.ps1` still holds the placeholders, and no `names.json` is shipped;
- the runbook names every script, and every script it names exists; the status line and the Ctrl-C sentence are present.

**It bites.** With one character changed in one runbook block (step 8's `-ne` in the fingerprint comparison made `-eq`), `test_every_runbook_block_appears_unchanged_in_its_scripts` failed
(the step-8 block no longer appeared in `trial-08-approve.ps1`); after the revert it passed.

## The rehearsal of the script form

Against the pretend gateway (`tests/trial/pretend_gateway.py`), in a fresh scratch folder with its own fake application-data folder, each step's script in its OWN new process, the step-1 copy
filled in with the rehearsal's names, the two step-6 scripts started in the background with `Start-Process` and ended by their exact process ids (and the program each had started, found by its
exact parent id).

- **PowerShell 7 (`pwsh -NoProfile -File`): ran end to end in 108 seconds, every step exit code 0.** Step 3 printed `3.13.7`, then `1 True 2`. Step 7 printed `state=unadopted latest report number=1`.
  Step 8 printed `price equal: True   cap equal: True` and `{"approved":true}`. Step 9 printed `state=unadopted  blockers=[]  open items=0`, then `adopting report number 1, 12 seconds old` and
  `{"adoption_id":1}`. Step 10 printed `state=usable  adopted report number=1  month=2026-10` and `PASSED` (settled 1330 = 1330), and after the pretend gateway moved forward a second time
  `PASSED` (settled 1780 = 1780). `trial-help-status.ps1` printed the reader's closed words, all zero. Step 11 printed nothing (no Leasewise process left). **Step 12 removed the rehearsal folder**
  (it printed `False`; the folder did not exist afterwards).
- **Windows PowerShell 5.1 (`powershell.exe -NoProfile -File`): NOT run, and this is a limit of this seat, not of the scripts.** On this machine `powershell.exe` refuses every `.ps1` file
  ("running scripts is disabled on this system"), including the rehearsal driver. The policy was not changed, as the rule says. The scripts therefore remain unproven in 5.1 by this rehearsal;
  the runbook blocks themselves were rehearsed in 5.1 by the earlier kit (`tests/trial/rehearse.ps1`), and the real trial ran the scripts under `pwsh`. The runbook says that if the policy refuses
  a script, the operator pastes the block instead.

## Gates

See the reply (the full suite in chunks, equal to `discover`; ruff and bandit counts).
