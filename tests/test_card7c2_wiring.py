"""Card 7c-2, step 3: the capture pass is part of every cycle, and it can never hold lease sending back.

`cycle` (and so `run` and `drain`) reads the journal, captures the park markers, then sends: the limit
pass first, then the leases. A fault inside capture is caught whole, counted, and keeps no text.
A configuration without `limit_events` behaves exactly as before. Synthetic data; a fake transport.
"""

import datetime
import json
import os
import unittest
import uuid
from unittest import mock

from quota_balancer.reporter import capture, config as cfg
from quota_balancer.reporter.cli import EXIT_OK, EXIT_REFUSED

from tests.reporter_env import Rig
from tests.test_card7a3 import cli, one_message
from tests.test_card7c2_capture import ACCOUNT_ID, LOUD, source
from tests.test_card7c2_delivery import LIMIT_PATH, Net
from tests.test_card7c2_reader import marker

PARK_DIR = os.path.join("state", "usage-limit-park")


def iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _Wired(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        self.bus = os.path.join(self.rig.ws.dir, "bus")
        self.folder = os.path.join(self.bus, PARK_DIR)
        os.makedirs(self.folder)

    def park(self, agent="agent-one", **over):
        now = self.rig.clock.ms
        doc = marker(agent=agent, now_ms=now, parked_at=iso(now - 60_000),
                     reset_epoch=now // 1000 + 3600, wake_epoch=now // 1000 + 3630, **over)
        with open(os.path.join(self.folder, agent + ".json"), "w", encoding="utf-8") as handle:
            json.dump(doc, handle)

    def start(self, net, sources=True, message=False):
        if message:
            one_message(self.rig.journal, self.rig.clock.ms)
        reporter = self.rig.open(transport=net)
        if sources:
            reporter.limit_sources = (source(self.bus),)
        return reporter

    def limit_rows(self):
        return self.rig.state.all("SELECT state, tries FROM limit_event")


class Cycle(_Wired):

    def test_a_cycle_captures_the_marker_and_delivers_it_in_the_same_cycle(self):
        net = Net()
        reporter = self.start(net)
        self.park()
        result = reporter.cycle()
        self.assertEqual(result.new_limit_events, 1)
        self.assertEqual(self.limit_rows(), [("sent", 0)])
        self.assertEqual(net.paths(), [LIMIT_PATH])
        self.assertEqual(reporter.cycle().new_limit_events, 0)               # the same fact is not queued again
        self.assertEqual(net.paths(), [LIMIT_PATH])                          # nor sent again

    def test_drain_does_the_same(self):
        net = Net()
        reporter = self.start(net)
        self.park()
        self.assertEqual(reporter.drain().new_limit_events, 1)
        self.assertEqual(self.limit_rows(), [("sent", 0)])

    def test_the_result_line_is_unchanged(self):
        reporter = self.start(Net())
        self.park()
        self.assertEqual(set(reporter.cycle().as_dict()), {"new_events", "sent", "blocked", "journal"})

    def test_without_sources_capture_does_nothing_and_is_never_called(self):
        reporter = self.start(Net(), sources=False)
        self.park()
        with mock.patch.object(capture, "run", side_effect=AssertionError("capture must not run")):
            reporter.cycle()
        self.assertEqual(self.limit_rows(), [])
        self.assertEqual(self.rig.state.all("SELECT * FROM counter WHERE agent='_limit_events'"), [])

    def test_a_parked_account_refusing_every_lease_still_gets_its_limit_event(self):
        net = Net()                                                           # leases: 429 ACCOUNT_PARKED
        reporter = self.start(net, message=True)
        self.park()
        reporter.cycle()
        self.assertEqual(self.limit_rows(), [("sent", 0)])
        self.assertEqual(net.paths()[0], LIMIT_PATH)

    def test_a_marker_stored_while_leases_back_off_is_still_captured_and_sent(self):
        net = Net()
        reporter = self.start(net, message=True)
        reporter.cycle()                                                       # the lease is refused and backs off
        self.park()
        reporter.cycle()
        self.assertEqual(self.limit_rows(), [("sent", 0)])


class Faults(_Wired):

    def test_a_fault_in_capture_is_counted_keeps_no_text_and_never_stops_lease_sending(self):
        lease = str(uuid.uuid4())
        net = Net(lease=[(201, {"lease_id": lease, "state": "active"}), (200, {})])
        reporter = self.start(net, message=True)
        with mock.patch.object(capture, "run", side_effect=RuntimeError(LOUD)):
            result = reporter.cycle()
        self.assertEqual(self.rig.state.one("SELECT state FROM acquisition")[0], "acquired")   # leases went on
        self.assertEqual(result.new_limit_events, 0)
        self.assertEqual(self.rig.state.counter("_limit_events", "capture_fault"), 1)
        dump = json.dumps([self.rig.state.all("SELECT * FROM %s" % t) for t in ("counter", "note", "limit_event")])
        self.assertNotIn(LOUD, dump)
        self.assertNotIn(LOUD, json.dumps(reporter.report()))

    def test_even_a_failing_fault_count_does_not_stop_sending(self):
        lease = str(uuid.uuid4())
        net = Net(lease=[(201, {"lease_id": lease, "state": "active"}), (200, {})])
        reporter = self.start(net, message=True)
        with mock.patch.object(capture, "run", side_effect=RuntimeError(LOUD)), \
                mock.patch.object(type(self.rig.state), "note_capture_fault", side_effect=RuntimeError(LOUD)):
            reporter.cycle()
        self.assertEqual(self.rig.state.one("SELECT state FROM acquisition")[0], "acquired")

    def test_a_bad_marker_never_stops_a_cycle_or_reaches_the_queue(self):
        net = Net()
        reporter = self.start(net, message=False)
        with open(os.path.join(self.folder, "agent-one.json"), "wb") as handle:
            handle.write(b'{"torn')
        result = reporter.cycle()
        self.assertIsNone(result.blocked)
        self.assertEqual(self.limit_rows(), [])
        self.assertEqual(self.rig.state.counter("_limit_events", "scan:read_later"), 1)


class Start(_Wired):
    """`reporter run|drain` resolves the account map at start, or refuses."""

    def write(self, accounts):
        with open(self.rig.ws.snapshot, "r", encoding="utf-8") as handle:
            snap = json.load(handle)
        snap["accounts"] = accounts
        with open(self.rig.ws.snapshot, "w", encoding="utf-8") as handle:
            json.dump(snap, handle)
        extra = ("[[limit_events.sources]]\nbus_root = %s\nos_user = \"operator-desktop\"\nprovider = \"claude\"\n"
                 "account = \"claude-main\"\n" % json.dumps(self.bus))
        return self.rig.ws.write_config("http://127.0.0.1:9", self.rig.agents_cfg, extra=extra)

    def test_a_resolved_map_starts_and_captures(self):
        path = self.write([{"alias": "claude-main", "id": ACCOUNT_ID}])
        net = Net()
        self.park_now()
        code, out, err = cli(["drain", "--config", path], permissions=lambda p: None, transport=net)
        self.assertEqual((code, err), (EXIT_OK, ""))
        self.assertEqual(net.paths(), [LIMIT_PATH])
        self.assertEqual(json.loads(net.calls[0][2])["account_id"], ACCOUNT_ID)

    def park_now(self):
        import time
        now = int(time.time() * 1000)
        doc = marker(now_ms=now, parked_at=iso(now - 60_000), reset_epoch=now // 1000 + 3600)
        with open(os.path.join(self.folder, "agent-one.json"), "w", encoding="utf-8") as handle:
            json.dump(doc, handle)

    def test_a_missing_account_refuses_start_with_a_fixed_sentence(self):
        path = self.write([{"alias": "someone-else", "id": ACCOUNT_ID}])
        code, out, err = cli(["drain", "--config", path], permissions=lambda p: None, transport=Net())
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn(cfg.CONFIG_UNRESOLVED, err)
        self.assertNotIn("claude-main", err)
        self.assertNotIn(self.bus, err)

    def test_an_ambiguous_account_refuses_start(self):
        path = self.write([{"alias": "claude-main", "id": ACCOUNT_ID},
                           {"alias": "claude-main", "id": str(uuid.uuid4())}])
        code, _out, err = cli(["drain", "--config", path], permissions=lambda p: None, transport=Net())
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn(cfg.CONFIG_UNRESOLVED, err)

    def test_a_configuration_without_the_block_starts_as_before(self):
        net = Net()
        code, out, err = cli(["drain", "--config", self.rig.config_path()
                              if hasattr(self.rig, "config_path") else self.rig.ws.config],
                             permissions=lambda p: None, transport=net)
        self.assertEqual((code, err), (EXIT_OK, ""))


if __name__ == "__main__":
    unittest.main()
