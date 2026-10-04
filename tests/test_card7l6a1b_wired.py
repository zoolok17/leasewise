"""Card L6a-1b, step 3: a configured `command` reads real receipt pages through the reporter.

* the command's bytes go straight to the same strict page rules as a fixture file; a page is captured and released exactly
  as for a fixture, the cursor moves, and the next read asks for `--after` the new cursor;
* every failure is a closed word in `status`, with nothing captured and the cursor unmoved, and the advice follows the
  CURRENT failure;
* the cycle has a receipt time budget, checked before each read starts, and the send passes still run;
* a stop ends a read in progress; nothing the program printed (or its path, or its environment) appears anywhere.
Windows only. Fake commands are small scripts the tests write, run by the base Python; never agenttalk, never a gateway.
"""

import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from quota_balancer.reporter import receipts, runner, status
from quota_balancer.reporter import run as run_mod

from tests.l6a1_env import GATEWAY_ALIAS, GATEWAY_ID, configure, lease, ledger, page, receipt, write_json
from tests.reporter_env import Rig
from tests.test_card7l6a1_delivery import Net
from tests.test_card7l6a1b_runner import PROGRAMS, PYTHON, WINDOWS, Probe

LOUD = "loud-value-l6a1b-wired-9d2c"

PAGER = r'''
import json, sys
ledger, log, mode = sys.argv[1:4]
args = sys.argv[4:]
with open(log, "a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")
with open(mode) as handle:
    word = handle.read().strip()
if word == "exit1":
    sys.stdout.write(%(loud)r)
    sys.stderr.write(%(loud)r)
    sys.exit(1)
if word == "garbage":
    sys.stdout.write("not json " + %(loud)r)
    sys.exit(0)
after = int(args[args.index("--after") + 1])
limit = int(args[args.index("--limit") + 1])
with open(ledger) as handle:
    doc = json.load(handle)
rows = [row for row in doc["receipts"] if row["seq"] > after]
shown = rows[:limit]
last = shown[-1]["seq"] if shown else after
sys.stdout.write(json.dumps({"envelope_version": 1, "generation": doc["generation"], "after_seq": after,
                             "next_seq": last, "has_more": len(rows) > len(shown), "receipts": shown}))
''' % {"loud": LOUD}


@unittest.skipUnless(WINDOWS, "a command source is for Windows only")
class _Wired(unittest.TestCase):

    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH") or None
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-l6a1b-run-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        self.pager = self.write("pager.py", PAGER)
        self.mode = self.write("mode.txt", "ok")
        self.log = os.path.join(self.dir, "calls.log")
        self.ledger = os.path.join(self.dir, "ledger.json")
        for name, text in PROGRAMS.items():
            self.write(name + ".py", text)

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return path

    def set_mode(self, word):
        self.write("mode.txt", word)

    def start(self, count=3, command=None, timeout=20, env=None, max_bytes=None, net=None):
        """A reporter whose source is a command: the pager over a ledger of `count` receipts, one per lease it knows."""
        self.leases = [lease() for _ in range(count)]
        self.rows = [receipt(one, n + 1) for n, one in enumerate(self.leases)]
        write_json(self.ledger, ledger(self.rows))
        configure(self.rig, [], bindings=self.leases)
        store = os.path.join(self.rig.ws.dir, "admission.db")
        command = command or [PYTHON, self.pager, self.ledger, self.log, self.mode]
        lines = ["[gateway_receipts]", "gateway = %s" % json.dumps(GATEWAY_ALIAS),
                 "admission_store = %s" % json.dumps(store), "command = %s" % json.dumps(command),
                 "timeout_seconds = %d" % timeout]
        if max_bytes:
            lines.append("max_bytes = %d" % max_bytes)
        if env:
            lines.append("[gateway_receipts.env]")
            lines += ["%s = %s" % (name, json.dumps(value)) for name, value in env.items()]
        self.rig.ws.write_config("http://127.0.0.1:9", self.rig.agents_cfg, extra="\n".join(lines) + "\n")
        self.net = net or Net()
        return self.rig.open(transport=self.net)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def count(self, table):
        return self.rig.state.one("SELECT COUNT(*) FROM %s" % table)[0]

    def receipt_status(self, reporter):
        return reporter.report()["gateway_receipts"]


