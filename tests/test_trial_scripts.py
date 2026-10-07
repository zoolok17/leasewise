"""The trial runbook's scripts must not drift from the runbook.

Every step of ``docs/TRIAL-R1-READER-4.md`` also ships as a small script in ``docs/trial-r1-reader-4/``. The real trial ran those
scripts, so a later edit of a runbook block that is not made in the script too (or the other way round) would send the operator
a different command from the one that was proven.

The check compares each script's COMPLETE executable body with its runbook block: only the pasted block's outer wrapper is
stripped (``. {``, the two ``$ErrorActionPreference`` lines, the final ``}``); internal braces, order and every executable line
stay. Blank lines and full-line comments are ignored (a here-string's lines are data and are never ignored). The only
differences allowed are the named adaptations in ``check_drift``; anything else is reported.

The checks are text only. Line endings are normalised first (a Windows checkout may hold CRLF; the repository holds LF; the
raw bytes of the scripts must be ASCII). One more test parses every script with PowerShell's own parser, when ``pwsh`` is on
the path (it parses; it never runs a script).
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = ROOT / "docs" / "TRIAL-R1-READER-4.md"
SCRIPTS = ROOT / "docs" / "trial-r1-reader-4"
COMMON = "trial-common.ps1"
DOT_SOURCE = ". (Join-Path $PSScriptRoot 'trial-common.ps1')"
DOT_SOURCE_NO_SOURCE = DOT_SOURCE + " -KeepOutOfSource"
STOP = "$ErrorActionPreference = 'Stop'"
CONTINUE = "$ErrorActionPreference = 'Continue'"
BYTECODE = "$env:PYTHONDONTWRITEBYTECODE = '1'"
STEP_ONE_PLACEHOLDERS = ("<C:\\path\\to\\python.exe>", "<C:\\path\\to\\agenttalk.exe>", "<account alias>", "<gateway alias>",
                         "<budget in micro-euro>", "<share in micro-euro>", "<C:\\path\\to\\leasewise-COMMIT.zip>",
                         "<the SHA-256 the lead gave you>")

# The runbook's PowerShell blocks, in order.
(B_NAMES, B_FOLDER, B_UNPACK, B_SETLOC, B_CHECK, B_STORE, B_IDENTITY, B_SERVER, B_READER, B_WAIT, B_APPROVE, B_BLOCKERS,
 B_ADOPT, B_PASSED, B_STOPPED, B_ROLLBACK, B_TOOOLD, B_HELP) = range(18)

# Each script and the block that is its whole body (the bodies of step 1, step 8 and the window scripts are built in check_drift).
SIMPLE = {
    "trial-02-unpack.ps1": B_UNPACK, "trial-03-check.ps1": B_CHECK, "trial-04-store.ps1": B_STORE,
    "trial-05-identity.ps1": B_IDENTITY, "trial-07-wait.ps1": B_WAIT, "trial-09a-blockers.ps1": B_BLOCKERS,
    "trial-09b-adopt.ps1": B_ADOPT, "trial-10-check.ps1": B_PASSED, "trial-11-stopped.ps1": B_STOPPED,
    "trial-12-rollback.ps1": B_ROLLBACK, "trial-help-status.ps1": B_HELP,
}
WINDOWS = {"trial-06a-server.ps1": B_SERVER, "trial-06b-reader.ps1": B_READER}
ALL_SCRIPTS = (sorted(SIMPLE) + sorted(WINDOWS) +
               ["trial-00-check.ps1", "trial-01-setup.ps1", "trial-08-approve.ps1", COMMON])


def norm(text: str) -> str:
    return text.replace("\r\n", "\n")


def runbook_blocks(runbook: str):
    """The runbook's powershell code blocks, as lists of lines, in order."""
    blocks, current = [], None
    for line in norm(runbook).split("\n"):
        if current is None:
            if line.startswith("```powershell"):
                current = []
        elif line.startswith("```"):
            blocks.append(current)
            current = None
        else:
            current.append(line)
    return blocks


def exec_lines(lines):
    """The lines that matter: no blank lines, no full-line comments (a here-string's lines are data and always stay)."""
    kept, here = [], None
    for line in lines:
        line = line.rstrip()
        if here is not None:
            kept.append(line)
            if line.startswith(here):
                here = None
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        kept.append(line)
        if line.endswith('@"'):
            here = '"@'
        elif line.endswith("@'"):
            here = "'@"
    return kept


