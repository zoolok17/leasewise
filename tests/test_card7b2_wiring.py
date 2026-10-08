"""Card 7b, step 7b-2: the readings capture is part of every cycle and can never hold anything else back.

`Reporter.cycle` looks at the capacity files at most every 30 seconds (the reporter's monotonic clock, memory only),
keeps the readings tables tidy every cycle, and never sends a reading in this step (delivery is step 7b-3). A fault in
readings capture is caught whole, counted, and keeps no text; it stops neither limit-event capture nor lease sending, and
the reverse. A configuration without `readings` behaves exactly as before. Synthetic data; a fake transport.
"""

import json
import os
import unittest
import uuid
from unittest import mock

from quota_balancer.reporter import capture, config as cfg, readings
from quota_balancer.reporter.cli import EXIT_OK, EXIT_REFUSED
from quota_balancer.reporter.state import READING_AGENT, State
from tests.card7b2_fixtures import CLAUDE_ACCOUNT, NOW, edited, put
from tests.reporter_env import Rig
from tests.test_card7a3 import cli, one_message
from tests.test_card7c2_capture import source as limit_source
from tests.test_card7b2_delivery import ReadingNet
from tests.test_card7c2_delivery import Net
from tests.test_card7c2_reader import marker

ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"
LOUD = "SECRET-USER-readings-9f3a"
PARK_DIR = os.path.join("state", "usage-limit-park")


def reading_source(bus_root, account="claude-main"):
    row = cfg.ResolvedReadingSource()
    row.bus_root, row.folder, row.provider = bus_root, cfg.folder_key(bus_root), "claude"
    row.agenttalk_account, row.account, row.account_id = CLAUDE_ACCOUNT, account, ACCOUNT_ID
    return row


class _Wired(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        self.rig.clock.ms = NOW
        self.bus = os.path.join(self.rig.ws.dir, "bus")
        self.folder = os.path.join(self.bus, "state")
        os.makedirs(os.path.join(self.bus, PARK_DIR))
        self.clock = [1000.0]
        self.write()

    def write(self, **over):
        put(self.folder, edited("claude_stream_both", source_agent="seat-a", **over))

    def start(self, net, readings_on=True, limits_on=False, message=False):
        if message:
            one_message(self.rig.journal, self.rig.clock.ms)
        reporter = self.rig.open(transport=net)
        reporter.monotonic = lambda: self.clock[0]
        if readings_on:
            reporter.reading_sources = (reading_source(self.bus),)
        if limits_on:
            reporter.limit_sources = (limit_source(self.bus),)
        return reporter

    def rows(self):
        return self.rig.state.all("SELECT state, window_key FROM reading ORDER BY id")

    def park(self):
        now = self.rig.clock.ms
        doc = marker(agent="agent-one", now_ms=now, parked_at="2026-10-08T11:59:00.000000Z",
                     reset_epoch=now // 1000 + 3600, wake_epoch=now // 1000 + 3630)
        with open(os.path.join(self.bus, PARK_DIR, "agent-one.json"), "w", encoding="utf-8") as handle:
            json.dump(doc, handle)


class Cycle(_Wired):
    def test_a_cycle_stores_the_readings_and_sends_them_in_the_same_cycle(self):
        net = ReadingNet()
        reporter = self.start(net)
        result = reporter.cycle()
        self.assertEqual(result.new_readings, 2)
        self.assertEqual(self.rows(), [("sent", "five_hour"), ("sent", "seven_day")])
        self.assertEqual(net.paths(), ["/v1/readings", "/v1/readings"])
        self.assertEqual(reporter.cycle().new_readings, 0)                       # the same fact is not stored or sent again
        self.assertEqual(len(net.paths()), 2)

    def test_the_result_line_is_unchanged(self):
        reporter = self.start(Net())
        self.assertEqual(set(reporter.cycle().as_dict()), {"new_events", "sent", "blocked", "journal"})

    def test_without_sources_nothing_runs_and_nothing_is_counted(self):
        reporter = self.start(Net(), readings_on=False)
        with mock.patch.object(readings, "run", side_effect=AssertionError("must not run")), \
                mock.patch.object(type(self.rig.state), "housekeep_readings", side_effect=AssertionError("must not run")):
            reporter.cycle()
            reporter.cycle()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.rig.state.all("SELECT * FROM counter WHERE agent=?", (READING_AGENT,)), [])
        self.assertEqual(self.rig.state.all("SELECT * FROM note WHERE name LIKE 'readings%'"), [])

    def test_the_files_are_looked_at_every_thirty_seconds_at_most(self):
        reporter = self.start(Net())
        with mock.patch.object(readings, "run", wraps=readings.run) as run:
            reporter.cycle()
            self.clock[0] += 29.9
            reporter.cycle()
            self.assertEqual(run.call_count, 1)
            self.clock[0] += 0.1
            reporter.cycle()
            self.assertEqual(run.call_count, 2)

    def test_a_restart_scans_at_once(self):
        reporter = self.start(Net())
        reporter.cycle()
        self.rig.close()
        reporter = self.start(Net())
        with mock.patch.object(readings, "run", wraps=readings.run) as run:
            reporter.cycle()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(self.rows()), 2)

    def test_the_tidy_up_runs_every_cycle_even_between_scans(self):
        reporter = self.start(Net())
        reporter.cycle()
        with mock.patch.object(type(self.rig.state), "housekeep_readings", wraps=self.rig.state.housekeep_readings) as tidy:
            self.clock[0] += 5
            reporter.cycle()
            reporter.cycle()
        self.assertEqual(tidy.call_count, 2)

    def test_a_pending_row_expires_through_the_cycle(self):
        reporter = self.start(Net())
        reporter.cycle()
        self.rig.clock.ms = NOW + 5 * 3_600_000
        self.clock[0] += 10
        reporter.cycle()
        self.assertEqual(self.rows(), [("expired", "five_hour"), ("pending", "seven_day")])        # the weekly reset is days away

    def test_a_new_figure_supersedes_through_the_cycle(self):
        reporter = self.start(Net())
        reporter.cycle()
        self.write(primary_observed_at="2026-10-08T12:00:20Z", primary_used_percent=50.0)
        self.rig.clock.ms = NOW + 30_000
        self.clock[0] += 31
        reporter.cycle()
        self.assertEqual(self.rows(), [("superseded", "five_hour"), ("pending", "seven_day"), ("pending", "five_hour")])


