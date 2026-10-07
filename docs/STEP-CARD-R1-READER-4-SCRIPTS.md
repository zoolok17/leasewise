# Step record - Card R1-READER-4-scripts: each runbook step also ships as a script

Work item `card-r1-reader-4-scripts`. Branch `card-r1-reader-4-scripts` from main `d33ed6d554ca3a1642ed7651231a4027fc302e1c`. **No product code changed; no schema change; no runbook block changed.**
Nothing real was touched in the rehearsals: a pretend gateway only, no agenttalk program, no ledger, no provider, no key.

## In plain words

The live trial of the money reader **passed** on the operator's desktop on 2026-10-07: all 12 steps, no closed word, and Leasewise's settled total equal to the gateway's on all four checks.

One thing went wrong on the way. Pasting a long, multi-line block into a PowerShell window was not reliable for the operator. The liaison therefore turned every block into a small script, one per
step, and the trial was run from those scripts. This card keeps them in the repository (`docs/trial-r1-reader-4/`), says so in the runbook, and adds a test so that a script and its runbook block
cannot drift apart without somebody noticing. It also adds the sentence the trial showed was missing: pressing Ctrl-C in the server window prints a few lines ending in `KeyboardInterrupt`; that is
normal and means the server has stopped.

## What changed

1. **The scripts** (copied from the liaison's folder; ASCII, LF in the index): `trial-common.ps1` (the shared start), one script per step (`trial-01-setup.ps1` ... `trial-12-rollback.ps1`, two for
   step 6 and two for step 9), `trial-help-status.ps1` for "If something goes wrong", and a new `trial-00-check.ps1` (fix round 1).
2. **The runbook, text only, no block changed:** the status line; the part "Two ways to run each step"; one "Script form:" line under every step heading and the two help sections; the Ctrl-C sentence in
   step 11; and the step-1 and step-6 prose about the default folder.
3. **The drift test** `tests/test_trial_scripts.py` (unittest, collected by `discover`).
4. **This record.**

## Fix round 1 (tk-46654bd94cb7; Codex's read tk-d3d3037dfd76)

- **(a) The shared start no longer touches a linked trial folder.** Before this round `trial-12-rollback.ps1` ran the shared start, which read `names.json` through the trial folder and entered the source
  folder before the block's own link check. Now the shared start, for every step, first looks at the default trial folder and at `names.json` WITHOUT entering them; if either is a link (a junction or a
  symbolic link) it stops with "... is a link. Nothing was changed. Tell the lead." The step-12 script also tells the shared start to stay out of the source folder (`-KeepOutOfSource`), so it enters nothing
  inside the trial folder before the block's own checks.
- **(a) The drift test now compares each script's complete executable body with its block** (see below), and catches the five mutations the first version missed.
- **(a) The runbook leads with the host that was rehearsed:** PowerShell 7 (`pwsh -NoProfile -File '<script>'`); Windows PowerShell 5.1 was not rehearsed with the scripts (it refused scripts on our machine);
  the machine's policy decides and is never changed.
- **(b) The default trial folder only, in both forms.** The runbook's own step-6 blocks (frozen) read `%LOCALAPPDATA%\leasewise-trial\names.json`, so another folder never worked in either form. The text now
  says so, says that this replaces the block's own "choose another name" advice (the block cannot change in this card), and tells the operator to roll an earlier trial's folder back with step 12 or tell the
  lead. The shared start also stops, with a plain message, if `names.json` names a different folder.
- **(b) One form for the whole run.** Mixing is not promised: a pasted block can need variables that only the pasted blocks before it leave in the window (step 8 needs `$Gateway`). The runbook says to choose
  the form before step 1, to check with `trial-00-check.ps1` (it prints the PowerShell version and `scripts run here`, nothing else), and, if a script is refused part-way, to stop and tell the lead.
- **(c) Notes from Codex.** The shared start is loaded by every script except the two step-6 window scripts, which load `names.json` themselves: said. "Nothing is carried over" means variables only; the files
  the steps leave on purpose (source, store, reader state, `names.json`) stay: said. Step 8's status refresh is listed below. The raw-LF requirement of the test and its claim that CRLF is normalised now agree:
  **the test normalises line endings** (a Windows checkout may hold CRLF) and checks that every script is pure ASCII; the repository's LF is checked at commit time (`git ls-files --eol`).
- **Promises of the first version that were not true, and what was done:** "each script finds `trial-common.ps1`" now names the step-6 exception; "nothing is carried over" now says variables only.

## Deliberate differences between a script and its runbook block

Everything else must be identical, line by line, in order, internal braces included. The test names each of these and fails on anything else:

1. the banner line (`'== Step N: ...'`) and the dot-source line at the top of a script (step 12's carries `-KeepOutOfSource`);
2. the wrapper of a pasted block (`. {`, the two `$ErrorActionPreference` lines and the final `}`): a script file needs none of it (the shared start sets the preference once);
3. step 1: the trailing explanation comments of six lines of the names block are dropped (the code on each line must be identical), and the helper definitions live in `trial-common.ps1` (checked there,
   whole and in order);
4. step 2's second block (`Set-Location $Src`): done by the shared start, with the runbook's own target (checked);
5. the two step-6 window scripts: their own stop line, then the shared entry guard (`trial-guard.ps1`, the same file the shared start uses), then the bytecode setting (they do not use the shared start);
6. **step 8: it starts with step 7's two status lines, exactly** (the pasted block used `$Gateway` left over from step 7's window; a script has its own process);
7. "If an adopt is refused as too old": its block is step 9a's block without the open-items part (the test maps one to the other).

