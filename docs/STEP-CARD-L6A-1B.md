# Step record - Card L6a-1b: command receipt sources with real containment

Work item `card-l6a-1b`. Branch `card-l6a-1b`, stacked on `card-l6a-2` (head `1d1bb0fede003c9fc49b346a924eb67d57de6b20`)
so that `config.py` does not conflict. Design: `C:\lw\records\DESIGN-L6A-1B.md` (SHA-256 `4af2e8fa8c33...04d41`), with the
lead's answers and corrections C1 to C4. Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only. Audience: the
lead and the reviewers.

## Step 1 of 3: the settings and the pure option check (no process is started)

(Step 2, the runner, follows below; the path rules of step 1 were tightened in step 2, see there.)

Status when step 1 was written (historical): steps 2 (the Windows runner) and 3 (wiring into the reporter and the status)
were not yet built. All three steps are built and merged; see the sections below.

### In plain words

The reporter will one day run agenttalk's receipt command and read what it prints. Before it may run anything, it has to
be sure the operator's settings are sane. This step builds only that: the reporter now checks the `command`, its options,
its time limit and its environment when it starts, and every mistake is refused with one fixed sentence that never repeats
what the file said. **Nothing is run (in this step; step 3 changes this).** Even a perfect setting is still refused with today's sentence ("Command receipt
sources are not available in this version; use a fixture file."), so nobody can switch the feature on by accident before the
safe runner exists.

Two small tools were built for the later steps and are tested on their own:

- a check for the options the reporter adds itself (`--after`, `--limit`, `--json`): the operator's command may not name them,
  in any spelling an option parser would accept;
- a function that writes the command line the way Windows programs read it back, identical to the standard library's.

### What was built

