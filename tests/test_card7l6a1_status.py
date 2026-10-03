"""Card L6a-1, step 5: `reporter status` shows gateway receipts (status version 3).

The report gains a top-level `gateway_receipts` object: whether it is configured, the last `seq`, the
rows by state, the failures by closed word, how often a read had to wait or was refused (by closed
reason), and the plain note that cached input is reported as 0. Closed words and counts only: never a
path, a command, a lease id, a reference, a figure from a receipt or exception text. Synthetic data only.
"""

import json
import os
import unittest
from unittest import mock

from quota_balancer.reporter import receipts, status

from tests.l6a1_env import GATEWAY_ALIAS, configure, lease, receipt, write_json
from tests.reporter_env import Rig
from tests.test_card7a3 import cli
from tests.test_card7l6a1_delivery import Net, error

NOTE = ("Cached input is reported as 0: the gateway records none, so 0 is not proof that none was used.")
OBJECT_KEYS = {"configured", "gateway", "last_seq", "last_read", "last_read_result", "stored", "rows", "failed",
               "read_later", "refused", "read_fault", "last_ok", "cached_input"}


class _Status(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        self.leases = []

    def reporter(self, rows=None, net=None, count=1):
        self.leases = [lease() for _ in range(count)]
        if rows is None:
            rows = [receipt(l, n + 1) for n, l in enumerate(self.leases)]
        self.fixture = configure(self.rig, rows)
        reporter = self.rig.open(transport=net or Net())
        with self.rig.state.txn():
            for lease_id in self.leases:
                self.rig.state.remember_lease(lease_id)
        return reporter


class Report(_Status):

    def test_the_status_version_is_3_and_the_object_has_a_closed_shape(self):
        report = self.reporter().report()
        self.assertEqual(report["status_version"], 3)
        self.assertEqual(set(report["gateway_receipts"]), OBJECT_KEYS)
        self.assertEqual(set(report["gateway_receipts"]["read_later"]), set(receipts.LATER_REASONS))
        self.assertEqual(set(report["gateway_receipts"]["refused"]), set(receipts.REFUSED_REASONS))

    def test_without_the_block_it_says_not_configured_and_shows_nothing_more(self):
        rig = Rig(server=False)
        self.addCleanup(rig.cleanup)
        object_ = rig.open(transport=Net()).report()["gateway_receipts"]
        self.assertEqual((object_["configured"], object_["gateway"], object_["last_seq"], object_["cached_input"]),
                         (False, None, 0, None))
        self.assertEqual(object_["rows"], {"pending": 0, "sent": 0, "failed_permanent": 0})
        self.assertNotIn("gateway receipts", status.render_text(rig.reporter.report()))

    def test_configured_before_any_read_it_says_so_with_the_note(self):
        object_ = self.reporter().report()["gateway_receipts"]
        self.assertEqual((object_["configured"], object_["gateway"], object_["last_seq"]), (True, GATEWAY_ALIAS, 0))
        self.assertEqual((object_["last_read"], object_["last_read_result"], object_["stored"]), (None, None, 0))
        self.assertEqual(object_["cached_input"], NOTE)
        self.assertEqual(status.CACHED_INPUT_NOTE, NOTE)

    def test_counts_after_a_read_and_a_delivery(self):
        reporter = self.reporter(count=3)
        reporter.cycle()
        object_ = reporter.report()["gateway_receipts"]
        self.assertEqual((object_["last_seq"], object_["stored"]), (3, 3))
        self.assertEqual(object_["rows"], {"pending": 0, "sent": 3, "failed_permanent": 0})
        self.assertEqual((object_["last_read_result"], object_["failed"], object_["read_fault"]), ("ok", {}, 0))
        self.assertIsNotNone(object_["last_read"])
        self.assertIsNotNone(object_["last_ok"])
        self.assertEqual(reporter.report()["operator_actions"], [])

    def test_the_text_has_one_line_with_the_counts_and_the_note(self):
        reporter = self.reporter(count=2)
        reporter.cycle()
        lines = [line for line in status.render_text(reporter.report()).splitlines()
                 if line.startswith("gateway receipts")]
        self.assertEqual(lines, ["gateway receipts: last seq 2, pending 0, delivered 2, failed 0. " + NOTE])

    def test_a_failed_row_is_counted_by_its_closed_word_and_asks_for_attention(self):
        net = Net(receipt=[error("RECEIPT_CONFLICT", 409)])
        reporter = self.reporter(net=net)
        reporter.cycle()
        object_ = reporter.report()["gateway_receipts"]
        self.assertEqual(object_["rows"], {"pending": 0, "sent": 0, "failed_permanent": 1})
        self.assertEqual(object_["failed"], {"RECEIPT_CONFLICT": 1})
        self.assertTrue(any("gateway receipt" in line for line in reporter.report()["operator_actions"]))

    def test_a_pending_row_is_counted_while_leasewise_is_down(self):
        reporter = self.reporter(net=Net(receipt=[(503, {})]))
        reporter.cycle()
        self.assertEqual(reporter.report()["gateway_receipts"]["rows"]["pending"], 1)

    def test_a_reference_that_is_not_ours_is_a_failed_row_with_its_word(self):
        reporter = self.reporter(rows=[receipt(lease(), 1)])
        reporter.cycle()
        object_ = reporter.report()["gateway_receipts"]
        self.assertEqual((object_["failed"], object_["last_seq"]), ({"UNKNOWN_LEASE": 1}, 1))

    def test_a_source_that_is_not_there_is_counted_as_read_later_and_a_bad_one_as_refused(self):
        reporter = self.reporter()
        os.remove(self.fixture)
        reporter.cycle()
        reporter.cycle()
        object_ = reporter.report()["gateway_receipts"]
        self.assertEqual((object_["read_later"]["source_missing"], object_["last_read_result"]), (2, "read_later"))
        self.assertEqual(reporter.report()["operator_actions"], [])         # a wait is not a fault
        write_json(self.fixture, [receipt(lease(), 1, outcome="nope")])
        reporter.cycle()
        object_ = reporter.report()["gateway_receipts"]
        self.assertEqual((object_["refused"]["bad_row"], object_["last_read_result"]), (1, "refused"))
        self.assertEqual((object_["last_seq"], object_["rows"]["pending"], object_["stored"]), (0, 0, 0))
        self.assertTrue(any("refused" in line for line in reporter.report()["operator_actions"]))
        # fixed: the next good read clears the attention
        write_json(self.fixture, [receipt(self.leases[0], 1)])
        reporter.cycle()
        self.assertEqual(reporter.report()["gateway_receipts"]["last_read_result"], "ok")
        self.assertEqual(reporter.report()["operator_actions"], [])

    def test_a_fault_in_reading_is_a_count_and_never_stops_sending(self):
        reporter = self.reporter(count=1)
        with mock.patch.object(receipts, "read_receipts", side_effect=RuntimeError("LOUD-FAULT-TEXT")):
            reporter.cycle()
        report = reporter.report()
        self.assertEqual(report["gateway_receipts"]["read_fault"], 1)
        self.assertTrue(any("reading gateway receipts" in line for line in report["operator_actions"]))
        self.assertNotIn("LOUD-FAULT-TEXT", json.dumps(report) + status.render_text(report))
        reporter.cycle()                                                      # and the next cycle reads and sends
        self.assertEqual(reporter.report()["gateway_receipts"]["rows"]["sent"], 1)

    def test_the_counts_survive_a_restart(self):
        reporter = self.reporter(count=2)
        reporter.cycle()
        self.rig.close()
        object_ = self.rig.open(transport=Net()).report()["gateway_receipts"]
        self.assertEqual((object_["last_seq"], object_["rows"]["sent"], object_["stored"]), (2, 2, 2))


class Command(_Status):
    """`reporter status` (not running): built from the state file and the configuration."""

    def test_the_command_reports_configured_and_the_note(self):
        reporter = self.reporter(count=1)
        reporter.cycle()
        self.rig.close()
        code, out, err = cli(["status", "--config", self.rig.ws.config, "--json"], permissions=lambda p: None)
        self.assertEqual((code, err), (0, ""))
        object_ = json.loads(out)["gateway_receipts"]
        self.assertEqual((object_["configured"], object_["last_seq"], object_["cached_input"]), (True, 1, NOTE))
        code, text, _err = cli(["status", "--config", self.rig.ws.config], permissions=lambda p: None)
        self.assertIn("gateway receipts: last seq 1, pending 0, delivered 1, failed 0. " + NOTE, text)

    def test_the_command_without_the_block_says_not_configured(self):
        path = self.rig.ws.write_config("http://127.0.0.1:9", self.rig.agents_cfg)
        code, out, err = cli(["status", "--config", path, "--json"], permissions=lambda p: None)
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["gateway_receipts"]["configured"])
        code, text, _err = cli(["status", "--config", path], permissions=lambda p: None)
        self.assertNotIn("gateway receipts", text)

    def test_a_running_reporters_snapshot_carries_the_object(self):
        reporter = self.reporter(count=1)
        reporter.cycle()
        snap = status.read_snapshot(status.snapshot_path(self.rig.state.path))
        self.assertEqual(snap["status_version"], 3)
        self.assertEqual(snap["gateway_receipts"]["rows"]["sent"], 1)
        self.assertEqual(snap["gateway_receipts"]["cached_input"], NOTE)


if __name__ == "__main__":
    unittest.main()
