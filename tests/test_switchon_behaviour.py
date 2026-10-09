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
        (self.bus / "state" / "seat-a.capacity.json").write_text("{}", encoding="ascii")
        self.links = []
        self.default_over = {}
        self.addCleanup(self.remove_links)

    # --- helpers ---------------------------------------------------------------------------------------------------

    def env(self, **over):
        env = {k: v for k, v in os.environ.items() if not k.startswith("LW_SWITCHON_")}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(ROOT), LW_SWITCHON_REHEARSAL="yes", LW_SWITCHON_ROOT=str(self.live),
                   LW_SWITCHON_BUS=str(self.bus), LW_SWITCHON_PORT=PORT, LW_SWITCHON_INPUT="prompt")
        for key, value in over.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def run_script(self, name, env=None, stdin=None, scripts=SCRIPTS):
        return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(Path(scripts) / name)], capture_output=True, text=True, input=stdin,
                              env=env or self.env(), timeout=180)

    def command(self, text, env=None):
        return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", text], capture_output=True, text=True, env=env or self.env(), timeout=180)

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

    def test_step_1_refuses_a_linked_archive_a_bad_python_and_a_live_folder_that_exists(self):
        real = self.tmp / "real-archives"
        real.mkdir()
        (real / "a.zip").write_bytes(b"PK")
        self.junction(self.tmp / "linked-archives", real)
        copy = self.tmp / "scripts"
        shutil.copytree(SCRIPTS, copy)
        step = copy / "switchon-01-setup.ps1"
        text = step.read_text(encoding="ascii")

        def fill(python, archive):
            return text.replace("<C:\\path\\to\\python.exe>", python).replace("<C:\\path\\to\\leasewise-COMMIT.zip>", archive).replace(
                "<the SHA-256 the lead gave you>", "b" * 64)
        for python, archive in ((sys.executable, str(self.tmp / "linked-archives" / "a.zip")), (str(self.tmp / "no.exe"), str(real / "a.zip")),
                                (sys.executable, "D:\\x\\a.zip")):
            step.write_text(fill(python, archive), encoding="ascii", newline="\n")
            done = self.run_script("switchon-01-setup.ps1", scripts=copy)
            self.assertNotEqual(done.returncode, 0, (python, archive))
            self.assertFalse(self.live.exists(), (python, archive))                    # nothing was made
        step.write_text(fill(sys.executable, str(real / "a.zip")), encoding="ascii", newline="\n")
        done = self.run_script("switchon-01-setup.ps1", scripts=copy)
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertEqual(sorted(p.name for p in self.live.iterdir()), [".owner.json", "journal", "names.json", "state", "store"])
        saved = json.loads((self.live / "names.json").read_text(encoding="ascii"))
        self.assertEqual(sorted(saved), sorted(["LiveRoot", "BusRoot", "Port", "Token", "Py", "Archive", "ExpectedSha", "MachineAlias", "ClaudeAlias",
                                                "CodexAlias", "ProjectAlias"]))
        again = self.run_script("switchon-01-setup.ps1", scripts=copy)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already exists. Nothing was changed", self.said(again))

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
        self.assertIn("contains a link. Nothing was deleted", self.said(done))
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
                            ("LW_SWITCHON_CLAUDE_TEXT", LOUD_CLAUDE), ("LW_SWITCHON_CODEX_TEXT", LOUD_CODEX), ("LW_SWITCHON_REHEARSAL", "no"),
                            ("LW_SWITCHON_REHEARSAL", "1"), ("LW_SWITCHON_REHEARSAL", "YES")):
            env = self.env(LW_SWITCHON_REHEARSAL=None, LW_SWITCHON_ROOT=None, LW_SWITCHON_BUS=None, LW_SWITCHON_PORT=None, LW_SWITCHON_INPUT=None)
            env[name] = value
            self.refuses(env, "A rehearsal setting (LW_SWITCHON_...) is present in this window")
            done = self.run_script("switchon-03-check.ps1", env)
            self.assertNotIn(LOUD, self.said(done))

    def test_without_any_rehearsal_variable_the_operator_defaults_are_used_and_nothing_is_created(self):
        env = self.env(LW_SWITCHON_REHEARSAL=None, LW_SWITCHON_ROOT=None, LW_SWITCHON_BUS=None, LW_SWITCHON_PORT=None, LW_SWITCHON_INPUT=None)
        done = self.command(". '%s'; $LiveRoot; $BusRoot; $PortNumber" % COMMON, env)
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertEqual(done.stdout.split(), ["C:\\lw\\live", "C:\\lw\\team\\.agenttalk", "8443"])
        self.assertFalse(os.path.exists("C:\\lw\\live"))

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
        self.refuses(self.env(LW_SWITCHON_INPUT="env"), 'needs both test texts')
        self.refuses(self.env(LW_SWITCHON_INPUT="env", LW_SWITCHON_CLAUDE_TEXT=LOUD_CLAUDE), 'needs both test texts')
        self.refuses(self.env(LW_SWITCHON_INPUT="prompt", LW_SWITCHON_CLAUDE_TEXT=LOUD_CLAUDE, LW_SWITCHON_CODEX_TEXT=LOUD_CODEX), 'must not set the test texts')
        self.refuses(self.env(LW_SWITCHON_INPUT="maybe"), 'must be "env" or "prompt"')
        done = self.run_script("switchon-03-check.ps1", self.env(LW_SWITCHON_INPUT="env", LW_SWITCHON_CLAUDE_TEXT=LOUD_CLAUDE, LW_SWITCHON_CODEX_TEXT="x"))
        self.assertNotIn(LOUD, self.said(done))

    def test_the_port_must_be_a_rehearsal_port(self):
        for port in ("8443", "80", "70000", "abc", "18455.5", "-1"):
            self.refuses(self.env(LW_SWITCHON_PORT=port), "rehearsal port")

    def test_a_complete_rehearsal_is_accepted(self):
        self.names()
        done = self.run_script("switchon-03-check.ps1")
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertIn("1 usage file(s) found", done.stdout)
        self.assertNotIn("{}", done.stdout)