def unwrap(block):
    """A pasted block without its outer wrapper (and without its ``Continue`` line); everything inside stays."""
    body = list(block)
    if body and body[0] == ". {":
        body = body[1:]
    if body and body[-1] == "}":
        body = body[:-1]
    if body and body[-1] == CONTINUE:
        body = body[:-1]
    if body and body[0] == STOP:
        body = body[1:]
    return exec_lines(body)


def strip_trailing_comment(line):
    return re.sub(r"\s+#.*$", "", line).rstrip()


def script_body(text):
    """A script's executable lines, split into (dot-source line or None, banner line or None, the rest)."""
    lines = exec_lines(norm(text).split("\n"))
    dot = None
    if lines and lines[0].startswith(". (Join-Path $PSScriptRoot"):
        dot, lines = lines[0], lines[1:]
    banner = None
    if dot is not None and lines and re.fullmatch(r"'== .*'", lines[0]):
        banner, lines = lines[0], lines[1:]
    return dot, banner, lines


def _first_difference(want, got):
    for index in range(max(len(want), len(got))):
        a = want[index] if index < len(want) else "<nothing>"
        b = got[index] if index < len(got) else "<nothing>"
        if a != b:
            return f"line {index + 1}: the runbook says {a!r}, the script has {b!r}"
    return None


