"""Card 7c-2, step 4: `reporter status` shows limit events (status version 2, decision D3).

The report gains a top-level `limit_events` object and says, in the words claude-agenttalk-lead
approved, that capture is best effort. Closed words and counts only: never a path, a file name, an
agent name, a message id, a parsed value or exception text. Synthetic data only.
"""

import json
import os
import unittest

from quota_balancer.reporter import capture, status

from tests.reporter_env import Rig
from tests.test_card7a3 import cli
from tests.test_card7c2_capture import source
from tests.test_card7c2_delivery import Net
from tests.test_card7c2_reader import marker

GAP = ("A park that starts and ends while the reporter is not running, or between two checks, is never "
       "reported. The seat still parks on its own, and the next refusal after the reporter is back is "
       "reported. This closes when agenttalk keeps a permanent \"parked\" record in its turn journal (a later "
       "agenttalk change, after the turn-journal pull request #300 is merged, together with Codex limit "
       "events). Until then, limit events are best effort.")
OBJECT_KEYS = {"configured", "sources", "last_scan", "scan", "captured", "rows", "failed", "capture_fault",
               "last_ok", "gap"}


class _Status(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        self.bus = os.path.join(self.rig.ws.dir, "bus")
        self.folder = os.path.join(self.bus, "state", "usage-limit-park")
        os.makedirs(self.folder)

    def reporter(self, net=None, sources=True):
        reporter = self.rig.open(transport=net or Net())
        if sources:
            reporter.limit_sources = (source(self.bus),)
        return reporter

    def park(self, agent="agent-one", **over):
        import time
        from tests.test_card7c2_wiring import iso
        now = int(time.time() * 1000)
        doc = marker(agent=agent, now_ms=now, parked_at=iso(now - 60_000), reset_epoch=now // 1000 + 3600,
                     wake_epoch=now // 1000 + 3630, **over)
        with open(os.path.join(self.folder, agent + ".json"), "w", encoding="utf-8") as handle:
            json.dump(doc, handle)


class Report(_Status):

    def test_the_status_version_is_5_and_the_object_has_a_closed_shape(self):
        report = self.reporter().report()
        self.assertEqual(report["status_version"], 5)
        self.assertEqual(set(report["limit_events"]), OBJECT_KEYS)

    def test_without_limit_sources_it_says_not_configured_and_carries_no_gap(self):
        report = self.reporter(sources=False).report()
        limit = report["limit_events"]
        self.assertFalse(limit["configured"])
        self.assertEqual((limit["sources"], limit["captured"], limit["gap"]), ([], 0, None))
        self.assertEqual(limit["rows"], {"pending": 0, "sent": 0, "failed_permanent": 0})
        self.assertNotIn("limit events", status.render_text(report))

    def test_configured_it_lists_the_map_in_use_and_the_gap_sentence_verbatim(self):
        report = self.reporter().report()
        limit = report["limit_events"]
        self.assertTrue(limit["configured"])
        self.assertEqual(limit["sources"], [{"os_user": "operator-desktop", "provider": "claude",
                                             "account": "claude-main"}])                   # aliases the operator wrote
        self.assertEqual(limit["gap"], GAP)
        self.assertEqual(status.LIMIT_GAP, GAP)

    def test_the_text_has_exactly_one_line_with_the_sentence(self):
        text = status.render_text(self.reporter().report())
        lines = [line for line in text.splitlines() if line.startswith("limit events")]
        self.assertEqual(lines, ["limit events (best effort): " + GAP])

    def test_counts_after_a_capture_and_a_delivery(self):
        reporter = self.reporter()
        self.park("agent-one")
        self.park("agent-two")                                    # the same limit: one event
        with open(os.path.join(self.folder, "agent-bad.json"), "wb") as handle:
            handle.write(b'{"torn')
        reporter.cycle()
        limit = reporter.report()["limit_events"]
        self.assertEqual(limit["captured"], 1)
        self.assertEqual(limit["rows"], {"pending": 0, "sent": 1, "failed_permanent": 0})
        self.assertEqual(limit["scan"]["read_later"], 1)
        self.assertEqual(set(limit["scan"]), set(capture.SKIP_COUNTS))
        self.assertIn("too_many_entries", limit["scan"])
        self.assertIsNotNone(limit["last_scan"])
        self.assertIsNotNone(limit["last_ok"])
        self.assertEqual(limit["capture_fault"], {"count": 0, "last": None})
        self.assertEqual(limit["failed"], {})

    def test_the_scan_counts_describe_the_last_scan_only(self):
        reporter = self.reporter()
        with open(os.path.join(self.folder, "agent-bad.json"), "wb") as handle:
            handle.write(b'{"torn')
        reporter.cycle()
        reporter.cycle()
        self.assertEqual(reporter.report()["limit_events"]["scan"]["read_later"], 1)       # not 2
        os.remove(os.path.join(self.folder, "agent-bad.json"))
        reporter.cycle()
        self.assertEqual(reporter.report()["limit_events"]["scan"]["read_later"], 0)

    def test_a_refused_row_is_counted_by_its_closed_word_and_asks_for_attention(self):
        net = Net(limit=[(403, {"error": {"code": "FORBIDDEN", "message": "x"}})])
        reporter = self.reporter(net)
        self.park()
        reporter.cycle()
        limit = reporter.report()["limit_events"]
        self.assertEqual(limit["rows"], {"pending": 0, "sent": 0, "failed_permanent": 1})
        self.assertEqual(limit["failed"], {"FORBIDDEN": 1})
        actions = reporter.report()["operator_actions"]
        self.assertTrue(any("limit event" in line for line in actions), actions)

    def test_a_capture_fault_shows_a_count_and_a_time_and_asks_for_attention(self):
        reporter = self.reporter()
        from unittest import mock
        with mock.patch.object(capture, "run", side_effect=RuntimeError("LOUD-FAULT-TEXT")):
            reporter.cycle()
        report = reporter.report()
        self.assertEqual(report["limit_events"]["capture_fault"]["count"], 1)
        self.assertIsNotNone(report["limit_events"]["capture_fault"]["last"])
        self.assertTrue(any("capture" in line for line in report["operator_actions"]))
        self.assertNotIn("LOUD-FAULT-TEXT", json.dumps(report) + status.render_text(report))

    def test_a_pending_row_is_counted_while_leasewise_is_down(self):
        net = Net(limit=[(503, {})])
        reporter = self.reporter(net)
        self.park()
        reporter.cycle()
        self.assertEqual(reporter.report()["limit_events"]["rows"]["pending"], 1)


class Command(_Status):
    """`reporter status` (not running): built from the state file and the configuration."""

    def write(self, with_block):
        extra = ""
        if with_block:
            extra = ("[[limit_events.sources]]\nbus_root = %s\nos_user = \"operator-desktop\"\nprovider = \"claude\"\n"
                     "account = \"claude-main\"\n" % json.dumps(self.bus))
        return self.rig.ws.write_config("http://127.0.0.1:9", self.rig.agents_cfg, extra=extra)

    def test_the_command_reports_configured_and_the_sentence(self):
        path = self.write(True)
        code, out, err = cli(["status", "--config", path, "--json"], permissions=lambda p: None)
        self.assertEqual((code, err), (0, ""))
        limit = json.loads(out)["limit_events"]
        self.assertTrue(limit["configured"])
        self.assertEqual(limit["gap"], GAP)
        code, text, _err = cli(["status", "--config", path], permissions=lambda p: None)
        self.assertIn("limit events (best effort): " + GAP, text)

    def test_the_command_without_the_block_says_not_configured(self):
        path = self.write(False)
        code, out, err = cli(["status", "--config", path, "--json"], permissions=lambda p: None)
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["limit_events"]["configured"])
        code, text, _err = cli(["status", "--config", path], permissions=lambda p: None)
        self.assertNotIn("limit events", text)

    def test_a_running_reporters_snapshot_carries_the_object(self):
        reporter = self.reporter()
        self.park()
        reporter.cycle()
        snap = status.read_snapshot(status.snapshot_path(self.rig.state.path))
        self.assertEqual(snap["status_version"], 5)
        self.assertEqual(snap["limit_events"]["gap"], GAP)
        self.assertEqual(snap["limit_events"]["captured"], 1)


if __name__ == "__main__":
    unittest.main()