class EndToEnd(_Wired):

    def test_a_page_is_captured_and_released_and_the_cursor_moves(self):
        reporter = self.start(count=3)
        result = reporter.cycle()
        self.assertEqual((result.new_receipts, result.sent, result.blocked), (3, 3, None))
        self.assertEqual(self.count("receipt_capture"), 3)
        self.assertEqual([call[0] for call in self.net.settles()], ["/v1/leases/%s/settle" % one for one in self.leases])
        self.assertEqual(self.receipt_status(reporter)["last_seq"], 3)
        first, = self.calls()
        self.assertEqual(first[-5:], ["--after", "0", "--limit", "100", "--json"])        # our options, last
        self.assertEqual(first[:3], [self.ledger, self.log, self.mode])                    # the configured ones, unchanged

    def test_the_next_cycle_asks_for_the_new_cursor(self):
        reporter = self.start(count=3)
        reporter.cycle()
        reporter.cycle()
        first, second = self.calls()
        self.assertEqual(first[-5:], ["--after", "0", "--limit", "100", "--json"])
        self.assertEqual(second[-5:], ["--after", "3", "--limit", "100", "--json"])

    def test_the_result_is_exactly_what_a_fixture_gives(self):
        reporter = self.start(count=3)
        reporter.cycle()
        mine = (self.rig.state.all("SELECT seq, ref_sha256, outcome, calls, input_tokens, output_tokens, actual_micro_eur,"
                                   " closed_at FROM receipt_capture ORDER BY seq"),
                self.rig.state.all("SELECT event_id, lease_id, gateway_id, body FROM receipt_delivery ORDER BY id"))
        other = Rig(server=False)
        self.addCleanup(other.cleanup)
        configure(other, self.rows, bindings=self.leases)
        fixture_reporter = other.open(transport=Net())
        fixture_reporter.cycle()
        theirs = (other.state.all("SELECT seq, ref_sha256, outcome, calls, input_tokens, output_tokens, actual_micro_eur,"
                                  " closed_at FROM receipt_capture ORDER BY seq"),
                  other.state.all("SELECT event_id, lease_id, gateway_id, body FROM receipt_delivery ORDER BY id"))
        self.assertEqual(mine, theirs)
        self.assertEqual(mine[1][0][2], GATEWAY_ID)

    def test_a_page_is_read_in_pieces_by_the_limit_and_the_cursor_follows(self):
        reporter = self.start(count=3)
        reporter.gateway_receipts.page_limit = 2
        reporter.cycle()
        self.assertEqual([call[-2] for call in self.calls()], ["2", "2"])                  # --limit 2, twice
        self.assertEqual([call[-4] for call in self.calls()], ["0", "2"])                  # after 0, then after 2
        self.assertEqual(self.count("receipt_capture"), 3)

    def test_the_source_kind_is_shown(self):
        self.assertEqual(self.receipt_status(self.start())["source_kind"], "command")
        rig = Rig(server=False)
        self.addCleanup(rig.cleanup)
        configure(rig, [], bindings=[])
        self.assertEqual(rig.open(transport=Net()).report()["gateway_receipts"]["source_kind"], "fixture")
        none = Rig(server=False)
        self.addCleanup(none.cleanup)
        self.assertIsNone(none.open(transport=Net()).report()["gateway_receipts"]["source_kind"])


