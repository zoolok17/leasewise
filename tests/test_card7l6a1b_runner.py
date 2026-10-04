"""Card L6a-1b, step 2: the contained Windows runner (`quota_balancer/reporter/runner.py`), which the reporter's receipt reading calls when a `command` source is configured.

What is proven here, with real processes (small scripts that the tests write, run by the base Python; never agenttalk, never a
gateway):

* the program is created inside its job and frozen, checked, and only then run; a helper it starts in its first instant is
  in the job too; a reporter that dies at the worst moment leaves nothing behind;
* one deadline with no thread: a program that never exits, one that never stops writing, a helper that holds the output
  open, a flood on standard error, too much output;
* the end of the job is confirmed, and a read whose end is not confirmed is never a success;
* every setup step can fail, and each failure leaves no program and no open handle;
* the program gets a small environment, no stray handle, standard input that ends at once, and the options in the right order.

Liveness is checked through a handle the test keeps (taken while the process is alive), never by name and never by a bare
process id that could be reused. Handshakes use files the programs write, not fixed sleeps; real-time bounds carry an explicit
scheduling margin. Windows only.
"""

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from quota_balancer.reporter import cmdline, runner

WINDOWS = sys.platform == "win32"
PYTHON = getattr(sys, "_base_executable", sys.executable)         # the real interpreter, not the venv launcher
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARGIN = 2.0                                                        # scheduling margin on real-time bounds, in seconds
PAGE = ('{"after_seq":0,"envelope_version":1,"generation":"0123456789abcdef0123456789abcdef","has_more":false,'
        '"next_seq":0,"receipts":[]}')

# --- the programs the tests run -----------------------------------------------------------------------------------

PROGRAMS = {
    "page": "import sys\nsys.stdout.write(%r)\n" % PAGE,
    "argv": ("import json, sys\nopen(sys.argv[1], 'w').write(json.dumps(sys.argv[2:]))\nsys.stdout.write(%r)\n" % PAGE),
    "env": ("import json, os, sys\nopen(sys.argv[1], 'w').write(json.dumps({k: v for k, v in os.environ.items() "
            "if not k.startswith('=')}))\nsys.stdout.write(%r)\n" % PAGE),
    "cwd": "import os, sys\nopen(sys.argv[1], 'w').write(os.getcwd())\nsys.stdout.write(%r)\n" % PAGE,
    "stdin": ("import sys\nopen(sys.argv[1], 'w').write(repr(sys.stdin.read()))\nsys.stdout.write(%r)\n" % PAGE),
    "exit1": "import sys\nsys.stdout.write(%r)\nsys.stdout.flush()\nsys.exit(1)\n" % PAGE,
    "empty": "pass\n",
    "flood": ("import sys\nsys.stderr.write('x' * (10 * 1024 * 1024))\nsys.stderr.flush()\nsys.stdout.write(%r)\n" % PAGE),
    "big": ("import sys\nsys.stdout.write('x' * int(sys.argv[1]))\n"),
    "endless": ("import sys\nwhile True:\n    sys.stdout.write('x' * 65536)\n    sys.stdout.flush()\n"),
    "sleep": ("import os, sys, time\nopen(sys.argv[1] + '.t', 'w').write(str(os.getpid()))\n"
              "os.replace(sys.argv[1] + '.t', sys.argv[1])\ntime.sleep(300)\n"),
    "first_action": "import sys\nopen(sys.argv[1], 'w').write('ran')\nsys.stdout.write(%r)\n" % PAGE,
    # the first thing it does is start a helper (which keeps running), wait until the helper says it is up, and exit
    "spawner": ("import os, subprocess, sys, time\nmarker = sys.argv[1]\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import os, sys, time\\n"
                "open(sys.argv[1] + \".t\", \"w\").write(str(os.getpid()))\\nos.replace(sys.argv[1] + \".t\", sys.argv[1])\\n"
                "time.sleep(300)', marker], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
                "stderr=subprocess.DEVNULL)\n"
                "while not os.path.exists(marker):\n    time.sleep(0.01)\nsys.stdout.write(%r)\n" % PAGE),
    # the same, but the helper keeps the output pipe open
    "holder": ("import os, subprocess, sys, time\nmarker = sys.argv[1]\n"
               "child = subprocess.Popen([sys.executable, '-c', 'import os, sys, time\\n"
               "open(sys.argv[1] + \".t\", \"w\").write(str(os.getpid()))\\nos.replace(sys.argv[1] + \".t\", sys.argv[1])\\n"
               "time.sleep(300)', marker], stdout=sys.stdout)\n"
               "while not os.path.exists(marker):\n    time.sleep(0.01)\nsys.stdout.write(%r)\nsys.stdout.flush()\n" % PAGE),
    # a helper that holds the output for a short, finite time (the first attempt took 8 s for this)
    "finite_holder": ("import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', 'import time\\ntime.sleep(8)'], "
                      "stdout=sys.stdout)\nsys.stdout.write(%r)\nsys.stdout.flush()\n" % PAGE),
    "breakaway": ("import subprocess, sys\ntry:\n    subprocess.Popen([sys.executable, '-c', 'pass'], "
                  "creationflags=0x01000000)\n    note = 'started'\nexcept OSError as exc:\n    note = type(exc).__name__\n"
                  "open(sys.argv[1], 'w').write(note)\nsys.stdout.write(%r)\n" % PAGE),
    "ready_then_sleep": ("import os, sys, time\nopen(sys.argv[1] + '.t', 'w').write(str(os.getpid()))\n"
                         "os.replace(sys.argv[1] + '.t', sys.argv[1])\ntime.sleep(300)\n"),
}


