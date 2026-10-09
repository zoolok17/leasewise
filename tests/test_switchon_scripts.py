"""The switch-on runbook's scripts must not drift from the runbook, and must keep the safety rules they promise (card 7b-on).

Every step of ``docs/SWITCHON-7B-LAPTOP-B.md`` is a small script in ``docs/switchon-7b/``. The operator runs the scripts, so a later edit of a
runbook block that is not made in the script too (or the other way round) would send the operator a different command from the one that was
rehearsed. The model is ``tests/test_trial_scripts.py`` of the money-reader trial.

The runbook block of a script is the script's body after its shared-start line (``. (Join-Path $PSScriptRoot 'switchon-common.ps1')``; step 0 has none,
so all of it). The comparison is exact line by line (trailing blanks ignored, line endings normalised): there are NO named adaptations in this
form, because the values the operator edits (step 1) are the same angle-bracket placeholders in the block and in the script. Header comments above the
shared-start line belong to the script only.

Two files are PINNED, not compared with a block: the shared start (``switchon-common.ps1``) and the entry guard (``switchon-guard.ps1``) must equal the
approved text (checked by SHA-256 of the text with line endings normalised) and must hold the checks they promise. Any change fails the test until the
approved hash here is updated on purpose.

The checks are text, plus, when ``pwsh`` is on the path, a parse of every script with PowerShell's own parser (it parses; it never runs a script) and a
run of the entry guard against throwaway folders (it only reads).
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = ROOT / "docs" / "SWITCHON-7B-LAPTOP-B.md"
SCRIPTS = ROOT / "docs" / "switchon-7b"
COMMON, GUARD = "switchon-common.ps1", "switchon-guard.ps1"
DOT_SOURCE = ". (Join-Path $PSScriptRoot 'switchon-common.ps1')"
STEPS = [
    "switchon-00-check.ps1", "switchon-01-setup.ps1", "switchon-02-unpack.ps1", "switchon-03-check.ps1", "switchon-04-store.ps1",
    "switchon-05-accounts.ps1", "switchon-06-check-accounts.ps1", "switchon-07-settings.ps1", "switchon-08-table.ps1",
    "switchon-09-check-table.ps1", "switchon-10-server.ps1", "switchon-11-reporter.ps1", "switchon-12-baseline.ps1", "switchon-13-done.ps1",
    "switchon-14-stopped.ps1", "switchon-15-rollback.ps1", "switchon-help-status.ps1"]
ALL = STEPS + [COMMON, GUARD]
# SHA-256 of the approved text of the two pinned files (line endings normalised to LF).
APPROVED = {
    COMMON: "e8ec10f2a5b6b5ffd34d2f797b3789a974763387d60f4d53aacd11b481328796",
    GUARD: "09f7065fafd9611026d5ddcd80b67faa7bf55aba1da6f9e6613cbf060bf4b71b",
}


def norm(text):
    return text.replace("\r\n", "\n")


def script_text(name, folder=SCRIPTS):
    return norm(Path(folder, name).read_bytes().decode("ascii"))


def body_of(name, folder=SCRIPTS):
    """The part of a script that its runbook block must equal."""
    lines = script_text(name, folder).rstrip("\n").split("\n")
    if name == STEPS[0]:
        return [line.rstrip() for line in lines if not line.startswith("#")]
    for number, line in enumerate(lines):
        if line.startswith(DOT_SOURCE):
            return [item.rstrip() for item in lines[number + 1:]]
    raise AssertionError(name + " has no shared-start line")


def runbook_blocks(text):
    """{script name: block lines} from the ``<!-- script: NAME -->`` markers, each followed by a powershell fence."""
    found, lines, index = {}, norm(text).split("\n"), 0
    while index < len(lines):
        match = re.fullmatch(r"<!-- script: (switchon-[\w\-]+\.ps1) -->", lines[index])
        if match:
            assert lines[index + 1].startswith("```powershell"), match.group(1)
            end = index + 2
            while not lines[end].startswith("```"):
                end += 1
            found[match.group(1)] = [item.rstrip() for item in lines[index + 2:end]]
            index = end
        index += 1
    return found


def check_drift(runbook_text, folder=SCRIPTS):
    """-> a list of problems (empty when every block equals its script's body and every script has a block)."""
    problems = []
    blocks = runbook_blocks(runbook_text)
    for name in STEPS:
        if name not in blocks:
            problems.append(name + ": the runbook has no block for it")
        elif blocks[name] != body_of(name, folder):
            problems.append(name + ": the runbook block differs from the script")
    for name in blocks:
        if name not in STEPS:
            problems.append(name + ": a runbook block for a script that is not a step")
    return problems


class Files(unittest.TestCase):
    def test_the_folder_holds_exactly_the_listed_scripts(self):
        self.assertEqual(sorted(p.name for p in SCRIPTS.iterdir()), sorted(ALL))

    def test_every_script_is_ascii_with_lf_line_endings(self):
        for name in ALL:
            raw = (SCRIPTS / name).read_bytes()
            raw.decode("ascii")
            self.assertNotIn(b"\r", raw, name)
            self.assertTrue(raw.endswith(b"\n"), name)

    def test_every_step_but_the_first_loads_the_shared_start_before_anything_else(self):
        for name in STEPS[1:]:
            code = [line for line in script_text(name).split("\n") if line.strip() and not line.startswith("#")]
            expected = DOT_SOURCE + (" -KeepOutOfSource" if name == "switchon-15-rollback.ps1" else "")
            if name == "switchon-12-baseline.ps1":                                        # its one switch is declared first, as PowerShell requires
                self.assertTrue(code[0].startswith("param([switch]$Again)"), name)
                code = code[1:]
            self.assertEqual(code[0], expected, name)
        code = [line for line in script_text(STEPS[0]).split("\n") if line.strip() and not line.startswith("#")]
        self.assertNotIn("Join-Path", " ".join(code))                     # step 0 reads nothing at all


class Drift(unittest.TestCase):
    def test_every_block_equals_its_script_and_every_script_has_a_block(self):
        self.assertEqual(check_drift(RUNBOOK.read_text(encoding="utf-8")), [])
        self.assertEqual(sorted(runbook_blocks(RUNBOOK.read_text(encoding="utf-8"))), sorted(STEPS))

    def test_the_check_notices_a_changed_block_a_missing_block_and_an_extra_block(self):
        text = norm(RUNBOOK.read_text(encoding="utf-8"))
        changed = text.replace("Get-FileHash -Algorithm SHA256", "Get-FileHash -Algorithm MD5", 1)
        self.assertIn("switchon-02-unpack.ps1: the runbook block differs from the script", check_drift(changed))
        removed = text.replace("<!-- script: switchon-04-store.ps1 -->", "", 1)
        self.assertIn("switchon-04-store.ps1: the runbook has no block for it", check_drift(removed))
        extra = text + "\n<!-- script: switchon-99-extra.ps1 -->\n```powershell\nx\n```\n"
        self.assertIn("switchon-99-extra.ps1: a runbook block for a script that is not a step", check_drift(extra))

    def test_the_check_notices_a_changed_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ALL:
                shutil.copy(SCRIPTS / name, Path(tmp, name))
            path = Path(tmp, "switchon-15-rollback.ps1")
            path.write_text(script_text("switchon-15-rollback.ps1").replace("-Recurse -Force", "-Recurse -Force -WhatIf:$false"), encoding="ascii", newline="\n")
            self.assertEqual(check_drift(RUNBOOK.read_text(encoding="utf-8"), tmp), ["switchon-15-rollback.ps1: the runbook block differs from the script"])

    def test_a_runbook_with_crlf_line_endings_still_matches(self):
        self.assertEqual(check_drift(RUNBOOK.read_text(encoding="utf-8").replace("\n", "\r\n")), [])


class Pinned(unittest.TestCase):
    def test_the_shared_start_and_the_guard_are_the_approved_text(self):
        for name in (COMMON, GUARD):
            self.assertEqual(hashlib.sha256(script_text(name).encode("ascii")).hexdigest(), APPROVED[name], name)

    def test_the_guard_holds_the_checks_it_promises(self):
        guard = script_text(GUARD)
        for needle in ("[IO.Path]::GetFullPath($Path)", "-notlike 'C:\\lw\\*'", "ReparsePoint", "(Join-Path $BusRoot 'state')", "$NamesFile",
                       "names.json was made for another live folder", "'C:\\lw\\live'", "'C:\\lw\\team\\.agenttalk'", "LW_SWITCHON_ROOT"):
            self.assertIn(needle, guard)
        self.assertLess(guard.index("GetFullPath"), guard.index("ReparsePoint"))        # the path check comes before anything is opened
        self.assertLess(guard.index("ReparsePoint"), guard.index("Get-Content"))        # and the link check before names.json is read

    def test_the_shared_start_runs_the_guard_before_it_reads_the_saved_names(self):
        common = script_text(COMMON)
        self.assertIn("switchon-guard.ps1", common)
        self.assertLess(common.index("switchon-guard.ps1"), common.index("Set-Variable"))
        self.assertIn("Protect-Path", common)
        self.assertIn("Assert-Private", common)


class Safety(unittest.TestCase):
    def all_code(self):
        return {name: [line for line in script_text(name).split("\n") if not line.lstrip().startswith("#")] for name in ALL}

    def test_nothing_reads_or_writes_the_profile_the_temp_folder_or_the_network(self):
        banned = ("LOCALAPPDATA", "USERPROFILE", "APPDATA", "$env:TEMP", "$env:TMP", "HOMEPATH", "Invoke-WebRequest", "Invoke-RestMethod", "curl",
                  "wget", "Set-ExecutionPolicy", "Start-Process", "Start-BitsTransfer", "New-NetFirewallRule", "reg add", "setx", "schtasks", "Register-")
        for name, lines in self.all_code().items():
            for line in lines:
                for word in banned:
                    self.assertNotIn(word, line, name)

    def test_the_only_address_is_this_machine_and_the_only_folders_are_inside_c_lw(self):
        for name, lines in self.all_code().items():
            for line in lines:
                for match in re.finditer(r"https?://[^\s'\"]+", line):
                    self.assertTrue(match.group(0).startswith("http://127.0.0.1:"), (name, match.group(0)))
                for match in re.finditer(r"\b[A-Za-z]:\\[^\s'\"\)]*", line):
                    text = match.group(0)
                    self.assertTrue(text.lower().startswith("c:\\lw") or (name == "switchon-01-setup.ps1" and text == "C:\\Python313\\python.exe"), (name, text))

    def test_only_the_rollback_deletes_and_only_the_live_folder(self):
        for name, lines in self.all_code().items():
            deletes = [line for line in lines if re.search(r"Remove-Item|\.Delete\(|::Delete\(|rmdir|rd /s|del /|robocopy|takeown|icacls", line)]
            if name == "switchon-15-rollback.ps1":
                self.assertEqual([line.split("  #")[0].strip() for line in deletes],
                                 ["Remove-Item -LiteralPath $Child.FullName -Recurse -Force",
                                  "if (Test-Path -LiteralPath $NamesFile) { Remove-Item -LiteralPath $NamesFile -Force }",
                                  "Remove-Item -LiteralPath $MarkerFile -Force", "[IO.Directory]::Delete($LiveRoot)"])
            else:
                self.assertEqual(deletes, [], name)

    def test_the_rollback_checks_for_links_and_the_marker_before_it_deletes(self):
        text = script_text("switchon-15-rollback.ps1")
        self.assertLess(text.index("ReparsePoint"), text.index("Remove-Item"))
        self.assertLess(text.index(".owner.json"), text.index("Remove-Item"))
        self.assertLess(text.index("$Links -gt 0"), text.index("Remove-Item"))
        self.assertLess(text.index("Assert-NoLink $MarkerFile"), text.index("Get-Content"))                              # the marker is no link before it is read
        self.assertLess(text.index("Get-LiveProcesses"), text.index("Remove-Item"))                                     # nothing is deleted while a window still runs
        self.assertLess(text.index("Window 1 or 2 is still running"), text.index("Remove-Item"))
        order = [text.index(word) for word in ("Remove-Item -LiteralPath $Child.FullName", "Remove-Item -LiteralPath $NamesFile", "Remove-Item -LiteralPath $MarkerFile",
                                               "[IO.Directory]::Delete($LiveRoot)")]
        self.assertEqual(order, sorted(order))                                # everything else, then names.json, then the marker, then the empty folder
        self.assertIn("names.json is missing", text)
        self.assertLess(text.index("Assert-Separate"), text.index("Remove-Item"))                                       # the roots are checked again right before the delete
        self.assertLess(text.index("Assert-NoLink $LiveRoot"), text.index("Get-Content"))
        self.assertIn("Stack[string]", text)                                  # a walk that never enters a link

    def test_the_unpack_goes_only_into_the_source_folder_of_the_live_folder(self):
        for name, lines in self.all_code().items():
            for line in lines:
                if "Expand-Archive" in line:
                    self.assertEqual((name, line.strip()), ("switchon-02-unpack.ps1", "Expand-Archive -LiteralPath $Archive -DestinationPath $Src"))
        self.assertIn("$Src        = Join-Path $LiveRoot 'src'", script_text("switchon-01-setup.ps1"))

    def test_no_script_prints_a_secret_or_an_account_text(self):
        secrets = r"Id|ClaudeText|CodexText|ConfigText|Rows|Token|Bytes|Inventory"
        for name, lines in self.all_code().items():
            for line in lines:
                self.assertNotRegex(line, r"Write-(Host|Output|Verbose|Debug)|\becho\b", name)
                if re.match(r"^\s*\$(%s)\s*$" % secrets, line):
                    self.fail("%s prints a variable that may hold a secret: %s" % (name, line))
                if re.match(r"^\s*['\"].*\$\(?(Id|ClaudeText|CodexText|ConfigText|Rows)\b", line):
                    self.fail("%s prints text with a secret in it: %s" % (name, line))
                if re.search(r"\$(Id|ClaudeText|CodexText|ConfigText|Rows)\b.*\|\s*(Out-File|Set-Content|Out-String|Tee-Object)", line):
                    self.fail("%s writes a secret somewhere other than the private file: %s" % (name, line))

    def test_the_account_texts_come_only_from_the_operator_and_go_only_into_the_private_settings(self):
        text = script_text("switchon-08-table.ps1")
        self.assertEqual(len(re.findall(r"Read-Host", text)), 4)
        self.assertEqual(len(re.findall(r"Read-Host -MaskInput", text)), 4)             # what is typed is never shown
        self.assertNotIn("-AsPlainText", text)
        self.assertIn("[IO.File]::AppendAllText($ConfigPath, $Rows, $Utf8)", text)
        self.assertLess(text.index("Assert-Private $ConfigPath $true"), text.index("Read-Host"))      # the file is checked BEFORE anything is asked
        self.assertLess(text.index("Assert-Private $LiveRoot $true"), text.index("Read-Host"))
        self.assertLess(text.index("-cnotmatch"), text.index("AppendAllText"))        # the shape is checked before anything is written
        self.assertLess(text.index("AppendAllText"), text.index("Assert-PrivateAfter $ConfigPath $true"))
        code = [line for line in text.split("\n") if not line.lstrip().startswith("#")]
        self.assertEqual([line for line in code if "env:LW_SWITCHON_" in line and "LW_SWITCHON_INPUT" not in line],
                         ["    $ClaudeName, $ClaudeHome = $env:LW_SWITCHON_CLAUDE_NAME, $env:LW_SWITCHON_CLAUDE_HOME",
                          "    $CodexName, $CodexHome = $env:LW_SWITCHON_CODEX_NAME, $env:LW_SWITCHON_CODEX_HOME"])
        self.assertLess(text.index("Invoke-UsageScan"), text.index("AppendAllText"))   # a typo is found against our own usage files before anything is written
        self.assertIn("does not match any of our usage files. Nothing was changed.", text)
        for name in STEPS:
            if name != "switchon-08-table.ps1":
                self.assertNotIn("$ClaudeText", script_text(name))
                self.assertNotIn("$CodexText", script_text(name))

    def test_the_settings_and_the_store_are_made_private_before_a_secret_exists(self):
        text = script_text("switchon-07-settings.ps1")
        for check in ("Assert-Private $LiveRoot $true", "Assert-Private (Split-Path $Store) $false", "Assert-Private $Store $false"):
            self.assertLess(text.index(check), text.index("RandomNumberGenerator"), check)          # before the value exists
            self.assertLess(text.index(check), text.index("identity-set"), check)
            self.assertLess(text.index(check), text.index("New-Item -ItemType File"), check)
        self.assertLess(text.index("Protect-Path $ConfigPath $false"), text.index("RandomNumberGenerator"))
        self.assertLess(text.index("Assert-Private $ConfigPath $true"), text.index("identity-set"))
        self.assertLess(text.index("identity-set"), text.index("Assert-PrivateAfter $Store $false"))     # and again after each write, with a different message
        self.assertLess(text.index("Assert-PrivateAfter $Store $false"), text.index("WriteAllText($ConfigPath"))
        self.assertLess(text.index("WriteAllText($ConfigPath"), text.index("Assert-PrivateAfter $ConfigPath $true"))
        setup = script_text("switchon-01-setup.ps1")
        self.assertLess(setup.index("WriteAllText((Join-Path $LiveRoot '.owner.json')"), setup.index("Protect-Path $LiveRoot $true"))    # the marker and names.json come first, at once
        self.assertLess(setup.index("WriteAllText($Names"), setup.index("Protect-Path $LiveRoot $true"))
        self.assertLess(setup.index("Assert-Private $LiveRoot $true"), setup.index("New-Item -ItemType Directory -Path $StateDir"))
        self.assertIn("was made but could not be made private. Nothing secret was written. Run step 15 to remove it, then tell the lead.", setup)
        self.assertNotIn("MaskInput", setup)                                  # nothing in step 1 is secret
        common = script_text(COMMON)
        self.assertIn("nothing secret has been written", common)
        self.assertIn("A secret HAS ALREADY BEEN WRITTEN", common)

    def test_no_secret_is_ever_on_a_command_line(self):
        for name, lines in self.all_code().items():
            for line in lines:
                self.assertNotIn("--identity-ref", line, name)
        lines = [line for line in self.all_code()["switchon-07-settings.ps1"] if "identity-set" in line and "throw" not in line]
        self.assertEqual(lines, ["$Id | & $Py @PyFlags -m quota_balancer --db $Store identity-set --machine $MachineAlias --identity-stdin --enabled true"])

    def test_the_done_check_has_the_conditions_it_promises(self):
        text = script_text("switchon-13-done.ps1")
        for needle in ("baseline.json", "$Newest[$Alias] -le $Cut", "$Newest[$Alias] -le $Delivered[$Alias]", "$Age -gt 180", "$Age -lt -30", "$LASTEXITCODE -ne 0", "$R.sources",
                       "@($Status.operator_actions).Count -ne 0", "failed_permanent -ne 0", "capture_fault.count -ne 0", "mode=ro", "$WaitMinutes = 10",
                       "ConvertFrom-Json -NoEnumerate", "'System.Management.Automation.PSCustomObject'", "$R.configured -ne $true", "lt $Cut - 30000",
                       "The clock moved back since step 12", "step 12 again with -Again", "ask the lead for the two turns again", "Nothing was changed"):
            self.assertIn(needle, text, needle)
        self.assertEqual(text.count("$LASTEXITCODE -ne 0"), 2)                       # the store query and the status command
        self.assertNotIn("-is [pscustomobject]", text)                               # PowerShell calls a list one too
        baseline = script_text("switchon-12-baseline.ps1")
        self.assertIn("A baseline was already recorded", baseline)
        self.assertIn("-and -not $Again", baseline)
        self.assertLess(baseline.index("mode=ro"), baseline.index("WriteAllText"))
        self.assertLess(baseline.index("$Cut = "), baseline.index("mode=ro"))          # the clock is read before the store is asked

    def test_the_guard_walks_the_whole_live_folder_before_it_reads_anything(self):
        guard = script_text("switchon-guard.ps1")
        self.assertIn("function Assert-NoLinkUnder", guard)
        self.assertIn("$Seen -gt 50000", guard)
        self.assertLess(guard.index("Assert-NoLinkUnder $LiveRoot"), guard.index("Get-Content -LiteralPath $NamesFile"))       # before names.json is read
        walk = guard[guard.index("function Assert-NoLinkUnder"):guard.index("function Assert-Separate")]
        self.assertIn("ReparsePoint", walk)
        self.assertNotIn("Get-ChildItem", walk)                                       # a listing that could follow a link is not used
        self.assertLess(walk.index("ReparsePoint"), walk.index("$Pending.Push($Root)"))   # the root itself is refused before it is queued
        self.assertLess(walk.index("if ($Entry.Attributes"), walk.index("$Pending.Push($Entry.FullName)"))

    def test_only_the_server_listens_and_only_on_this_machine(self):
        for name, lines in self.all_code().items():
            for line in lines:
                if "serve" in line and name not in ("switchon-10-server.ps1", COMMON):
                    self.assertNotIn("'serve'", line, name)
        self.assertIn("'--host', '127.0.0.1'", script_text(COMMON))
        server, reporter = script_text("switchon-10-server.ps1"), script_text("switchon-11-reporter.ps1")
        self.assertLess(server.index("Get-PortListeners $PortNumber"), server.index("& $Py"))                  # nothing starts while anything listens on the port
        self.assertIn("Something on this machine already listens on port", server)
        self.assertLess(reporter.index("Assert-OurServerListens"), reporter.index("& $Py"))                    # the identity value is never sent to a stranger
        self.assertLess(reporter.index("Assert-NoReporterRunning"), reporter.index("& $Py"))
        common = script_text(COMMON)
        for needle in ("Get-NetTCPConnection -State Listen", "CmdletizationQuery_NotFound,Get-NetTCPConnection", "$Row.ParentPath -ine $Py", "$Row.ParentCommandLine -cne $Row.CommandLine",
                       "$Rows[0].Address -cne '127.0.0.1'", "is not window 1's Leasewise server. Nothing was started, and the identity value was not sent. Tell the lead."):
            self.assertIn(needle, common, needle)
        self.assertNotIn("0.0.0.0", "".join(script_text(name) for name in ALL))

    def test_every_python_call_uses_the_one_shared_flag_list_and_none_depends_on_the_environment(self):
        common = script_text(COMMON)
        self.assertIn("$PyFlags = @('-E', '-s', '-B')", common)
        self.assertNotIn("PYTHONDONTWRITEBYTECODE", "".join(script_text(name) for name in ALL))          # -E ignores it; -B replaces it
        self.assertNotRegex(re.search(r"\$PyFlags = @\([^)]*\)", common).group(0), r"-I")                       # not -I: the scripts run from the source folder
        calls = 0
        for name, lines in self.all_code().items():
            for line in lines:
                if re.search(r"& \$Py\b", line):
                    calls += 1
                    self.assertIn("& $Py @PyFlags ", line, (name, line))
        self.assertGreaterEqual(calls, 14)

    def test_the_usage_files_are_counted_by_the_reporters_own_scan_through_one_shared_helper(self):
        common = script_text(COMMON)
        self.assertEqual(common.count("function Invoke-UsageScan"), 1)
        self.assertNotIn("Get-UsageAccounts", "".join(script_text(name) for name in ALL))
        for needle in ("from quota_balancer.reporter import capacityreader", "capacityreader.scan(sys.argv[1]", "found.folder_unreadable", "found.too_many_entries",
                       "sys.stdin.read()", "USAGE_SCAN_FAILED", "except BaseException:", "ConvertTo-Json -Compress -EscapeHandling EscapeNonAscii", "$Stdin | & $Py @PyFlags -c $UsageHelper $BusRoot $Mode"):
            self.assertIn(needle, common, needle)
        for copied in ("EnumerateFileSystemEntries", "16384", "Select-Object -First 64", "ReadAllText", "$Version -eq 2"):
            self.assertNotIn(copied, common, copied)                              # no copy of the reporter's rules in PowerShell
        self.assertNotIn("print(text", common)
        for name in ("switchon-03-check.ps1", "switchon-08-table.ps1"):
            self.assertIn("Invoke-UsageScan", script_text(name), name)
        self.assertNotIn("Invoke-UsageScan", "".join(script_text(n) for n in STEPS if n not in ("switchon-03-check.ps1", "switchon-08-table.ps1")))
        step8 = script_text("switchon-08-table.ps1")
        self.assertIn("Invoke-UsageScan 'match' @{ claude = $ClaudeText; codex = $CodexText }", step8)      # the texts go in as data, never as an argument
        self.assertLess(step8.index("Invoke-UsageScan"), step8.index("AppendAllText"))

    def test_step_11_compares_the_exact_command_step_10_launches(self):
        common, server = script_text(COMMON), script_text("switchon-10-server.ps1")
        self.assertIn("$ServeArgs = Get-ServeArgs", server)                       # one list for both steps
        self.assertIn("@('-m', 'quota_balancer', '--db', $Store, 'serve', '--host', '127.0.0.1', '--port', [string]$PortNumber)", common)
        for needle in ("$Want = @($PyFlags) + @(Get-ServeArgs)", "$Words.Count -ne ($Want.Count + 1)", "$Words[$Index + 1] -ceq $Want[$Index]", "-ieq $Store",
                       "if ($Row.Path -ieq $Py) { return }", "$Row.ParentCommandLine -cne $Row.CommandLine", "$Config -notlike 'C:\lw\*'", "executable", "-not $Row.CommandLine -or -not $Row.Path"):
            self.assertIn(needle, common, needle)
        self.assertNotIn("-contains 'serve'", common)                              # not a bag of words any more

    def test_the_reporter_is_started_with_the_private_settings_and_nothing_else(self):
        self.assertIn("@('-m', 'quota_balancer', 'reporter', 'run', '--config', $ConfigPath)", script_text("switchon-11-reporter.ps1"))

    def test_the_settings_have_no_other_lane(self):
        text = script_text("switchon-07-settings.ps1") + script_text("switchon-08-table.ps1")
        for word in ("limit_events", "gateway_receipts", "gateway_report", "admission", "[agents"):
            self.assertNotIn(word, text)
        self.assertEqual(text.count("[[readings.sources]]"), 2)


class Runbook(unittest.TestCase):
    def setUp(self):
        self.text = norm(RUNBOOK.read_text(encoding="utf-8"))

    def test_it_states_the_link_rule_the_time_cut_and_the_corrected_wording(self):
        flat = " ".join(self.text.split())
        for needle in ("No link anywhere in the live folder", "never following a link", "50,000 entries", "only you, SYSTEM and Administrators can write in it",
                       "switchon-12-baseline.ps1 -Again", "Use `-Again` for nothing else", "30 seconds ahead", "the stated clock tolerance",
                       "does **not** prove the reporter is still running", "Waiting does not prove that the reporter's delivery queue is empty",
                       "later than both the clock time and the delivered maximum"):
            self.assertIn(needle, flat, needle)
        self.assertNotIn("privacy helpers are unchanged", flat)
        self.assertNotIn("running for about a minute", flat)

    def test_it_has_every_step_and_the_sections_the_operator_needs(self):
        for needle in ("# Switching on usage readings on Laptop B", "## What this does, in plain words", "## Before you start", "## How to run each step",
                       "## Where things live", "## Step 0", "## Step 1", "## Step 2", "## Step 3", "## Step 4", "## Step 5 (operator step)",
                       "### Check after step 5 (step 6)", "## Step 7", "## Step 8 (operator step)", "### Check after step 8 (step 9)", "## Step 10", "## Step 11",
                       "## Step 12: record the baseline", "## Wait for a real turn, then step 13", "## Step 14", "## Step 15", "## If something goes wrong", "## Tell the lead when you are done",
                       "## Technical detail", "agenttalk capacity", "C:\\lw\\live", "127.0.0.1", "FORBIDDEN", "PASSED",
                       "Done when", "never run `pip install`", "must **never** be changed"):
            self.assertIn(needle, self.text, needle)

    def test_the_operator_steps_say_who_runs_them_and_the_text_stays_on_the_screen(self):
        self.assertIn("**type the parts only from your own screen, only into this script's four", self.text)
        self.assertIn("never into the repository, a message to the lead, a report or a chat", " ".join(self.text.split()))
        self.assertIn("**Never send** `reporter.toml`", self.text)

    def test_it_holds_placeholders_only(self):
        self.assertIsNone(re.search(r"(claude|codex):[^<\s:`]+:home-[0-9a-f]{8}", self.text))
        self.assertIsNone(re.search(r"home-[0-9a-f]{8}", self.text))
        self.assertNotIn("\\Users\\", self.text)

    def test_every_script_it_names_exists_and_every_script_is_named(self):
        named = set(re.findall(r"switchon-[\w\-]+\.ps1", self.text))
        self.assertEqual(named - {"switchon-common.ps1", "switchon-guard.ps1"}, set(STEPS))
        self.assertTrue(named <= set(ALL) | {"switchon-99-extra.ps1"})

    def test_it_says_what_it_does_not_do(self):
        flat = " ".join(self.text.split())
        for needle in ("nothing leaves this machine", "does **not** touch the gateway, the money reader or limit events",
                       "never reads a file of another team and never reads a folder outside `C:\\lw`", "It never touches the agenttalk folder",
                       "**Never set an `LW_SWITCHON_...` environment variable.**"):
            self.assertIn(needle, flat, needle)


@unittest.skipUnless(shutil.which("pwsh") and sys.platform == "win32", "needs PowerShell 7 on Windows")
class WithPowerShell(unittest.TestCase):
    def pwsh(self, *args, env=None):
        return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", *args], capture_output=True, text=True, timeout=120,
                              env=dict(os.environ, **(env or {})))

    def test_every_script_parses(self):
        command = ("$bad = 0; foreach ($f in Get-ChildItem -LiteralPath '%s' -Filter '*.ps1') { $e = $null; $t = $null; "
                   "[void][System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$t, [ref]$e); "
                   "if ($e.Count) { $bad++; $f.Name } }; exit $bad" % SCRIPTS)
        done = self.pwsh("-Command", command)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, ""), done.stdout + done.stderr)

    def test_step_0_prints_the_version_and_the_sentence(self):
        done = self.pwsh("-File", str(SCRIPTS / STEPS[0]))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split("\n")[1].strip(), "scripts run here")


if __name__ == "__main__":
    unittest.main()
