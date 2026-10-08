"""Card 7a-3: the commands `run`, `drain` and `status`, the 4.9 status fields,
the configuration-fault stops and the privacy rule.

Rows: drain_one_pass, run_stops_and_resumes, one_owner, status_fields,
status_while_running, journal_refused, config_fault_stop, expire_sweep_hint,
privacy_7a3. Real in-process Leasewise where the answer matters; a fake
transport where it must not be reachable.
"""

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest

from quota_balancer.reporter import cli as reporter_cli
from quota_balancer.reporter import config as cfg
from quota_balancer.reporter import events, rounds
from quota_balancer.reporter import state as state_mod
from quota_balancer.reporter import status as status_mod
from quota_balancer.reporter.cli import main as reporter_main
from quota_balancer.reporter.state import State

from tests.journal_fixture import Journal, iso, line
from tests.reporter_env import FAKE_IDENTITY, Net, Rig, agent_table
from tests.test_card7a3 import Script, cli, error, granted, one_message

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

AGENT_FIELDS = {
    "configured", "silent", "stopped_by_configuration_fault", "streams",
    "events_read", "last_event_at", "dropped_total", "damaged_lines",
    "damaged_stream_records", "messages", "finals", "late_observations",
    "acquisitions", "reports", "last_successful_call",
    "permanent_failures", "permanent_failures_total"}
TOP_FIELDS = {
    "status_version", "generated_at", "source", "rounds_completed", "agents",
    "journal_refused", "last_401", "limit_events", "readings", "gateway_receipts", "money_reader", "silent_configured_seats",
    "operator_actions"}


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def run_module(*args, timeout=60):
    return subprocess.run(
        [sys.executable, "-m", "quota_balancer", "reporter", *args],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout)


class _Live(unittest.TestCase):
    patch_clock = True

    def setUp(self):
        self.rig = Rig(patch_clock=self.patch_clock)
        self.addCleanup(self.rig.cleanup)
        self.stream = one_message(self.rig.journal, self.rig.clock.ms)

    def drain(self, *extra):
        # With the server on the test clock the reporter uses it too.
        kw = {"clock": self.rig.clock} if self.patch_clock else {}
        return cli(["drain", "--config", self.rig.ws.config, *extra], **kw)

    def leases(self):
        return self.rig.lw.rows("SELECT enforcement, state FROM leases")

    def receipts(self):
        return self.rig.lw.rows(
            "SELECT final, outcome, calls, input_tokens, cached_input_tokens,"
            " output_tokens, actual_micro_eur, seq FROM receipts "
            "ORDER BY final")


# --- drain ----------------------------------------------------------------------

class Drain(_Live):
    def test_drain_makes_one_pass_sends_everything_due_and_exits(self):
        code, out, err = self.drain()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"blocked": None, "journal": "ok",
                                           "new_events": 3, "sent": 3})
        self.assertEqual(self.leases(), [("shadow", "settled")])
        self.assertEqual(self.receipts(), [
            (0, "completed", 1, 170, 50, 10, None, None),
            (1, "completed", 1, 170, 50, 10, None, None)])

    def test_a_second_drain_finds_nothing_to_do(self):
        self.drain()
        code, out, _ = self.drain()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["sent"], 0)
        self.assertEqual(json.loads(out)["new_events"], 0)
        self.assertEqual(len(self.receipts()), 2)

    def test_drain_picks_up_what_the_journal_gained_since(self):
        self.drain()
        self.stream.dispatch("m2", "t-m2", self.rig.clock.ms - 20_000,
                             message_at=self.rig.clock.ms - 21_000)
        self.stream.disposed("m2", "delivery_failed", self.rig.clock.ms - 10_000,
                             message_at=self.rig.clock.ms - 21_000)
        code, out, _ = self.drain()
        self.assertEqual((code, json.loads(out)["sent"]), (0, 3))
        self.assertEqual(len(self.leases()), 2)
        self.assertEqual(self.rig.lw.rows(
            "SELECT outcome FROM receipts WHERE final=1 ORDER BY outcome"),
            [("completed",), ("failed",)])

    def test_the_commands_are_run_drain_and_status_only(self):
        for argv in (["schedule"], ["expire-sweep"], []):
            with self.subTest(argv), self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    reporter_main(argv)

    def test_drain_while_leasewise_is_down_exits_3_and_keeps_everything(self):
        port = free_port()
        self.rig.ws.write_config("http://127.0.0.1:%d" % port,
                                 {"alpha": agent_table()})
        code, out, err = self.drain()
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["blocked"], "unreachable")
        self.assertEqual(err, "")
        # recovery: the same state sends the same bytes
        self.rig.ws.write_config(self.rig.lw.url, {"alpha": agent_table()})
        self.rig.clock.advance(3600)          # past any back-off
        code, out, _ = self.drain()
        self.assertEqual((code, json.loads(out)["sent"]), (0, 3))
        self.assertEqual(self.leases(), [("shadow", "settled")])


