"""The trial runbook's scripts must not drift from the runbook (text only: no PowerShell is run).

Every step of ``docs/TRIAL-R1-READER-4.md`` also ships as a small script in ``docs/trial-r1-reader-4/``. The real trial ran those
scripts, so a later edit of a runbook block that is not made in the script too (or the other way round) would send the operator
a different command from the one that was proven. These checks compare the TEXT, with line endings normalised (a Windows
checkout has CRLF; the repository has LF).
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = ROOT / "docs" / "TRIAL-R1-READER-4.md"
SCRIPTS = ROOT / "docs" / "trial-r1-reader-4"
COMMON = "trial-common.ps1"

# The runbook's PowerShell blocks in order, and the script(s) that must hold each one. The block of step 1 that defines the
# two privacy helpers is held by the shared start (trial-common.ps1) and by the step-1 script.
BLOCK_SCRIPTS = [
    ("step 1 names", ["trial-01-setup.ps1"]),
    ("step 1 folder", ["trial-01-setup.ps1", COMMON]),
    ("step 2 unpack", ["trial-02-unpack.ps1"]),
    ("step 2 set-location", [COMMON]),
    ("step 3", ["trial-03-check.ps1"]),
    ("step 4", ["trial-04-store.ps1"]),
    ("step 5", ["trial-05-identity.ps1"]),
    ("step 6 server", ["trial-06a-server.ps1"]),
    ("step 6 reader", ["trial-06b-reader.ps1"]),
    ("step 7", ["trial-07-wait.ps1"]),
    ("step 8", ["trial-08-approve.ps1"]),
    ("step 9 blockers", ["trial-09a-blockers.ps1"]),
    ("step 9 adopt", ["trial-09b-adopt.ps1"]),
    ("step 10", ["trial-10-check.ps1"]),
    ("step 11", ["trial-11-stopped.ps1"]),
    ("step 12", ["trial-12-rollback.ps1"]),
    ("adopt refused as too old", ["trial-09a-blockers.ps1"]),
    ("if something goes wrong", ["trial-help-status.ps1"]),
]

# The only places a script differs from its block, each stated and checked on purpose:
# - a pasted block is wrapped in ``. {`` ... ``}`` and sets the error preference at both ends; a script file needs neither
#   (the shared start sets the preference once);
# - block 0 (step 1 names): the script keeps the values but drops the trailing explanation comments of six lines;
# - block 3 (``Set-Location $Src``): the shared start does it after step 2 (``Set-Location -LiteralPath $Src``);
# - block 16 ("adopt refused as too old"): the step-9 script prints the same state line and also the number of open items.
WRAPPER_LINES = {". {", "}", "$ErrorActionPreference = 'Stop'", "$ErrorActionPreference = 'Continue'"}
COMMENT_TRIMMED_BLOCK = 0
SET_LOCATION_BLOCK = 3
LONGER_LINE_BLOCK = 16


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def runbook_blocks():
    """The runbook's powershell code blocks, as lists of lines, in order."""
    blocks, current = [], None
    for line in _text(RUNBOOK).split("\n"):
        if current is None:
            if line.startswith("```powershell"):
                current = []
        elif line.startswith("```"):
            blocks.append(current)
            current = None
        else:
            current.append(line)
    return blocks


def script_lines(name: str):
    return _text(SCRIPTS / name).split("\n")


def _strip_comment(line: str) -> str:
    return re.sub(r"\s+#.*$", "", line).rstrip()


def _contains_in_order(haystack, needles):
    """Every needle is found, in the given order, among the haystack lines. Returns the first missing needle or None."""
    pos = 0
    for needle in needles:
        while pos < len(haystack) and haystack[pos] != needle:
            pos += 1
        if pos == len(haystack):
            return needle
        pos += 1
    return None