def write_programs(directory):
    return {name: _write(os.path.join(directory, name + ".py"), text) for name, text in PROGRAMS.items()}


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


# --- a handle the test keeps on a process ---------------------------------------------------------------------------

if WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = ctypes.c_void_p
    _k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    _k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    _k32.WaitForSingleObject.restype = ctypes.c_ulong
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]


class Watch:
    """A handle on a process taken while it is alive: it answers whether the process is gone, and a reused process id can
    never fool it."""

    def __init__(self, pid):
        self.handle = _k32.OpenProcess(0x00100000 | 0x1000, False, pid)            # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
        # no handle: we do NOT know whether the process is gone (it may be, or Windows may have refused); see `opened`

    @property
    def opened(self):
        return bool(self.handle)

    def gone(self, seconds=0):
        if not self.handle:
            raise AssertionError("no process handle was retained, so whether the process is gone is unknown")
        return _k32.WaitForSingleObject(self.handle, int(seconds * 1000)) == 0

    def close(self):
        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = None


def slurp(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def wait_for(path, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if os.path.exists(path):
            try:
                with open(path, encoding="ascii") as handle:
                    text = handle.read()
            except OSError:
                text = ""                                       # the program is replacing the file right now
            if text:
                return text
        time.sleep(0.01)
    raise AssertionError("the handshake file never appeared")


class Probe:
    """A `stop` object that also keeps handles on the processes named in marker files as soon as they appear (so liveness is
    checked through a retained handle), and can run a check at that moment. `is_set` answers True once every `ready` path
    is ready and `then` has run, so a read can be ended on purpose. A ready path that is also a marker (it names a process)
    is ready only once its handle is retained: the file existing is not enough, because the file may not have been read yet.
    A ready path that names no process is ready when it exists. `opener` is the way a marker is opened (a test can make it
    fail)."""

    def __init__(self, markers=(), ready=None, then=None, opener=open):
        self.markers, self.ready, self.then, self.opener = list(markers), ready, then, opener
        self.watches = {}
        self.fired = False

    def look(self):
        """Take a handle on every marked process whose marker can be read now. The runner reaches this through `is_set`, at its own pace; a
        test that must have the handle before it acts (a deliberate fault) calls it itself."""
        for path in self.markers:
            if path not in self.watches and os.path.exists(path):
                try:
                    with self.opener(path, encoding="ascii") as handle:
                        text = handle.read()
                except OSError:
                    continue                                    # the program is replacing the file right now: look again
                if text:
                    watch = Watch(int(text))
                    if watch.opened:                            # a failed open is unknown, never "gone": try again next look
                        self.watches[path] = watch

    def hold(self, path, seconds=10.0):
        """Look until the process named in `path` is held (it is alive: the marker names it and the test has not ended it yet)."""
        end = time.monotonic() + seconds
        while path not in self.watches:
            self.look()
            if time.monotonic() > end:
                raise AssertionError("the marker never named a process we could hold")
            time.sleep(0.01)

    def is_set(self):
        self.look()
        if self.ready and not self.fired and all(self._is_ready(path) for path in self.ready):
            self.fired = True
            if self.then:
                self.then()
            return True
        return False

    def _is_ready(self, path):
        if path in self.markers:
            return path in self.watches
        return os.path.exists(path)

    def close(self):
        for watch in self.watches.values():
            watch.close()


# --- a Windows layer that can be made to fail, and that keeps a handle on every process it creates -------------------------

class Spy(runner._Win if WINDOWS else object):
    """The real calls, recorded; `fail` makes one call answer a fixed value instead; `after` runs a function once the real call
    has returned (with its result). Every handle it hands out is noted, and every close, so a leak shows."""

    def __init__(self, fail=None, after=None):
        self.fail, self.after = dict(fail or {}), dict(after or {})
        self.calls, self.opened, self.closed, self.watch = [], set(), [], None
        self.pid = None

    def note(self, handle):
        if handle:
            self.opened.add(handle)
        return handle


def _wrap(name):
    real = getattr(runner._Win, name)

    def method(self, *args, **kwargs):
        self.calls.append(name)
        if name in self.fail:
            value = self.fail[name]
            return value(*args) if callable(value) else value
        value = real(self, *args, **kwargs)
        if name == "create_job" or name == "open_nul":
            self.note(value)
        elif name == "create_pipe" and value:
            self.note(value[0]), self.note(value[1])
        elif name == "create_process" and value[0]:
            self.note(value[0][0]), self.note(value[0][1])
            self.pid = value[0][2]
            self.watch = Watch(self.pid)                  # kept from the moment the frozen process exists
        elif name == "close":
            self.closed.append(args[0])
        if name in self.after:
            self.after[name](self, value, *args)
        return value
    method.__name__ = name
    return method


if WINDOWS:
    for _name in [n for n in dir(runner._Win) if not n.startswith("_")]:
        setattr(Spy, _name, _wrap(_name))


@unittest.skipUnless(WINDOWS, "the runner is for Windows only")
class _Case(unittest.TestCase):

    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH") or None
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-l6a1b-run-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.programs = write_programs(self.dir)
        self.spies, self.probes = [], []
        self.addCleanup(self._close_watches)

    def _close_watches(self):
        for spy in self.spies:
            if spy.watch:
                spy.watch.close()
        for probe in self.probes:
            probe.close()

    def path(self, name):
        return os.path.join(self.dir, name)

    def command(self, program, *args):
        return [PYTHON, self.programs[program]] + [str(a) for a in args]

    def run_it(self, program, *args, spy=None, timeout=15, max_bytes=1024 * 1024, env=(), stop=None, after=0, limit=100):
        spy = spy or Spy()
        self.spies.append(spy)
        if stop is not None and isinstance(stop, Probe):
            self.probes.append(stop)
        started = time.monotonic()
        result = runner.run(self.command(program, *args), after, limit, timeout_seconds=timeout, max_bytes=max_bytes,
                            env=env, stop=stop, win=spy)
        self.elapsed = time.monotonic() - started
        return result

    def assert_clean(self, spy):
        """The root process is gone and every handle that was handed out was closed."""
        if spy.watch is not None:
            self.assertTrue(spy.watch.gone(3), "the program is still running")
        self.assertEqual(spy.opened - set(spy.closed), set(), "a handle was left open")


class Normal(_Case):

    def test_a_normal_page_is_read_and_everything_is_ended_and_confirmed(self):
        spy = Spy()
        result = self.run_it("page", spy=spy)
        self.assertEqual((result.word, result.data, result.cleanup_confirmed), (runner.OK, PAGE.encode(), True))
        self.assert_clean(spy)

    def test_the_options_come_last_and_the_configured_arguments_arrive_unchanged(self):
        marker = self.path("argv.json")
        awkward = ["a b", "", "tail\\", 'q"uote' if False else "x y\\\\", "-m", "--root", "\u00e4 \u65e5"]
        result = self.run_it("argv", marker, *awkward, after=7, limit=25)
        self.assertEqual(result.word, runner.OK)
        self.assertEqual(json.loads(slurp(marker)),
                         awkward + ["--after", "7", "--limit", "25", "--json"])

    def test_the_working_directory_is_the_folder_of_the_program_file(self):
        marker = self.path("cwd.txt")
        self.assertEqual(self.run_it("cwd", marker).word, runner.OK)
        self.assertEqual(os.path.normcase(slurp(marker)),
                         os.path.normcase(os.path.dirname(PYTHON)))

    def test_standard_input_ends_at_once(self):
        marker = self.path("stdin.txt")
        self.assertEqual(self.run_it("stdin", marker).word, runner.OK)
        self.assertEqual(slurp(marker), "''")

    def test_the_output_of_an_empty_run_is_empty_bytes_and_ok(self):
        result = self.run_it("empty")
        self.assertEqual((result.word, result.data), (runner.OK, b""))

    def test_a_non_zero_exit_is_exit_code_and_the_output_is_not_used(self):
        result = self.run_it("exit1")
        self.assertEqual((result.word, result.data, result.cleanup_confirmed), (runner.EXIT_CODE, None, True))

    def test_a_flood_on_standard_error_changes_nothing(self):
        result = self.run_it("flood")
        self.assertEqual((result.word, result.data), (runner.OK, PAGE.encode()))

    def test_output_of_exactly_max_bytes_is_read_and_one_more_is_too_large(self):
        self.assertEqual(self.run_it("big", 2048, max_bytes=2048).data, b"x" * 2048)
        result = self.run_it("big", 2049, max_bytes=2048)
        self.assertEqual((result.word, result.data), (runner.TOO_LARGE, None))

    def test_a_program_that_never_stops_writing_is_too_large_at_once(self):
        spy = Spy()
        result = self.run_it("endless", spy=spy, max_bytes=1024 * 1024)
        self.assertEqual(result.word, runner.TOO_LARGE)
        self.assertLess(self.elapsed, 10 + MARGIN)
        self.assert_clean(spy)


class Containment(_Case):

    def test_a_helper_started_in_the_programs_first_instant_is_in_the_job_and_ends_with_it(self):
        marker = self.path("child.pid")
        probe = Probe(markers=[marker])
        spy = Spy()
        result = self.run_it("spawner", marker, spy=spy, stop=probe)
        self.assertEqual(result.word, runner.OK)                      # the root exited, a helper was left running
        self.assertTrue(result.cleanup_confirmed)                     # and the end of the whole job was confirmed
        helper = probe.watches[marker]
        self.assertTrue(helper.gone(3), "the helper was not ended")
        self.assert_clean(spy)

    def test_the_program_is_frozen_and_already_inside_its_job_until_it_is_resumed(self):
        marker = self.path("ran.txt")
        seen = {}

        def look(spy, _value, process, job):
            seen["in_job"] = spy.real_in_job = _value
            seen["ran_before"] = os.path.exists(marker)
            time.sleep(0.4)                                           # a long moment in which it could have run
            seen["ran_after_wait"] = os.path.exists(marker)
        spy = Spy(after={"is_process_in_job": look})
        result = self.run_it("first_action", marker, spy=spy)
        self.assertEqual(result.word, runner.OK)
        self.assertEqual((seen["in_job"], seen["ran_before"], seen["ran_after_wait"]), (True, False, False))
        self.assertTrue(os.path.exists(marker))                       # it ran, after the resume

    def test_the_job_is_named_in_the_creation_attributes_and_nothing_is_assigned_afterwards(self):
        spy = Spy()
        self.assertEqual(self.run_it("page", spy=spy).word, runner.OK)
        self.assertNotIn("assign", " ".join(spy.calls))
        self.assertLess(spy.calls.index("update_attribute"), spy.calls.index("create_process"))
        self.assertEqual(spy.calls.count("update_attribute"), 2)       # the handle list and the job list
        self.assertLess(spy.calls.index("create_process"), spy.calls.index("is_process_in_job"))
        self.assertLess(spy.calls.index("is_process_in_job"), spy.calls.index("resume_thread"))

    def test_a_childs_attempt_to_break_away_from_the_job_is_refused(self):
        marker = self.path("note.txt")
        self.assertEqual(self.run_it("breakaway", marker).word, runner.OK)
        self.assertEqual(slurp(marker), "PermissionError")

    def test_an_unrelated_inheritable_handle_does_not_reach_the_program(self):
        read, write = os.pipe()
        os.set_inheritable(write, True)                               # inheritable, and named nowhere
        seen = {}

        def check():
            os.close(write)                                           # our own copy is closed now
            peeked = runner._Win().peek(_osfhandle(read))
            seen["peek"] = peeked
        marker = self.path("up.pid")
        probe = Probe(markers=[marker], ready=[marker], then=check)
        spy = Spy()
        try:
            result = self.run_it("ready_then_sleep", marker, spy=spy, stop=probe)
        finally:
            os.close(read)
        self.assertEqual(result.word, runner.INTERRUPTED)
        self.assertEqual(seen["peek"][0], "eof")                      # no process holds the other end of the pipe
        self.assert_clean(spy)


def _osfhandle(fd):
    import msvcrt
    return msvcrt.get_osfhandle(fd)


class Deadlines(_Case):

    def test_a_program_that_never_exits_ends_at_the_deadline_and_everything_is_gone(self):
        marker = self.path("sleep.pid")
        probe = Probe(markers=[marker])
        spy = Spy()
        result = self.run_it("sleep", marker, spy=spy, stop=probe, timeout=1)
        self.assertEqual((result.word, result.data, result.cleanup_confirmed), (runner.TIMEOUT, None, True))
        self.assertLess(self.elapsed, 1 + MARGIN)
        self.assertGreaterEqual(self.elapsed, 1)
        self.assertTrue(probe.watches[marker].gone(3))
        self.assert_clean(spy)

    def test_a_helper_that_holds_the_output_after_the_program_exited_is_a_leftover_not_a_hang(self):
        marker = self.path("holder.pid")
        probe = Probe(markers=[marker])
        spy = Spy()
        result = self.run_it("holder", marker, spy=spy, stop=probe, timeout=20)
        self.assertEqual((result.word, result.data, result.cleanup_confirmed), (runner.LEFTOVER, None, True))
        self.assertLess(self.elapsed, runner.LINGER_SECONDS + MARGIN + 2)       # not the 20 s deadline
        self.assertTrue(probe.watches[marker].gone(3))
        self.assert_clean(spy)

    def test_a_finite_helper_holding_the_pipe_does_not_delay_the_return(self):
        """The first attempt took 8.2 s for a 1 s deadline here, waiting for a helper to let go of the pipe."""
        spy = Spy()
        result = self.run_it("finite_holder", spy=spy, timeout=1)
        self.assertIn(result.word, (runner.LEFTOVER, runner.TIMEOUT))
        self.assertLess(self.elapsed, 1 + MARGIN)
        self.assert_clean(spy)

    def test_the_leftover_grace_never_passes_the_deadline(self):
        marker = self.path("holder2.pid")
        probe = Probe(markers=[marker])
        with mock.patch.object(runner, "LINGER_SECONDS", 30.0):
            result = self.run_it("holder", marker, stop=probe, timeout=1)
        self.assertIn(result.word, (runner.LEFTOVER, runner.TIMEOUT))
        self.assertLess(self.elapsed, 1 + MARGIN)

    def test_a_stop_ends_the_read_as_interrupted_with_the_job_ended(self):
        marker = self.path("up2.pid")
        probe = Probe(markers=[marker], ready=[marker])
        spy = Spy()
        result = self.run_it("sleep", marker, spy=spy, stop=probe)
        self.assertEqual((result.word, result.cleanup_confirmed), (runner.INTERRUPTED, True))
        self.assertLess(self.elapsed, 10)
        self.assertTrue(probe.watches[marker].gone(3))
        self.assert_clean(spy)

    def test_an_error_after_the_resume_still_ends_the_program_and_closes_everything(self):
        marker = self.path("boom.pid")
        probe = Probe(markers=[marker])
        calls = {"n": 0}

        def boom(*_args):
            calls["n"] += 1
            if calls["n"] > 3 and os.path.exists(marker):
                probe.hold(marker)          # the fault must not outrun the probe's first look: the program is held before the runner fails
                raise RuntimeError("synthetic fault")
            return ("empty", 0)
        spy = Spy(fail={"peek": boom})
        with self.assertRaises(RuntimeError):
            self.run_it("sleep", marker, spy=spy, stop=probe)
        self.assertTrue(probe.watches[marker].gone(3))
        self.assert_clean(spy)


class Setup(_Case):
    """Every setup step can fail. Each failure is a closed word, runs nothing, and leaves nothing open."""

    def check(self, name, value, word):
        marker = self.path("never-%s.txt" % name)
        spy = Spy(fail={name: value})
        result = self.run_it("first_action", marker, spy=spy)
        self.assertEqual((result.word, result.data), (word, None), name)
        self.assertFalse(os.path.exists(marker), "the program ran")
        self.assert_clean(spy)
        return spy

    def test_job_and_pipe_and_attribute_failures(self):
        self.check("create_job", None, runner.CANNOT_CONTAIN)
        self.check("set_limits", False, runner.CANNOT_CONTAIN)
        self.check("create_pipe", None, runner.CANNOT_RUN)
        self.check("make_private", False, runner.CANNOT_RUN)
        self.check("open_nul", None, runner.CANNOT_RUN)
        self.check("init_attributes", None, runner.CANNOT_CONTAIN)

    def test_a_job_attribute_that_is_unsupported_or_refused_is_never_worked_around(self):
        """No create-then-assign: when the job cannot be named at creation, the program is not created at all."""
        calls = {"n": 0}

        def second_fails(*_args):
            calls["n"] += 1
            return calls["n"] == 1                                     # the handle list works, the job list does not
        spy = self.check("update_attribute", second_fails, runner.CANNOT_CONTAIN)
        self.assertNotIn("create_process", spy.calls)
        self.assertEqual(calls["n"], 2)
        spy = self.check("update_attribute", False, runner.CANNOT_CONTAIN)
        self.assertNotIn("create_process", spy.calls)

    def test_a_process_that_cannot_be_created(self):
        self.check("create_process", (None, 2), runner.CANNOT_RUN)             # file not found
        self.check("create_process", (None, 193), runner.CANNOT_RUN)           # not a valid program
        spy = self.check("create_process", (None, 5), runner.CANNOT_CONTAIN)   # refused (an enclosing job, say): fail closed
        self.assertNotIn("resume_thread", spy.calls)

    def test_a_process_not_proven_to_be_in_its_job_is_ended_and_never_resumed(self):
        for answer in (False, None):
            with self.subTest(answer=answer):
                spy = self.check("is_process_in_job", answer, runner.CANNOT_CONTAIN)
                self.assertNotIn("resume_thread", spy.calls)

    def test_a_resume_that_fails_or_finds_the_wrong_count_ends_the_process(self):
        for answer in (-1, 0, 2):
            with self.subTest(answer=answer):
                self.check("resume_thread", answer, runner.CANNOT_CONTAIN)

    def test_a_missing_program_file_is_cannot_run_before_anything_is_made(self):
        spy = Spy()
        result = runner.run([os.path.join(self.dir, "nothing.exe")], 0, 1, timeout_seconds=1, max_bytes=1000, win=spy)
        self.assertEqual((result.word, spy.calls), (runner.CANNOT_RUN, []))

    def test_a_nul_in_an_environment_value_is_refused_before_anything_is_made(self):
        spy = Spy()
        result = runner.run(self.command("page"), 0, 1, timeout_seconds=1, max_bytes=1000, env=[("A", "x\x00y")], win=spy)
        self.assertEqual((result.word, spy.calls), (runner.CANNOT_RUN, []))

    def test_every_windows_call_is_declared_with_its_types(self):
        for name in runner.FUNCTIONS:
            function = getattr(runner._k32, name)
            self.assertIsNotNone(function.argtypes, name)
            self.assertTrue(hasattr(function, "restype"), name)
        self.assertGreaterEqual(len(runner.FUNCTIONS), 18)


class Ending(_Case):
    """The end of the job is confirmed, and a read whose end is not confirmed is never a success."""

    def test_a_failed_termination_is_cleanup_incomplete_and_not_a_success(self):
        spy = Spy(fail={"terminate_job": False})
        result = self.run_it("page", spy=spy)
        self.assertEqual((result.word, result.data, result.cleanup_confirmed), (runner.CLEANUP_INCOMPLETE, None, False))
        self.assert_clean(spy)                                         # closing the job (kill on close) was the net

    def test_a_failed_question_about_the_job_is_not_a_confirmation(self):
        spy = Spy(fail={"job_active": None})
        result = self.run_it("page", spy=spy)
        self.assertEqual((result.word, result.data, result.cleanup_confirmed), (runner.CLEANUP_INCOMPLETE, None, False))

    def test_an_allowance_that_runs_out_is_not_a_confirmation_and_ends_in_time(self):
        spy = Spy(fail={"job_active": 1})
        with mock.patch.object(runner, "CLEANUP_SECONDS", 0.3):
            result = self.run_it("page", spy=spy)
        self.assertEqual((result.word, result.cleanup_confirmed), (runner.CLEANUP_INCOMPLETE, False))
        self.assertLess(self.elapsed, 0.3 + MARGIN + 1)

    def test_a_failure_word_keeps_its_word_but_says_the_end_was_not_confirmed(self):
        spy = Spy(fail={"terminate_job": False})
        result = self.run_it("exit1", spy=spy)
        self.assertEqual((result.word, result.cleanup_confirmed), (runner.EXIT_CODE, False))

    def test_a_signalled_root_with_an_outstanding_child_is_confirmed_once_the_child_is_gone(self):
        marker = self.path("child2.pid")
        probe = Probe(markers=[marker])
        counts = []

        def record(spy, value, *args):
            counts.append(value)
        spy = Spy(after={"job_active": record})
        result = self.run_it("spawner", marker, spy=spy, stop=probe)
        self.assertEqual((result.word, result.cleanup_confirmed), (runner.OK, True))
        self.assertEqual(counts[-1], 0)
        self.assertGreaterEqual(counts[0], 0)
        self.assertTrue(probe.watches[marker].gone(3))

    def test_a_failure_to_end_a_process_that_is_not_proven_in_its_job_is_not_confirmed(self):
        spy = Spy(fail={"is_process_in_job": False, "terminate_process": False})
        result = self.run_it("page", spy=spy)
        self.assertEqual((result.word, result.cleanup_confirmed), (runner.CANNOT_CONTAIN, False))
        # the process was frozen and never resumed; the job handle closing killed it all the same
        self.assertTrue(spy.watch.gone(3))


class Crashes(_Case):
    """The reporter itself dies. These run a helper process that uses the runner, and end the helper by its exact id."""

    HELPER = r'''
import os, sys, time
sys.path.insert(0, %(repo)r)
from quota_balancer.reporter import runner

class Win(runner._Win):
    def create_process(self, *args):
        made, error = super().create_process(*args)
        if made and %(freeze)r:
            with open(%(marker)r + ".t", "w") as handle:
                handle.write(str(made[2]))
            os.replace(%(marker)r + ".t", %(marker)r)
            time.sleep(600)                 # the helper stops right after the creation: nothing resumes the program
        return made, error

result = runner.run(%(command)r, 0, 10, timeout_seconds=60, max_bytes=1000, win=Win())
'''

    def start_helper(self, freeze, command, marker):
        script = self.path("helper.py")
        _write(script, self.HELPER % {"repo": REPO, "freeze": freeze, "marker": marker, "command": command})
        env = dict(os.environ, PYTHONPATH=REPO)
        helper = subprocess.Popen([sys.executable, script], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._end_helper, helper)
        return helper

    def _end_helper(self, helper):
        if helper.poll() is None:
            helper.kill()                                               # by its own handle: this exact process
        helper.wait(10)

    def test_a_reporter_that_dies_right_after_creating_the_program_leaves_nothing_behind(self):
        ran = self.path("ran-crash.txt")
        pid_file = self.path("frozen.pid")
        helper = self.start_helper(True, self.command("first_action", ran), pid_file)
        frozen = Watch(int(wait_for(pid_file)))                         # the frozen process exists; the helper is stopped
        self.addCleanup(frozen.close)
        self.assertFalse(frozen.gone(0.3))
        helper.kill()                                                   # the exact process id the test started
        helper.wait(10)
        self.assertTrue(frozen.gone(5), "the frozen program outlived its reporter")
        self.assertFalse(os.path.exists(ran), "the program ran")

    def test_a_reporter_that_dies_while_the_program_runs_leaves_nothing_behind(self):
        up, child = self.path("up3.pid"), self.path("child3.pid")
        script = self.path("tree.py")
        _write(script, ("import os, subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', 'import os, sys, time\\n"
                        "open(sys.argv[1] + \".t\", \"w\").write(str(os.getpid()))\\nos.replace(sys.argv[1] + \".t\", "
                        "sys.argv[1])\\ntime.sleep(300)', %r])\nopen(%r + '.t', 'w').write(str(os.getpid()))\n"
                        "os.replace(%r + '.t', %r)\ntime.sleep(300)\n") % (child, up, up, up))
        helper = self.start_helper(False, [PYTHON, script], self.path("unused"))
        root, grandchild = Watch(int(wait_for(up))), Watch(int(wait_for(child)))
        self.addCleanup(root.close)
        self.addCleanup(grandchild.close)
        self.assertFalse(root.gone(0.2) or grandchild.gone(0.2))
        helper.kill()
        helper.wait(10)
        self.assertTrue(root.gone(5) and grandchild.gone(5), "the tree outlived its reporter")


class BufferLifetime(_Case):
    """F1 (Codex's read): the attribute list keeps pointers to the two value arrays until it is deleted, so both arrays must
    still be alive when `delete_attributes` runs, on success and on every partial setup failure."""

    def run_checking(self, fail=None):
        import gc
        import weakref
        seen = {"refs": [], "alive": None, "stored_before_first_update": None}
        owners = []

        class Owner(runner._Owned):
            def __init__(self, win):
                super().__init__(win)
                owners.append(self)

        def remember(spy, value, attributes, attribute, array):
            seen["refs"].append(weakref.ref(array))

        def failing(answer):
            def update(attributes, attribute, array):
                if seen["stored_before_first_update"] is None:
                    seen["stored_before_first_update"] = len(owners[0].values)
                seen["refs"].append(weakref.ref(array))
                return answer(len(seen["refs"]))
            return update

        def at_delete(spy, value, attributes):
            gc.collect()
            seen["alive"] = [ref() is not None for ref in seen["refs"]]
            seen["owned"] = len(owners[0].values)
        marker = self.path("never-life.txt")
        spy = Spy(fail={"update_attribute": failing(fail)} if fail else {},
                  after={"update_attribute": remember, "delete_attributes": at_delete})
        self.spies.append(spy)
        with mock.patch.object(runner, "_Owned", Owner):
            result = runner.run(self.command("first_action", marker), 0, 1, timeout_seconds=10, max_bytes=100000, win=spy)
        gc.collect()
        return result, seen, [ref() is None for ref in seen["refs"]]

    def test_both_arrays_are_alive_when_the_attribute_list_is_deleted(self):
        result, seen, gone_after = self.run_checking()
        self.assertEqual(result.word, runner.OK)
        self.assertEqual(seen["alive"], [True, True])                 # the handle list and the job list
        self.assertEqual(seen["owned"], 2)                            # and the owner still held both then
        self.assertEqual(gone_after, [True, True])                    # released only afterwards

    def test_they_are_alive_when_the_first_attribute_fails(self):
        result, seen, _gone = self.run_checking(fail=lambda n: False)
        self.assertEqual(result.word, runner.CANNOT_CONTAIN)
        self.assertEqual(seen["stored_before_first_update"], 2)       # both were stored before either was handed over
        self.assertEqual(seen["alive"], [True])                       # the one handed over is alive; the owner holds both
        self.assertEqual(seen["owned"], 2)

    def test_they_are_alive_when_the_second_attribute_fails(self):
        result, seen, _gone = self.run_checking(fail=lambda n: n == 1)    # the handle list works, the job list does not
        self.assertEqual(result.word, runner.CANNOT_CONTAIN)
        self.assertEqual(seen["stored_before_first_update"], 2)
        self.assertEqual(seen["alive"], [True, True])
        self.assertEqual(seen["owned"], 2)


class Grace(_Case):
    """F2 (Codex's read): once the main program has exited, ONE grace deadline applies, also while data keeps flowing."""

    WRITER = '''
import os, sys, time
open(sys.argv[1] + '.t', 'w').write(str(os.getpid()))
os.replace(sys.argv[1] + '.t', sys.argv[1])
payload = b' ' * (2 * 1024 * 1024) + %r
for offset in range(0, len(payload), 4096):
    os.write(1, payload[offset:offset + 4096])
    time.sleep(0.003)
''' % PAGE.encode()

    ROOT = '''
import os, subprocess, sys, time
marker = sys.argv[2]
subprocess.Popen([sys.executable, sys.argv[1], marker], stdout=sys.stdout)
while not os.path.exists(marker):
    time.sleep(0.01)
os._exit(0)
'''

    def test_a_helper_that_keeps_writing_valid_page_bytes_after_the_main_program_exits_is_a_leftover_not_a_page(self):
        writer = _write(self.path("writer.py"), self.WRITER)
        root = _write(self.path("root_writer.py"), self.ROOT)
        marker = self.path("writer.pid")

        class SlowReader(Spy):
            """A scheduling delay between the peek and the read, so that data is always waiting (Codex's setup)."""

            def __init__(self):
                super().__init__()
                self.first_exit = None

            def read(self, handle, count):
                if self.first_exit is None and self.watch is not None and self.watch.gone(0):
                    self.first_exit = time.monotonic()                   # the main program has exited (a kept handle)
                time.sleep(0.04)
                return super().read(handle, count)

        probe = Probe(markers=[marker])
        spy = SlowReader()
        self.spies.append(spy)
        self.probes.append(probe)
        result = runner.run([PYTHON, root, writer, marker], 0, 1, timeout_seconds=20, max_bytes=3 * 1024 * 1024,
                            stop=probe, win=spy)
        ended = time.monotonic()
        self.assertEqual((result.word, result.data, result.cleanup_confirmed), (runner.LEFTOVER, None, True))
        self.assertIsNotNone(spy.first_exit)
        self.assertLess(ended - spy.first_exit, runner.LINGER_SECONDS + MARGIN)    # the grace plus a stated margin
        self.assertTrue(probe.watches[marker].gone(3))                             # the helper is gone with the job
        self.assert_clean(spy)

    def test_a_normal_program_that_writes_a_large_page_and_exits_is_still_ok(self):
        big = _write(self.path("bigpage.py"), "import sys\nsys.stdout.write(' ' * (900 * 1024) + %r)\n" % PAGE)
        result = runner.run([PYTHON, big], 0, 1, timeout_seconds=20, max_bytes=1024 * 1024)
        self.assertEqual(result.word, runner.OK)
        self.assertEqual(len(result.data), 900 * 1024 + len(PAGE))
        self.assertTrue(result.cleanup_confirmed)

    def test_the_quiet_holder_and_the_deadline_cap_still_hold(self):
        marker = self.path("holder3.pid")
        probe = Probe(markers=[marker])
        result = self.run_it("holder", marker, stop=probe, timeout=20)
        self.assertEqual(result.word, runner.LEFTOVER)
        self.assertLess(self.elapsed, runner.LINGER_SECONDS + MARGIN + 2)
        with mock.patch.object(runner, "LINGER_SECONDS", 30.0):
            self.run_it("holder", self.path("holder4.pid"), timeout=1)
        self.assertLess(self.elapsed, 1 + MARGIN)                                  # the grace never passes the deadline


class Nested(_Case):
    """C3: a job that already contains the reporter. It runs in a helper, because a process can never leave a job."""

    HELPER = r'''
import ctypes, json, sys
sys.path.insert(0, %(repo)r)
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.CreateJobObjectW.restype = ctypes.c_void_p
k.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
k.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
k.GetCurrentProcess.restype = ctypes.c_void_p
job = k.CreateJobObjectW(None, None)
assigned = bool(k.AssignProcessToJobObject(job, k.GetCurrentProcess()))
from quota_balancer.reporter import runner
result = runner.run(%(command)r, 0, 10, timeout_seconds=30, max_bytes=100000)
print(json.dumps({"assigned": assigned, "word": result.word, "confirmed": result.cleanup_confirmed,
                  "data": result.data.decode() if result.data else None}))
'''

    def test_a_reporter_inside_a_job_can_still_run_a_contained_program(self):
        script = self.path("nested.py")
        _write(script, self.HELPER % {"repo": REPO, "command": self.command("page")})
        done = subprocess.run([sys.executable, script], env=dict(os.environ, PYTHONPATH=REPO), capture_output=True,
                              text=True, timeout=60)
        answer = json.loads(done.stdout.strip().splitlines()[-1])
        if not answer["assigned"]:
            self.skipTest("this machine does not allow the helper to join a job of its own")
        self.assertEqual((answer["word"], answer["confirmed"], answer["data"]), (runner.OK, True, PAGE))


@unittest.skipUnless(WINDOWS, "the runner is for Windows only")
class ProbeReadiness(unittest.TestCase):
    """The test tool itself: a marker that names a process is ready only once it has been read and its handle retained."""

    def test_a_marker_that_could_not_be_opened_is_not_ready_and_its_callback_does_not_run(self):
        with tempfile.TemporaryDirectory(prefix="quota-l6a1b-probe-") as folder:
            marker = os.path.join(folder, "up.pid")
            with open(marker, "w", encoding="ascii") as handle:
                handle.write(str(os.getpid()))
            failures, ran = [2], []

            def opener(path, **kwargs):
                if failures[0]:
                    failures[0] -= 1
                    raise PermissionError("the program is replacing the file right now")
                return open(path, **kwargs)
            probe = Probe(markers=[marker], ready=[marker], then=lambda: ran.append(sorted(probe.watches)), opener=opener)
            self.addCleanup(probe.close)
            self.assertFalse(probe.is_set())                          # the file exists, but it could not be read
            self.assertFalse(probe.is_set())
            self.assertEqual((probe.watches, ran, probe.fired), ({}, [], False))
            self.assertTrue(probe.is_set())                           # read at last: handle retained, then ready
            self.assertEqual(ran, [[marker]])                         # and the callback saw the retained handle
            self.assertFalse(probe.is_set())                          # it fires once

    def test_a_process_that_could_not_be_opened_is_unknown_never_gone_and_never_ready(self):
        with tempfile.TemporaryDirectory(prefix="quota-l6a1b-probe-") as folder:
            marker = os.path.join(folder, "up.pid")
            with open(marker, "w", encoding="ascii") as handle:
                handle.write(str(os.getpid()))
            ran = []
            probe = Probe(markers=[marker], ready=[marker], then=lambda: ran.append(True))
            self.addCleanup(probe.close)
            with mock.patch.object(_k32, "OpenProcess", return_value=0):
                self.assertFalse(probe.is_set())
                self.assertFalse(probe.is_set())
                self.assertEqual((probe.watches, ran, probe.fired), ({}, [], False))
                watch = Watch(os.getpid())
                self.assertFalse(watch.opened)
                with self.assertRaises(AssertionError):
                    watch.gone()                                  # it never answers True when it does not know
            self.assertTrue(probe.is_set())                       # a real handle at last: ready, and the callback runs once
            self.assertEqual(ran, [True])
            self.assertTrue(probe.watches[marker].opened)
            self.assertFalse(probe.watches[marker].gone())        # this very process is alive

    def test_a_ready_path_that_names_no_process_is_ready_when_it_exists(self):
        with tempfile.TemporaryDirectory(prefix="quota-l6a1b-probe-") as folder:
            plain = os.path.join(folder, "go")
            probe = Probe(ready=[plain])
            self.assertFalse(probe.is_set())
            with open(plain, "w", encoding="ascii") as handle:
                handle.write("x")
            self.assertTrue(probe.is_set())


class Environment(unittest.TestCase):
    """The environment block is a pure function; the real programs see it too (Windows only)."""

    def test_only_listed_names_are_taken_and_the_operators_value_wins_without_regard_to_case(self):
        inherited = {"Path": "C:\\a", "SYSTEMROOT": "C:\\Windows", "SECRET_TOKEN": "loud", "temp": "C:\\t", "OTHER": "x"}
        block = runner.environment_block([("path", "C:\\mine"), ("EXTRA", "1")], inherited)
        entries = block.rstrip("\x00").split("\x00")
        self.assertEqual(entries, ["EXTRA=1", "path=C:\\mine", "SYSTEMROOT=C:\\Windows", "temp=C:\\t"])
        self.assertNotIn("loud", block)
        self.assertTrue(block.endswith("\x00\x00"))

    def test_the_last_operator_entry_wins_and_the_order_is_by_name(self):
        block = runner.environment_block([("B", "1"), ("a", "2"), ("b", "3")], {})
        self.assertEqual(block.rstrip("\x00").split("\x00"), ["a=2", "b=3"])

    def test_an_empty_environment_is_two_nuls(self):
        self.assertEqual(runner.environment_block([], {}), "\x00\x00")

    def test_a_nul_or_an_equals_sign_in_a_name_or_a_value_is_refused(self):
        for extra in ([("A", "x\x00y")], [("A\x00", "x")], [("A=B", "x")], [("", "x")]):
            with self.subTest(extra=repr(extra)):
                with self.assertRaises(ValueError):
                    runner.environment_block(extra, {})

    def test_the_program_sees_only_the_listed_names_and_the_operators_entries(self):
        if not WINDOWS:
            self.skipTest("Windows only")
        with tempfile.TemporaryDirectory(dir=os.environ.get("AGENTTALK_SCRATCH") or None) as directory:
            programs = write_programs(directory)
            marker = os.path.join(directory, "env.json")
            with mock.patch.dict(os.environ, {"LOUD_SECRET_L6A1B": "loud-value"}):
                result = runner.run([PYTHON, programs["env"], marker], 0, 10, timeout_seconds=15, max_bytes=100000,
                                    env=[("AGENTTALK_ROOT", "C:\\lw\\team"), ("path", "C:\\mine")])
            self.assertEqual(result.word, runner.OK)
            seen = json.loads(slurp(marker))
            names = {name.upper() for name in seen}
            self.assertNotIn("LOUD_SECRET_L6A1B", names)
            self.assertLessEqual(names, set(runner.INHERITED) | {"AGENTTALK_ROOT"})
            self.assertEqual({value for name, value in seen.items() if name.upper() == "PATH"}, {"C:\\mine"})
            self.assertEqual(seen["AGENTTALK_ROOT"], "C:\\lw\\team")


class Source(unittest.TestCase):

    def test_the_package_never_imports_subprocess(self):
        path = os.path.join(REPO, "quota_balancer", "reporter", "runner.py")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("import subprocess", text)
        self.assertNotIn("from subprocess", text)
        self.assertNotIn("taskkill", text)

    def test_termination_and_question_failures_are_never_hidden_by_suppress(self):
        path = os.path.join(REPO, "quota_balancer", "reporter", "runner.py")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("suppress", text)

    def test_the_quoting_the_runner_uses_is_the_tested_function(self):
        self.assertIs(runner.cmdline, cmdline)


if __name__ == "__main__":
    unittest.main()