class DrainSubprocess(_Live):
    """The server on the real clock, because the reporter is another process."""

    patch_clock = False

    def test_drain_over_the_real_module_entry_point(self):
        done = run_module("drain", "--config", self.rig.ws.config)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["sent"], 3)
        self.assertEqual(self.leases(), [("shadow", "settled")])
        again = run_module("status", "--config", self.rig.ws.config, "--json")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(json.loads(again.stdout)["agents"]["alpha"]
                         ["finals"]["sent"], 1)


# --- one owner of the state file ---------------------------------------------------

class OneOwner(_Live):
    def hold(self):
        held = State(self.rig.ws.state)
        self.addCleanup(held.close)
        return held

    def test_a_second_reporter_fails_at_once_with_a_fixed_message(self):
        self.hold()
        for verb in ("run", "drain"):
            with self.subTest(verb):
                started = time.monotonic()
                code, out, err = cli([verb, "--config", self.rig.ws.config])
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(json.loads(err), {"error": {
                    "code": "STATE_OWNED", "message": reporter_cli.MSG_OWNED}})
        self.assertEqual(self.leases(), [])

    def test_the_same_across_two_real_processes(self):
        self.hold()
        started = time.monotonic()
        done = run_module("drain", "--config", self.rig.ws.config)
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(done.returncode, 2)
        self.assertEqual(json.loads(done.stderr)["error"]["code"],
                         "STATE_OWNED")
        self.assertEqual(done.stdout, "")

    def test_a_running_reporter_blocks_a_drain_and_status_reads_its_copy(self):
        stop = threading.Event()
        box = {}
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            thread = threading.Thread(target=lambda: box.update(
                code=reporter_main(["run", "--config", self.rig.ws.config],
                                   stop=stop)))
            thread.start()
            try:
                snapshot = status_mod.snapshot_path(self.rig.ws.state)
                deadline = time.monotonic() + 30
                while not os.path.exists(snapshot) \
                        and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(os.path.exists(snapshot))
                code, _out, err = cli(["drain", "--config",
                                       self.rig.ws.config])
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(err)["error"]["code"],
                                 "STATE_OWNED")
                code, out2, _ = cli(["status", "--config",
                                     self.rig.ws.config, "--json"])
                self.assertEqual(code, 0)
                report = json.loads(out2)
                self.assertEqual(report["source"],
                                 "snapshot of the running reporter")
                self.assertEqual(set(report), TOP_FIELDS)
            finally:
                stop.set()
                thread.join(30)
        self.assertEqual(box["code"], 0)

    def test_status_while_owned_with_no_copy_says_so_and_exits_2(self):
        self.hold()
        code, out, err = cli(["status", "--config", self.rig.ws.config])
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(json.loads(err)["error"]["code"], "STATE_OWNED")


# --- run: stops cleanly at any point and resumes -------------------------------------