def check_drift(runbook: str, scripts: dict):
    """Everything that would make a script differ from its runbook block. Returns a list of problems (empty: no drift).

    The ONLY adaptations allowed, each stated here:
      * the banner line (``'== Step N: ...'``) and the dot-source line at the top of a script (step 12's carries ``-KeepOutOfSource``);
      * step 1: the dropped trailing comments of the names block (the code on each line must be identical), and the helper
        definitions, which live in trial-common.ps1 (checked there, in order);
      * step 2's ``Set-Location``: done by the shared start (its target must be the runbook's);
      * step 6's two window scripts: their own copy of the stop line and of the bytecode setting;
      * step 8: starts with step 7's two status lines (exactly);
      * "If an adopt is refused as too old": its block is step 9a's block without the open-items part (mapped to step 9a).
    """
    problems = []
    blocks = runbook_blocks(runbook)
    if len(blocks) != 18:
        return [f"the runbook has {len(blocks)} PowerShell blocks, this check expects 18"]
    for name in ALL_SCRIPTS:
        if name not in scripts:
            problems.append(f"{name}: the script is missing")
    if problems:
        return problems
    for name in sorted(set(scripts) - set(ALL_SCRIPTS)):
        problems.append(f"{name}: a script this check does not know")
    body = [unwrap(b) for b in blocks]
    parts = {name: script_body(text) for name, text in scripts.items()}

    def compare(name, want, allow_dot=DOT_SOURCE, banner_required=True):
        dot, banner, got = parts[name]
        if dot != allow_dot:
            problems.append(f"{name}: it must start with {allow_dot!r}, it starts with {dot!r}")
        if banner_required and banner is None:
            problems.append(f"{name}: the banner line is missing")
        diff = _first_difference(want, got)
        if diff:
            problems.append(f"{name}: {diff}")

    for name, index in SIMPLE.items():
        compare(name, body[index], DOT_SOURCE_NO_SOURCE if name == "trial-12-rollback.ps1" else DOT_SOURCE)

    # step 1: the names block (comments may be dropped, code may not), the folder block without the helpers
    folder = body[B_FOLDER]
    try:
        start = next(i for i, line in enumerate(folder) if line.startswith(BYTECODE))
        end = next(i for i, line in enumerate(folder) if line.startswith("if (Test-Path -LiteralPath $Trial)"))
    except StopIteration:
        return problems + ["step 1's folder block no longer has the shape this check expects"]
    shared, head, tail = folder[start:end], folder[:start], folder[end:]
    dot, banner, got = parts["trial-01-setup.ps1"]
    names_part = got[:len(body[B_NAMES])]
    for want, have in zip(body[B_NAMES], names_part, strict=False):   # the lengths are compared just below
        if have != want and have != strip_trailing_comment(want):
            problems.append(f"trial-01-setup.ps1: the names block differs: the runbook says {want!r}, the script has {have!r}")
    if len(names_part) != len(body[B_NAMES]):
        problems.append("trial-01-setup.ps1: the names block is incomplete")
    if dot != DOT_SOURCE or banner is None:
        problems.append("trial-01-setup.ps1: it must start with the shared start and a banner line")
    diff = _first_difference(head + tail, got[len(body[B_NAMES]):])
    if diff:
        problems.append(f"trial-01-setup.ps1: the folder block differs: {diff}")

    # the shared start: the stop line first, step 1's helper definitions in order, step 2's Set-Location target, link check first
    common_text = norm(scripts[COMMON])
    common = exec_lines(common_text.split("\n"))
    code = [line for line in common if not line.startswith("param(")]
    if not code or code[0] != STOP:
        problems.append(f"{COMMON}: its first executable line must be {STOP!r}")
    at = next((i for i in range(len(common)) if common[i:i + len(shared)] == shared), None)
    if at is None:
        problems.append(f"{COMMON}: step 1's helper definitions are not there, whole and in order")
    elif at > 0 and STOP not in common[:at]:
        problems.append(f"{COMMON}: the stop line must come before the helpers")
    target = [line for line in body[B_SETLOC] if line.startswith("Set-Location ")]
    if len(target) != 1:
        problems.append("step 2's second block no longer is one Set-Location line")
    else:
        wanted = "Set-Location -LiteralPath " + target[0][len("Set-Location "):].strip()
        if not any(wanted in line for line in common):
            problems.append(f"{COMMON}: it must do {wanted!r} (the runbook's Set-Location target)")
    if "param([switch]$KeepOutOfSource)" not in common_text:
        problems.append(f"{COMMON}: the -KeepOutOfSource switch is missing")
    if "ReparsePoint" not in common_text or common_text.find("ReparsePoint") > common_text.find("Get-Content"):
        problems.append(f"{COMMON}: the link check must come before names.json is read")
    if "$N.Trial -ne $DefaultTrial" not in common_text:
        problems.append(f"{COMMON}: the default-folder check is missing")

    # step 8: step 7's two status lines, exactly, then the block
    wait_lines = [line.strip() for line in body[B_WAIT]]
    refresh = [line for line in wait_lines if line.startswith(("$Status = ", "$Gateway = "))]
    if len(refresh) != 2:
        problems.append("step 7's block no longer has exactly two status lines")
    compare("trial-08-approve.ps1", refresh + body[B_APPROVE])

    # the two window scripts: their own stop line and bytecode setting, then the block
    for name, index in WINDOWS.items():
        dot, banner, got = parts[name]
        if dot is not None:
            problems.append(f"{name}: a window script loads names.json itself and does not dot-source the shared start")
        rest = list(got)
        if rest[:1] == [STOP]:
            rest = rest[1:]
        else:
            problems.append(f"{name}: it must start with its own stop line")
        if rest.count(BYTECODE) != 1:
            problems.append(f"{name}: it must hold the bytecode setting once")
        else:
            rest.remove(BYTECODE)
        diff = _first_difference(body[index], rest)
        if diff:
            problems.append(f"{name}: {diff}")

    # "too old" is step 9a's block without the open-items part
    old = body[B_TOOOLD]
    adopt_line = old[-1]
    expected = list(old[:-1]) + [adopt_line.replace("blockers=[{1}]'", "blockers=[{1}]  open items={2}'", 1)
                                 + ", @($Gateway.evidence.open_transitions).Count"]
    diff = _first_difference(body[B_BLOCKERS], expected)
    if diff:
        problems.append(f"the 'too old' block is no longer step 9a's block without the open-items part: {diff}")

    # step 0: a harmless check, nothing else
    dot, banner, got = parts["trial-00-check.ps1"]
    if dot is not None or got != ["'PowerShell ' + $PSVersionTable.PSVersion.ToString()", "'scripts run here'"]:
        problems.append("trial-00-check.ps1 must only print the PowerShell version and 'scripts run here'")

    # step 1 holds placeholders, not values
    setup = norm(scripts["trial-01-setup.ps1"])
    for placeholder in STEP_ONE_PLACEHOLDERS:
        if placeholder not in setup:
            problems.append(f"trial-01-setup.ps1: the placeholder {placeholder} is missing")
    return problems


