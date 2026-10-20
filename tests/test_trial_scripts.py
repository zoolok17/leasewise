"""The trial runbook's scripts must not drift from the runbook.

Every step of ``docs/TRIAL-R1-READER-4.md`` also ships as a small script in ``docs/trial-r1-reader-4/``. The real trial ran those
scripts, so a later edit of a runbook block that is not made in the script too (or the other way round) would send the operator
a different command from the one that was proven.

The check compares each script's COMPLETE executable body with its runbook block: only the pasted block's outer wrapper is
stripped (``. {``, the two ``$ErrorActionPreference`` lines, the final ``}``); internal braces, order and every executable line
stay. Blank lines and full-line comments are ignored (a here-string's lines are data and are never ignored). The only
differences allowed are the named adaptations in ``check_drift``; anything else is reported.

Two files are not compared with a block at all, so they are PINNED instead: the shared start (``trial-common.ps1``) and the
entry guard (``trial-guard.ps1``) must equal the approved copies below, character for character (line endings normalised). Any
change to either fails the test until the approved copy here is updated on purpose.

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
GUARD = "trial-guard.ps1"
GUARD_DOT = ". (Join-Path $PSScriptRoot 'trial-guard.ps1')"
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
               ["trial-00-check.ps1", "trial-01-setup.ps1", "trial-08-approve.ps1", COMMON, GUARD])

# The approved copies of the two pinned files (see the docstring).
APPROVED = {
    COMMON: r'''param([switch]$KeepOutOfSource)    # step 12 sets this: it must not enter the source folder (which lives inside the folder it deletes)
# Shared start for every trial step script (made 2026-10-07 by the desktop lead so the operator need not paste blocks).
# It only does what the pasted runbook got from step 1's block and the open window: stop at the first error, the UTF-8 writer,
# the two privacy helpers (copied unchanged from step 1), the saved names, and Leasewise's source folder as the working folder.
$ErrorActionPreference = 'Stop'
$env:PYTHONDONTWRITEBYTECODE = '1'                       # keeps the source folder clean
$Utf8 = New-Object System.Text.UTF8Encoding($false)      # UTF-8 without a byte-order mark, for every file written below

# Who may open the private things: you (by security identifier), SYSTEM and Administrators. Names are never compared, only identifiers.
$Me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$Allowed = @($Me, 'S-1-5-18', 'S-1-5-32-544')
function Protect-Path {
    param([string]$Path, [bool]$Folder)
    $Item = Get-Item -LiteralPath $Path -Force
    $Acl = [IO.FileSystemAclExtensions]::GetAccessControl($Item, [Security.AccessControl.AccessControlSections]::Access)   # the access list only: writing back the owner or audit parts needs an administrator privilege
    $Acl.SetAccessRuleProtection($true, $false)                          # nothing inherited from the folder above
    foreach ($Rule in @($Acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))) { [void]$Acl.RemoveAccessRule($Rule) }     # by identifier: the .Access shortcut is empty on this kind of object
    $Flags = if ($Folder) { 'ContainerInherit,ObjectInherit' } else { 'None' }
    foreach ($Sid in $Allowed) {
        $Who = New-Object Security.Principal.SecurityIdentifier($Sid)
        $Acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($Who, 'FullControl', $Flags, 'None', 'Allow')))
    }
    [IO.FileSystemAclExtensions]::SetAccessControl($Item, $Acl)
}
function Assert-Private {
    param([string]$Path, [bool]$OwnList)                                  # $OwnList: the item has its own list and inherits nothing
    $Acl = Get-Acl -LiteralPath $Path
    if ($OwnList -and -not $Acl.AreAccessRulesProtected) { throw "$Path still inherits access from its folder. Stop here; nothing secret has been written." }
    $Seen = @()
    foreach ($Rule in $Acl.Access) {
        if ($Rule.AccessControlType -ne 'Allow') { continue }
        $Sid = $Rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($Allowed -notcontains $Sid) { throw "Someone else can open $Path. Stop here; nothing secret has been written." }
        if ($OwnList -and $Rule.IsInherited) { throw "$Path inherits an entry from its folder. Stop here; nothing secret has been written." }
        $Seen += $Sid
    }
    if ($Seen -notcontains $Me) { throw "You cannot open $Path. Stop here." }
}

# The entry guard (a link in the way, or a different trial folder: stop before anything is read, listed or entered), then the names saved by step 1.
. (Join-Path $PSScriptRoot 'trial-guard.ps1')
if ($N) {
    foreach ($P in $N.PSObject.Properties) { Set-Variable -Name $P.Name -Value $P.Value }
    $GatewayPre = @($GatewayPre)                         # an empty list stays a list
    if (-not $KeepOutOfSource -and (Test-Path -LiteralPath $Src)) { Set-Location -LiteralPath $Src }   # run every Leasewise command from inside the source folder (after step 2)
}
''',
    GUARD: r'''# The one entry guard of the trial scripts. The shared start (trial-common.ps1) and the two step-6 window scripts all dot-source THIS file, so
# the check cannot differ between copies. It runs before anything is entered or run, and it stops (nothing is changed) when:
#   * the default trial folder, or its names.json, is a link (a junction or symbolic link): nothing is read, listed or entered through it;
#   * names.json names a trial folder other than the default one: only the default folder works with these steps (the step-6 windows read it).
# It leaves $DefaultTrial, $NamesFile and $N (the saved names, or nothing before step 1 has run) for the caller.
$DefaultTrial = Join-Path $env:LOCALAPPDATA 'leasewise-trial'
$NamesFile = Join-Path $DefaultTrial 'names.json'
foreach ($Item in $DefaultTrial, $NamesFile) {
    $Info = Get-Item -LiteralPath $Item -Force -ErrorAction SilentlyContinue
    if ($Info -and ($Info.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "$Item is a link. Nothing was changed. Tell the lead." }
}
$N = $null
if (Test-Path -LiteralPath $NamesFile) {
    $N = Get-Content -LiteralPath $NamesFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($N.Trial -ne $DefaultTrial) { throw "names.json names the trial folder $($N.Trial), but only $DefaultTrial works with these steps. Nothing was changed. Tell the lead." }
}
''',
}
# A banner is ONE single-quoted literal and nothing else (no second command after a semicolon).
BANNER = re.compile(r"'== (?:[^']|'')*'")


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
    if dot is not None and lines and BANNER.fullmatch(lines[0]):
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
      * the banner line (one single-quoted literal) and the dot-source line at the top of a script (step 12's carries ``-KeepOutOfSource``);
      * step 1: the dropped trailing comments of the names block (the code on each line must be identical), and the helper
        definitions, which live in trial-common.ps1 (checked there, in order);
      * step 2's ``Set-Location``: done by the shared start (its target must be the runbook's);
      * step 6's two window scripts: their own copy of the stop line, the shared entry guard (trial-guard.ps1, the same file the
        shared start uses) and the bytecode setting;
      * the shared start and the entry guard are not compared with a block: they are pinned to the approved copies;
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
    if GUARD_DOT not in common:
        problems.append(f"{COMMON}: it must dot-source the entry guard ({GUARD})")
    for name in (COMMON, GUARD):
        if norm(scripts[name]) != APPROVED[name]:
            problems.append(f"{name}: it differs from the approved copy pinned in this test")
    guard_text = norm(scripts[GUARD])
    if "ReparsePoint" not in guard_text or guard_text.find("ReparsePoint") > guard_text.find("Get-Content"):
        problems.append(f"{GUARD}: the link check must come before names.json is read")
    if "$N.Trial -ne $DefaultTrial" not in guard_text:
        problems.append(f"{GUARD}: the default-folder check is missing")

    # step 8: step 7's two status lines, exactly, then the block
    wait_lines = [line.strip() for line in body[B_WAIT]]
    refresh = [line for line in wait_lines if line.startswith(("$Status = ", "$Gateway = "))]
    if len(refresh) != 2:
        problems.append("step 7's block no longer has exactly two status lines")
    compare("trial-08-approve.ps1", refresh + body[B_APPROVE])

    # the two window scripts: their own stop line, then the same entry guard, the bytecode setting, then the block
    for name, index in WINDOWS.items():
        dot, banner, got = parts[name]
        if dot is not None:
            problems.append(f"{name}: a window script loads names.json itself and does not dot-source the shared start")
        rest = list(got)
        if rest[:1] == [STOP]:
            rest = rest[1:]
        else:
            problems.append(f"{name}: it must start with its own stop line")
        if rest[:1] == [GUARD_DOT]:
            rest = rest[1:]
        else:
            problems.append(f"{name}: the entry guard must come right after the stop line, before names.json is read")
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
        scripts[path.name] = norm(raw.decode("ascii"))                # LF, whatever the checkout holds
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


def _replace(key, old, new):
    return lambda files: mutate(files, key, old, new)


def _append(key, extra):
    def change(files):
        changed = dict(files)
        changed[key] = changed[key] + extra
        return changed
    return change


def _move_link_check(files):
    changed = dict(files)
    text = changed[GUARD]
    block = text[text.index("foreach ($Item in"):text.index("$N = $null")]
    changed[GUARD] = text.replace(block, "") + block
    return changed


def _remove_guard_from(name):
    return _replace(name, "\n" + GUARD_DOT, "")


# label -> (the mutation, a word the reported problem must contain)
MUTATIONS = {
    "an added executable line in rollback": (_replace("trial-12-rollback.ps1", "'== Step 12: roll back'", "'== Step 12: roll back'\nWrite-Output 'extra'"), "trial-12-rollback.ps1"),
    "a placeholder kept but overridden later": (_replace("trial-01-setup.ps1", "$AccountAlias = '<account alias>'", "$AccountAlias = '<account alias>'\n$AccountAlias = 'real-account'"), "trial-01-setup.ps1"),
    "the shared start stop line removed": (_replace(COMMON, "$ErrorActionPreference = 'Stop'\n$env:PYTHONDONTWRITEBYTECODE", "$env:PYTHONDONTWRITEBYTECODE"), COMMON),
    "an internal closing brace removed": (_replace("trial-09b-adopt.ps1", "\n}\n", "\n"), "trial-09b-adopt.ps1"),
    "the runbook Set-Location target changed": (_replace("runbook", "Set-Location $Src", "Set-Location $Trial"), "Set-Location"),
    "one character of a runbook block": (_replace("runbook", "'PASSED'", "'PASSEX'"), "trial-10-check.ps1"),
    "a script line removed": (_replace("trial-10-check.ps1", "if ($Gateway.evidence.state -ne 'usable') { throw 'status does not say usable' }\n", ""), "trial-10-check.ps1"),
    "a placeholder replaced by a value": (_replace("trial-01-setup.ps1", "'<account alias>'", "'real-account'"), "trial-01-setup.ps1"),
    "the step 8 status refresh changed": (_replace("trial-08-approve.ps1", "$Status = (& $Py -m quota_balancer --db $Store status --json) | ConvertFrom-Json", "$Status = (& $Py -m quota_balancer --db $Store status) | ConvertFrom-Json"), "trial-08-approve.ps1"),
    "an executable line appended to the shared start": (_append(COMMON, "Write-Output 'synthetic extra command'\n"), COMMON),
    "the keep-out-of-source condition removed": (_replace(COMMON, "-not $KeepOutOfSource -and ", ""), COMMON),
    "a banner with a second command": (_replace("trial-12-rollback.ps1", "'== Step 12: roll back'", "'== Step 12: roll back'; Write-Output 'synthetic extra command'"), "trial-12-rollback.ps1"),
    "the guard removed from the step 6 server script": (_remove_guard_from("trial-06a-server.ps1"), "trial-06a-server.ps1"),
    "the guard removed from the step 6 reader script": (_remove_guard_from("trial-06b-reader.ps1"), "trial-06b-reader.ps1"),
    "the link check removed from the guard": (_replace(GUARD, "-band [IO.FileAttributes]::ReparsePoint", "-band 0"), GUARD),
    "the link check moved after names.json is read": (_move_link_check, GUARD),
}


class TrialScriptsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.runbook = norm(RUNBOOK.read_bytes().decode("utf-8"))
        cls.scripts = read_scripts(SCRIPTS)

    def problems(self, runbook=None, scripts=None):
        return check_drift(runbook or self.runbook, scripts or self.scripts)

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

    def test_the_runbook_prose_no_longer_says_to_choose_another_name(self):
        prose = re.sub(r"(?ms)^```powershell\n.*?^```", "", self.runbook)       # the frozen blocks keep their own words
        self.assertNotIn("choose another name", prose.lower().replace("do not choose another name", "").replace("**do not choose another name**", ""))
        self.assertIn("Do not choose another name", prose)
        self.assertIn("an earlier trial's folder is still there", prose)

    def test_the_parser_accepts_every_script(self):
        errors = parse_errors(self.scripts)
        if errors is None:
            self.skipTest("pwsh is not on the path: PowerShell's own parser is not available here")
        self.assertEqual(errors, [])

    # ---- mutations the check must report (each fed to the real checker, on LF text and on CRLF copies)
    def reported(self, label):
        change, needle = MUTATIONS[label]
        for crlf in (False, True):
            files = change({"runbook": self.runbook, **self.scripts})
            if crlf:
                files = {key: text.replace("\n", "\r\n") for key, text in files.items()}
            runbook = files.pop("runbook")
            found = check_drift(runbook, files)
            self.assertTrue(any(needle in p for p in found), (label, "CRLF" if crlf else "LF", found))

    def test_mutation_an_added_executable_line_in_rollback(self):
        self.reported("an added executable line in rollback")

    def test_mutation_a_placeholder_kept_but_overridden_later(self):
        self.reported("a placeholder kept but overridden later")

    def test_mutation_the_shared_start_stop_line_removed(self):
        self.reported("the shared start stop line removed")

    def test_mutation_an_internal_closing_brace_removed(self):
        self.reported("an internal closing brace removed")

    def test_mutation_the_runbooks_set_location_target_changed(self):
        self.reported("the runbook Set-Location target changed")

    def test_mutation_one_character_of_a_runbook_block(self):
        self.reported("one character of a runbook block")

    def test_mutation_a_script_line_removed(self):
        self.reported("a script line removed")

    def test_mutation_a_placeholder_replaced_by_a_value(self):
        self.reported("a placeholder replaced by a value")

    def test_mutation_the_step_8_status_refresh_changed(self):
        self.reported("the step 8 status refresh changed")

    def test_mutation_an_executable_line_appended_to_the_shared_start(self):
        self.reported("an executable line appended to the shared start")

    def test_mutation_the_keep_out_of_source_condition_removed(self):
        self.reported("the keep-out-of-source condition removed")

    def test_mutation_a_banner_with_a_second_command(self):
        self.reported("a banner with a second command")

    def test_mutation_the_guard_removed_from_the_step_6_server_script(self):
        self.reported("the guard removed from the step 6 server script")

    def test_mutation_the_guard_removed_from_the_step_6_reader_script(self):
        self.reported("the guard removed from the step 6 reader script")

    def test_mutation_the_link_check_removed_from_the_guard(self):
        self.reported("the link check removed from the guard")

    def test_mutation_the_link_check_is_moved_after_names_json_is_read(self):
        self.reported("the link check moved after names.json is read")

    def test_mutation_a_script_the_runbook_does_not_name(self):
        scripts = dict(self.scripts)
        scripts["trial-99-extra.ps1"] = DOT_SOURCE + "\n"
        self.assertTrue(check_inventory(self.runbook, scripts))
        self.assertTrue(any("trial-99-extra.ps1" in p for p in check_drift(self.runbook, scripts)))

    def test_every_mutation_is_reported_on_crlf_copies_and_the_real_files_are_clean_on_crlf(self):
        crlf = {name: text.replace("\n", "\r\n") for name, text in self.scripts.items()}
        self.assertEqual(check_drift(self.runbook.replace("\n", "\r\n"), crlf), [])
        for label in MUTATIONS:
            with self.subTest(label):
                self.reported(label)

    def test_mutation_the_parser_reports_a_removed_brace(self):
        if parse_errors(self.scripts) is None:
            self.skipTest("pwsh is not on the path: PowerShell's own parser is not available here")
        scripts = dict(self.scripts)
        scripts["trial-09b-adopt.ps1"] = scripts["trial-09b-adopt.ps1"].replace("\n}\n", "\n", 1)
        self.assertTrue(parse_errors(scripts))


if __name__ == "__main__":
    unittest.main()
