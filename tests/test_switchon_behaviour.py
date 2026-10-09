"""Card 7b-on, fix round 1: what the switch-on scripts DO, run with PowerShell 7 in throwaway folders (synthetic data only).

Codex's review reproduced five defects of the first version; every case below is one of them (or its neighbour) turned into a test:

  * containment: a link ANYWHERE on the way to a file the scripts use, a saved root or port that differs from the checked one, a linked source
    folder, a linked archive or archive parent, a linked ownership marker;
  * rehearsal mode: only with an explicit switch and ALL of its settings, only inside C:\\lw\\scratch, never with the two roots overlapping,
    and any stray rehearsal variable stops the operator's run;
  * the masked prompt: the real console branch (a pseudo console), with loud markers that must appear nowhere it shows or records;
  * privacy before writing: a refusal before a write changes no byte, and the messages tell a refusal before a write from a failure after one;
  * the done check: it passes only for figures newer than the baseline, a recent reporter scan and a succeeding status command;
  * no secret on a command line: the enrollment gets the identity value through standard input.

The tests need PowerShell 7 on Windows and a throwaway folder inside C:\\lw\\scratch (AGENTTALK_SCRATCH); otherwise they skip.
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "docs" / "switchon-7b"
COMMON = SCRIPTS / "switchon-common.ps1"
PORT = "18455"
LOUD_CLAUDE = "claude:SYNTHETIC-LOUD-USER-5521:home-aaaaaaaa"
LOUD_CODEX = "codex:SYNTHETIC-LOUD-USER-5521:home-bbbbbbbb"
LOUD = "SYNTHETIC-LOUD-USER-5521"
PARTS = dict(LW_SWITCHON_INPUT="env", LW_SWITCHON_CLAUDE_NAME=LOUD, LW_SWITCHON_CLAUDE_HOME="aaaaaaaa", LW_SWITCHON_CODEX_NAME=LOUD,
             LW_SWITCHON_CODEX_HOME="home-bbbbbbbb")                      # the four test parts of step 8 (one with, one without the home- in front)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def scratch_base():
    base = os.environ.get("AGENTTALK_SCRATCH")
    if base and base.lower().startswith("c:\\lw\\scratch\\") and os.path.isdir(base):
        return base
    return None


@unittest.skipUnless(shutil.which("pwsh") and sys.platform == "win32" and scratch_base(), "needs PowerShell 7 on Windows and AGENTTALK_SCRATCH inside C:\\lw\\scratch")
class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=scratch_base(), prefix="quota-swb-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.live, self.bus = self.tmp / "live", self.tmp / "bus"
        (self.bus / "state").mkdir(parents=True)
        self.usage("seat-a", LOUD_CLAUDE)
        self.usage("seat-b", LOUD_CODEX)
        self.links = []
        self.default_over = {}
        self.addCleanup(self.remove_links)

    # --- helpers ---------------------------------------------------------------------------------------------------

    def env(self, **over):
        env = {k: v for k, v in os.environ.items() if not k.startswith("LW_SWITCHON_")}
        env.update(LW_SWITCHON_REHEARSAL="yes", LW_SWITCHON_ROOT=str(self.live),
                   LW_SWITCHON_BUS=str(self.bus), LW_SWITCHON_PORT=PORT, LW_SWITCHON_INPUT="prompt")
        for key, value in over.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def usage(self, stem, who, **over):
        """A usage file in the shape the reporter accepts (only the fields the scripts look at)."""
        record = {"schema_version": 2, "scope": "account", "account": who}
        record.update(over)
        (self.bus / "state" / (stem + ".capacity.json")).write_text(json.dumps(record), encoding="ascii")

    def install_source(self):
        """Leasewise's source in the live folder, as step 2 leaves it (a copy: a link there would be refused)."""
        if not (self.live / "src" / "quota_balancer").exists():
            shutil.copytree(ROOT / "quota_balancer", self.live / "src" / "quota_balancer", ignore=shutil.ignore_patterns("__pycache__"))

    def run_script(self, name, env=None, stdin=None, scripts=SCRIPTS, args=(), interactive=False):
        flags = ["-NoProfile"] if interactive else ["-NoProfile", "-NonInteractive"]
        return subprocess.run(["pwsh", *flags, "-File", str(Path(scripts) / name), *args], capture_output=True, text=True, input=stdin,
                              env=env or self.env(), timeout=180, cwd=str(self.tmp))

    def command(self, text, env=None):
        return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", text], capture_output=True, text=True, env=env or self.env(), timeout=180, cwd=str(self.tmp))

    @staticmethod
    def said(done):
        return " ".join(ANSI.sub("", done.stdout + " " + done.stderr).replace("| ", "").split())

    def junction(self, link, target):
        done = self.command("New-Item -ItemType Junction -Path '%s' -Target '%s' | Out-Null" % (link, target))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.links.append(Path(link))

    def remove_links(self):
        for link in self.links:
            if os.path.lexists(link):
                os.rmdir(link)                                          # only the link itself, never its target

    def names(self, **over):
        saved = {"LiveRoot": str(self.live), "BusRoot": str(self.bus), "Port": int(PORT), "Token": "0123456789abcdef" * 2, "Py": sys.executable,
                 "Archive": str(self.tmp / "leasewise.zip"), "ExpectedSha": "a" * 64, "MachineAlias": "laptop-b", "ClaudeAlias": "claude-main",
                 "CodexAlias": "codex-main", "ProjectAlias": "leasewise-team"}
        saved.update(self.default_over)
        saved.update(over)
        self.live.mkdir(exist_ok=True)
        (self.live / "names.json").write_text(json.dumps(saved), encoding="ascii")
        return saved

    def private_live(self, config=True):
        """The live folder, names.json and (optionally) an empty settings file, all private, as steps 1 and 7 leave them."""
        self.names()
        text = "Protect-Path $LiveRoot $true; "
        if config:
            text += "[IO.File]::WriteAllText($ConfigPath, \"version = 1`n\"); Protect-Path $ConfigPath $false"
        done = self.command(". '%s'; %s" % (COMMON, text))
        self.assertEqual(done.returncode, 0, self.said(done))

    def open_to_everyone(self, path):
        text = ("$a = Get-Acl -LiteralPath '%s'; $a.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule("
                "(New-Object Security.Principal.SecurityIdentifier('S-1-1-0')), 'Read', 'Allow'))); Set-Acl -LiteralPath '%s' -AclObject $a" % (path, path))
        done = self.command(text)
        self.assertEqual(done.returncode, 0, done.stderr)

    @staticmethod
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Containment(Base):
    def test_a_link_in_an_ancestor_of_the_live_root_is_refused(self):
        real = self.tmp / "real"
        (real / "live").mkdir(parents=True)
        link = self.tmp / "linked-parent"
        self.junction(link, real)
        done = self.run_script("switchon-03-check.ps1", self.env(LW_SWITCHON_ROOT=str(link / "live")))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("linked-parent is a link. Nothing was changed", self.said(done))

    def test_a_link_at_the_live_folder_the_bus_folder_or_its_state_folder_is_refused(self):
        real = self.tmp / "real-live"
        real.mkdir()
        self.junction(self.tmp / "live-link", real)
        self.junction(self.tmp / "bus-link", self.bus)
        (self.tmp / "bus2").mkdir()
        self.junction(self.tmp / "bus2" / "state", self.bus / "state")
        for over in ({"LW_SWITCHON_ROOT": str(self.tmp / "live-link")}, {"LW_SWITCHON_BUS": str(self.tmp / "bus-link")},
                     {"LW_SWITCHON_BUS": str(self.tmp / "bus2")}):
            done = self.run_script("switchon-03-check.ps1", self.env(**over))
            self.assertNotEqual(done.returncode, 0, over)
            self.assertIn("is a link. Nothing was changed", self.said(done))

    def test_a_saved_bus_a_saved_live_folder_or_a_saved_port_that_differs_from_the_checked_one_stops_the_step(self):
        other = self.tmp / "other-bus"
        (other / "state").mkdir(parents=True)
        for over in ({"BusRoot": str(other)}, {"LiveRoot": str(self.tmp / "elsewhere")}, {"Port": 18999}):
            self.names(**over)
            done = self.run_script("switchon-03-check.ps1")
            self.assertNotEqual(done.returncode, 0, over)
            self.assertIn("names.json was made for another live folder, agenttalk folder or port", self.said(done))
            self.assertNotIn("usage file(s) found", done.stdout)

    def test_only_an_allow_list_of_names_is_read_and_every_path_is_worked_out_again(self):
        self.names(Src=str(self.tmp / "evil-src"), Store=str(self.tmp / "evil.db"), BaseUrl="http://evil.example:80", ConfigPath=str(self.tmp / "evil.toml"),
                   StateDir=str(self.tmp / "evil-state"), Journal=str(self.tmp / "evil-journal"), PolicyIds=str(self.tmp / "evil.json"))
        done = self.command(". '%s'; $Src; $Store; $BaseUrl; $ConfigPath; $StateDir; $Journal; $PolicyIds; $Names" % COMMON)
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertEqual(done.stdout.split(), [str(self.live / "src"), str(self.live / "store" / "inventory.db"), "http://127.0.0.1:" + PORT,
                                               str(self.live / "reporter.toml"), str(self.live / "state"), str(self.live / "journal"),
                                               str(self.live / "policy-ids.json"), str(self.live / "names.json")])

    def test_names_that_are_not_what_step_1_makes_are_refused(self):
        for over in ({"Token": "not-hex"}, {"ExpectedSha": "short"}, {"ClaudeAlias": "Bad Alias"}, {"Py": str(self.tmp / "no-such-python.exe")},
                     {"Py": "python.exe"}, {"MachineAlias": "x" * 70}):
            self.names(**over)
            done = self.run_script("switchon-03-check.ps1")
            self.assertNotEqual(done.returncode, 0, over)

    def test_a_linked_source_folder_is_refused_before_it_is_entered(self):
        target = self.tmp / "source-target"
        target.mkdir()
        self.names()
        self.junction(self.live / "src", target)
        done = self.run_script("switchon-03-check.ps1")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("src is a link. Nothing was changed", self.said(done))
        self.assertNotIn("usage file(s) found", done.stdout)

    def python_that_leaves_a_trace(self):
        """A Python stand-in that writes a file whenever it is started, so a test can prove Python never ran."""
        trace = self.tmp / "python-ran.txt"
        shim = self.tmp / "tracer.ps1"
        shim.write_text('[IO.File]::AppendAllText("%s", "ran`n")\n& "%s" @args\nexit $LASTEXITCODE\n' % (trace, sys.executable), encoding="ascii")
        self.default_over = {"Py": str(shim)}
        return trace

    def test_a_linked_package_below_a_plain_source_folder_is_refused_before_python_starts(self):
        trace = self.python_that_leaves_a_trace()
        self.names()
        package = self.tmp / "evil-package"
        package.mkdir()
        (package / "__init__.py").write_text("open(r'%s', 'w').write('imported')\n" % (self.tmp / "imported.txt"), encoding="ascii")
        (self.live / "src").mkdir()
        self.junction(self.live / "src" / "quota_balancer", package)
        for step in ("switchon-03-check.ps1", "switchon-04-store.ps1", "switchon-09-check-table.ps1", "switchon-13-done.ps1"):
            done = self.run_script(step)
            self.assertNotEqual(done.returncode, 0, step)
            self.assertIn("quota_balancer is a link. Nothing was changed", self.said(done), step)
            self.assertFalse(trace.exists(), step + ": Python was started")
        self.assertFalse((self.tmp / "imported.txt").exists())

    def test_a_link_at_the_reporters_database_or_at_a_store_file_is_refused_before_python_starts(self):
        trace = self.python_that_leaves_a_trace()
        self.names()
        target = self.tmp / "elsewhere"
        target.mkdir()
        (self.live / "state").mkdir()
        (self.live / "store").mkdir()
        for link in (self.live / "state" / "reporter.db", self.live / "store" / "inventory.db"):
            self.junction(link, target)
            done = self.run_script("switchon-03-check.ps1")
            self.assertNotEqual(done.returncode, 0, str(link))
            self.assertIn(link.name + " is a link. Nothing was changed", self.said(done))
            self.assertFalse(trace.exists(), str(link))
            os.rmdir(link)

    def test_a_link_inside_a_folder_below_the_live_folder_is_found_at_any_depth_and_the_target_is_not_entered(self):
        self.names()
        target = self.tmp / "elsewhere"
        target.mkdir()
        (target / "never-listed.txt").write_text("x", encoding="ascii")
        deep = self.live / "a" / "b" / "c"
        deep.mkdir(parents=True)
        self.junction(deep / "link", target)
        done = self.run_script("switchon-03-check.ps1")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("link is a link. Nothing was changed", self.said(done))
        self.assertNotIn("never-listed", self.said(done))

    def test_the_walk_is_bounded(self):
        self.names()
        text = (SCRIPTS / "switchon-guard.ps1").read_text(encoding="ascii")
        bounded = self.tmp / "scripts"
        shutil.copytree(SCRIPTS, bounded)
        guard = bounded / "switchon-guard.ps1"
        guard.write_text(text.replace("$Seen -gt 50000", "$Seen -gt 5"), encoding="ascii", newline="\n")
        for number in range(8):
            (self.live / ("file%d.txt" % number)).write_text("x", encoding="ascii")
        done = self.run_script("switchon-03-check.ps1", scripts=bounded)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("more than 50,000 entries", self.said(done))
        self.assertIn("-gt 50000", text)                                              # the real script keeps the stated bound

    def test_a_linked_archive_parent_and_an_archive_outside_c_lw_or_without_zip_are_refused(self):
        real = self.tmp / "real-archives"
        real.mkdir()
        (real / "a.zip").write_bytes(b"PK")
        self.junction(self.tmp / "linked-archives", real)
        for archive in (str(self.tmp / "linked-archives" / "a.zip"), "D:\\elsewhere\\a.zip", str(real / "a.txt"), str(self.tmp / "missing.zip"),
                        str(real) + "\\..\\real-archives\\a.zip"):
            self.names(Archive=archive)
            done = self.run_script("switchon-02-unpack.ps1")
            self.assertNotEqual(done.returncode, 0, archive)
            self.assertFalse((self.live / "src").exists(), archive)                    # nothing was unpacked

    def step_1_env(self, python, archive, sha="b" * 64):
        return self.env(LW_SWITCHON_PYTHON=python, LW_SWITCHON_ARCHIVE=archive, LW_SWITCHON_SHA=sha)

    def test_step_1_refuses_a_linked_archive_a_bad_python_and_a_live_folder_that_exists(self):
        real = self.tmp / "real-archives"
        real.mkdir()
        (real / "a.zip").write_bytes(b"PK")
        self.junction(self.tmp / "linked-archives", real)
        for python, archive, sha in ((sys.executable, str(self.tmp / "linked-archives" / "a.zip"), "b" * 64), (str(self.tmp / "no.exe"), str(real / "a.zip"), "b" * 64),
                                     (sys.executable, "D:\\x\\a.zip", "b" * 64), (sys.executable, str(real / "a.zip"), "not a checksum")):
            done = self.run_script("switchon-01-setup.ps1", self.step_1_env(python, archive, sha))
            self.assertNotEqual(done.returncode, 0, (python, archive, sha))
            self.assertFalse(self.live.exists(), (python, archive))                    # nothing was made
        done = self.run_script("switchon-01-setup.ps1", self.step_1_env(sys.executable, str(real / "a.zip")))
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertEqual(sorted(p.name for p in self.live.iterdir()), [".owner.json", "journal", "names.json", "state", "store"])
        saved = json.loads((self.live / "names.json").read_text(encoding="ascii"))
        self.assertEqual(sorted(saved), sorted(["LiveRoot", "BusRoot", "Port", "Token", "Py", "Archive", "ExpectedSha", "MachineAlias", "ClaudeAlias",
                                                "CodexAlias", "ProjectAlias"]))
        again = self.run_script("switchon-01-setup.ps1", self.step_1_env(sys.executable, str(real / "a.zip")))
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already exists. Nothing was changed", self.said(again))

    def test_step_1_asks_three_plain_questions_and_nothing_is_edited(self):
        real = self.tmp / "real-archives"
        real.mkdir()
        (real / "a.zip").write_bytes(b"PK")
        text = (SCRIPTS / "switchon-01-setup.ps1").read_text(encoding="ascii")
        self.assertIsNone(re.search(r"'<[^']*>'", text))                                                  # no angle-bracket placeholder is left to edit
        answers = '"%s"\n"%s"\n%s\n' % (sys.executable, real / "a.zip", "c" * 64)                       # quote marks (Explorer's "Copy as path") are removed
        done = self.run_script("switchon-01-setup.ps1", stdin=answers, interactive=True)
        self.assertEqual(done.returncode, 0, self.said(done))
        for question in ("The full path of python.exe", "The full path of the archive", "The SHA-256 checksum"):
            self.assertEqual(text.count("Read-Host '" + question), 1, question)            # exactly one question each ...
        self.assertEqual(text.count("Read-Host"), 3)                                  # ... three questions, none of them masked
        saved = json.loads((self.live / "names.json").read_text(encoding="ascii"))
        self.assertEqual((saved["Py"], saved["Archive"], saved["ExpectedSha"]), (sys.executable, str(real / "a.zip"), "c" * 64))

    def test_a_privacy_failure_in_step_1_leaves_the_marker_and_names_json_and_step_15_removes_the_folder(self):
        real = self.tmp / "real-archives"
        real.mkdir()
        (real / "a.zip").write_bytes(b"PK")
        scripts = self.tmp / "scripts-private-fails"
        shutil.copytree(SCRIPTS, scripts)
        common = scripts / "switchon-common.ps1"
        common.write_text(common.read_text(encoding="ascii").replace("function Protect-Path {", "function Protect-Path {\n    throw 'simulated: the access list cannot be set'", 1),
                          encoding="ascii", newline="\n")
        done = self.run_script("switchon-01-setup.ps1", self.step_1_env(sys.executable, str(real / "a.zip")), scripts=scripts)
        self.assertNotEqual(done.returncode, 0)
        said = self.said(done)
        self.assertIn("was made but could not be made private. Nothing secret was written. Run step 15 to remove it, then tell the lead.", said)
        self.assertNotIn("nothing was changed", said.lower().replace("nothing secret was written", ""))     # the old, untrue sentence is gone
        self.assertEqual(sorted(p.name for p in self.live.iterdir()), [".owner.json", "names.json"])
        self.assertTrue(json.loads((self.live / ".owner.json").read_text(encoding="ascii"))["token"])
        again = self.run_script("switchon-15-rollback.ps1")                              # the real step 15, with the real privacy helpers
        self.assertEqual(again.returncode, 0, self.said(again))
        self.assertFalse(self.live.exists())

    def test_a_linked_marker_is_refused_by_the_rollback_and_nothing_is_deleted(self):
        self.private_live(config=False)
        elsewhere = self.tmp / "marker-target"
        elsewhere.mkdir()
        (self.live / "sentinel.txt").write_text("keep", encoding="ascii")
        self.junction(self.live / ".owner.json", elsewhere)
        done = self.run_script("switchon-15-rollback.ps1")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(".owner.json is a link. Nothing was changed", self.said(done))
        self.assertTrue((self.live / "sentinel.txt").exists())

    def test_the_rollback_deletes_a_plain_folder_with_its_marker_and_nothing_else(self):
        self.private_live(config=False)
        (self.live / ".owner.json").write_text(json.dumps({"token": "0123456789abcdef" * 2, "path": str(self.live)}), encoding="ascii")
        (self.live / "sub").mkdir()
        (self.live / "sub" / "x.txt").write_text("x", encoding="ascii")
        done = self.run_script("switchon-15-rollback.ps1")
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertFalse(self.live.exists())
        self.assertTrue((self.bus / "state" / "seat-a.capacity.json").exists())

    def test_the_rollback_refuses_a_foreign_marker_and_a_link_inside(self):
        self.private_live(config=False)
        (self.live / ".owner.json").write_text(json.dumps({"token": "f" * 32, "path": str(self.live)}), encoding="ascii")
        done = self.run_script("switchon-15-rollback.ps1")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("not the folder this runbook made", self.said(done))
        (self.live / ".owner.json").write_text(json.dumps({"token": "0123456789abcdef" * 2, "path": str(self.live)}), encoding="ascii")
        target = self.tmp / "inner-target"
        target.mkdir()
        (target / "precious.txt").write_text("keep", encoding="ascii")
        self.junction(self.live / "inner-link", target)
        done = self.run_script("switchon-15-rollback.ps1")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("inner-link is a link. Nothing was changed", self.said(done))      # the entry guard finds it first; the rollback's own look is the second line of defence
        self.assertTrue((target / "precious.txt").exists())