class RunStopsAndResumes(_Live):
    def test_a_stop_after_a_cycle_ends_run_and_frees_the_state(self):
        reporter = self.rig.open()
        cycles = []
        reporter.run(on_cycle=lambda r: (cycles.append(r), reporter.stop.set()))
        self.assertEqual(len(cycles), 1)
        self.assertEqual(self.leases(), [("shadow", "settled")])
        self.rig.close()
        State(self.rig.ws.state).close()          # the lock is free again

    def test_a_stop_between_two_requests_loses_and_duplicates_nothing(self):
        for index in range(0, 3):
            with self.subTest(stop_before_request=index):
                rig = Rig()
                self.addCleanup(rig.cleanup)
                one_message(rig.journal, rig.clock.ms)
                stop = threading.Event()
                rig.net.before = lambda i, s=stop, k=index: (
                    s.set() if i == k else None)
                rig.open(stop=stop)
                result = rig.reporter.cycle()
                self.assertEqual(len(rig.net.calls), index + 1)
                self.assertEqual(result.sent, index + 1)
                rig.net.before = None
                rig.restart()                    # a fresh process, no stop
                rig.drain_all()
                self.assertEqual(rig.lw.rows(
                    "SELECT COUNT(*), SUM(final) FROM receipts"), [(2, 1)])
                self.assertEqual(rig.lw.rows("SELECT COUNT(*) FROM leases"),
                                 [(1,)])
                self.assertEqual(len(set(c[1] for c in rig.net.calls)), 3)

    def test_run_waits_longer_while_leasewise_does_not_answer(self):
        waits = []
        rig = self.rig

        class Stop:
            def __init__(self, cycles):
                self.flag = False
                self.cycles = cycles

            def is_set(self):
                return self.flag

            def set(self):
                self.flag = True

            def wait(self, seconds):
                waits.append(seconds)
                rig.clock.advance(seconds)       # the wait really passes
                if len(waits) >= self.cycles:
                    self.flag = True

        self.rig.net.down = True
        reporter = self.rig.open(stop=Stop(7))
        reporter.run()
        self.assertEqual(len(waits), 7)
        self.assertGreaterEqual(min(waits), reporter.config.poll_seconds)
        self.assertEqual(waits, sorted(waits))                 # grows
        self.assertGreater(waits[-1], waits[0])
        self.assertLessEqual(max(waits), 900)                  # 15 minutes
        # ...and goes back to the poll interval once the answer returns
        self.rig.net.down = False
        waits.clear()
        reporter.stop = Stop(1)
        self.rig.clock.advance(3600)
        reporter.run()
        self.assertEqual(waits, [reporter.config.poll_seconds])
        self.assertEqual(reporter.failures, 0)

    def test_a_quiet_cycle_does_not_end_the_back_off_streak(self):
        self.rig.net.down = True
        reporter = self.rig.open()
        reporter.cycle()
        self.assertEqual(reporter.failures, 1)
        quiet = reporter.cycle()            # the item is still waiting
        self.assertEqual((quiet.sent, quiet.blocked), (0, None))
        self.assertEqual(reporter.failures, 1)
        self.rig.clock.advance(3600)
        reporter.cycle()
        self.assertEqual(reporter.failures, 2)

    def test_the_stop_signals_are_wired_to_the_stop_flag(self):
        import signal
        flag = threading.Event()
        before = {n: signal.getsignal(getattr(signal, n))
                  for n in ("SIGINT", "SIGTERM")}
        try:
            reporter_cli._install_stop(flag)
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            self.assertTrue(flag.is_set())
        finally:
            for name, handler in before.items():
                signal.signal(getattr(signal, name), handler)


# --- the journal is refused ----------------------------------------------------------