class TrialScriptsTest(unittest.TestCase):

    def test_the_runbook_has_the_blocks_this_test_expects(self):
        self.assertEqual(len(runbook_blocks()), len(BLOCK_SCRIPTS))

    def test_every_runbook_block_appears_unchanged_in_its_scripts(self):
        blocks = runbook_blocks()
        for index, (label, names) in enumerate(BLOCK_SCRIPTS):
            if index == SET_LOCATION_BLOCK:
                self.assertIn("Set-Location -LiteralPath $Src", "\n".join(script_lines(COMMON)), label)
                continue
            body = [line for line in blocks[index] if line not in WRAPPER_LINES]
            held = []
            for name in names:
                held += script_lines(name)
            if index == COMMENT_TRIMMED_BLOCK:
                body = [_strip_comment(line) for line in body]
                held = [_strip_comment(line) for line in held]
            if index == LONGER_LINE_BLOCK:
                prefix = "'state={0}  blockers=[{1}]"
                self.assertTrue(any(line.startswith(prefix) for line in held), label)
                body = [line for line in body if not line.startswith(prefix)]
            if index == 1:
                # the helper definitions are in the shared start, the rest of the block in the step-1 script
                held = script_lines("trial-01-setup.ps1") + script_lines(COMMON)
                missing = [line for line in body if line.strip() and line not in held]
            else:
                missing = _contains_in_order(held, [line for line in body if line.strip()])
                missing = [missing] if missing is not None else []
            self.assertEqual(missing, [], f"{label}: the runbook block and its script differ at {missing[:1]}")

    def test_every_script_is_ascii_and_starts_with_the_shared_start(self):
        names = sorted(p.name for p in SCRIPTS.glob("*.ps1"))
        self.assertGreaterEqual(len(names), 16)
        for name in names:
            raw = (SCRIPTS / name).read_bytes()
            raw.decode("ascii")                                      # raises on any non-ASCII byte
            self.assertNotIn(b"\r", raw, f"{name} must have LF line endings")
            if name == COMMON:
                continue
            code = [line for line in _text(SCRIPTS / name).split("\n") if line.strip() and not line.lstrip().startswith("#")]
            if name in ("trial-06a-server.ps1", "trial-06b-reader.ps1"):
                # the two window scripts load names.json themselves (they run in a window of their own)
                self.assertIn("names.json", code[1], name)
            else:
                self.assertEqual(code[0], ". (Join-Path $PSScriptRoot 'trial-common.ps1')", name)

    def test_the_step_1_script_holds_placeholders_not_values(self):
        text = _text(SCRIPTS / "trial-01-setup.ps1")
        for placeholder in ("<C:\\path\\to\\python.exe>", "<C:\\path\\to\\agenttalk.exe>", "<account alias>", "<gateway alias>",
                            "<budget in micro-euro>", "<share in micro-euro>", "<C:\\path\\to\\leasewise-COMMIT.zip>",
                            "<the SHA-256 the lead gave you>"):
            self.assertIn(placeholder, text)
        self.assertFalse(list(SCRIPTS.glob("names.json")), "names.json holds the operator's values and must not be shipped")

    def test_the_runbook_names_every_script_and_every_named_script_exists(self):
        named = set(re.findall(r"trial-[0-9a-z-]+\.ps1", _text(RUNBOOK)))
        shipped = {p.name for p in SCRIPTS.glob("*.ps1")}
        self.assertEqual(named - shipped, set(), "the runbook names a script that does not exist")
        self.assertEqual(shipped - named, set(), "a script is not named in the runbook")

    def test_the_runbook_says_the_trial_passed_and_what_ctrl_c_prints(self):
        text = _text(RUNBOOK)
        self.assertIn("passed against the real gateway on 2026-10-07", text)
        self.assertNotIn("not yet run against the real gateway", text)
        self.assertIn("KeyboardInterrupt", text)
        self.assertIn("## Two ways to run each step", text)


if __name__ == "__main__":
    unittest.main()
