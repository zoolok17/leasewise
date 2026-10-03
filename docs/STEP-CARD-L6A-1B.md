# Step record - Card L6a-1b: command receipt sources with real containment

Work item `card-l6a-1b`. Branch `card-l6a-1b`, stacked on `card-l6a-2` (head `1d1bb0fede003c9fc49b346a924eb67d57de6b20`)
so that `config.py` does not conflict. Design: `C:\lw\records\DESIGN-L6A-1B.md` (SHA-256 `4af2e8fa8c33...04d41`), with the
lead's answers and corrections C1 to C4. Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only. Audience: the
lead and the reviewers.

## Step 1 of 3: the settings and the pure option check (no process is started)

(Step 2, the runner, follows below; the path rules of step 1 were tightened in step 2, see there.)

Status: **built**. Steps 2 (the Windows runner) and 3 (wiring into the reporter and the status) are not built.

### In plain words

The reporter will one day run agenttalk's receipt command and read what it prints. Before it may run anything, it has to
be sure the operator's settings are sane. This step builds only that: the reporter now checks the `command`, its options,
its time limit and its environment when it starts, and every mistake is refused with one fixed sentence that never repeats
what the file said. **Nothing is run.** Even a perfect setting is still refused with today's sentence ("Command receipt
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

Status: **built**. The reporter does not call the runner yet: a valid `command` is still refused at start-up with today's
sentence. Step 3 wires it in (the cycle budget, the status words, the page rules on the command's bytes).

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
- A launcher that exits before the real program is `leftover_process` or `exit_code`, and the work is ended.
- Helpers started through another Windows service are outside any job. A drive letter mapped to remote storage is not detected.
- No memory or processor limits in the job, only a limit on the number of processes (32).