class JournalRefused(_Live):
    def segment(self):
        return self.stream.path()

    def test_an_unknown_schema_version_stops_the_round_loudly(self):
        # something already stored, Leasewise down: items wait
        self.rig.net.down = True
        self.rig.open()
        self.rig.reporter.cycle()
        self.rig.close()
        size = os.path.getsize(self.segment())
        with open(self.segment(), "ab") as handle:
            handle.write(line({"v": 2, "kind": "something_new"}))
        self.rig.net.down = False
        self.rig.clock.advance(3600)
        code, out, err = self.drain()
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(out)["journal"],
                         "stopped: schema_version")
        self.assertIn("THE JOURNAL IS REFUSED", err)
        self.assertIn("schema_version", err)
        # what was already stored still goes out; nothing new was read
        self.assertEqual(self.leases(), [("shadow", "settled")])
        code, out, _ = cli(["status", "--config", self.rig.ws.config,
                            "--json"])
        report = json.loads(out)
        self.assertEqual(report["journal_refused"]["reason"],
                         "schema_version")
        self.assertTrue(any("journal" in a for a in report["operator_actions"]))
        text = cli(["status", "--config", self.rig.ws.config])[1]
        self.assertIn("THE JOURNAL IS REFUSED", text)
        # the file is put right: the next round clears the notice
        with open(self.segment(), "r+b") as handle:
            handle.truncate(size)
        code, out, _ = self.drain()
        self.assertEqual(code, 0)
        report = json.loads(cli(["status", "--config", self.rig.ws.config,
                                 "--json"])[1])
        self.assertIsNone(report["journal_refused"])

    def test_the_round_writes_nothing_when_a_version_is_refused(self):
        with open(self.segment(), "ab") as handle:
            handle.write(line({"v": 2, "kind": "something_new"}))
        self.rig.open()
        before = self.rig.state.one("SELECT COUNT(*) FROM event")[0]
        self.rig.reporter.cycle()
        self.assertEqual(self.rig.state.one("SELECT COUNT(*) FROM event")[0],
                         before)
        self.assertEqual(self.rig.state.one(
            "SELECT COUNT(*) FROM acquisition")[0], 0)

    def test_run_prints_the_refusal_on_every_cycle(self):
        with open(self.segment(), "ab") as handle:
            handle.write(line({"v": 2, "kind": "something_new"}))
        stop = threading.Event()
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            self.rig.close()
            thread = threading.Thread(
                target=lambda: reporter_main(
                    ["run", "--config", self.rig.ws.config], stop=stop))
            thread.start()
            deadline = time.monotonic() + 30
            while "REFUSED" not in err.getvalue() \
                    and time.monotonic() < deadline:
                time.sleep(0.05)
            stop.set()
            thread.join(30)
        self.assertIn("THE JOURNAL IS REFUSED", err.getvalue())

    def test_a_segment_shorter_than_its_cursor_is_refused_too(self):
        self.drain()
        with open(self.segment(), "r+b") as handle:
            handle.truncate(10)
        code, out, err = self.drain()
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(out)["journal"],
                         "stopped: segment_shorter_than_cursor")
        self.assertIn("THE JOURNAL IS REFUSED", err)


# --- agents stopped by a configuration fault --------------------------------------------

class ConfigFaultStop(_Live):
    def test_a_stopped_agent_is_listed_with_its_code_and_resumes_on_restart(
            self):
        agents = {"alpha": agent_table()}
        self.rig.ws.write_config(self.rig.lw.url, agents,
                                 identity="FAKE-WRONG-IDENTITY")
        code, out, _ = self.drain()
        self.assertEqual(code, 0)
        report = json.loads(cli(["status", "--config", self.rig.ws.config,
                                 "--json"])[1])
        alpha = report["agents"]["alpha"]
        self.assertEqual(alpha["stopped_by_configuration_fault"],
                         {"code": "FORBIDDEN"})
        self.assertEqual(alpha["acquisitions"]["failed"], 1)
        self.assertEqual(alpha["acquisitions"]["failed_codes"],
                         {"FORBIDDEN": 1})
        self.assertTrue(any("configuration fault" in a
                            for a in report["operator_actions"]))
        text = cli(["status", "--config", self.rig.ws.config])[1]
        self.assertIn("stopped by a configuration fault: FORBIDDEN", text)
        self.assertEqual(self.leases(), [])
        # the operator fixes the identity and starts again; a new message goes
        self.rig.ws.write_config(self.rig.lw.url, agents)
        self.stream.dispatch("m2", "t-m2", self.rig.clock.ms - 20_000,
                             message_at=self.rig.clock.ms - 21_000)
        self.stream.disposed("m2", "completed", self.rig.clock.ms - 10_000,
                             message_at=self.rig.clock.ms - 21_000)
        code, out, _ = self.drain()
        self.assertEqual(code, 0)
        self.assertEqual(self.leases(), [("shadow", "settled")])
        report = json.loads(cli(["status", "--config", self.rig.ws.config,
                                 "--json"])[1])
        self.assertIsNone(report["agents"]["alpha"]
                          ["stopped_by_configuration_fault"])
        # the message that failed for good stays failed, and says so
        self.assertEqual(report["agents"]["alpha"]["acquisitions"]["failed"], 1)

    def test_one_stopped_agent_does_not_stop_the_other(self):
        agents = {"alpha": agent_table("s6", max_calls=3),   # beyond the profile
                  "beta": agent_table("s7")}
        rig = Rig(agents=agents)
        self.addCleanup(rig.cleanup)
        one_message(rig.journal, rig.clock.ms)
        one_message(Journal(rig.ws.journal, "beta"), rig.clock.ms, mid="mb")
        rig.open()
        rig.reporter.cycle()
        self.assertEqual(
            rig.state.all("SELECT agent, failed_code FROM agent_state"),
            [("alpha", "POLICY_DENIED")])
        self.assertEqual(rig.lw.rows("SELECT COUNT(*) FROM leases"), [(1,)])
        self.assertEqual(rig.state.one(
            "SELECT state FROM acquisition WHERE agent='beta'")[0],
            "acquired")