class RehearsalMode(Base):
    def refuses(self, env, word, script="switchon-03-check.ps1"):
        done = self.run_script(script, env)
        self.assertNotEqual(done.returncode, 0, word)
        self.assertIn(word, self.said(done))
        self.assertIn("Nothing was changed", self.said(done))

    def test_a_partial_set_of_settings_is_refused(self):
        for missing in ("LW_SWITCHON_ROOT", "LW_SWITCHON_BUS", "LW_SWITCHON_PORT", "LW_SWITCHON_INPUT"):
            self.refuses(self.env(**{missing: None}), "needs all of its settings and %s is missing" % missing)

    def test_every_rehearsal_variable_stops_the_operators_run_when_the_switch_is_not_set(self):
        for name, value in (("LW_SWITCHON_ROOT", str(self.live)), ("LW_SWITCHON_BUS", str(self.bus)), ("LW_SWITCHON_PORT", PORT), ("LW_SWITCHON_INPUT", "prompt"),
                            ("LW_SWITCHON_CLAUDE_NAME", LOUD), ("LW_SWITCHON_CLAUDE_HOME", "aaaaaaaa"), ("LW_SWITCHON_CODEX_NAME", LOUD), ("LW_SWITCHON_CODEX_HOME", "bbbbbbbb"),
                            ("LW_SWITCHON_PYTHON", sys.executable), ("LW_SWITCHON_ARCHIVE", "C:\\lw\\x.zip"), ("LW_SWITCHON_SHA", "a" * 64), ("LW_SWITCHON_REHEARSAL", "no"),
                            ("LW_SWITCHON_REHEARSAL", "1"), ("LW_SWITCHON_REHEARSAL", "YES")):
            env = self.env(LW_SWITCHON_REHEARSAL=None, LW_SWITCHON_ROOT=None, LW_SWITCHON_BUS=None, LW_SWITCHON_PORT=None, LW_SWITCHON_INPUT=None)
            env[name] = value
            self.refuses(env, "A rehearsal setting (LW_SWITCHON_...) is present in this window")
            done = self.run_script("switchon-03-check.ps1", env)
            self.assertNotIn(LOUD, self.said(done))

    def test_without_any_rehearsal_variable_the_operator_defaults_are_chosen_without_touching_the_file_system(self):
        # Only the guard's final `else` branch (plain assignments) is run; the real default folders are never opened, listed or tested.
        guard = (SCRIPTS / "switchon-guard.ps1").read_text(encoding="ascii")
        branch = re.search(r"\nelse \{\n(.*?)\n\}\n", guard, re.S).group(1)
        self.assertNotRegex(branch, r"Test-Path|Get-|Assert-|Set-|New-|Remove-|Join-Path")
        env = self.env(LW_SWITCHON_REHEARSAL=None, LW_SWITCHON_ROOT=None, LW_SWITCHON_BUS=None, LW_SWITCHON_PORT=None, LW_SWITCHON_INPUT=None)
        done = self.command("%s; $LiveRoot; $BusRoot; $PortNumber" % branch, env)
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertEqual(done.stdout.split(), ["C:\\lw\\live", "C:\\lw\\team\\.agenttalk", "8443"])

    def test_a_start_without_any_rehearsal_variable_stops_on_a_stray_one_before_it_opens_any_folder(self):
        env = self.env(LW_SWITCHON_REHEARSAL=None, LW_SWITCHON_ROOT=None, LW_SWITCHON_BUS=None, LW_SWITCHON_PORT=None, LW_SWITCHON_INPUT=None,
                       LW_SWITCHON_CLAUDE_NAME="x")
        self.refuses(env, "A rehearsal setting (LW_SWITCHON_...) is present in this window")

    def test_the_two_roots_may_not_overlap(self):
        nested = self.live / "nested-bus"
        (nested / "state").mkdir(parents=True)
        inside = self.bus / "live-inside"
        inside.mkdir()
        for over in ({"LW_SWITCHON_BUS": str(nested)}, {"LW_SWITCHON_BUS": str(self.live)}, {"LW_SWITCHON_ROOT": str(inside)},
                     {"LW_SWITCHON_ROOT": str(self.bus)}, {"LW_SWITCHON_ROOT": str(self.live).upper(), "LW_SWITCHON_BUS": str(self.live).lower()}):
            self.refuses(self.env(**over), "the same, or one lies inside the other")

    def test_a_nested_bus_stops_the_rollback_before_it_deletes_anything(self):
        nested = self.live / "nested-bus"
        (nested / "state").mkdir(parents=True)
        (nested / "state" / "seat.capacity.json").write_text("{}", encoding="ascii")
        self.names(BusRoot=str(nested))
        (self.live / ".owner.json").write_text(json.dumps({"token": "0123456789abcdef" * 2, "path": str(self.live)}), encoding="ascii")
        done = self.run_script("switchon-15-rollback.ps1", self.env(LW_SWITCHON_BUS=str(nested)))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("one lies inside the other", self.said(done))
        self.assertTrue((nested / "state" / "seat.capacity.json").exists())

    def test_the_roots_must_lie_inside_the_scratch_folder(self):
        self.refuses(self.env(LW_SWITCHON_ROOT="C:\\lw\\not-scratch\\live"), "is not inside C:\\lw\\scratch")
        self.refuses(self.env(LW_SWITCHON_BUS="C:\\lw\\team\\.agenttalk"), "is not inside C:\\lw\\scratch")

    def test_the_input_setting_must_agree_with_the_test_texts(self):
        self.refuses(self.env(LW_SWITCHON_INPUT="env"), 'needs all four test parts')
        self.refuses(self.env(LW_SWITCHON_INPUT="env", LW_SWITCHON_CLAUDE_NAME=LOUD, LW_SWITCHON_CLAUDE_HOME="aaaaaaaa"), 'needs all four test parts')
        self.refuses(self.env(**dict(PARTS, LW_SWITCHON_INPUT="prompt")), 'must not set the test parts')
        self.refuses(self.env(LW_SWITCHON_PYTHON=sys.executable), 'all set or none is')
        self.refuses(self.env(LW_SWITCHON_PYTHON=sys.executable, LW_SWITCHON_ARCHIVE="C:\\lw\\x.zip"), 'all set or none is')
        self.refuses(self.env(LW_SWITCHON_INPUT="maybe"), 'must be "env" or "prompt"')
        done = self.run_script("switchon-03-check.ps1", self.env(**dict(PARTS, LW_SWITCHON_CODEX_HOME="x")))
        self.assertNotIn(LOUD, self.said(done))

    def test_the_port_must_be_a_rehearsal_port(self):
        for port in ("8443", "80", "70000", "abc", "18455.5", "-1"):
            self.refuses(self.env(LW_SWITCHON_PORT=port), "rehearsal port")

    def test_a_complete_rehearsal_is_accepted(self):
        self.names()
        done = self.run_script("switchon-03-check.ps1")
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertIn("2 usage file(s) found", done.stdout)
        self.assertIn("claude: 1 usable usage file(s)", done.stdout)
        self.assertIn("codex: 1 usable usage file(s)", done.stdout)
        self.assertNotIn(LOUD, done.stdout + done.stderr)                              # counted, never shown


