"""Card L6a-1, step 2: a stop at every hand-off of the receipt custody chain loses nothing and repeats nothing
(DESIGN-6A 8.3, "what each crash leaves").

The five hand-offs, each followed by a restart (the reporter is simply dropped where the stop lands):
  1. after the page is read, before stage 1 commits   -> nothing stored; the same page is read again
  2. after stage 1 (capture + cursor)                 -> evidence kept; the next cycle releases it
  3. during stage 2                                   -> rolled back whole (named below); released again, same bytes
  4. after stage 2, before the send                   -> a pending delivery, sent from its stored bytes
  5. after the send, before the answer is applied     -> the same bytes go again and the replay is a 200
Also: every resend is byte for byte the first, even after a restart; a lost 200 is answered by a replay 200.
The stop is a BaseException, so nothing in the reporter can swallow it, as a real crash cannot be caught.
Synthetic data only: a fake Leasewise transport and generated ids.
"""

import json
import os
import unittest
from unittest import mock

from quota_balancer.reporter.state import State

from tests.l6a1_env import GATEWAY_ID, OTHER_GATEWAY_ID, add_binding, lease, receipt, write_json
from tests.reporter_env import Crash
from tests.test_card7l6a1_delivery import Net, _Rig, error


def stop_at(stage):
    def fault(name):
        if name == stage:
            raise Crash()
    return fault


class Handoffs(_Rig):

    def counts(self):
        state = self.rig.state
        return {"captures": state.one("SELECT COUNT(*) FROM receipt_capture")[0],
                "deliveries": state.one("SELECT COUNT(*) FROM receipt_delivery")[0],
                "cursor": state.current_cursor(1)[1],
                "states": [row[0] for row in state.all("SELECT state FROM receipt_state ORDER BY capture_id")]}

    def stop(self, stage, net=None, count=1):
        """Start a reporter with `count` bound receipts, run one cycle that stops at `stage`, and drop it."""
        reporter = self.start(net or Net(), count=count)
        self.rig.close()
        reporter = self.rig.open(transport=self.net, fault=stop_at(stage))
        with self.assertRaises(Crash):
            reporter.cycle()
        self.rig.close()

    def restart(self):
        return self.rig.open(transport=self.net)

    def finish(self, count=1):
        """After the restart: cycle until nothing is left, then check the one-of-everything outcome."""
        reporter = self.restart()
        for _ in range(3):
            reporter.cycle()
        self.assertEqual(self.counts(), {"captures": count, "deliveries": count, "cursor": count,
                                         "states": ["sent"] * count})
        return reporter

    def test_1_a_stop_after_the_read_and_before_stage_1_stores_nothing_and_the_page_is_read_again(self):
        self.stop("read")
        self.rig.open(transport=self.net)
        self.assertEqual(self.counts(), {"captures": 0, "deliveries": 0, "cursor": 0, "states": []})
        self.assertEqual(self.net.calls, [])
        self.rig.close()
        self.finish()
        self.assertEqual(len(self.net.settles()), 1)

    def test_2_a_stop_after_stage_1_keeps_the_evidence_and_the_cursor_and_releases_it_next_time(self):
        self.stop("captured")
        self.rig.open(transport=self.net)
        self.assertEqual(self.counts(), {"captures": 1, "deliveries": 0, "cursor": 1, "states": ["captured"]})
        self.assertEqual(self.net.calls, [])
        self.rig.close()
        self.finish()
        self.assertEqual(len(self.net.settles()), 1)

    def test_3_a_stop_during_stage_2_is_the_fault_between_the_delivery_insert_and_the_state_update(self):
        """The release is ONE transaction: a stop between the delivery insert and the state update leaves the
        evidence and no body, and the next release builds the very same body."""
        reporter = self.start(Net())
        self.rig.close()
        reporter = self.rig.open(transport=self.net)
        with mock.patch.object(State, "_mark_pending", side_effect=Crash()):
            with self.assertRaises(Crash):
                reporter.cycle()
        self.rig.close()
        self.rig.open(transport=self.net)
        self.assertEqual(self.counts(), {"captures": 1, "deliveries": 0, "cursor": 1, "states": ["captured"]})
        self.assertEqual(self.net.calls, [])
        self.rig.close()
        self.finish()
        self.assertEqual(len(self.net.settles()), 1)

    def test_4_a_stop_after_stage_2_and_before_the_send_sends_the_stored_bytes(self):
        self.stop("released")
        self.rig.open(transport=self.net)
        self.assertEqual(self.counts(), {"captures": 1, "deliveries": 1, "cursor": 1, "states": ["pending"]})
        stored = self.rig.state.one("SELECT body FROM receipt_delivery")[0]
        self.assertEqual(self.net.calls, [])
        self.rig.close()
        self.finish()
        self.assertEqual([c[2] for c in self.net.settles()], [stored.encode("utf-8")])

    def test_5_a_stop_after_the_send_and_before_the_answer_is_applied_resends_the_same_bytes_and_a_replay_is_200(self):
        self.stop("answered")
        self.assertEqual(len(self.net.settles()), 1)                     # it went out, its answer was never applied
        self.rig.open(transport=self.net)
        self.assertEqual(self.counts()["states"], ["pending"])
        self.rig.close()
        self.finish()
        first, second = self.net.settles()
        self.assertEqual((first[0], first[1], first[2]), (second[0], second[1], second[2]))   # path, key, bytes

    def test_a_stop_after_the_answer_is_applied_sends_nothing_again(self):
        self.stop("applied")
        self.assertEqual(len(self.net.settles()), 1)
        self.finish()
        self.assertEqual(len(self.net.settles()), 1)

    def several(self, stage):
        self.stop(stage, count=3)
        self.finish(count=3)
        self.assertEqual(len({c[1] for c in self.net.settles()}), 3)    # three receipts, three events, never a fourth

    def test_with_three_receipts_a_stop_after_the_read_loses_none(self):
        self.several("read")

    def test_with_three_receipts_a_stop_after_stage_1_loses_none(self):
        self.several("captured")

    def test_with_three_receipts_a_stop_after_stage_2_loses_none(self):
        self.several("released")

    def test_with_three_receipts_a_stop_after_the_first_send_loses_none_and_repeats_only_that_one(self):
        self.several("answered")

    def test_with_three_receipts_a_stop_after_the_first_answer_loses_none(self):
        self.several("applied")

    # --- byte-identical resends -------------------------------------------------------------------

    def test_a_resend_after_a_retry_and_a_restart_is_byte_for_byte_the_first(self):
        reporter = self.start(Net(receipt=[(503, {}), (503, {}), (200, {"state": "settled", "released_micro_eur": 1,
                                                                        "retained_micro_eur": 2})]))
        reporter.cycle()
        self.rig.close()
        self.rig.clock.advance(3600)
        reporter = self.rig.open(transport=self.net)
        reporter.cycle()
        self.rig.close()
        self.rig.clock.advance(3600)
        reporter = self.rig.open(transport=self.net)
        reporter.cycle()
        calls = self.net.settles()
        self.assertEqual(len(calls), 3)
        self.assertEqual(len({(c[0], c[1], c[2]) for c in calls}), 1)           # one path, one key, one set of bytes
        self.assertEqual(self.rig.state.one("SELECT state FROM receipt_state")[0], "sent")
        self.assertEqual(calls[0][2], self.rig.state.one("SELECT body FROM receipt_delivery")[0].encode("utf-8"))

    def test_a_lost_200_resends_the_same_bytes_and_the_replay_answer_closes_the_row(self):
        reporter = self.start(Net(receipt=[(503, {}), (200, {"state": "settled", "released_micro_eur": 1,
                                                              "retained_micro_eur": 2})]))
        reporter.cycle()                                                 # the answer never arrives (here: a 503)
        self.assertEqual(self.rig.state.one("SELECT state, tries FROM receipt_state"), ("pending", 1))
        self.rig.clock.advance(3600)
        reporter.cycle()
        first, second = self.net.settles()
        self.assertEqual(first[2], second[2])
        self.assertEqual(self.rig.state.one("SELECT state FROM receipt_state")[0], "sent")

    def test_a_refused_row_is_never_rebuilt_by_a_restart(self):
        reporter = self.start(Net(receipt=[error("RECEIPT_CONFLICT", 409)]))
        reporter.cycle()
        before = self.rig.state.all("SELECT * FROM receipt_delivery")
        self.rig.close()
        self.rig.open(transport=self.net).cycle()
        self.assertEqual(self.rig.state.all("SELECT * FROM receipt_delivery"), before)
        self.assertEqual(len(self.net.settles()), 1)
        self.assertEqual(self.rig.state.one("SELECT state FROM receipt_state")[0], "failed_permanent")