# --- status --------------------------------------------------------------------------------

class StatusFields(_Live):
    def status(self, *extra):
        code, out, err = cli(["status", "--config", self.rig.ws.config,
                              "--json", *extra])
        self.assertEqual((code, err), (0, ""), err)
        return json.loads(out)

    def test_every_listed_field_is_present(self):
        self.drain()
        report = self.status()
        self.assertEqual(set(report), TOP_FIELDS)
        alpha = report["agents"]["alpha"]
        self.assertEqual(set(alpha), AGENT_FIELDS)
        self.assertEqual(set(alpha["streams"]),
                         {"seen", "missing", "with_gaps", "with_torn_tails"})
        self.assertEqual(set(alpha["messages"]),
                         {"known", "complete", "incomplete",
                          "incomplete_reasons", "open", "outcome_unknown"})
        self.assertEqual(set(alpha["finals"]), {"pending", "sent", "failed"})
        self.assertEqual(set(alpha["acquisitions"]),
                         {"pending", "acquired", "failed", "failed_codes"})
        self.assertEqual(set(alpha["reports"]),
                         {"pending", "sent", "dropped", "failed", "over_cap"})
        self.assertEqual(alpha["streams"]["seen"], 1)
        self.assertEqual(alpha["messages"]["known"], 1)
        self.assertEqual(alpha["messages"]["complete"], 1)
        self.assertEqual(alpha["finals"]["sent"], 1)
        self.assertEqual(alpha["reports"]["sent"], 1)
        self.assertEqual(alpha["acquisitions"]["acquired"], 1)
        self.assertIsNotNone(alpha["last_successful_call"])
        self.assertEqual(alpha["events_read"], 3)
        self.assertEqual(report["source"], "state")
        self.assertIsNone(report["last_401"])

    def test_the_text_view_says_the_same_in_plain_words(self):
        self.drain()
        code, out, _ = cli(["status", "--config", self.rig.ws.config])
        self.assertEqual(code, 0)
        self.assertIn("alpha:", out)
        self.assertIn("1 messages", out)
        self.assertNotIn("{", out)

    def test_a_reporter_that_never_ran_reports_an_empty_state(self):
        report = self.status()
        self.assertEqual(report["rounds_completed"], 0)
        self.assertEqual(report["agents"]["alpha"]["events_read"], 0)
        self.assertTrue(report["agents"]["alpha"]["silent"])
        self.assertEqual(report["silent_configured_seats"], ["alpha"])
        self.assertFalse(os.path.exists(self.rig.ws.state))   # not created

    def test_a_configured_seat_with_no_recent_events_is_silent(self):
        agents = {"alpha": agent_table("s6"), "gamma": agent_table("s7")}
        rig = Rig(agents=agents)
        self.addCleanup(rig.cleanup)
        one_message(rig.journal, rig.clock.ms)
        code, out, _ = cli(["drain", "--config", rig.ws.config])
        report = json.loads(cli(["status", "--config", rig.ws.config,
                                 "--json"])[1])
        self.assertEqual(report["silent_configured_seats"], ["gamma"])
        self.assertFalse(report["agents"]["alpha"]["silent"])
        # and an agent whose last event is older than the limit
        with open(rig.ws.config, encoding="utf-8") as handle:
            text = handle.read()
        with open(rig.ws.config, "w", encoding="utf-8") as handle:
            handle.write(text.replace('version = 1',
                                      'version = 1\nsilent_after_seconds = 1'))
        report = json.loads(cli(["status", "--config", rig.ws.config,
                                 "--json"])[1])
        self.assertEqual(report["silent_configured_seats"],
                         ["alpha", "gamma"])

    def test_the_last_401_is_shown_with_its_code(self):
        self.rig.net.down = False
        script = Script(error(401, "UNAUTHENTICATED"))
        code, out, _ = cli(["drain", "--config", self.rig.ws.config],
                           transport=script)
        self.assertEqual(code, 3)
        report = self.status()
        self.assertEqual(report["last_401"]["code"], "UNAUTHENTICATED")
        self.assertTrue(report["last_401"]["at"].endswith("Z"))
        self.assertTrue(any("identity" in a for a in report["operator_actions"]))
        text = cli(["status", "--config", self.rig.ws.config])[1]
        self.assertIn("refused our identity", text)

    def test_open_and_incomplete_messages_point_to_expire_sweep(self):
        self.stream.dispatch("m2", "t-m2", self.rig.clock.ms - 20_000,
                             message_at=self.rig.clock.ms - 21_000,
                             ended=False)
        self.drain()
        report = self.status()
        self.assertEqual(report["agents"]["alpha"]["messages"]["open"], 1)
        hints = [a for a in report["operator_actions"]
                 if "expire-sweep" in a]
        self.assertEqual(len(hints), 1)
        self.assertIn("never a timer", hints[0])
        # a clean store gives no such hint
        clean = Rig()
        self.addCleanup(clean.cleanup)
        one_message(clean.journal, clean.clock.ms)
        cli(["drain", "--config", clean.ws.config])
        report = json.loads(cli(["status", "--config", clean.ws.config,
                                 "--json"])[1])
        self.assertEqual(report["operator_actions"], [])