class Failures(_Wired):
    """Each failure is a closed word in `status`: nothing captured, the cursor unmoved, a count, and advice that follows the
    CURRENT failure."""

    def settle(self, reporter, word):
        shown = self.receipt_status(reporter)
        self.assertEqual(self.count("receipt_capture"), 0)
        self.assertEqual(shown["last_seq"], 0)
        self.assertEqual((shown["last_read_result"], shown["last_read_reason"]), ("read_later", word))
        self.assertEqual(shown["read_later"][word], 1)
        return shown

    def advice(self, reporter, sentence):
        return sentence in reporter.report()["operator_actions"]

    def test_a_non_zero_exit_is_exit_code_and_the_page_it_printed_is_not_used(self):
        reporter = self.start()
        self.set_mode("exit1")
        reporter.cycle()
        self.settle(reporter, "exit_code")
        self.assertTrue(self.advice(reporter, status.NOT_FINISHING_ACTION))

    def test_text_that_is_not_json_is_not_json(self):
        reporter = self.start()
        self.set_mode("garbage")
        reporter.cycle()
        self.settle(reporter, "not_json")

    def test_a_program_that_cannot_be_started_is_cannot_run(self):
        reporter = self.start(command=[r"C:\nowhere\quota-l6a1b\missing.exe"])
        reporter.cycle()
        self.settle(reporter, "cannot_run")
        self.assertTrue(self.advice(reporter, status.CONTAIN_ACTION))

    def test_a_containment_step_that_fails_is_cannot_contain(self):
        reporter = self.start()
        with mock.patch.object(runner._Win, "create_job", return_value=None):
            reporter.cycle()
        self.settle(reporter, "cannot_contain")
        self.assertTrue(self.advice(reporter, status.CONTAIN_ACTION))
        self.assertEqual(self.calls(), [])                                               # the program was never started

    def test_a_program_that_never_ends_is_a_timeout(self):
        marker = os.path.join(self.dir, "sleep.pid")
        reporter = self.start(command=[PYTHON, os.path.join(self.dir, "sleep.py"), marker], timeout=1)
        started = time.monotonic()
        reporter.cycle()
        self.assertLess(time.monotonic() - started, 1 + 3)
        self.settle(reporter, "timeout")
        self.assertTrue(self.advice(reporter, status.NOT_FINISHING_ACTION))

    def test_a_helper_that_holds_the_output_is_a_leftover_process(self):
        marker = os.path.join(self.dir, "holder.pid")
        reporter = self.start(command=[PYTHON, os.path.join(self.dir, "holder.py"), marker])
        reporter.cycle()
        self.settle(reporter, "leftover_process")

    def test_a_failure_to_look_at_the_pipe_is_read_failed(self):
        reporter = self.start()
        with mock.patch.object(runner._Win, "peek", return_value=("error", 5)):
            reporter.cycle()
        self.settle(reporter, "read_failed")

    def test_too_much_output_is_a_refused_page(self):
        reporter = self.start(command=[PYTHON, os.path.join(self.dir, "big.py"), "5000"], max_bytes=1024)
        reporter.cycle()
        shown = self.receipt_status(reporter)
        self.assertEqual((shown["last_read_result"], shown["refused"]["too_large"]), ("refused", 1))
        self.assertEqual((self.count("receipt_capture"), shown["last_seq"]), (0, 0))

    def test_a_stop_ends_a_read_in_progress_as_interrupted(self):
        marker = os.path.join(self.dir, "up.pid")
        reporter = self.start(command=[PYTHON, os.path.join(self.dir, "sleep.py"), marker], timeout=30)
        probe = Probe(markers=[marker], ready=[marker])
        reporter.stop = probe
        self.addCleanup(probe.close)
        started = time.monotonic()
        reporter.cycle()
        self.assertLess(time.monotonic() - started, 10)                                  # not the 30 s deadline
        self.settle(reporter, "interrupted")
        self.assertTrue(probe.watches[marker].gone(3))                                   # and the program is gone

    def test_an_end_that_is_not_confirmed_is_counted_never_a_success_and_clears_when_confirmed(self):
        reporter = self.start()
        with mock.patch.object(runner._Win, "terminate_job", return_value=False):
            reporter.cycle()
        shown = self.receipt_status(reporter)
        self.assertEqual((self.count("receipt_capture"), shown["last_seq"]), (0, 0))       # the page was not used
        self.assertEqual(shown["cleanup_incomplete"], {"count": 1, "current": True})
        report = reporter.report()
        self.assertIn(status.CLEANUP_ACTION, report["operator_actions"])
        text = status.render_text(report)
        self.assertIn("termination requested but not confirmed 1 time(s) (not confirmed now)", text)
        reporter.cycle()                                                                    # this time it is confirmed
        shown = self.receipt_status(reporter)
        self.assertEqual(shown["cleanup_incomplete"], {"count": 1, "current": False})
        self.assertNotIn(status.CLEANUP_ACTION, reporter.report()["operator_actions"])
        self.assertEqual(self.count("receipt_capture"), 3)

    def test_the_advice_follows_the_current_failure_not_a_count_that_never_goes_down(self):
        reporter = self.start()
        self.set_mode("exit1")
        reporter.cycle()
        self.assertTrue(self.advice(reporter, status.NOT_FINISHING_ACTION))
        self.set_mode("ok")
        reporter.cycle()
        shown = self.receipt_status(reporter)
        self.assertEqual((shown["last_read_result"], shown["last_read_reason"]), ("ok", "ok"))
        self.assertEqual(shown["read_later"]["exit_code"], 1)                                # the history stays
        self.assertFalse(self.advice(reporter, status.NOT_FINISHING_ACTION))                 # the advice does not
        self.assertEqual(self.count("receipt_capture"), 3)

    def test_the_send_passes_are_independent_of_a_failing_read(self):
        """Receipts already waiting are still sent while the source fails."""
        reporter = self.start(count=2)
        reporter.cycle()
        self.assertEqual(len(self.net.settles()), 2)
        self.set_mode("exit1")
        self.write("ledger.json", json.dumps(ledger(self.rows + [receipt(lease(), 3)])))
        result = reporter.cycle()
        self.assertEqual(result.new_receipts, 0)
        self.assertEqual(self.receipt_status(reporter)["last_read_reason"], "exit_code")