class Prompt(Base):
    """The real interactive branch of step 8, in a pseudo console, with loud markers."""

    def run_in_console(self, env, transcript=None):
        from tests.switchon_console import run_in_console
        pwsh = shutil.which("pwsh")
        inner = "& '%s'" % (SCRIPTS / "switchon-08-table.ps1")
        if transcript:
            inner = "Start-Transcript -Path '%s' -Force | Out-Null; try { %s } finally { Stop-Transcript | Out-Null }" % (transcript, inner)
        line = '"%s" -NoProfile -Command "%s"' % (pwsh, inner)
        return run_in_console(line, [LOUD + "\r", "aaaaaaaa\r", LOUD + "\r", "home-bbbbbbbb\r"], env=env, cwd=str(ROOT), wait_seconds=60)

    def test_what_is_typed_is_never_shown_or_recorded(self):
        self.private_live()
        transcript = self.tmp / "transcript.txt"
        code, shown = self.run_in_console(self.env(), transcript)
        self.assertEqual(code, 0, ANSI.sub("", shown))
        self.assertIn("two rows written", shown)
        self.assertIn("Claude: matches 1 usage file(s)", shown)
        self.assertIn("Codex: matches 1 usage file(s)", shown)
        self.assertNotIn(LOUD, shown)                                          # not on the screen the operator sees
        self.assertNotIn(LOUD, transcript.read_text(encoding="utf-8", errors="replace"))      # and not in a transcript
        self.assertIn(LOUD, (self.live / "reporter.toml").read_text(encoding="utf-8"))        # it is in the private settings file, and only there
        for path in self.tmp.rglob("*"):
            if path.is_file() and path.name not in ("reporter.toml", "capacity.json") and path != self.live / "reporter.toml":
                if path.name.endswith(".capacity.json"):
                    continue
                self.assertNotIn(LOUD, path.read_text(encoding="utf-8", errors="replace"), str(path))

    def test_a_wrong_shape_typed_at_the_masked_prompt_is_refused_without_showing_it(self):
        from tests.switchon_console import run_in_console
        self.private_live()
        before = self.digest(self.live / "reporter.toml")
        line = '"%s" -NoProfile -Command "& \'%s\'"' % (shutil.which("pwsh"), SCRIPTS / "switchon-08-table.ps1")
        code, shown = run_in_console(line, [LOUD + "\r", "ZZZZ\r", LOUD + "\r", "bbbbbbbb\r"], env=self.env(), cwd=str(ROOT), wait_seconds=60)
        self.assertNotEqual(code, 0)
        self.assertIn("The Claude home part must be exactly 8 characters", shown)
        self.assertNotIn(LOUD, shown)
        self.assertEqual(self.digest(self.live / "reporter.toml"), before)

    def test_the_console_helper_gives_back_every_handle_even_when_the_child_has_to_be_stopped(self):
        import ctypes
        import ctypes.wintypes as wt
        from tests.switchon_console import k32, run_in_console
        k32.GetCurrentProcess.restype = wt.HANDLE
        k32.GetProcessHandleCount.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]

        def handles():
            count = wt.DWORD()
            self.assertTrue(k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(count)))
            return count.value
        cmd = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        run_in_console('"%s" /c echo warm-up' % cmd, [], first_delay=0.5, wait_seconds=20)
        before = handles()
        for _ in range(3):
            code, shown = run_in_console('"%s" /c echo done' % cmd, [], first_delay=0.5, wait_seconds=20)
            self.assertEqual(code, 0)
            self.assertIn("done", shown)
        code, _ = run_in_console('"%s" /c ping -n 8 127.0.0.1 > nul' % cmd, [], first_delay=0.5, wait_seconds=1)       # outlives the wait: stopped by its own handle
        self.assertEqual(code, 1)
        self.assertLessEqual(handles(), before + 2)                                    # four runs, no leak of the six handles each one opens

    def test_the_env_branch_exists_only_for_the_rehearsal_and_writes_the_same_rows(self):
        self.private_live()
        done = self.run_script("switchon-08-table.ps1", self.env(**PARTS))
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertNotIn(LOUD, done.stdout + done.stderr)
        config = (self.live / "reporter.toml").read_text(encoding="utf-8")
        self.assertEqual(config.count("[[readings.sources]]"), 2)
        self.assertIn('account = "claude-main"', config)
        again = self.run_script("switchon-08-table.ps1", self.env(**PARTS))
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already in the settings file", self.said(again))