class StatusFromTheJournal(unittest.TestCase):
    """The journal-side counters of 4.9, one at a time."""

    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        self.now = self.rig.clock.ms

    def report(self):
        self.rig.open(transport=Script(granted(), (200, {}), (200, {})))
        self.rig.reporter.cycle()
        return self.rig.reporter.report()["agents"]["alpha"]

    def test_dropped_total_is_the_journals_own_count(self):
        s = self.rig.journal.add_stream("g1", self.now - 100_000)
        s.dispatch("m1", "t1", self.now - 50_000,
                   message_at=self.now - 60_000)
        s.drop()
        s.drop()
        s.disposed("m1", "completed", self.now - 40_000,
                   message_at=self.now - 60_000)
        s.close(self.now - 39_000)
        alpha = self.report()
        self.assertEqual(alpha["dropped_total"], 2)
        self.assertEqual(alpha["streams"]["with_gaps"], 1)
        # a dropped event is a numbering gap too; the gap is named first
        self.assertEqual(alpha["messages"]["incomplete_reasons"],
                         {"seq_gap": 1})
        self.assertEqual(alpha["messages"]["incomplete"], 1)

    def test_a_numbering_gap_is_a_stream_with_a_gap(self):
        s = self.rig.journal.add_stream("g1", self.now - 100_000)
        s.dispatch("m1", "t1", self.now - 50_000,
                   message_at=self.now - 60_000)
        s.lose()
        s.disposed("m1", "completed", self.now - 40_000,
                   message_at=self.now - 60_000)
        alpha = self.report()
        self.assertEqual(alpha["streams"]["with_gaps"], 1)
        self.assertEqual(alpha["messages"]["incomplete_reasons"],
                         {"seq_gap": 1})

    def test_a_torn_tail_is_counted_once_it_is_confirmed(self):
        s = self.rig.journal.add_stream("g1", self.now - 100_000)
        s.dispatch("m1", "t1", self.now - 50_000,
                   message_at=self.now - 60_000)
        s.tear()
        s.rotate(self.now - 45_000)          # a later segment: the tear is final
        s.disposed("m1", "completed", self.now - 40_000,
                   message_at=self.now - 60_000)
        alpha = self.report()
        self.assertEqual(alpha["streams"]["with_torn_tails"], 1)
        self.assertEqual(alpha["messages"]["incomplete_reasons"],
                         {"torn_tail": 1})

    def test_a_registered_stream_whose_files_are_gone_is_missing(self):
        self.rig.journal.add_stream("g0", self.now - 200_000, header=False)
        s = self.rig.journal.add_stream("g1", self.now - 100_000,
                                        previous="g0")
        s.dispatch("m1", "t1", self.now - 50_000,
                   message_at=self.now - 60_000)
        s.disposed("m1", "completed", self.now - 40_000,
                   message_at=self.now - 60_000)
        alpha = self.report()
        self.assertEqual(alpha["streams"]["missing"], 1)
        self.assertEqual(alpha["streams"]["seen"], 2)

    def test_outcome_unknown_and_open_messages_are_counted_apart(self):
        s = self.rig.journal.add_stream("g1", self.now - 100_000)
        s.dispatch("m1", "t1", self.now - 50_000,
                   message_at=self.now - 60_000)
        s.disposed("m1", "outcome_unknown", self.now - 45_000,
                   message_at=self.now - 60_000)
        s.dispatch("m2", "t2", self.now - 30_000,
                   message_at=self.now - 31_000, ended=False)
        alpha = self.report()
        self.assertEqual(alpha["messages"]["outcome_unknown"], 1)
        self.assertEqual(alpha["messages"]["open"], 1)
        self.assertEqual(alpha["finals"]["sent"], 0)          # no final

    def test_late_observations_are_counted_and_never_sent(self):
        s = self.rig.journal.add_stream("g1", self.now - 100_000)
        s.dispatch("m1", "t1", self.now - 50_000,
                   message_at=self.now - 60_000)
        s.disposed("m1", "completed", self.now - 40_000,
                   message_at=self.now - 60_000)
        self.report()
        s.dispatch("m1", "t-late", self.now - 30_000,
                   message_at=self.now - 60_000)
        script = Script(granted(), (200, {}), (200, {}))
        self.rig.restart(transport=script)
        self.rig.reporter.cycle()
        alpha = self.rig.reporter.report()["agents"]["alpha"]
        self.assertEqual(alpha["late_observations"], 2)
        self.assertEqual(script.sent, [])

    def test_acquisitions_pending_and_reports_dropped_are_counted(self):
        s = self.rig.journal.add_stream("g1", self.now - 100_000)
        s.dispatch("m1", "t1", self.now - 50_000,
                   message_at=self.now - 60_000)
        s.disposed("m1", "completed", self.now - 40_000,
                   message_at=self.now - 60_000)
        self.rig.open(transport=Script(ConnectionRefusedError("x")))
        self.rig.reporter.cycle()
        alpha = self.rig.reporter.report()["agents"]["alpha"]
        self.assertEqual(alpha["acquisitions"]["pending"], 1)
        self.assertEqual(alpha["reports"]["pending"], 1)
        self.assertEqual(alpha["finals"]["pending"], 1)
        self.assertIsNone(alpha["last_successful_call"])
        self.rig.restart(transport=Script(granted(), error(410,
                         "LEASE_EXPIRED"), (200, {})))
        self.rig.state.conn.execute("UPDATE acquisition SET not_before_ms=0")
        self.rig.reporter.cycle()
        alpha = self.rig.reporter.report()["agents"]["alpha"]
        self.assertEqual(alpha["reports"]["dropped"], 1)
        self.assertIsNotNone(alpha["last_successful_call"])

    def test_reports_over_the_cap_are_counted(self):
        s = self.rig.journal.add_stream("g1", self.now - 100_000)
        for n in range(rounds.MAX_REPORTS_PER_MESSAGE + 3):
            s.dispatch("m1", "t%d" % n, self.now - 50_000 + n,
                       message_at=self.now - 60_000)
        alpha = self.report()
        self.assertEqual(alpha["reports"]["over_cap"], 3)

    def test_damaged_lines_are_counted(self):
        s = self.rig.journal.add_stream("g1", self.now - 100_000)
        s.raw(b"this is not json\n")
        s.raw(b'{"v":1,"kind":["x"]}\n')
        alpha = self.report()
        self.assertEqual(alpha["damaged_lines"], 2)