class Faults(_Wired):
    def test_a_fault_in_the_readings_capture_is_counted_keeps_no_text_and_never_stops_lease_sending(self):
        lease = str(uuid.uuid4())
        net = Net(lease=[(201, {"lease_id": lease, "state": "active"}), (200, {})])
        reporter = self.start(net, message=True)
        with mock.patch.object(readings, "run", side_effect=RuntimeError(LOUD)):
            result = reporter.cycle()
        self.assertEqual(self.rig.state.one("SELECT state FROM acquisition")[0], "acquired")
        self.assertEqual(result.new_readings, 0)
        self.assertEqual(self.rig.state.counter(READING_AGENT, "capture_fault"), 1)
        dump = json.dumps([self.rig.state.all("SELECT * FROM %s" % t) for t in ("counter", "note", "reading")])
        self.assertNotIn(LOUD, dump)

    def test_a_fault_in_the_readings_capture_does_not_stop_limit_events(self):
        net = Net()
        reporter = self.start(net, limits_on=True)
        self.park()
        with mock.patch.object(readings, "run", side_effect=RuntimeError(LOUD)):
            result = reporter.cycle()
        self.assertEqual(result.new_limit_events, 1)
        self.assertEqual(self.rig.state.one("SELECT state FROM limit_event")[0], "sent")

    def test_a_fault_in_limit_capture_does_not_stop_readings(self):
        reporter = self.start(Net(), limits_on=True)
        self.park()
        with mock.patch.object(capture, "run", side_effect=RuntimeError(LOUD)):
            result = reporter.cycle()
        self.assertEqual(result.new_readings, 2)
        self.assertEqual(self.rig.state.counter("_limit_events", "capture_fault"), 1)
        self.assertEqual(self.rig.state.counter(READING_AGENT, "capture_fault"), 0)

    def test_a_fault_in_the_tidy_up_is_caught_too(self):
        reporter = self.start(Net())
        with mock.patch.object(type(self.rig.state), "housekeep_readings", side_effect=RuntimeError(LOUD)):
            reporter.cycle()
        self.assertEqual(self.rig.state.counter(READING_AGENT, "capture_fault"), 1)

    def test_even_the_fault_count_may_fail_and_the_cycle_goes_on(self):
        reporter = self.start(Net())
        with mock.patch.object(readings, "run", side_effect=RuntimeError(LOUD)), \
                mock.patch.object(type(self.rig.state), "note_readings_fault", side_effect=RuntimeError(LOUD)):
            reporter.cycle()

    def test_a_bad_file_is_a_count_not_a_fault(self):
        put(self.folder, edited("claude_stream_both", source_agent="seat-z", scope="seat"))
        reporter = self.start(Net())
        reporter.cycle()
        self.assertEqual(self.rig.state.counter(READING_AGENT, "capture_fault"), 0)
        self.assertEqual(self.rig.state.counter(READING_AGENT, "scan:file_refused"), 1)