class PrivacyBeforeWriting(Base):
    ENV_TEXTS = PARTS

    def test_step_8_refuses_an_open_settings_file_before_asking_and_changes_no_byte(self):
        self.private_live()
        config = self.live / "reporter.toml"
        self.open_to_everyone(config)
        before = self.digest(config)
        done = self.run_script("switchon-08-table.ps1", self.env(**self.ENV_TEXTS))
        self.assertNotEqual(done.returncode, 0)
        said = self.said(done)
        self.assertIn("Someone else can open", said)
        self.assertIn("nothing secret has been written, and nothing was changed", said)
        self.assertNotIn("ALREADY BEEN WRITTEN", said)
        self.assertEqual(self.digest(config), before)                                 # not one byte
        self.assertNotIn(LOUD, done.stdout + done.stderr)

    def test_step_8_refuses_an_open_live_folder_before_it_asks_for_anything(self):
        self.private_live()
        self.open_to_everyone(self.live)
        before = self.digest(self.live / "reporter.toml")
        done = self.run_script("switchon-08-table.ps1", self.env(**self.ENV_TEXTS))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("nothing secret has been written", self.said(done))
        self.assertEqual(self.digest(self.live / "reporter.toml"), before)

    def test_step_7_refuses_an_open_store_before_the_identity_exists_and_creates_nothing(self):
        self.private_live(config=False)
        (self.live / "store").mkdir()
        store = self.live / "store" / "inventory.db"
        store.write_bytes(b"not a database")
        self.command(". '%s'; Protect-Path (Join-Path $LiveRoot 'store') $true" % COMMON)
        self.open_to_everyone(store)
        before = self.digest(store)
        done = self.run_script("switchon-07-settings.ps1")
        self.assertNotEqual(done.returncode, 0)
        said = self.said(done)
        self.assertIn("nothing secret has been written, and nothing was changed", said)
        self.assertNotIn("identity-set failed", said)                                  # it never got as far as enrolling
        self.assertFalse((self.live / "reporter.toml").exists())                       # not even an empty settings file
        self.assertEqual(self.digest(store), before)

    def test_a_failure_after_a_write_says_so_in_different_words(self):
        self.private_live()
        config = self.live / "reporter.toml"
        self.open_to_everyone(config)
        after = self.command(". '%s'; Assert-PrivateAfter $ConfigPath $true" % COMMON)
        before = self.command(". '%s'; Assert-Private $ConfigPath $true" % COMMON)
        self.assertIn("A secret HAS ALREADY BEEN WRITTEN there", self.said(after))
        self.assertNotIn("nothing secret has been written", self.said(after))
        self.assertIn("nothing secret has been written", self.said(before))
        self.assertNotIn("ALREADY BEEN WRITTEN", self.said(before))


LISTENER = ("import socket, sys, time\n"
            "family = socket.AF_INET6 if ':' in sys.argv[1] else socket.AF_INET\n"
            "s = socket.socket(family)\n"
            "s.bind((sys.argv[1], int(sys.argv[2])))\n"
            "s.listen(5)\n"
            "time.sleep(120)\n")
SLEEPER = "import time; time.sleep(120)"


class Processes(Base):
    """Real processes started by a test (and only those), stopped by exact process id in the cleanup."""

    def start(self, args, cwd=None, env=None):
        process = subprocess.Popen(args, cwd=cwd, env=env or {k: v for k, v in os.environ.items() if not k.startswith("LW_SWITCHON_")},
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop, process)
        return process

    @staticmethod
    def stop(process):
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)       # this process and its children, by exact id
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            pass

    def listen(self, host="127.0.0.1", port=int(PORT)):
        import socket
        process = self.start([sys.executable, "-E", "-s", "-B", "-c", LISTENER, host, str(port)])
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                socket.create_connection((host, port), timeout=1).close()
                return process
            except OSError:
                time.sleep(0.2)
        self.fail("the test listener did not start")

    def our_server(self):
        """Window 1 as step 10 starts it: the real Leasewise server, started by step 1's Python from the source folder."""
        self.install_source()
        (self.live / "store").mkdir(exist_ok=True)
        store = self.live / "store" / "inventory.db"
        init = subprocess.run([sys.executable, "-E", "-s", "-B", "-m", "quota_balancer", "--db", str(store), "init"], cwd=self.live / "src", capture_output=True, text=True)
        self.assertEqual(init.returncode, 0, init.stderr)
        process = self.start([sys.executable, "-E", "-s", "-B", "-m", "quota_balancer", "--db", str(store), "serve", "--host", "127.0.0.1", "--port", PORT],
                             cwd=self.live / "src")
        import socket
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", int(PORT)), timeout=1).close()
                return process, store
            except OSError:
                time.sleep(0.3)
        self.fail("the Leasewise server did not start")

    def check_function(self, text):
        return self.command(". '%s'; %s" % (COMMON, text))