class Mapping(_Rig):
    """Requirement 6 through the whole reporter: a capture keeps the gateway its source belonged to."""

    def test_a_restart_with_the_name_mapped_to_another_gateway_never_releases_the_old_capture(self):
        one = lease()
        reporter = self.start(Net(), rows=[receipt(one, 1)])
        reporter.cycle()                                                    # no binding yet: captured, quarantined
        self.assertEqual(self.rig.state.all("SELECT state, reason FROM receipt_state"),
                         [("quarantined", "unknown_reference")])
        self.rig.close()
        with open(self.rig.ws.snapshot, "r", encoding="utf-8") as handle:
            snap = json.load(handle)
        snap["gateways"][0]["id"] = OTHER_GATEWAY_ID                         # the same name, another gateway
        write_json(self.rig.ws.snapshot, snap)
        add_binding(os.path.join(self.rig.ws.dir, "admission.db"), {"lease_id": one, "gateway_id": OTHER_GATEWAY_ID})
        reporter = self.rig.open(transport=self.net)
        reporter.cycle()
        rows = self.rig.state.all("SELECT c.source_id, c.gateway_id, s.state, s.reason FROM receipt_capture c"
                                  " JOIN receipt_state s ON s.capture_id=c.id ORDER BY c.id")
        self.assertEqual(rows[0], (1, GATEWAY_ID, "quarantined", "wrong_gateway"))     # the old one is never released
        self.assertEqual(self.rig.state.all("SELECT c.source_id FROM receipt_delivery d JOIN receipt_capture c"
                                            " ON c.id=d.capture_id"), [(2,)])           # only the new source's own capture
        self.assertEqual(len(self.net.settles()), 1)


if __name__ == "__main__":
    unittest.main()
