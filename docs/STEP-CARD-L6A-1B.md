# Step record - Card L6a-1b: command receipt sources with real containment

Work item `card-l6a-1b`. Branch `card-l6a-1b`, stacked on `card-l6a-2` (head `1d1bb0fede003c9fc49b346a924eb67d57de6b20`)
so that `config.py` does not conflict. Design: `C:\lw\records\DESIGN-L6A-1B.md` (SHA-256 `4af2e8fa8c33...04d41`), with the
lead's answers and corrections C1 to C4. Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only. Audience: the
lead and the reviewers.

## Step 1 of 3: the settings and the pure option check (no process is started)

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
| network path | `gateway_receipts: the command may not start from a network path` |
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