## Fix round 2 (tk-c7bab31f9a96; Codex's delta tk-15f90afeb9d1 at 8756998d)

- **The two step-6 scripts now pass the same entry checks.** The link check, the load of `names.json` and the default-folder check moved into ONE file, `trial-guard.ps1`. The shared start
  (`trial-common.ps1`) and both step-6 scripts dot-source it, so the check cannot drift between copies. The unchanged step-6 block body follows the guard. Before this round the two window scripts
  read `names.json` through a linked folder, entered the saved source folder and ran the saved program with no check at all.
- **The drift test now pins the regions it used to exempt.** `trial-common.ps1` and `trial-guard.ps1` must equal approved copies kept in the test, character for character (line endings
  normalised), so any change fails until the test is updated on purpose. A banner line is accepted only when the whole line is one single-quoted literal (no `;`, nothing after it).
  Negative tests, each also run on CRLF copies: an executable line appended to the shared start; the `-not $KeepOutOfSource -and` part removed; a banner with a second command; the guard removed from each
  step-6 script; the link check removed from the guard; the link check moved after `names.json` is read.
- **Line endings in the mutation tests:** the scripts are normalised to LF when read, the mutation patterns are LF, and one test runs the real files and every mutation on CRLF-converted copies
  (in memory) and still reports each one.
- **Runbook prose (item b):** after the step-1 block the page said "choose another name"; it now says what the rest of the page says: an earlier trial's folder is still there, roll it back with step 12 or
  tell the lead, do not choose another name. **The recorded residual:** the frozen step-1 block still prints "Choose a new name for `$Trial`" when the folder exists; the page says in two places that this
  advice is replaced, because the block cannot change in this card.

## The drift test and what it guards

- each script's complete executable body equals its block (blank lines and full-line comments aside; here-string lines always count), apart from the differences above;
- the shared start and the entry guard are PINNED to approved copies (fix round 2); besides that, the shared start: the stop line first, step 1's helper definitions whole and in order, the runbook's `Set-Location` target, the guard dot-sourced; the guard: the link check before `names.json` is read, the default-folder check;
- `trial-00-check.ps1` only prints the version and `scripts run here`; `trial-01-setup.ps1` holds the placeholders; the runbook names every script and every named script exists; every script is pure ASCII;
- one test per mutation, each feeding a mutated text to the real checker: an added executable line in step 12; a placeholder kept but overridden later; the shared start's stop line removed; an internal
  closing brace removed (step 9b); the runbook's `Set-Location` target changed; a runbook character changed; a script line removed; an unnamed extra script; a placeholder replaced by a value; step 8's
  status refresh changed; the link check moved after `names.json` is read;
- PowerShell's own parser accepts every script (run when `pwsh` is on the path; it parses, it never runs a script; the test is skipped with a stated reason otherwise), and rejects the script with a
  removed brace.

**How the new tests were shown to bite.** The first version of the test (commit `e9480ae3`) was run against the five mutations Codex found: it reported **no drift for any of them** (all six of its tests
passed). The new checker reports each one (the mutation tests assert that). Before the fix, the step-12 link probe below also read `names.json` and entered `src` through a linked trial folder.

## The rehearsals

Against the pretend gateway, with PowerShell 7 (`pwsh -NoProfile -File`), each script in its OWN new process, the step-6 scripts started with `Start-Process` and ended by exact process ids (and the
program each had started, found by its exact parent id). **Windows PowerShell 5.1 was not run:** on the development machine it refuses every `.ps1` file, and the policy was not changed.

- **The whole run, again, in 108 seconds, every step exit code 0:** step 0 printed `PowerShell 7.6.6` and `scripts run here`; step 3 `3.13.7` and `1 True 2`; step 7 `state=unadopted latest report
  number=1`; step 8 `price equal: True   cap equal: True`; step 9 `state=unadopted  blockers=[]  open items=0` then `adopting report number 1, 12 seconds old` and `{"adoption_id":1}`; step 10 `PASSED`
  twice (1330 = 1330, then 1780 = 1780); step 11 nothing; step 12 `False`, and the rehearsal folder was gone.
- **Step 12 with the trial folder as a junction** (in a scratch folder, to a sibling folder holding `names.json`, a source folder and a sentinel file; deletion stubbed): it stopped with "... is a link.
  Nothing was changed. Tell the lead."; **no file was read and nothing was entered** (the reads and the folder changes were recorded and both lists were empty); the sentinel and the target's
  `names.json` were intact. The first version of the scripts, on the same probe, read `names.json` and entered `src`. The junction was removed on its own, not recursively, afterwards.
- **A `names.json` that names another folder:** the step-3 script stopped with exit code 1 and "names.json names the trial folder <that folder>, but only <the default> works with these steps. Nothing was
  changed. Tell the lead."
- Not probed: a `names.json` that is itself a link (the machine cannot make a file link without a privilege); the check is the same one as for the folder and is covered by the text test.

## Gates

See the reply (the full suite in chunks, equal to `discover`; ruff and bandit counts; every runbook block byte-identical to `main` `d33ed6d`).