def check_inventory(runbook: str, scripts: dict):
    """The runbook names every script and every script it names exists; every script is pure ASCII."""
    problems = []
    named = set(re.findall(r"trial-[0-9a-z-]+\.ps1", norm(runbook)))
    for name in sorted(named - set(scripts)):
        problems.append(f"the runbook names {name}, which does not exist")
    for name in sorted(set(scripts) - named):
        problems.append(f"{name} is not named in the runbook")
    return problems


def read_scripts(folder: Path):
    scripts = {}
    for path in sorted(folder.glob("*.ps1")):
        raw = path.read_bytes()
        raw.decode("ascii")                                          # raises on any non-ASCII byte
        scripts[path.name] = raw.decode("ascii")
    return scripts


def parse_errors(scripts: dict):
    """PowerShell's own parser over every script (a copy with the step-1 placeholders filled in); never runs one."""
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name, text in scripts.items():
            text = text.replace("<budget in micro-euro>", "1").replace("<share in micro-euro>", "1")
            path = Path(tmp) / name
            path.write_bytes(text.encode("ascii"))
            paths.append(str(path))
        # the paths travel in an environment variable: arguments after -Command would be appended to the command text and RUN
        command = ("foreach ($p in ($env:TRIAL_SCRIPT_PATHS -split '[|]')) { $e = $null; "
                   "[void][System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$null, [ref]$e); "
                   "foreach ($x in $e) { (Split-Path $p -Leaf) + ': ' + $x.Message } }")
        environment = dict(os.environ, TRIAL_SCRIPT_PATHS="|".join(paths))
        done = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
                              capture_output=True, text=True, timeout=120, env=environment)
        return [line for line in done.stdout.splitlines() if line.strip()] + ([done.stderr.strip()] if done.returncode else [])


def mutate(files: dict, key: str, old: str, new: str):
    changed = dict(files)
    assert old in changed[key], (key, old)
    changed[key] = changed[key].replace(old, new, 1)
    return changed


class TrialScriptsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.runbook = norm(RUNBOOK.read_bytes().decode("utf-8"))
        cls.scripts = read_scripts(SCRIPTS)

    def problems(self, runbook=None, scripts=None):
        return check_drift(runbook or self.runbook, scripts or self.scripts)

    def mutated(self, key, old, new):
        files = {"runbook": self.runbook, **self.scripts}
        files = mutate(files, key, old, new)
        return self.problems(files.pop("runbook"), files)

    # ---- the real files hold together
    def test_the_scripts_do_not_drift_from_the_runbook(self):
        self.assertEqual(self.problems(), [])

    def test_the_runbook_names_every_script_and_every_named_script_exists(self):
        self.assertEqual(check_inventory(self.runbook, self.scripts), [])

    def test_every_script_is_pure_ascii(self):
        self.assertEqual(sorted(self.scripts), sorted(ALL_SCRIPTS))              # read_scripts already refused any non-ASCII byte

    def test_the_step_1_script_holds_placeholders_not_values(self):
        self.assertEqual([p for p in STEP_ONE_PLACEHOLDERS if p not in self.scripts["trial-01-setup.ps1"]], [])
        self.assertFalse(list(SCRIPTS.glob("names.json")), "names.json holds the operator's values and must not be shipped")

    def test_the_runbook_says_the_trial_passed_and_what_ctrl_c_prints(self):
        self.assertIn("passed against the real gateway on 2026-10-07", self.runbook)
        self.assertNotIn("not yet run against the real gateway", self.runbook)
        self.assertIn("KeyboardInterrupt", self.runbook)

    def test_the_runbook_tells_one_form_per_run_the_rehearsed_host_and_the_default_folder(self):
        text = " ".join(self.runbook.split())
        for sentence in ("Choose one form before step 1 and keep it to the end", "rehearsed with **PowerShell 7**",
                         "`pwsh -NoProfile -File '<script>'`", "was **not** rehearsed with the scripts", "trial-00-check.ps1",
                         "Both forms use the default trial folder only", "Do not choose another name"):
            self.assertIn(sentence, text)

    def test_the_parser_accepts_every_script(self):
        errors = parse_errors(self.scripts)
        if errors is None:
            self.skipTest("pwsh is not on the path: PowerShell's own parser is not available here")
        self.assertEqual(errors, [])

    # ---- mutations the check must report (each fed to the real checker)
    def test_mutation_an_added_executable_line_in_rollback(self):
        found = self.mutated("trial-12-rollback.ps1", "'== Step 12: roll back'", "'== Step 12: roll back'\nWrite-Output 'extra'")
        self.assertTrue(any("trial-12-rollback.ps1" in p for p in found), found)

    def test_mutation_a_placeholder_kept_but_overridden_later(self):
        found = self.mutated("trial-01-setup.ps1", "$AccountAlias = '<account alias>'",
                             "$AccountAlias = '<account alias>'\n$AccountAlias = 'real-account'")
        self.assertTrue(any("trial-01-setup.ps1" in p for p in found), found)

    def test_mutation_the_shared_start_stop_line_removed(self):
        found = self.mutated(COMMON, "$ErrorActionPreference = 'Stop'\n$env:PYTHONDONTWRITEBYTECODE", "$env:PYTHONDONTWRITEBYTECODE")
        self.assertTrue(any(COMMON in p for p in found), found)

    def test_mutation_an_internal_closing_brace_removed(self):
        found = self.mutated("trial-09b-adopt.ps1", "\n}\n", "\n")
        self.assertTrue(any("trial-09b-adopt.ps1" in p for p in found), found)

    def test_mutation_the_runbooks_set_location_target_changed(self):
        found = self.mutated("runbook", "Set-Location $Src", "Set-Location $Trial")
        self.assertTrue(any("Set-Location" in p for p in found), found)

    def test_mutation_one_character_of_a_runbook_block(self):
        found = self.mutated("runbook", "'PASSED'", "'PASSEX'")
        self.assertTrue(any("trial-10-check.ps1" in p for p in found), found)

    def test_mutation_a_script_line_removed(self):
        found = self.mutated("trial-10-check.ps1", "if ($Gateway.evidence.state -ne 'usable') { throw 'status does not say usable' }\n", "")
        self.assertTrue(any("trial-10-check.ps1" in p for p in found), found)

    def test_mutation_a_script_the_runbook_does_not_name(self):
        scripts = dict(self.scripts)
        scripts["trial-99-extra.ps1"] = DOT_SOURCE + "\n"
        self.assertTrue(check_inventory(self.runbook, scripts))
        self.assertTrue(any("trial-99-extra.ps1" in p for p in check_drift(self.runbook, scripts)))

    def test_mutation_a_placeholder_replaced_by_a_value(self):
        found = self.mutated("trial-01-setup.ps1", "'<account alias>'", "'real-account'")
        self.assertTrue(any("trial-01-setup.ps1" in p for p in found), found)

    def test_mutation_the_step_8_status_refresh_changed(self):
        found = self.mutated("trial-08-approve.ps1", "$Status = (& $Py -m quota_balancer --db $Store status --json) | ConvertFrom-Json",
                             "$Status = (& $Py -m quota_balancer --db $Store status) | ConvertFrom-Json")
        self.assertTrue(any("trial-08-approve.ps1" in p for p in found), found)

    def test_mutation_the_link_check_is_moved_after_names_json_is_read(self):
        files = {"runbook": self.runbook, **self.scripts}
        text = files[COMMON]
        link_block = text[text.index("# A link in the way"):text.index("# The names saved by step 1")]
        moved = text.replace(link_block, "") + link_block
        files[COMMON] = moved
        runbook = files.pop("runbook")
        self.assertTrue(any("link check" in p for p in self.problems(runbook, files)))

    def test_mutation_the_parser_reports_a_removed_brace(self):
        if parse_errors(self.scripts) is None:
            self.skipTest("pwsh is not on the path: PowerShell's own parser is not available here")
        scripts = dict(self.scripts)
        scripts["trial-09b-adopt.ps1"] = scripts["trial-09b-adopt.ps1"].replace("\n}\n", "\n", 1)
        self.assertTrue(parse_errors(scripts))

    def test_line_endings_are_normalised_before_comparing(self):
        crlf = {name: text.replace("\n", "\r\n") for name, text in self.scripts.items()}
        self.assertEqual(check_drift(self.runbook.replace("\n", "\r\n"), crlf), [])


if __name__ == "__main__":
    unittest.main()