class Prompt(Base):
    """The real interactive branch of step 8, in a pseudo console, with loud markers."""

    def run_in_console(self, env, transcript=None):
        from tests.switchon_console import run_in_console
        pwsh = shutil.which("pwsh")
        inner = "& '%s'" % (SCRIPTS / "switchon-08-table.ps1")
        if transcript:
            inner = "Start-Transcript -Path '%s' -Force | Out-Null; try { %s } finally { Stop-Transcript | Out-Null }" % (transcript, inner)
        line = '"%s" -NoProfile -Command "%s"' % (pwsh, inner)
        return run_in_console(line, [LOUD_CLAUDE + "\r", LOUD_CODEX + "\r"], env=env, cwd=str(ROOT), wait_seconds=60)

    def test_what_is_typed_is_never_shown_or_recorded(self):
        self.private_live()
        transcript = self.tmp / "transcript.txt"
        code, shown = self.run_in_console(self.env(), transcript)
        self.assertEqual(code, 0, ANSI.sub("", shown))
        self.assertIn("two rows written", shown)
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
        code, shown = run_in_console(line, ["claude:" + LOUD + ":home-ZZZZ\r", LOUD_CODEX + "\r"], env=self.env(), cwd=str(ROOT), wait_seconds=60)
        self.assertNotEqual(code, 0)
        self.assertIn("The Claude text is not in the form", shown)
        self.assertNotIn(LOUD, shown)
        self.assertEqual(self.digest(self.live / "reporter.toml"), before)

    def test_the_env_branch_exists_only_for_the_rehearsal_and_writes_the_same_rows(self):
        self.private_live()
        done = self.run_script("switchon-08-table.ps1", self.env(LW_SWITCHON_INPUT="env", LW_SWITCHON_CLAUDE_TEXT=LOUD_CLAUDE, LW_SWITCHON_CODEX_TEXT=LOUD_CODEX))
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertNotIn(LOUD, done.stdout + done.stderr)
        config = (self.live / "reporter.toml").read_text(encoding="utf-8")
        self.assertEqual(config.count("[[readings.sources]]"), 2)
        self.assertIn('account = "claude-main"', config)
        again = self.run_script("switchon-08-table.ps1", self.env(LW_SWITCHON_INPUT="env", LW_SWITCHON_CLAUDE_TEXT=LOUD_CLAUDE, LW_SWITCHON_CODEX_TEXT=LOUD_CODEX))
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already in the settings file", self.said(again))


class PrivacyBeforeWriting(Base):
    ENV_TEXTS = dict(LW_SWITCHON_INPUT="env", LW_SWITCHON_CLAUDE_TEXT=LOUD_CLAUDE, LW_SWITCHON_CODEX_TEXT=LOUD_CODEX)

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