class Step8Parts(Base):
    def run8(self, **parts):
        return self.run_script("switchon-08-table.ps1", self.env(**dict(PARTS, **parts)))

    def test_four_parts_are_put_together_and_checked_against_our_usage_files(self):
        self.private_live()
        done = self.run8()
        self.assertEqual(done.returncode, 0, self.said(done))
        config = (self.live / "reporter.toml").read_text(encoding="utf-8")
        self.assertIn('agenttalk_account = "%s"' % LOUD_CLAUDE, config)                 # claude:<name>:home-<8>, put together by the script
        self.assertIn('agenttalk_account = "%s"' % LOUD_CODEX, config)                  # the home- in front was optional
        self.assertIn("Claude: matches 1 usage file(s)", done.stdout)
        self.assertIn("Codex: matches 1 usage file(s)", done.stdout)
        self.assertNotIn(LOUD, done.stdout + done.stderr)

    def test_a_wrong_part_is_refused_without_showing_it(self):
        self.private_live()
        before = self.digest(self.live / "reporter.toml")
        cases = [dict(LW_SWITCHON_CLAUDE_NAME="x"), dict(LW_SWITCHON_CLAUDE_NAME=LOUD + ":x"), dict(LW_SWITCHON_CLAUDE_NAME=" " + LOUD),
                 dict(LW_SWITCHON_CLAUDE_NAME=LOUD + " "), dict(LW_SWITCHON_CLAUDE_NAME=LOUD + "\t"), dict(LW_SWITCHON_CLAUDE_NAME="x" * 257),
                 dict(LW_SWITCHON_CODEX_NAME=LOUD + ":x"), dict(LW_SWITCHON_CLAUDE_HOME="aaaaaaa"), dict(LW_SWITCHON_CLAUDE_HOME="AAAAAAAA"),
                 dict(LW_SWITCHON_CLAUDE_HOME="home-aaaaaaa"), dict(LW_SWITCHON_CLAUDE_HOME="aaaaaaaaa"), dict(LW_SWITCHON_CLAUDE_HOME="gggggggg"),
                 dict(LW_SWITCHON_CLAUDE_HOME="home-home-aaaaaaaa"), dict(LW_SWITCHON_CODEX_HOME="bbbbbbbb "), dict(LW_SWITCHON_CODEX_HOME=LOUD)]
        for case in cases:
            done = self.run8(**case)
            self.assertNotEqual(done.returncode, 0, case)
            said = self.said(done)
            self.assertIn("Nothing was changed. Run step 8 again.", said, case)
            self.assertRegex(said, r"The (Claude|Codex) (name is not in the allowed form|home part must be exactly 8)", case)
            self.assertNotIn(LOUD, said, case)                                          # which part is wrong, never what was typed
            self.assertEqual(self.digest(self.live / "reporter.toml"), before, case)

    def test_a_typo_that_matches_no_usage_file_is_refused_and_nothing_is_written(self):
        self.private_live()
        before = self.digest(self.live / "reporter.toml")
        for case, who in ((dict(LW_SWITCHON_CLAUDE_NAME=LOUD + "X"), "Claude"), (dict(LW_SWITCHON_CLAUDE_HOME="aaaaaaab"), "Claude"),
                          (dict(LW_SWITCHON_CODEX_HOME="aaaaaaaa"), "Codex"), (dict(LW_SWITCHON_CODEX_NAME="other-user"), "Codex")):
            done = self.run8(**case)
            self.assertNotEqual(done.returncode, 0, case)
            said = self.said(done)
            self.assertIn("The %s text you typed does not match any of our usage files. Nothing was changed. Compare with your agenttalk capacity screen and run step 8 again." % who, said)
            self.assertNotIn(LOUD, said)
            self.assertEqual(self.digest(self.live / "reporter.toml"), before, case)

    def test_the_usage_files_that_count_are_those_the_reporter_could_use(self):
        self.private_live()
        (self.bus / "state" / "seat-a.capacity.json").unlink()
        self.usage("seat-old", LOUD_CLAUDE, schema_version=1)                            # an old format does not count as a match
        done = self.run8()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("The Claude text you typed does not match any of our usage files", self.said(done))
        self.usage("seat-a", LOUD_CLAUDE)
        self.assertEqual(self.run8().returncode, 0)


class UsageFormat(Base):
    def step_3(self):
        return self.run_script("switchon-03-check.ps1")

    def refused(self, provider):
        done = self.step_3()
        self.assertNotEqual(done.returncode, 0)
        said = self.said(done)
        self.assertIn("No usable %s usage file was found." % provider, said)
        self.assertIn("Every seat must run agenttalk 0.99.0 or newer", said)
        self.assertIn("a later agenttalk may write another format: tell the lead", said)
        self.assertIn("Nothing was changed", said)
        self.assertNotIn(LOUD, done.stdout + done.stderr)

    def setUp(self):
        super().setUp()
        self.names()

    def test_a_good_pair_of_files_passes_and_only_counts_are_shown(self):
        done = self.step_3()
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertIn("claude: 1 usable usage file(s)", done.stdout)
        self.assertIn("codex: 1 usable usage file(s)", done.stdout)
        self.assertNotIn(LOUD, done.stdout + done.stderr)

    def test_old_format_files_only_are_refused(self):
        self.usage("seat-a", LOUD_CLAUDE, schema_version=1)
        self.usage("seat-b", LOUD_CODEX, schema_version=1)
        self.refused("claude")

    def test_one_provider_missing_is_refused_for_that_provider(self):
        (self.bus / "state" / "seat-b.capacity.json").unlink()
        self.refused("codex")
        self.usage("seat-b", LOUD_CODEX)
        (self.bus / "state" / "seat-a.capacity.json").unlink()
        self.refused("claude")

    def test_each_part_of_a_usable_file_is_checked(self):
        for over in (dict(schema_version=3), dict(schema_version=2.0), dict(schema_version="2"), dict(schema_version=True), dict(schema_version=None),
                     dict(scope="seat"), dict(scope=None), dict(account=None), dict(account=""), dict(account="claude:"), dict(account=7), dict(account="gemini:x:home-aaaaaaaa"),
                     dict(account="CLAUDE:x:home-aaaaaaaa")):
            self.usage("seat-a", LOUD_CLAUDE, **over)
            self.refused("claude")
        for text in ("not json", "[]", "null", '"text"', "{}"):
            (self.bus / "state" / "seat-a.capacity.json").write_text(text, encoding="ascii")
            self.refused("claude")

    def test_a_file_over_16_kib_a_link_and_a_65th_file_are_ignored_as_the_reporter_ignores_them(self):
        state = self.bus / "state"
        (state / "seat-a.capacity.json").unlink()
        self.usage("seat-a", LOUD_CLAUDE, filler="x" * 17000)                           # over 16 KiB
        self.refused("claude")
        (state / "seat-a.capacity.json").unlink()
        real = self.tmp / "real-seat.json"
        real.write_text(json.dumps({"schema_version": 2, "scope": "account", "account": LOUD_CLAUDE}), encoding="ascii")
        self.junction(state / "seat-link.capacity.json", self.tmp)                      # a link named like a usage file
        self.refused("claude")
        os.rmdir(state / "seat-link.capacity.json")
        self.links.remove(state / "seat-link.capacity.json")
        for number in range(64):
            self.usage("a%02d" % number, "gemini:x:home-aaaaaaaa")                       # 64 files that sort first and are not usable
        self.usage("z-last", LOUD_CLAUDE)                                               # the 66th by name: the reporter reads the first 64 only
        self.refused("claude")