- `quota_balancer/reporter/cmdline.py` (new, pure): `names_reserved_option` and `quote_command`. It does not import `subprocess`.
- `quota_balancer/reporter/config.py`: `[gateway_receipts]` keys `command`, `timeout_seconds` and `env` (design "Configuration");
  `fixture` is now optional, with exactly one of `fixture` and `command`. The checks run in this order, each with its own
  sentence: exactly one source; `timeout_seconds` or `env` beside a fixture; not Windows; the shape (1 to 16 texts, each 1 to
  1024 characters); an absolute path with a drive; not a network path (anything starting `\\` or `//`, including
  `\\?\UNC\`); an `.exe` (C2); no double quote or control character; no reserved option; `timeout_seconds`; `env`. Only then
  the old sentence.
- The old test for a command block was rewritten (a valid block still gets the old sentence); the fixture tests are unchanged.

### The rules (C1, C2)

- **Reserved options (C1).** Only an argument that starts with `--` is looked at. Its name is the text after `--` up to the
  first `=`. It is refused when the name is empty (exactly `--`, or `--=x`) or is a prefix of `after`, `limit` or `json`. A
  plain word (`json`), a single dash (`-m`, `-a`) and a longer word that merely starts with a reserved one (`--afterx`,
  `--limits`) pass.
- **The program is an `.exe` (C2).** Windows runs a `.bat` or `.cmd` through `cmd.exe`, which splits arguments by other rules, so
  our quoting would no longer be safe.

### Exact sentences

| Case | Sentence |
|---|---|
| both or neither source | `gateway_receipts: name exactly one of fixture or command` |
| not Windows | `Command receipt sources are only available on Windows in this version; use a fixture file.` |
| shape | `gateway_receipts: the command must be a list of 1 to 16 texts` |
| not absolute | `gateway_receipts: the command must start with an absolute path` |
| network path | `gateway_receipts: the command may not start from a network or device path` |
| not an `.exe` | `gateway_receipts: the command must start with an .exe file` |
| quote or control character | `gateway_receipts: the command has a character it may not have` |
| reserved option | `gateway_receipts: the command may not name --after, --limit or --json in any spelling; the reporter adds them` |
| `timeout_seconds` | `gateway_receipts: timeout_seconds must be a whole number from 1 to 60` |
| with a fixture | `gateway_receipts: timeout_seconds and env belong to a command source` |
| `env` | `gateway_receipts: env must be a table of at most 8 texts`, `gateway_receipts: an env name is not valid`, `gateway_receipts: an env value has a character it may not have` |
| everything fine | `Command receipt sources are not available in this version; use a fixture file.` (until step 3) |

### Tests

`test_card7l6a1b_config.py` (new; the count is in the gates) and `test_card7l6a1_config.py` (two tests rewritten). Red first:
against the old `config.py` the new file fails (17 errors and failures of 26 at that moment). They cover every sentence, the
non-Windows refusal (the platform check is patched), every spelling in the lead's list plus every prefix of every reserved
word with and without a value, accepted near misses, 50 awkward quoting inputs against `subprocess.list2cmdline` (and a
small independent reader that splits the line back), and that no refusal repeats text from the file.

### Limits

- Nothing is run, so nothing about containment, the deadline or the output is proven here (steps 2 and 3).
- A drive-relative path (`C:x.exe`) and a path without a drive are "not absolute". A `\\?\C:\...` path of a local drive is
  refused as a network path (the sentence is a little too narrow for it; the effect is fail closed).
- `timeout_seconds` and `env` are checked but stored nowhere yet.

## Step 2 of 3: the contained Windows runner (built, not wired in)

Status: **built**. The reporter did not call the runner in this step; step 3 (below) wires it in.

### In plain words

The runner is the one piece that runs another program on purpose, so it is the piece that must never leave anything
behind. This step builds it and proves it on real processes, and also revises the design after Codex's read (the revised
design is `C:\lw\records\DESIGN-L6A-1B.md`, "Revision 1").

What it promises, and what each promise rests on:

- **The program starts already inside its protective job.** It is created frozen and the job is named when it is created,
  so there is no moment in which it is outside. The runner then checks that it really is inside, and only then lets it
  run. If the reporter dies at the worst instant (right after creating the frozen program), Windows closes the reporter's
  job handle and ends the program. A test does exactly that.
- **One clock, no waiting on anything that can hang.** Output is read by looking at the pipe without blocking, never by a
  thread. A program that never ends, one that never stops writing, a helper that holds the output open, and a flood on its
  error output all end at the deadline or sooner.
- **The end is confirmed.** After the job is ended, the runner asks the job how many processes it still holds and waits
  for zero. If it cannot confirm that, the read says so and is **never a success**, and its output is not used.
- **Other safeguards.** The program gets standard input that ends at once, its error output is thrown away, only two
  handles are passed to it (nothing else the reporter holds can leak), a small environment, and the options in the right
  order.

The honest notes: the clock is **cooperative** (it holds while the Windows calls return normally; creating the process,
the setup before the clock starts and a possible wait inside `PeekNamedPipe` are outside it, and the reporter must be the
only reader of the pipe); the shortened-option guard is lexical, so the real command's own option parser must still be
checked before live use; and this is containment of lifetime, not a sandbox against the operator's own command.

### What was built

- `quota_balancer/reporter/runner.py` (new). `run(command, after, limit, *, timeout_seconds, max_bytes, env, stop, clock,
  sleep, win)` returns a `Result` (a closed word, the bytes only when the word is `ok`, and whether the end of the job was
  confirmed). One small class, `_Win`, wraps every Windows call, each declared with its types (a handle is never cut to 32
  bits), so a test can make any single call fail. No `subprocess` in the package. `environment_block` builds the program's
  environment (pure).
- The order: job (kill-on-close, at most 32 processes, ends a crashing program at once) -> pipe (the read end private) ->
  `NUL` -> attribute list with the handle list and the **job list** -> create the process frozen -> close our copies of the
  write end and `NUL` -> `IsProcessInJob` must say yes -> `ResumeThread` (the earlier count must be exactly 1) -> read ->
  end the job and confirm -> free the list -> close every handle, the job last.
- If the job list is refused or unsupported, nothing is created: there is **no fallback to create-then-assign**.
- `config.py` (the F6 path rules): `/` is read as `\\`; every UNC and device form is refused with `gateway_receipts: the
  command may not start from a network or device path`; only a fully qualified `X:\...` is accepted (`C:foo` and `\foo` are
  "not absolute").

### Words the runner answers

`ok`; read later: `cannot_run`, `cannot_contain`, `timeout`, `interrupted`, `exit_code`, `leftover_process`, `read_failed`
(new in the build: looking at the pipe or the program failed), `cleanup_incomplete`; refused: `too_large`. Only `ok` carries bytes.

### Tests (`test_card7l6a1b_runner.py`, 45; `test_card7l6a1b_config.py` extended)

Real processes only: small scripts that the tests write, run by the base Python (never agenttalk, never a gateway).
Liveness is checked through a handle the test keeps while the process is alive; process milestones use files the programs
write (never fixed sleeps as proof); real-time bounds carry a two-second scheduling margin. The nested-job test runs in a
helper process (C3), and the crash tests end their helper by its own process handle (C4).

| What | Where |
|---|---|
| normal page, options last and awkward arguments unchanged, working directory, standard input ends, empty output, non-zero exit, error-output flood, exactly `max_bytes` and one more, a never-ending writer | `Normal` |
| a helper started in the program's first instant is in the job and ends with it; the program is frozen and already inside its job until it is resumed; the job is named in the creation attributes and nothing is assigned afterwards; a child's breakaway attempt is refused; an unrelated inheritable handle does not reach the program | `Containment` |
| never exits; a helper holding the output after exit (leftover, not a hang); a finite helper holding the pipe (the first attempt's 8.2 s case); the leftover grace never passes the deadline; stop; an error after the resume | `Deadlines` |
| every setup step failing (job, limits, pipe, private read end, `NUL`, attribute list, each attribute, process creation with each kind of error, in-job check, resume): a closed word, the program never ran, no handle left open; no create-then-assign | `Setup` |
| failed termination, failed question, an allowance that runs out, a failure word that keeps its word, a signalled root with an outstanding child, a failure to end a process not proven in its job | `Ending` |
| the reporter dies right after creating the program; the reporter dies while the program and its helper run | `Crashes` |
| a reporter that is already inside a job | `Nested` |
| the environment block (case-insensitive merge, the operator wins, NUL refused) and what a real program sees | `Environment` |
| no `subprocess`, no `taskkill`, no `suppress`; every call declared with its types | `Source`, `Setup` |

How the tests were written: the runner did not exist, so the tests of the new module were written with it. Two mutations
were then tried to prove the tests bite: creating the program without freezing it fails 5 of the 5 containment tests; leaving
the job out of the creation attributes fails 31 of 45. The path rules in `config.py` were red first (the old sentence and
the old forward-slash acceptance fail the new cases).

### Limits

- Windows only; other systems refuse a `command` at start-up (step 1).
- The deadline is cooperative (see the notes above); the receipt-reading bound per cycle is
  `max(timeout_seconds, 30) + timeout_seconds + 2 s`, about 52 s at the default (step 3 enforces and tests it).
- A helper that keeps stdout open beyond the post-exit grace causes `leftover_process`. Helpers that do not hold stdout are ended
  during job cleanup, and a successful page may still be accepted (a test shows this). Configure a program that waits for the
  work needed to produce its page.
- Helpers started through another Windows service are outside any job. A drive letter mapped to remote storage is not detected.
- No memory or processor limits in the job, only a limit on the number of processes (32).

## Step 3 of 3: the runner is wired into the reporter (status, cycle budget, privacy)

Status: **built**. A correctly configured `command` now reads real receipt pages. Live use still waits for two things:
agenttalk's real receipt command, and the lead's check of how that command reads its options (the compatibility requirement).

### In plain words

The runner of step 2 is connected to the reporter. A `command` on Windows is no longer refused at start-up (every other
refusal stays), and each cycle runs it to read a page: the page goes through exactly the same strict rules as a fixture file,
it is captured and released the same way, the cursor moves, and the next read asks for the receipts after the new cursor.

Every failure is a plain word in `status` (it could not start, a protective step failed, it was too slow, it was stopped, it
exited with an error, a helper held the output, looking at it failed) and then **nothing is captured and the cursor does not
move**. The advice in `status` follows the failure that is true now: it goes away when a read succeeds, instead of
staying because of an old count.

Reading receipts cannot hold up the sending of other reports for long: a cycle has a time budget for receipt reading,
checked before each read starts, and when it is spent the cycle stops reading and goes on to send.

### What was built

- `config.py`: `command`, `timeout_seconds` and `env` are stored on the source (the program path keeps backslashes); the old
  "not available" sentence is gone; the "only on Windows" sentence stays.
- `receipts.py`: `read_page(source, after_seq, stop)` runs the contained runner for a command source and hands its bytes
  **straight** to `parse_page(raw, after_seq, source.page_limit)` (never the fixture-only whole-ledger format). `ok` is a page;
  `too_large` is a refused page; every other word is read later with that word. `ReadResult.cleanup_confirmed` says whether the
  end of the job was confirmed. `LATER_REASONS` gains the command words.
- `run.py` (`_take_pages`): the cycle's receipt budget `max(timeout_seconds, 30)` seconds on a monotonic clock, checked
  before each read; the reporter's stop event is handed to the read; after each command read the end of the job is noted.
- `state.py`: the current failure word and the current end-of-job state are kept as notes (replaced by the next read), and
  the unconfirmed ends are counted.
- `status.py` (additive, `status_version` stays 3): `source_kind`, `last_read_reason`, `cleanup_incomplete` (`count` and
  `current`), the new read-later keys, the advice sentences, and a text line for the source and for the unconfirmed ends
  ("termination requested but not confirmed"). A snapshot of an older reporter is read with defaults.

### Tests (`test_card7l6a1b_wired.py`, 23; the older config, status and adapter tests were adjusted)

Fake commands are small scripts the tests write, run by the base Python (never agenttalk, never a gateway).

| What | Where |
|---|---|
| a page is captured and released, the cursor moves, the second cycle asks `--after` the new cursor, our three options are last and the configured arguments arrive unchanged; the result equals a fixture's row for row; a limit of 2 reads in pieces | `EndToEnd` |
| each failure word reaches `status` with nothing captured and the cursor unmoved: `exit_code`, `not_json`, `cannot_run`, `cannot_contain`, `timeout`, `leftover_process`, `read_failed`, a refused `too_large`, `interrupted` (a stop mid-read; the program is gone); an unconfirmed end is counted, never a success, and clears; the advice follows the current failure; the send passes are independent of a failing read | `Failures` |
| the cycle budget with a virtual clock: a read that starts just before the budget ends runs and no further read starts; the send passes still run; the budget is at least 30 and otherwise the time limit; the next cycle goes on from the cursor | `Budget` |
| a loud string in the output, the error output, the path, an argument and the environment appears nowhere in status, notes, counters, tables or the source's repr | `Privacy` |
| a status snapshot of an older reporter is read with defaults | `OldSnapshot` |

Red first: against the previous source, all 23 fail. Two mutations were tried to prove the tests bite: removing the budget
check fails 4 of the 4 budget tests, and not handing the stop event to the read makes the stop test run to the 30 s deadline.

### Honest notes

- The cycle bound, with normally returning Windows calls, is `max(t, 30) + t + 2` seconds for receipt reading (about 52 s at
  the default, about 122 s at 60); it bounds receipt reading, not the whole cycle.
- Live use needs the real receipt command and a check that its parser reads `--after N --limit M --json` as options.
- Limits as in step 2 (Windows only, mapped drives, the cooperative deadline, helpers started through another service).

## Fix round 1 (Codex's read of the runner)

Codex read the runner. The containment holds (the program starts inside its job) and the HTTPS fix works. Two runner details
were wrong. Neither changes `run(...)` or `Result`, so step 3 stands as built.

### In plain words

- **F1, two small blocks of memory freed too early.** Windows is told to keep using two small arrays (the list of handles the
  program inherits, and the job it is created in) until the attribute list is deleted. Python could free them as soon as the
  setup function returned. It worked by luck. Now both arrays belong to the same owner as the attribute list, are stored before
  either is handed over, and are released only after the list has been deleted.
- **F2, the half-second grace was not enforced while data flowed.** When the main program had exited but a helper kept writing
  output without a pause, the loop always found more data and never looked at the grace, so the helper's output could be
  accepted as the page (Codex measured a page accepted 2.6 s after the main program ended). Now, on every pass, including the
  pass that has just read data, the loop asks (with a wait of zero time) whether the main program has exited. Once it has, there
  is **one** grace deadline, `min(overall deadline, exit + 0.5 s)`, enforced while data is being drained as well as while idle. If
  the output pipe is still open after the grace, the answer is `leftover_process`, with no bytes, and the usual job cleanup runs.

### Tests (in `test_card7l6a1b_runner.py`, 6 new)

| What | Where |
|---|---|
| at the moment the attribute list is deleted both arrays are alive: on success, when the first attribute update fails, and when the second fails (the owner held both before the first update) | `BufferLifetime` |
| a helper keeps writing valid page bytes after the main program exited (the main program's exit is seen through a handle the test keeps; a scheduling delay between peek and read keeps data always waiting): `leftover_process`, within the grace plus a two-second margin, no bytes, the helper gone with the job | `Grace` |
| a normal program that writes a 900 KB page and exits is still `ok`; the quiet holder and the deadline cap of the grace still hold | `Grace` |

Red first: against the previous runner, 4 of the 6 fail (the three lifetime tests, which need the owner to hold the arrays, and
the helper test, which got `ok` with 2 MB of output). The handshakes are files the programs write and handles the test keeps,
not sleeps.

## Follow-up: wording, the test tool, and one precondition

In plain words: no behaviour changed. Three small things were put right after Codex's last read.

- **The reporter must run as the same operating-system user as the gateway.** The receipt command reads that user's own
  ledger and credential file. The operator's `env` table will need that user's application-data variable. The names and
  paths of the receipt command's own settings are not written here: they are provisional until agenttalk's change for them is
  merged. The README ("A command source") says the same.
- **The test tool no longer guesses (N2).** In `test_card7l6a1b_runner.py` a "ready" path that is also a process marker now
  counts as ready only once the marker has been read and its process handle is retained; plain existence counts only for paths
  that name no process. A new test makes the opening of the marker fail on purpose and shows that neither the readiness nor its
  callback fires until the handle is held (red against the old rule: 1 of 2 new tests fails). This may also be the cause of
  the one flaky run seen earlier in `test_an_unrelated_inheritable_handle_does_not_reach_the_program`; that is not proven.
- **The deadline answer (N1).** The real-clock test in `test_card7l6a1b_https.py` accepts exactly one of `store_busy` or
  `coordinator_unreachable`, because a transport failure just before the deadline is the second one (with the normal
  back-off); both keep the saved request. A new test with a controlled clock pins the exact deadline answer, `store_busy`.
- **Wording (N3).** The runner, its test, `receipts.py` and the README no longer say the reporter does not call the runner, and
  the launcher limit now says what happens and what to do.

## Follow-up 2: the launcher sentence, and a test helper that could not open a process

In plain words: no product behaviour changed.

- **The launcher sentence** (README, and the limit in step 3 above) said a program that exits before its helper always gives
  `leftover_process` or `exit_code`. It is narrower: only a helper that keeps the output open past the half-second grace does. A
  helper that does not hold the output is ended during cleanup, and the page can still be accepted. The wording now says so.
- **The test helper `Watch`** (`test_card7l6a1b_runner.py`) stored nothing when Windows refused to open a process, and later
  answered "gone". It now knows it has no handle: `opened` is False and `gone()` refuses to answer (an assertion with a clear
  message). `Probe` keeps a watch only when it has a real handle, so it never reports ready, or runs its callback, without one; it
  tries again on the next look, and a process that really never gives a handle ends the test at its own time-out. A new test makes
  `OpenProcess` return 0: neither readiness nor the callback fires, and `gone()` never says True. It fails against the old helper.