class Budget(_Wired):
    """The cycle's receipt time budget, `max(timeout_seconds, 30)`, checked before a read starts, with a virtual clock."""

    def run_with(self, per_read, timeout=20):
        reporter = self.start(count=3, timeout=timeout)
        now = {"t": 1000.0}
        reads = []
        reporter.monotonic = lambda: now["t"]

        def fake(source, after, stop=None, month_rows=False):          # the real read_page now also takes the row shape (card R1-RC)
            reads.append(after)
            now["t"] += per_read                                    # this read takes `per_read` virtual seconds
            doc = page([receipt(self.leases[after], after + 1)], after=after, has_more=True)
            return receipts.ReadResult(receipts.OK, None, receipts.parse_page(json.dumps(doc).encode(), after))
        with mock.patch.object(run_mod.receipts, "read_page", fake):
            result = reporter.cycle()
        return reporter, result, reads

    def test_a_read_that_starts_just_before_the_budget_ends_runs_and_then_no_more_are_started(self):
        reporter, result, reads = self.run_with(per_read=29.9)
        self.assertEqual(reads, [0, 1])                             # the second started at 29.9 < 30: it ran; no third
        self.assertEqual(self.count("receipt_capture"), 2)

    def test_the_send_passes_still_run_when_the_budget_is_spent(self):
        reporter, result, reads = self.run_with(per_read=29.9)
        self.assertEqual(len(self.net.settles()), 2)                # the two receipts read were released and sent
        self.assertEqual(result.sent, 2)

    def test_the_budget_is_at_least_30_and_otherwise_the_time_limit(self):
        reporter, _result, reads = self.run_with(per_read=20, timeout=20)
        self.assertEqual(reads, [0, 1])                             # 0 and 20 start before 30; 40 does not
        other = Rig(server=False)
        self.addCleanup(other.cleanup)
        self.rig = other
        reporter, _result, reads = self.run_with(per_read=50, timeout=60)
        self.assertEqual(reads, [0, 1])                             # budget 60: 0 and 50 start, 100 does not

    def test_the_next_cycle_goes_on_from_the_cursor(self):
        reporter, _result, reads = self.run_with(per_read=29.9)
        self.assertEqual(reads, [0, 1])
        self.assertEqual(self.receipt_status(reporter)["last_seq"], 2)


class Privacy(_Wired):

    def dump(self, reporter):
        """Everything the reporter keeps or shows."""
        shown = [json.dumps(reporter.report()), status.render_text(reporter.report())]
        for (name,) in reporter.state.all("SELECT name FROM sqlite_master WHERE type='table'"):
            shown.append(repr(reporter.state.all("SELECT * FROM %s" % name)))
        shown.append(repr(reporter.gateway_receipts))
        return "\n".join(shown)

    def test_a_loud_string_in_the_output_the_error_output_the_path_and_the_environment_appears_nowhere(self):
        folder = os.path.join(self.dir, "dir-" + LOUD)
        os.makedirs(folder)
        pager = os.path.join(folder, "pager.py")
        shutil.copy(self.pager, pager)
        reporter = self.start(command=[PYTHON, pager, self.ledger, self.log, self.mode, "arg-" + LOUD],
                              env={"LOUD_ENV": LOUD})
        for word in ("exit1", "garbage", "ok"):
            self.set_mode(word)
            reporter.cycle()
        self.set_mode("exit1")
        with mock.patch.object(runner._Win, "terminate_job", return_value=False):
            reporter.cycle()
        with mock.patch.object(runner._Win, "peek", return_value=("error", 5)):
            reporter.cycle()
        text = self.dump(reporter)
        self.assertNotIn(LOUD, text)
        self.assertTrue(self.calls())                                # the fake command did run, and say it


class OldSnapshot(_Wired):

    def test_a_status_snapshot_of_an_older_reporter_is_read_with_defaults(self):
        reporter = self.start()
        reporter.cycle()
        report = reporter.report()
        for key in ("last_read_reason", "source_kind", "cleanup_incomplete"):
            del report["gateway_receipts"][key]                      # an older reporter wrote none of these
        text = status.render_text(report)                            # does not raise
        self.assertIn("gateway receipt source: fixture; last read: not read yet", text)
        self.assertNotIn("termination requested", text)


if __name__ == "__main__":
    unittest.main()