class PortChecks(Processes):
    def setUp(self):
        super().setUp()
        self.names()
        self.install_source()
        (self.live / "store").mkdir(exist_ok=True)

    def trace_python(self):
        trace = self.tmp / "python-calls.txt"
        shim = self.tmp / "tracer.ps1"
        shim.write_text('[IO.File]::AppendAllText("%s", ($args -join " ") + "`n")\nexit 0\n' % trace, encoding="ascii")
        self.default_over = {"Py": str(shim)}
        self.names()
        return trace

    def test_step_10_starts_the_server_only_when_nothing_listens_on_the_port(self):
        trace = self.trace_python()
        done = self.run_script("switchon-10-server.ps1")
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertEqual(trace.read_text(encoding="ascii").strip(),
                         "-E -s -B -m quota_balancer --db %s serve --host 127.0.0.1 --port %s" % (self.live / "store" / "inventory.db", PORT))

    def test_step_10_is_refused_while_anything_listens_on_the_port(self):
        trace = self.trace_python()
        holder = self.listen()
        done = self.run_script("switchon-10-server.ps1")
        self.assertNotEqual(done.returncode, 0)
        said = self.said(done)
        self.assertIn("Something on this machine already listens on port %s (process " % PORT, said)
        self.assertRegex(said, r"\(process \d+, program python\.exe\)")
        self.assertIn("Nothing was started. If it is window 1 from earlier, go back to that window; otherwise tell the lead.", said)
        self.assertFalse(trace.exists())                                                # the server was never started
        self.assertIsNone(holder.poll())

    def test_step_10_is_refused_for_a_listener_on_any_address_including_ipv6(self):
        trace = self.trace_python()
        for host in ("0.0.0.0", "::1", "::"):
            try:
                holder = self.listen(host)
            except (OSError, AssertionError):
                continue                                                                # this machine cannot listen there
            done = self.run_script("switchon-10-server.ps1")
            self.assertNotEqual(done.returncode, 0, host)
            self.assertIn("already listens on port", self.said(done), host)
            self.assertFalse(trace.exists(), host)
            self.stop(holder)

    def test_step_10_stops_when_the_list_of_listeners_cannot_be_read(self):
        trace = self.trace_python()
        scripts = self.tmp / "scripts-no-list"
        shutil.copytree(SCRIPTS, scripts)
        common = scripts / "switchon-common.ps1"
        common.write_text(common.read_text(encoding="ascii").replace("Get-NetTCPConnection -State Listen", "Get-NetTCPConnectionBroken -State Listen"), encoding="ascii", newline="\n")
        done = self.run_script("switchon-10-server.ps1", scripts=scripts)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("could not be read. Nothing was started. Tell the lead.", self.said(done))
        self.assertFalse(trace.exists())

    def test_step_11_refuses_when_nothing_listens(self):
        trace = self.trace_python()
        done = self.run_script("switchon-11-reporter.ps1")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Nothing listens on port %s yet. Start window 1 (step 10) first" % PORT, self.said(done))
        self.assertFalse(trace.exists())

    def test_step_11_refuses_a_listener_that_is_not_window_1s_server_and_sends_nothing(self):
        trace = self.trace_python()
        self.listen()
        done = self.run_script("switchon-11-reporter.ps1")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Port %s is held by a program that is not window 1's Leasewise server. Nothing was started, and the identity value was not sent. Tell the lead." % PORT,
                      self.said(done))
        self.assertFalse(trace.exists())                                                # the reporter (which carries the identity value) never started

    def test_step_11_refuses_a_listener_with_the_right_words_but_another_store_or_another_address(self):
        trace = self.trace_python()
        other = self.tmp / "other.db"
        for host, store in (("127.0.0.1", other), ("0.0.0.0", self.live / "store" / "inventory.db")):
            holder = self.start([sys.executable, "-E", "-s", "-B", "-c", LISTENER, host, PORT, "-m", "quota_balancer", "--db", str(store), "serve"])
            import socket
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    socket.create_connection(("127.0.0.1", int(PORT)), timeout=1).close()
                    break
                except OSError:
                    time.sleep(0.2)
            done = self.run_script("switchon-11-reporter.ps1")
            self.assertNotEqual(done.returncode, 0, (host, store))
            self.assertIn("is not window 1's Leasewise server", self.said(done), (host, store))
            self.assertFalse(trace.exists())
            self.stop(holder)

    def test_our_own_server_is_accepted_including_through_the_venv_launcher(self):
        server, store = self.our_server()
        owner = self.command("$o = (Get-NetTCPConnection -State Listen -LocalPort %s).OwningProcess; $o; (Get-CimInstance Win32_Process | Where-Object ProcessId -eq $o).ParentProcessId" % PORT)
        owner_id, owner_parent = (int(word) for word in owner.stdout.split())
        done = self.check_function("Assert-OurServerListens; 'accepted'")
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertIn("accepted", done.stdout)
        # Either Python owns the socket itself, or (a venv's python.exe is a launcher) a child owns it and the launcher is its parent: both were accepted above.
        self.assertTrue(owner_id == server.pid or owner_parent == server.pid, (owner_id, owner_parent, server.pid))

    def test_our_own_server_is_refused_when_step_1s_python_is_another_program(self):
        self.our_server()
        other = shutil.which("pwsh")
        self.default_over = {"Py": other}
        self.names()
        done = self.check_function("Assert-OurServerListens; 'accepted'")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("is not window 1's Leasewise server", self.said(done))

    def test_a_second_reporter_for_the_same_settings_is_refused_and_another_settings_file_is_not(self):
        trace = self.trace_python()
        config = self.live / "reporter.toml"
        self.start([sys.executable, "-E", "-s", "-B", "-c", SLEEPER, "-m", "quota_balancer", "reporter", "run", "--config", str(self.tmp / "elsewhere.toml")])
        self.assertEqual(self.check_function("Assert-NoReporterRunning; 'free'").returncode, 0)
        self.start([sys.executable, "-E", "-s", "-B", "-c", SLEEPER, "-m", "quota_balancer", "reporter", "run", "--config", str(config)])
        time.sleep(1)
        done = self.check_function("Assert-NoReporterRunning; 'free'")
        self.assertNotEqual(done.returncode, 0)
        self.assertRegex(self.said(done), r"A reporter for this settings file is already running \(process \d+\)\. Nothing was started\.")
        self.assertFalse(trace.exists())


class Step5Again(Base):
    def test_a_second_run_is_refused_and_the_store_is_unchanged(self):
        self.private_live(config=False)
        self.install_source()
        (self.live / "store").mkdir()
        self.assertEqual(self.run_script("switchon-04-store.ps1").returncode, 0)
        first = self.run_script("switchon-05-accounts.ps1")
        self.assertEqual(first.returncode, 0, self.said(first))
        before = self.digest(self.live / "store" / "inventory.db")
        again = self.run_script("switchon-05-accounts.ps1")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("Step 5 was already done. Nothing was changed. Go on with step 6.", self.said(again))
        self.assertEqual(self.digest(self.live / "store" / "inventory.db"), before)


