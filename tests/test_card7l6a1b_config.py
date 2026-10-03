"""Card L6a-1b, step 1: the settings of a receipt command are checked at start-up, and nothing is started.

* `command` (a program for the reporter to run), `timeout_seconds` and `env` are checked, and every bad setting is refused
  with one fixed sentence that never holds text from the file;
* the pure option check refuses an argument that could be read as one of the options the reporter adds itself
  (`--after`, `--limit`, `--json`), and only those;
* the pure quoting function gives the same command line as the standard library's;
* a command block that passes every check is STILL refused, with today's sentence, until the runner arrives (step 3).
Synthetic data only; no process is started.
"""

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from quota_balancer.reporter import cmdline
from quota_balancer.reporter import config as cfg

from tests.l6a1_env import GATEWAY_ALIAS
from tests.reporter_env import agent_table, config_text

LOUD = "loud-value-l6a1b-5f3a"
ACCEPTED = "<accepted>"
PROGRAM = r"C:\tools\receipts\agenttalk.exe"


def _q(value):
    return json.dumps(value)


class _Case(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH") or None
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-l6a1b-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.store = os.path.join(self.dir, "admission.db")
        patcher = mock.patch.object(cfg, "_windows", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def block(self, command=None, keys="", env=None, fixture=False):
        lines = ["[gateway_receipts]", "gateway = %s" % _q(GATEWAY_ALIAS), "admission_store = %s" % _q(self.store)]
        if fixture:
            lines.append("fixture = %s" % _q(os.path.join(self.dir, "r.json")))
        if command is not None:
            lines.append("command = %s" % _q(command))
        text = "\n".join(lines) + "\n" + keys
        if env is not None:
            text += "[gateway_receipts.env]\n" + "".join("%s = %s\n" % (name, _q(value)) for name, value in env.items())
        return text

    def parse(self, raw):
        return cfg.parse_config(config_text("http://127.0.0.1:8443", os.path.join(self.dir, "journal"),
                                            os.path.join(self.dir, "state", "r.db"), os.path.join(self.dir, "p.json"),
                                            {"alpha": agent_table()}, extra=raw))

    def message(self, raw):
        """The refusal's sentence, or ACCEPTED when the block is a valid source."""
        try:
            self.parse(raw)
        except cfg.ConfigError as caught:
            self.assertEqual(caught.code, cfg.CONFIG_INVALID)
            return caught.message
        return ACCEPTED


class Sentences(_Case):
    """Every refusal, with its exact sentence."""

    def test_both_sources_or_neither(self):
        self.assertEqual(self.message(self.block(command=[PROGRAM], fixture=True)),
                         "gateway_receipts: name exactly one of fixture or command")
        self.assertEqual(self.message(self.block()), "gateway_receipts: name exactly one of fixture or command")

    def test_a_command_is_refused_when_the_system_is_not_windows(self):
        with mock.patch.object(cfg, "_windows", return_value=False):
            self.assertEqual(self.message(self.block(command=[PROGRAM])),
                             "Command receipt sources are only available on Windows in this version; use a fixture file.")
        self.assertEqual(cfg.MSG_COMMAND_NOT_WINDOWS,
                         "Command receipt sources are only available on Windows in this version; use a fixture file.")

    def test_the_shape(self):
        shape = "gateway_receipts: the command must be a list of 1 to 16 texts"
        too_long = [PROGRAM] + ["x"] * 16
        for bad in ([], LOUD, 5, True, too_long, [PROGRAM, 5], [PROGRAM, ""], [PROGRAM, "y" * 1025], {}):
            with self.subTest(bad=repr(bad)[:30]):
                self.assertEqual(self.message(self.block(command=bad)), shape)
        self.assertEqual(self.message(self.block(command=[PROGRAM] + ["x"] * 15)),
                         ACCEPTED)                         # 16 texts is the most

    def test_the_program_must_be_an_absolute_path(self):
        absolute = "gateway_receipts: the command must start with an absolute path"
        for bad in ("agenttalk.exe", r"tools\agenttalk.exe", r"\tools\agenttalk.exe", "/tools/agenttalk.exe",
                    r"C:agenttalk.exe", "./agenttalk.exe"):
            with self.subTest(bad=bad):
                self.assertEqual(self.message(self.block(command=[bad])), absolute)

    def test_a_network_path_is_refused(self):
        network = "gateway_receipts: the command may not start from a network or device path"
        for bad in (r"\\server\share\agenttalk.exe", "//server/share/agenttalk.exe", r"\\?\UNC\server\share\a.exe",
                    r"\\?\C:\tools\agenttalk.exe", r"\\.\C:\tools\agenttalk.exe", "//?/C:/tools/agenttalk.exe",
                    "//./C:/tools/agenttalk.exe", r"\/server/share/a.exe", r"/\server/share/a.exe",
                    r"//server\share\a.exe", r"\\localhost\C$\a.exe"):
            with self.subTest(bad=bad):
                self.assertEqual(self.message(self.block(command=[bad])), network)

    def test_the_program_must_be_an_exe(self):
        exe = "gateway_receipts: the command must start with an .exe file"
        for bad in (r"C:\tools\run.bat", r"C:\tools\run.cmd", r"C:\tools\agenttalk", r"C:\tools\agenttalk.exe.txt",
                    r"C:\tools\agenttalk.exe ", r"C:\tools\agenttalk.exe.", r"C:\tools\agenttalk.com",
                    r"C:\tools\agenttalk.ps1"):
            with self.subTest(bad=bad):
                self.assertEqual(self.message(self.block(command=[bad])), exe)
        for good in (r"C:\tools\agenttalk.exe", r"C:\tools\AGENTTALK.EXE", r"C:/tools/agenttalk.Exe", r"D:\a b\x.exe"):
            with self.subTest(good=good):
                self.assertEqual(self.message(self.block(command=[good])), ACCEPTED)

    def test_a_double_quote_or_a_control_character_in_any_argument(self):
        bad_char = "gateway_receipts: the command has a character it may not have"
        for bad in ('say "hi"', 'a"b', "line\nbreak", "tab\there", "nul\x00", "bell\x07", "del\x7f", "\x1b[0m"):
            with self.subTest(bad=repr(bad)):
                self.assertEqual(self.message(self.block(command=[PROGRAM, "-m", bad])), bad_char)
        self.assertEqual(self.message(self.block(command=[r"C:\tools\a" + '"' + r"b.exe"])), bad_char)

    def test_a_reserved_option_in_any_spelling(self):
        reserved = ("gateway_receipts: the command may not name --after, --limit or --json in any spelling; "
                    "the reporter adds them")
        for bad in cmdline_refused():
            for before in ([], ["-m", "agenttalk", "gateway", "receipts"], ["--root", "x", "-v"]):
                with self.subTest(bad=bad, before=len(before)):
                    self.assertEqual(self.message(self.block(command=[PROGRAM] + before + [bad])), reserved)

    def test_timeout_seconds(self):
        sentence = "gateway_receipts: timeout_seconds must be a whole number from 1 to 60"
        for bad in ("0", "-1", "61", "true", "false", "5.0", "1e1", _q("5"), "[5]", "{}"):
            with self.subTest(bad=bad):
                self.assertEqual(self.message(self.block(command=[PROGRAM], keys="timeout_seconds = %s\n" % bad)),
                                 sentence)
        for good in (1, 20, 60):
            with self.subTest(good=good):
                self.assertEqual(self.message(self.block(command=[PROGRAM], keys="timeout_seconds = %d\n" % good)),
                                 ACCEPTED)

    def test_timeout_and_env_belong_to_a_command_source(self):
        sentence = "gateway_receipts: timeout_seconds and env belong to a command source"
        self.assertEqual(self.message(self.block(fixture=True, keys="timeout_seconds = 5\n")), sentence)
        self.assertEqual(self.message(self.block(fixture=True, env={"A": "x"})), sentence)

    def test_the_env_table(self):
        shape = "gateway_receipts: env must be a table of at most 8 texts"
        name = "gateway_receipts: an env name is not valid"
        value = "gateway_receipts: an env value has a character it may not have"
        nine = {"V%d" % number: "x" for number in range(9)}
        self.assertEqual(self.message(self.block(command=[PROGRAM], env=nine)), shape)
        self.assertEqual(self.message(self.block(command=[PROGRAM], keys="env = 5\n")), shape)
        self.assertEqual(self.message(self.block(command=[PROGRAM], keys="env = {A = 5}\n")), shape)
        self.assertEqual(self.message(self.block(command=[PROGRAM], keys='env = {A = ""}\n')), shape)
        for bad in ("lower", "1A", "A-B", "A B", "A" * 65, "\u00c4"):
            with self.subTest(name=bad):
                self.assertEqual(self.message(self.block(command=[PROGRAM], keys="env = {%s = \"x\"}\n" % _q(bad))), name)
        for bad in ("a\nb", "a\x00b", "a\x7fb", "a\x1bb"):
            with self.subTest(value=repr(bad)):
                self.assertEqual(self.message(self.block(command=[PROGRAM], keys="env = {A = %s}\n" % _q(bad))), value)
        eight = {"V%d" % number: "x" for number in range(8)}
        self.assertEqual(self.message(self.block(command=[PROGRAM], env=eight)), ACCEPTED)
        self.assertEqual(self.message(self.block(command=[PROGRAM], keys='env = {A_1 = "a b"}\n')),
                         ACCEPTED)

    def test_no_refusal_holds_text_from_the_file(self):
        bad_blocks = (
            self.block(command=[PROGRAM, LOUD + '"']), self.block(command=[LOUD]), self.block(command=[LOUD + ".bat"]),
            self.block(command=[r"C:\x\\" + LOUD + ".exe", "--after=" + LOUD]),
            self.block(command=[PROGRAM], env={"A": LOUD + "\x00"}), self.block(command=[PROGRAM], env={LOUD: "x"}),
            self.block(command=[PROGRAM], keys="timeout_seconds = %s\n" % _q(LOUD)))
        for raw in bad_blocks:
            self.assertNotIn(LOUD, self.message(raw))


class Platform(unittest.TestCase):

    def test_the_platform_check_is_true_only_on_windows(self):
        for name, expected in (("win32", True), ("linux", False), ("darwin", False), ("cygwin", False)):
            with mock.patch.object(cfg.sys, "platform", name):
                self.assertEqual(cfg._windows(), expected)


class Accepted(_Case):
    """A block that passes every check is a source now (card L6a-1b, step 3)."""

    def test_a_block_that_passes_every_check_is_accepted_and_keeps_its_settings(self):
        raw = self.block(command=[PROGRAM, "-m", "agenttalk", "gateway", "receipts"], keys="timeout_seconds = 30\n",
                         env={"AGENTTALK_ROOT": r"C:\lw\team", "TEMP_X": "a b"})
        source = self.parse(raw).gateway_receipts
        self.assertEqual(source.command, (PROGRAM, "-m", "agenttalk", "gateway", "receipts"))
        self.assertEqual((source.timeout_seconds, source.env, source.fixture),
                         (30, (("AGENTTALK_ROOT", r"C:\lw\team"), ("TEMP_X", "a b")), None))
        self.assertEqual(repr(source), "GatewayReceipts(<hidden>)")

    def test_the_default_time_limit_is_20_and_the_program_path_is_kept_with_backslashes(self):
        source = self.parse(self.block(command=["C:/tools/receipts/agenttalk.exe", "x"])).gateway_receipts
        self.assertEqual((source.timeout_seconds, source.command[0]), (20, r"C:\tools\receipts\agenttalk.exe"))

    def test_the_checks_still_come_first_and_their_own_sentence_wins(self):
        self.assertNotEqual(self.message(self.block(command=[PROGRAM, "--after"])), ACCEPTED)

    def test_a_fixture_block_works_as_before(self):
        source = self.parse(self.block(fixture=True)).gateway_receipts
        self.assertEqual((source.gateway, source.max_bytes, source.page_limit), (GATEWAY_ALIAS, 1024 * 1024, 100))

    def test_a_fixture_block_with_the_other_settings_still_works(self):
        source = self.parse(self.block(fixture=True, keys="max_bytes = 2048\npage_limit = 7\n")).gateway_receipts
        self.assertEqual((source.max_bytes, source.page_limit), (2048, 7))


def cmdline_refused():
    return ["--after", "--after=5", "--aft", "--a", "--limit", "--lim=1", "--l", "--json", "--j", "--js=x", "--",
            "--=x", "--afte", "--limit=", "--json=1", "--jso"]


def cmdline_accepted():
    return ["--afterx", "--root", "--limits", "-m", "-a", "a", "json", "after", "limit", "--jsonx", "--b", "--x=after",
            "-after", "-", "--root=--after", "--Limit", "--AFTER", "x--after", "", "---", "--after-x", "--limit_x"]


class OptionCheck(unittest.TestCase):

    def test_these_are_refused(self):
        for text in cmdline_refused():
            with self.subTest(text=text):
                self.assertTrue(cmdline.names_reserved_option(text))

    def test_these_pass(self):
        for text in cmdline_accepted():
            with self.subTest(text=text):
                self.assertFalse(cmdline.names_reserved_option(text))

    def test_every_prefix_of_every_reserved_word_is_refused_with_and_without_a_value(self):
        for word in cmdline.RESERVED_OPTIONS:
            for size in range(1, len(word) + 1):
                for tail in ("", "=", "=7", "=--after"):
                    with self.subTest(word=word, size=size, tail=tail):
                        self.assertTrue(cmdline.names_reserved_option("--" + word[:size] + tail))

    def test_it_is_pure(self):
        before = dict(os.environ)
        self.assertFalse(cmdline.names_reserved_option("--root"))
        self.assertEqual(dict(os.environ), before)

    def test_the_reserved_words_are_the_three_the_reporter_adds(self):
        self.assertEqual(cmdline.RESERVED_OPTIONS, ("after", "limit", "json"))


AWKWARD = [
    [], [""], ["a"], ["a", "b"], ["a b"], ["a\tb"], ["a  b"], [" "], ["\t"], ["", ""], ["a", "", "b"],
    ["a\\"], ["a b\\"], ["a b\\\\"], ["a\\\\"], ["\\"], ["\\\\"], ["\\\\\\"], ["a\\b"], ["a\\\\b"],
    ['a"b'], ['"'], ['""'], ['\\"'], ['\\\\"'], ['\\\\\\"'], ['a\\"b'], ['a b"c'], ['a b\\"c'], ['a b\\\\"c'],
    ['"a b"'], ['say "hi there"'], ['\\"a b\\"'], ["C:\\Program Files\\x\\"], ["C:\\Program Files\\x\\\\"],
    ["C:\\a\\b.exe", "--after", "5"], ["x", 'a b\\\\', "y"], ["\u00e4 \u00f6", "\u65e5\u672c\u8a9e"],
    ["emoji \U0001F600", "end\\"], ["a\nb"], ["a\r\nb c"], ["tab\t\\\\"], ["=", "--x=a b", "-m"],
    ["a" * 300 + " " + "b" * 300 + "\\"], ["\\" * 10 + '"' + "\\" * 5], ['"' * 5], [" " + '"' + " "],
    ["--limit=1", "a b", "\\\\server\\share\\"], ["%PATH%", "^&", "|<>"], ["*", "?", "a*b"],
]


class Quoting(unittest.TestCase):

    def test_the_command_line_is_exactly_what_the_standard_library_makes(self):
        self.assertGreaterEqual(len(AWKWARD), 30)
        for arguments in AWKWARD:
            with self.subTest(arguments=repr(arguments)[:60]):
                self.assertEqual(cmdline.quote_command(arguments), subprocess.list2cmdline(arguments))

    def test_it_follows_the_rules_in_plain_cases(self):
        self.assertEqual(cmdline.quote_command(["a", "b c", "", 'd"e']), 'a "b c" "" d\\"e')
        self.assertEqual(cmdline.quote_command(["trail\\", "a b\\"]), 'trail\\ "a b\\\\"')
        self.assertEqual(cmdline.quote_command(['q\\"x']), 'q\\\\\\"x')

    def test_the_line_splits_back_into_the_same_arguments_by_the_c_runtime_rules(self):
        """A small independent reader of the line (the C runtime's rules) gets the arguments back."""
        for arguments in AWKWARD:
            if not arguments or "\n" in "".join(arguments):
                continue
            with self.subTest(arguments=repr(arguments)[:60]):
                self.assertEqual(_split(cmdline.quote_command(arguments)), arguments)

    def test_it_never_imports_the_subprocess_module(self):
        for name in ("cmdline.py", "config.py"):
            path = os.path.join(os.path.dirname(cmdline.__file__), name)
            with open(path, encoding="utf-8") as handle:
                self.assertNotIn("import subprocess", handle.read())
        self.assertNotIn("subprocess", cmdline.__dict__)


def _split(line):
    """The C runtime's rules for reading a command line back into arguments (enough to check our own quoting)."""
    out, index, size = [], 0, len(line)
    while index < size:
        while index < size and line[index] in " \t":
            index += 1
        if index >= size:
            break
        argument, quoted = [], False
        while index < size and (quoted or line[index] not in " \t"):
            slashes = 0
            while index < size and line[index] == "\\":
                slashes += 1
                index += 1
            if index < size and line[index] == '"':
                argument.append("\\" * (slashes // 2))
                if slashes % 2:
                    argument.append('"')
                else:
                    quoted = not quoted
                index += 1
            else:
                argument.append("\\" * slashes)
                if index < size and (quoted or line[index] not in " \t"):
                    argument.append(line[index])
                    index += 1
        out.append("".join(argument))
    return out


if __name__ == "__main__":
    unittest.main()