# --- privacy ---------------------------------------------------------------------------------

SENT_ID = "SENTINEL-IDENTITY-5c1d9e7a"
SENT_HOST = "sentinel-host-8a7f3b.invalid"
SENT_URL = "https://%s:9443" % SENT_HOST


class Privacy(unittest.TestCase):
    """The identity value and the URL never leave the configuration file:
    not into status, any output, the state or an exception's text."""

    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        self.rig.ws.write_config(SENT_URL, {"alpha": agent_table()},
                                 identity=SENT_ID)
        one_message(self.rig.journal, self.rig.clock.ms)

    def leaky(self, *args, **kw):
        def transport(method, path, headers, body, timeout):
            raise OSError("cannot reach %s with %s" % (SENT_URL, SENT_ID))
        return transport

    def everything(self, outputs):
        found = []
        needles = (SENT_ID, SENT_HOST, "9443")
        for label, text in outputs:
            for needle in needles:
                if needle in text:
                    found.append((label, needle))
        return found

    def test_no_output_state_or_status_carries_a_sentinel(self):
        outputs = []
        for verb in ("drain", "status", "status --json"):
            code, out, err = cli(verb.split() + ["--config", self.rig.ws.config],
                                 transport=self.leaky())
            outputs += [(verb + " out", out), (verb + " err", err)]
        # a transport that answers with the sentinels in the body too
        echo = Script((500, json.dumps({"error": {
            "code": "STORE_ERROR", "message": SENT_ID + SENT_URL}}).encode()))
        code, out, err = cli(["drain", "--config", self.rig.ws.config],
                             transport=echo)
        outputs += [("echo out", out), ("echo err", err)]
        snapshot = status_mod.snapshot_path(self.rig.ws.state)
        with open(snapshot, "r", encoding="ascii") as handle:
            outputs.append(("snapshot", handle.read()))
        directory = os.path.dirname(self.rig.ws.state)
        for name in os.listdir(directory):
            with open(os.path.join(directory, name), "rb") as handle:
                outputs.append((name, handle.read().decode("latin-1")))
        self.assertEqual(self.everything(outputs), [])
        self.assertGreater(len(outputs), 8)

    def test_an_internal_fault_prints_a_fixed_sentence(self):
        def boom(*a, **k):
            raise RuntimeError("failed with %s at %s" % (SENT_ID, SENT_URL))
        from unittest import mock
        with mock.patch.object(rounds, "run_round", boom):
            code, out, err = cli(["drain", "--config", self.rig.ws.config],
                                 transport=self.leaky())
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err), {"error": {
            "code": "REPORTER_FAILED", "message": reporter_cli.MSG_INTERNAL}})
        self.assertEqual(self.everything([("err", err), ("out", out)]), [])

    def test_configuration_errors_never_quote_the_file(self):
        path = self.rig.ws.config
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        for label, bad in (
                ("toml", text + "\n%s = = %s\n" % (SENT_ID, SENT_URL)),
                ("key", text + '\n%s = "x"\n' % "sentinel_key"),
                ("url", text.replace(SENT_URL, "ftp://" + SENT_HOST)),
                ("identity", text.replace(
                    SENT_ID, SENT_ID + " with a space")),
                ("agent", text.replace('seat = "s6"',
                                       'seat = "%s"' % SENT_ID))):
            with self.subTest(label):
                with self.assertRaises(cfg.ConfigError) as caught:
                    cfg.parse_config(bad)
                message = caught.exception.message
                self.assertEqual(self.everything([("m", message)]), [])
                self.assertNotIn("sentinel_key", message)
                self.assertNotIn(SENT_ID, repr(caught.exception))

    def test_the_objects_hide_the_secrets_in_repr(self):
        config = cfg.load_config(self.rig.ws.config)
        self.assertEqual(self.everything([
            ("config", repr(config)), ("str", str(config))]), [])
        reporter = self.rig.open(transport=self.leaky())
        self.assertEqual(self.everything([
            ("client", repr(reporter.client))]), [])

    def test_a_refused_credential_does_not_echo_the_answer(self):
        script = Script((401, json.dumps({"error": {
            "code": "UNAUTHENTICATED", "message": SENT_ID}}).encode()))
        cli(["drain", "--config", self.rig.ws.config], transport=script)
        code, out, err = cli(["status", "--config", self.rig.ws.config,
                              "--json"])
        self.assertEqual(self.everything([("s", out), ("e", err)]), [])
        self.assertIn("UNAUTHENTICATED", out)


if __name__ == "__main__":
    unittest.main()