class Step15Safety(Processes):
    def marker(self, folder=None):
        folder = folder or self.live
        (folder / ".owner.json").write_text(json.dumps({"token": "0123456789abcdef" * 2, "path": str(folder)}), encoding="ascii")

    def test_a_folder_with_only_the_marker_and_no_names_json_is_removed(self):
        self.live.mkdir()
        self.marker()
        done = self.run_script("switchon-15-rollback.ps1")
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertFalse(self.live.exists())
        self.assertTrue((self.bus / "state").exists())

    def test_a_folder_without_names_json_that_holds_anything_else_is_refused(self):
        self.live.mkdir()
        self.marker()
        (self.live / "store").mkdir()
        (self.live / "store" / "keep.txt").write_text("x", encoding="ascii")
        done = self.run_script("switchon-15-rollback.ps1")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("names.json is missing and the folder holds more than the ownership marker", self.said(done))
        self.assertIn("Nothing was deleted", self.said(done))
        self.assertTrue((self.live / "store" / "keep.txt").exists())
        (self.live / "store" / "keep.txt").unlink()
        (self.live / "store").rmdir()
        (self.live / ".owner.json").write_text(json.dumps({"token": "0123456789abcdef" * 2, "path": str(self.tmp / "elsewhere")}), encoding="ascii")
        self.assertNotEqual(self.run_script("switchon-15-rollback.ps1").returncode, 0)    # a marker that names another folder
        (self.live / ".owner.json").unlink()
        done = self.run_script("switchon-15-rollback.ps1")                                # no marker at all
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("holds no ownership marker", self.said(done))
        self.assertTrue(self.live.exists())

    def test_a_running_window_stops_the_rollback_before_anything_is_deleted(self):
        self.private_live(config=False)
        self.marker()
        (self.live / "keep.txt").write_text("x", encoding="ascii")
        runner = self.start([sys.executable, "-E", "-s", "-B", "-c", SLEEPER, "--db", str(self.live / "store" / "inventory.db")])
        time.sleep(1)
        done = self.run_script("switchon-15-rollback.ps1")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Window 1 or 2 is still running (process ", self.said(done))
        self.assertIn("Press Ctrl-C in it, then run step 15 again. Nothing was deleted.", self.said(done))
        self.assertTrue((self.live / "keep.txt").exists())
        self.assertTrue((self.live / ".owner.json").exists())
        self.stop(runner)
        done = self.run_script("switchon-15-rollback.ps1")
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertFalse(self.live.exists())

    def test_a_locked_file_leaves_the_marker_and_names_json_and_a_second_run_succeeds(self):
        self.private_live(config=False)
        self.marker()
        (self.live / "store").mkdir()
        (self.live / "store" / "free.txt").write_text("x", encoding="ascii")
        locked = self.live / "store" / "locked.db"
        locked.write_text("x", encoding="ascii")
        handle = open(locked, "r+b")                                                    # Windows will not delete a file that is open like this
        try:
            done = self.run_script("switchon-15-rollback.ps1")
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("A file could not be deleted (is a program still using it?)", self.said(done))
            self.assertTrue((self.live / ".owner.json").exists())                         # still there, so every check works again
            self.assertTrue((self.live / "names.json").exists())
        finally:
            handle.close()
        done = self.run_script("switchon-15-rollback.ps1")
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertFalse(self.live.exists())

    def test_step_14_shows_only_processes_that_name_the_live_folder_with_number_and_program(self):
        self.private_live(config=False)
        inside = self.start([sys.executable, "-E", "-s", "-B", "-c", SLEEPER, str(self.live / "store" / "x")])
        outside = self.start([sys.executable, "-E", "-s", "-B", "-c", SLEEPER, str(self.tmp / "elsewhere")])
        time.sleep(1)
        done = self.run_script("switchon-14-stopped.ps1")
        self.assertEqual(done.returncode, 0, self.said(done))
        listed = re.findall(r"^\s*(\d+)\s+(\S+)\s*$", done.stdout, re.M)
        numbers = [number for number, _ in listed]
        self.assertIn(str(inside.pid), numbers)                                         # (a venv's launcher and its child both name the folder)
        self.assertNotIn(str(outside.pid), numbers)
        self.assertTrue(all(name.lower().startswith("python") for _, name in listed), listed)
        self.assertNotIn(str(self.tmp / "elsewhere"), done.stdout)


class PythonWithoutOutsideInfluence(Base):
    def fake_path(self):
        fake = self.tmp / "fake-python-path"
        (fake / "quota_balancer").mkdir(parents=True)
        trace = self.tmp / "fake-imported.txt"
        for module in (fake / "quota_balancer" / "__init__.py", fake / "quota_balancer" / "__main__.py", fake / "sqlite3.py"):
            module.write_text("open(r'%s', 'a').write('imported')\n" % trace, encoding="ascii")
        return fake, trace

    def test_a_fake_package_and_a_fake_standard_module_on_pythonpath_are_not_imported(self):
        fake, trace = self.fake_path()
        self.private_live(config=False)
        (self.live / "store").mkdir()
        env = self.env(PYTHONPATH=str(fake))
        done = self.run_script("switchon-04-store.ps1", env)                              # no quota_balancer in the source folder: only the fake could answer
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse(trace.exists(), "the fake package was imported")
        self.install_source()
        self.assertEqual(self.run_script("switchon-04-store.ps1", env).returncode, 0)
        self.assertEqual(self.run_script("switchon-06-check-accounts.ps1", env).returncode, 1)     # the real sqlite3 ran: the store has no accounts, the answer is "NOT an allowed reporter"
        self.assertFalse(trace.exists(), "the fake standard module was imported")

    def test_no_bytecode_files_are_written_and_the_user_site_is_off(self):
        self.private_live(config=False)
        (self.live / "store").mkdir()
        self.install_source()
        self.assertEqual(self.run_script("switchon-04-store.ps1").returncode, 0)
        self.assertEqual(list((self.live / "src").rglob("__pycache__")), [])
        done = self.command(". '%s'; & $Py @PyFlags -c 'import sys, site; print(site.ENABLE_USER_SITE, sys.flags.ignore_environment, sys.flags.no_user_site, sys.dont_write_bytecode)'" % COMMON)
        self.assertEqual(done.stdout.split(), ["False", "1", "1", "True"])


class DoneCheck(Base):
    """Steps 12 and 13 against a real sqlite file and a status shim; the wait is set to zero in a COPY of the script (the real one waits ten minutes).

    Times are real: the baseline's clock time is ten minutes ago, the newest delivered figures are older than that, and a new figure is one observed later.
    """

    def setUp(self):
        super().setUp()
        self.default_over = {"Py": str(self.tmp / "shim.ps1")}
        self.status_file = self.tmp / "status.json"
        self.set_shim(status_exit=0)                     # the shim must exist before names.json names it
        self.private_live()
        (self.live / "store").mkdir(exist_ok=True)
        self.store = self.live / "store" / "inventory.db"
        self.scripts = self.tmp / "scripts"
        shutil.copytree(SCRIPTS, self.scripts)
        done = self.scripts / "switchon-13-done.ps1"
        done.write_text(done.read_text(encoding="ascii").replace("$WaitMinutes = 10", "$WaitMinutes = 0"), encoding="ascii", newline="\n")
        conn = sqlite3.connect(self.store)
        conn.executescript("CREATE TABLE accounts(id TEXT, alias TEXT); CREATE TABLE windows(account_id TEXT, observed_at INTEGER);"
                           "INSERT INTO accounts VALUES('a','claude-main'),('b','codex-main');")
        conn.commit()
        conn.close()
        self.cut = int(time.time() * 1000) - 600_000
        self.old_claude, self.old_codex = self.cut - 60_000, self.cut - 120_000
        self.write_baseline()

    def set_shim(self, status_exit):
        shim = self.tmp / "shim.ps1"
        shim.write_text('if ($args -contains "-c") { & "%s" @args; exit $LASTEXITCODE }\nif ($args -contains "status") { if (%d -ne 0) { exit %d }; Get-Content -LiteralPath "%s" -Raw; exit 0 }\nexit 0\n'
                        % (sys.executable, status_exit, status_exit, self.status_file), encoding="ascii")

    def write_baseline(self, **over):
        record = {"schema": 1, "cut_ms": self.cut, "delivered": {"claude-main": self.old_claude, "codex-main": self.old_codex}}
        record.update(over)
        (self.live / "baseline.json").write_text(json.dumps(record), encoding="ascii")

    def rows(self, claude, codex):
        conn = sqlite3.connect(self.store)
        conn.execute("DELETE FROM windows")
        for account, observed in (("a", claude), ("b", codex)):
            if observed is not None:
                conn.execute("INSERT INTO windows VALUES(?, ?)", (account, observed))
        conn.commit()
        conn.close()

    def good_rows(self):
        self.rows(self.cut + 1000, self.cut + 2000)

    def status_body(self, scan_age=10, sources=None, actions=(), failed=0, faults=0, configured=True, scan=None):
        if scan is None and scan_age is not None:
            scan = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - scan_age))
        sources = sources if sources is not None else [{"provider": "claude", "account": "claude-main"}, {"provider": "codex", "account": "codex-main"}]
        return {"operator_actions": list(actions), "readings": {
            "configured": configured, "sources": sources, "last_scan": scan, "rows": {"sent": 4, "pending": 0, "failed_permanent": failed},
            "capture_fault": {"count": faults, "last": None}}}

    def status(self, **over):
        self.status_file.write_text(json.dumps(self.status_body(**over)), encoding="ascii")

    def check(self):
        return self.run_script("switchon-13-done.ps1", scripts=self.scripts)

    def fails(self, word, case=None):
        done = self.check()
        self.assertNotEqual(done.returncode, 0, case)
        self.assertNotIn("PASSED", done.stdout, case)
        self.assertIn(word, self.said(done), case)
        self.assertIn("Nothing was changed", self.said(done), case)
        return done

    def test_new_figures_a_recent_scan_and_a_clean_status_pass(self):
        self.good_rows()
        self.status()
        done = self.check()
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertIn("PASSED", done.stdout)

    def test_old_rows_only_fail(self):
        for claude, codex in ((self.old_claude, self.cut + 5), (self.old_claude - 1, self.cut + 5), (None, self.cut + 5), (self.cut + 5, self.old_codex)):
            self.rows(claude, codex)
            self.status()
            self.fails("no accepted figure observed after the baseline", (claude, codex))

    def test_a_figure_observed_before_the_time_cut_but_delivered_after_it_fails(self):
        # newer than the delivered maximum, older than the clock time of step 12: it was only delivered late
        for late in (self.old_claude + 1, self.cut - 1, self.cut):
            self.rows(late, self.cut + 2000)
            self.status()
            self.fails("claude-main: no accepted figure observed after the baseline", late)
        self.rows(self.cut + 1, self.cut + 2000)
        self.assertEqual(self.check().returncode, 0)

    def test_a_figure_must_beat_both_the_time_cut_and_the_delivered_maximum(self):
        self.cut = int(time.time() * 1000) - 600_000
        self.old_claude = self.cut + 90_000                      # a delivered figure later than the cut (the usual case)
        self.write_baseline(delivered={"claude-main": self.old_claude, "codex-main": None})
        self.rows(self.old_claude, self.cut + 5)
        self.status()
        self.fails("claude-main: no accepted figure observed after the baseline")
        self.rows(self.old_claude + 1, self.cut + 5)
        self.assertEqual(self.check().returncode, 0)               # none delivered for codex-main: only the cut counts

    def test_a_reporter_that_never_ran_or_has_stopped_fails(self):
        self.good_rows()
        for age in (None, 181, 3600):
            self.status(scan_age=age)
            self.fails("has not scanned", age)
        self.status(scan_age=170)
        self.assertEqual(self.check().returncode, 0)

    def test_a_scan_time_ahead_of_the_clock_fails_beyond_thirty_seconds_and_passes_within(self):
        self.good_rows()
        self.status(scan_age=-60)
        self.fails("more than 30 seconds ahead")
        self.status(scan_age=-3600)
        self.fails("more than 30 seconds ahead")
        for age in (-15, 0):
            self.status(scan_age=age)
            self.assertEqual(self.check().returncode, 0, age)

    def test_a_scan_time_that_cannot_be_read_fails(self):
        self.good_rows()
        for scan in ("yesterday", "", 12345, True, ["2026-01-01T00:00:00Z"]):
            self.status(scan=scan)
            done = self.check()
            self.assertNotEqual(done.returncode, 0, scan)
            self.assertNotIn("PASSED", done.stdout, scan)

    def test_a_status_that_is_null_a_list_or_missing_a_part_fails(self):
        self.good_rows()
        body = self.status_body()
        cases = [("null", "is not a JSON object"), ("[]", "is not a JSON object"), ("[1, 2]", "is not a JSON object"), ('"text"', "is not a JSON object"),
                 ("not json at all", "is not a JSON object"), ("", "is not a JSON object"), (json.dumps([body]), "is not a JSON object"),
                 ("{}", "missing a part"), (json.dumps(self.without(body, "readings")), "missing a part"), (json.dumps(self.without(body, "operator_actions")), "missing a part"),
                 (json.dumps(self.without(body, "readings", "rows")), "missing a part"), (json.dumps(self.without(body, "readings", "capture_fault")), "missing a part"),
                 (json.dumps(self.without(body, "readings", "sources")), "missing a part"), (json.dumps(self.without(body, "readings", "last_scan")), "missing a part"),
                 (json.dumps(self.without(body, "readings", "configured")), "missing a part"), (json.dumps(self.without(body, "readings", "rows", "failed_permanent")), "missing a part"),
                 (json.dumps(self.without(body, "readings", "capture_fault", "count")), "missing a part"),
                 (json.dumps(dict(body, operator_actions=None)), "missing a part"), (json.dumps(dict(body, operator_actions="none")), "missing a part"),
                 (json.dumps(dict(body, readings=[1])), "missing a part"), (json.dumps(dict(body, readings=None)), "missing a part")]
        for text, word in cases:
            self.status_file.write_text(text, encoding="ascii")
            self.fails(word, text[:60])
        stringy = self.status_body()
        stringy["readings"]["rows"]["failed_permanent"] = "0"
        self.status_file.write_text(json.dumps(stringy), encoding="ascii")
        self.fails("missing a part", "a string where a number belongs")

    @staticmethod
    def without(body, *path):
        copy = json.loads(json.dumps(body))
        node = copy
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]
        return copy

    def test_a_failed_status_command_fails(self):
        self.good_rows()
        self.status()
        self.set_shim(status_exit=3)
        self.fails("reporter status could not be read")

    def test_a_failed_store_query_fails(self):
        self.status()
        self.store.unlink()
        done = self.check()
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("PASSED", done.stdout)

    def test_the_wrong_sources_a_warning_a_failure_or_a_fault_fail(self):
        self.good_rows()
        cases = (dict(sources=[{"provider": "claude", "account": "claude-main"}]),
                 dict(sources=[{"provider": "claude", "account": "claude-main"}, {"provider": "codex", "account": "codex-main"},
                               {"provider": "codex", "account": "extra"}]),
                 dict(sources=[{"provider": "claude", "account": "claude-main"}, {"provider": "codex", "account": "other"}]),
                 dict(sources=[]), dict(sources=["claude-main", "codex-main"]), dict(configured=False), dict(actions=["a warning"]), dict(failed=1),
                 dict(faults=1))
        for over in cases:
            self.status(**over)
            done = self.check()
            self.assertNotEqual(done.returncode, 0, over)
            self.assertNotIn("PASSED", done.stdout)

    def test_without_a_baseline_it_stops_at_once(self):
        (self.live / "baseline.json").unlink()
        done = self.check()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("No baseline was recorded", self.said(done))

    def test_a_baseline_that_is_not_what_step_12_writes_fails(self):
        self.good_rows()
        self.status()
        good = {"schema": 1, "cut_ms": self.cut, "delivered": {"claude-main": self.old_claude, "codex-main": None}}
        broken = [("not json", None), ("null", None), ("[]", None), ("[1]", None), ('"x"', None)]
        for name in ("schema", "cut_ms", "delivered"):
            broken.append((json.dumps({k: v for k, v in good.items() if k != name}), None))
        broken += [(json.dumps(dict(good, schema=2)), None), (json.dumps(dict(good, schema="1")), None), (json.dumps(dict(good, extra=1)), None),
                   (json.dumps(dict(good, cut_ms="123")), None), (json.dumps(dict(good, cut_ms=0)), None), (json.dumps(dict(good, cut_ms=-5)), None),
                   (json.dumps(dict(good, cut_ms=1.5)), None), (json.dumps(dict(good, cut_ms=None)), None),
                   (json.dumps(dict(good, delivered=None)), None), (json.dumps(dict(good, delivered=[1, 2])), None),
                   (json.dumps(dict(good, delivered={"claude-main": 5})), None), (json.dumps(dict(good, delivered={"claude-main": 5, "codex-main": 6, "x": 7})), None),
                   (json.dumps(dict(good, delivered={"claude-main": -1, "codex-main": None})), None),
                   (json.dumps(dict(good, delivered={"claude-main": "5", "codex-main": None})), None),
                   (json.dumps(dict(good, delivered={"claude-main": 5.5, "codex-main": None})), None),
                   (json.dumps({"claude-main": self.old_claude, "codex-main": self.old_codex}), None)]       # the first version's file
        for text, _ in broken:
            (self.live / "baseline.json").write_text(text, encoding="ascii")
            self.fails("The baseline is not the file step 12 made", text[:70])
        (self.live / "baseline.json").write_text(json.dumps(good), encoding="ascii")
        self.assertEqual(self.check().returncode, 0)

    def test_a_clock_that_moved_back_since_step_12_stops_with_the_instruction(self):
        self.good_rows()
        self.status()
        self.write_baseline(cut_ms=int(time.time() * 1000) + 120_000)
        done = self.fails("The clock moved back since step 12")
        self.assertIn("run step 12 again with -Again", " ".join(self.said(done).replace("Run", "run").split()))
        self.assertIn("ask the lead for the two turns again", self.said(done))
        self.write_baseline(cut_ms=int(time.time() * 1000) + 15_000)               # within the 30-second tolerance: not the clock message
        self.assertNotIn("clock moved back", self.said(self.check()))

    def test_the_baseline_step_records_the_clock_and_the_newest_delivered_figure(self):
        (self.live / "baseline.json").unlink()
        self.rows(self.old_claude, None)
        before = int(time.time() * 1000)
        done = self.run_script("switchon-12-baseline.ps1")
        after = int(time.time() * 1000)
        self.assertEqual(done.returncode, 0, self.said(done))
        record = json.loads((self.live / "baseline.json").read_text(encoding="ascii"))
        self.assertEqual(sorted(record), ["cut_ms", "delivered", "schema"])
        self.assertEqual(record["schema"], 1)
        self.assertTrue(before <= record["cut_ms"] <= after, record)
        self.assertEqual(record["delivered"], {"claude-main": self.old_claude, "codex-main": None})
        self.assertIn("codex-main: no accepted figure yet", done.stdout)

    def test_a_plain_rerun_of_the_baseline_refuses_and_again_replaces_it(self):
        (self.live / "baseline.json").unlink()
        self.rows(self.old_claude, self.old_codex)
        self.assertEqual(self.run_script("switchon-12-baseline.ps1").returncode, 0)
        first = (self.live / "baseline.json").read_bytes()
        time.sleep(0.05)
        again = self.run_script("switchon-12-baseline.ps1")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already recorded", self.said(again))
        self.assertEqual((self.live / "baseline.json").read_bytes(), first)         # not one byte changed
        self.rows(self.old_claude + 10, self.old_codex + 10)
        replaced = self.run_script("switchon-12-baseline.ps1", args=("-Again",))
        self.assertEqual(replaced.returncode, 0, self.said(replaced))
        record = json.loads((self.live / "baseline.json").read_text(encoding="ascii"))
        self.assertEqual(record["delivered"], {"claude-main": self.old_claude + 10, "codex-main": self.old_codex + 10})
        self.assertGreater(record["cut_ms"], json.loads(first)["cut_ms"])

    def shifted_copy(self, seconds):
        done = self.scripts / "switchon-13-done.ps1"
        text = done.read_text(encoding="ascii")
        calls = self.tmp / "clock-calls.txt"
        stub = ("function Get-Now { $n = if (Test-Path -LiteralPath '%s') { [int](Get-Content -LiteralPath '%s') } else { 0 }; Set-Content -LiteralPath '%s' ($n + 1); "
                "if ($n -eq 0) { [DateTimeOffset]::UtcNow } else { [DateTimeOffset]::UtcNow.AddSeconds(%d) } }" % (calls, calls, calls, seconds))
        self.assertIn("function Get-Now { [DateTimeOffset]::UtcNow }", text)
        done.write_text(text.replace("function Get-Now { [DateTimeOffset]::UtcNow }", stub), encoding="ascii", newline="\n")
        return calls

    def test_a_clock_that_is_fine_at_the_start_and_moved_back_at_the_decision_does_not_pass(self):
        self.good_rows()
        self.status()
        self.shifted_copy(-1200)
        done = self.check()
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("PASSED", done.stdout)
        self.assertIn("The clock moved back since step 12. Nothing was changed. Run step 12 again with -Again, then ask the lead for the two turns again.", self.said(done))

    def test_the_scan_age_is_measured_against_the_same_fresh_reading(self):
        self.good_rows()
        self.status(scan_age=10)
        self.shifted_copy(400)                                                          # the real clock says 10 seconds; the reading at the decision says 410
        done = self.check()
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("PASSED", done.stdout)
        self.assertIn("has not scanned in the last three minutes", self.said(done))

    def test_an_honest_clock_still_passes(self):
        self.good_rows()
        self.status()
        self.shifted_copy(0)
        done = self.check()
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertIn("PASSED", done.stdout)