class DoneCheck(Base):
    """Step 13 against a real sqlite file and a status shim; the wait is set to zero in a COPY of the script (the real one waits ten minutes)."""

    def setUp(self):
        super().setUp()
        self.default_over = {"Py": str(self.tmp / "shim.ps1")}
        self.status_file = self.tmp / "status.json"
        self.set_shim(status_exit=0)                     # the shim must exist before names.json names it
        self.private_live()
        (self.live / "store").mkdir(exist_ok=True)
        self.store = self.live / "store" / "inventory.db"
        self.status_file = self.tmp / "status.json"
        self.scripts = self.tmp / "scripts"
        shutil.copytree(SCRIPTS, self.scripts)
        done = self.scripts / "switchon-13-done.ps1"
        done.write_text(done.read_text(encoding="ascii").replace("$WaitMinutes = 10", "$WaitMinutes = 0"), encoding="ascii", newline="\n")
        conn = sqlite3.connect(self.store)
        conn.executescript("CREATE TABLE accounts(id TEXT, alias TEXT); CREATE TABLE windows(account_id TEXT, observed_at INTEGER);"
                           "INSERT INTO accounts VALUES('a','claude-main'),('b','codex-main');")
        conn.commit()
        conn.close()
        self.base = 1_000_000_000_000
        self.write_baseline(claude=self.base, codex=self.base)

    def set_shim(self, status_exit):
        shim = self.tmp / "shim.ps1"
        shim.write_text('if ($args[0] -eq "-c") { & "%s" @args; exit $LASTEXITCODE }\nif ($args -contains "status") { if (%d -ne 0) { exit %d }; Get-Content -LiteralPath "%s" -Raw; exit 0 }\nexit 0\n'
                        % (sys.executable, status_exit, status_exit, self.status_file), encoding="ascii")

    def write_baseline(self, claude, codex):
        (self.live / "baseline.json").write_text(json.dumps({"claude-main": claude, "codex-main": codex}), encoding="ascii")

    def rows(self, claude, codex):
        conn = sqlite3.connect(self.store)
        conn.execute("DELETE FROM windows")
        for account, observed in (("a", claude), ("b", codex)):
            if observed is not None:
                conn.execute("INSERT INTO windows VALUES(?, ?)", (account, observed))
        conn.commit()
        conn.close()

    def status(self, scan_age=10, sources=None, actions=(), failed=0, faults=0, configured=True):
        last_scan = None if scan_age is None else time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - scan_age))
        sources = sources if sources is not None else [{"provider": "claude", "account": "claude-main"}, {"provider": "codex", "account": "codex-main"}]
        self.status_file.write_text(json.dumps({"operator_actions": list(actions), "readings": {
            "configured": configured, "sources": sources, "last_scan": last_scan, "rows": {"sent": 4, "pending": 0, "failed_permanent": failed},
            "capture_fault": {"count": faults, "last": None}}}), encoding="ascii")

    def check(self):
        return self.run_script("switchon-13-done.ps1", scripts=self.scripts)

    def test_new_figures_a_recent_scan_and_a_clean_status_pass(self):
        self.rows(self.base + 1, self.base + 2)
        self.status()
        done = self.check()
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertIn("PASSED", done.stdout)
        self.assertNotIn("LOUD", done.stdout)

    def test_old_rows_only_fail(self):
        for claude, codex in ((self.base, self.base + 5), (self.base - 1, self.base + 5), (None, self.base + 5), (self.base + 5, self.base)):
            self.rows(claude, codex)
            self.status()
            done = self.check()
            self.assertNotEqual(done.returncode, 0, (claude, codex))
            self.assertNotIn("PASSED", done.stdout)
            self.assertIn("no accepted figure newer than the baseline", self.said(done))

    def test_a_reporter_that_never_ran_or_has_stopped_fails(self):
        self.rows(self.base + 1, self.base + 2)
        for age in (None, 181, 3600):
            self.status(scan_age=age)
            done = self.check()
            self.assertNotEqual(done.returncode, 0, age)
            self.assertNotIn("PASSED", done.stdout)
            self.assertRegex(self.said(done), "has not scanned")
        self.status(scan_age=170)
        self.assertEqual(self.check().returncode, 0)

    def test_a_failed_status_command_fails(self):
        self.rows(self.base + 1, self.base + 2)
        self.status()
        self.set_shim(status_exit=3)
        done = self.check()
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("PASSED", done.stdout)
        self.assertIn("reporter status could not be read", self.said(done))

    def test_a_failed_store_query_fails(self):
        self.status()
        self.store.unlink()
        done = self.check()
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("PASSED", done.stdout)

    def test_the_wrong_sources_a_warning_a_failure_or_a_fault_fail(self):
        self.rows(self.base + 1, self.base + 2)
        cases = (dict(sources=[{"provider": "claude", "account": "claude-main"}]),
                 dict(sources=[{"provider": "claude", "account": "claude-main"}, {"provider": "codex", "account": "codex-main"},
                               {"provider": "codex", "account": "extra"}]),
                 dict(sources=[{"provider": "claude", "account": "claude-main"}, {"provider": "codex", "account": "other"}]),
                 dict(configured=False), dict(actions=["a warning"]), dict(failed=1), dict(faults=1))
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

    def test_the_baseline_step_records_the_newest_accepted_figure_once(self):
        (self.live / "baseline.json").unlink()
        self.rows(self.base + 7, None)
        done = self.run_script("switchon-12-baseline.ps1")
        self.assertEqual(done.returncode, 0, self.said(done))
        self.assertEqual(json.loads((self.live / "baseline.json").read_text(encoding="ascii")), {"claude-main": self.base + 7, "codex-main": 0})
        self.assertIn("codex-main: no accepted figure yet", done.stdout)
        again = self.run_script("switchon-12-baseline.ps1")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already recorded", self.said(again))


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
        (self.live / "src").mkdir()
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