class Start(unittest.TestCase):
    """`reporter run|drain` resolves the readings map at start, or refuses."""

    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        self.bus = os.path.join(self.rig.ws.dir, "bus")
        self.folder = os.path.join(self.bus, "state")
        os.makedirs(self.folder)

    def write(self, accounts):
        with open(self.rig.ws.snapshot, "r", encoding="utf-8") as handle:
            snap = json.load(handle)
        snap["accounts"] = accounts
        with open(self.rig.ws.snapshot, "w", encoding="utf-8") as handle:
            json.dump(snap, handle)
        extra = "\n".join(["[[readings.sources]]", "bus_root = " + json.dumps(self.bus), 'provider = "claude"',
                           "agenttalk_account = " + json.dumps(CLAUDE_ACCOUNT), 'account = "claude-main"', ""])
        return self.rig.ws.write_config("http://127.0.0.1:9", self.rig.agents_cfg, extra=extra)

    def fresh_file(self):
        import time
        now = time.time()
        stamp = lambda seconds: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + seconds))    # noqa: E731
        put(self.folder, edited("claude_stream_both", source_agent="seat-a", observed_at=stamp(-10),
                                primary_observed_at=stamp(-10), secondary_observed_at=stamp(-10),
                                primary_resets_at=int(now) + 3600, secondary_resets_at=int(now) + 86_400))

    def test_a_resolved_map_starts_stores_and_sends_the_readings(self):
        path = self.write([{"alias": "claude-main", "id": ACCOUNT_ID}])
        self.fresh_file()
        net = ReadingNet()
        code, out, err = cli(["drain", "--config", path], permissions=lambda p: None, transport=net)
        self.assertEqual((code, err), (EXIT_OK, ""))
        self.assertEqual(net.paths(), ["/v1/readings", "/v1/readings"])
        state = State(self.rig.ws.state)
        try:
            self.assertEqual(state.all("SELECT state, window_key FROM reading ORDER BY id"),
                             [("sent", "five_hour"), ("sent", "seven_day")])
            self.assertEqual(json.loads(state.one("SELECT body FROM reading")[0])["account_id"], ACCOUNT_ID)
        finally:
            state.close()

    def test_a_missing_account_refuses_start_with_a_fixed_sentence(self):
        path = self.write([{"alias": "someone-else", "id": ACCOUNT_ID}])
        code, out, err = cli(["drain", "--config", path], permissions=lambda p: None, transport=Net())
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn(cfg.CONFIG_UNRESOLVED, err)
        self.assertIn("readings: an account is not in the policy-ids snapshot", err)
        for word in ("claude-main", self.bus, CLAUDE_ACCOUNT, "<os-user>"):
            self.assertNotIn(word, err)

    def test_an_old_snapshot_without_accounts_refuses_start(self):
        path = self.write([])
        code, out, err = cli(["drain", "--config", path], permissions=lambda p: None, transport=Net())
        self.assertEqual(code, EXIT_REFUSED)


if __name__ == "__main__":
    unittest.main()