class NoSecretOnACommandLine(Base):
    """Steps 4, 5 and 7 with a Python shim that records every argument and what it is given on standard input."""

    def test_the_identity_value_goes_through_standard_input_only(self):
        log = self.tmp / "calls.jsonl"
        shim = self.tmp / "shim.ps1"
        shim.write_text(
            '$in = @($input)\n'
            '$line = [ordered]@{ argv = @($args); stdin = ($in -join "`n") } | ConvertTo-Json -Compress\n'
            '[IO.File]::AppendAllText("%s", $line + "`n")\n'
            '$in | & "%s" @args\nexit $LASTEXITCODE\n' % (log, sys.executable), encoding="ascii")
        self.default_over = {"Py": str(shim)}
        self.private_live(config=False)
        self.install_source()
        (self.live / "store").mkdir()
        for step in ("switchon-04-store.ps1", "switchon-05-accounts.ps1", "switchon-07-settings.ps1"):
            done = self.run_script(step)
            self.assertEqual(done.returncode, 0, step + " " + self.said(done))
        config = (self.live / "reporter.toml").read_text(encoding="utf-8")
        identity = re.search(r'^identity = "([0-9a-f]{48})"$', config, re.M).group(1)
        calls = [json.loads(line) for line in log.read_text(encoding="ascii").splitlines()]
        enrol = [call for call in calls if "identity-set" in call["argv"]]
        self.assertEqual(len(enrol), 1)
        for call in calls:
            self.assertNotIn(identity, " ".join(call["argv"]))                      # on no command line
            self.assertNotRegex(" ".join(call["argv"]), r"[0-9a-f]{40,}")
            self.assertNotIn("--identity-ref", call["argv"])
        self.assertEqual(enrol[0]["stdin"].strip(), identity)                       # it went through standard input
        self.assertIn("--identity-stdin", enrol[0]["argv"])
        conn = sqlite3.connect(self.live / "store" / "inventory.db")
        try:
            self.assertEqual(conn.execute("SELECT identity_ref, enabled FROM machines WHERE alias='laptop-b'").fetchone(), (identity, 1))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
