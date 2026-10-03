"""Card L6a-1, step 6: the whole path, end to end, against the REAL Leasewise handler.

A lease is acquired on a test store for a seat that has a gateway (`g1`, on this machine); a labelled
synthetic receipt for it is put in the fixture file; ONE reporter cycle reads it, stores it, and sends the
final settle over loopback to the real in-process server (with the test's simulated clock). Then the
lease is settled with the mapped values. No network beyond localhost, no real gateway, no real provider.

What the real handler does today with an already-closed lease: a NEW final receipt for a settled lease is
answered RECEIPT_CONFLICT (and parks the account); the same event again is a 200 replay. LEASE_CLOSED is
what a lease CHECK answers, so the reporter's "LEASE_CLOSED is done" rule is covered by the answer-table
tests, not here.
"""

import os
import unittest
from unittest import mock

from quota_balancer import lease as lease_mod
from quota_balancer import store
from quota_balancer.reporter.run import UNREACHABLE

from tests.l6a1_env import GENERATION_B, add_binding, configure, ledger, receipt, write_json
from tests.reporter_env import Rig
from tests.test_cardl1a import _grant_request, _sized_snapshot

GATEWAY = "g1"                            # the gateway alias in the test store (machine desktop)


class _Case:
    """The few things `_sized_snapshot` wants of a test case: a folder and a database."""

    def __init__(self, rig):
        self.tmp, self.db = rig.lw.tmp, rig.lw.db

    def assertEqual(self, a, b, msg=None):
        assert a == b, msg


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()                                                  # a real in-process Leasewise
        self.addCleanup(self.rig.cleanup)
        ids = store.policy_ids(self.rig.lw.db)
        self.ids = {"seats": {r["seat"]: r["id"] for r in ids["seats"] if r["machine"] == "desktop"},
                    "models": {r["model"]: r["id"] for r in ids["models"]},
                    "gateways": {r["alias"]: r["id"] for r in ids["gateways"]}}
        self.fixture = os.path.join(self.rig.ws.dir, "receipts.json")
        self.rows = []

    def grant(self, seat="s1", gateway=GATEWAY, metered=False):
        """A lease from the real handler's own function, on the test store, at the test clock."""
        body = _grant_request(self.ids, seat, GATEWAY, self.rig.clock.ms)
        body["gateway_id"] = self.ids["gateways"][gateway] if gateway else None
        if not metered:
            return lease_mod.grant(self.rig.lw.db, body, self.rig.clock.ms)
        _sized_snapshot(_Case(self.rig))
        with mock.patch.object(lease_mod, "METERED_ENFORCEMENT_ACTIVATED", True):
            return lease_mod.grant(self.rig.lw.db, body, self.rig.clock.ms)

    def start(self, rows):
        self.store = os.path.join(self.rig.ws.dir, "admission.db")
        configure(self.rig, rows, fixture=self.fixture, gateway=GATEWAY, add_gateway=False, store=self.store)
        reporter = self.rig.open()
        return reporter

    def know(self, *lease_ids, gateway=GATEWAY):
        """What the admission command of card L6a-2 will do when it acquires a gateway-bound lease: it writes the
        binding (the lease, the gateway it expects) into the admission store before the turn starts. The reporter
        only reads it."""
        for lease_id in lease_ids:
            add_binding(self.store, {"lease_id": lease_id, "gateway_id": self.ids["gateways"][gateway]})

    def lease_row(self, lease_id):
        return self.rig.lw.rows("SELECT state, outcome FROM leases WHERE id=?", (lease_id,))[0]

    def receipts(self, lease_id):
        return self.rig.lw.rows(
            "SELECT gateway_id, seq, calls, input_tokens, cached_input_tokens, output_tokens, actual_micro_eur,"
            " outcome, final FROM receipts WHERE lease_id=?", (lease_id,))

    def test_a_receipt_settles_the_lease_with_the_mapped_values(self):
        made = self.grant()
        lease_id = made["lease_id"]
        self.assertEqual(self.lease_row(lease_id), ("active", None))
        reporter = self.start([receipt(lease_id, 1, calls=2, input_tokens=1200, output_tokens=300,
                                       actual_micro_eur=450)])
        self.know(lease_id)
        result = reporter.cycle()
        self.assertEqual((result.new_receipts, result.sent, result.blocked), (1, 1, None))
        self.assertEqual(self.lease_row(lease_id), ("settled", "completed"))
        self.assertEqual(self.receipts(lease_id), [(self.ids["gateways"][GATEWAY], 1, 2, 1200, 0, 300, 450,
                                                    "completed", 1)])
        self.assertEqual(self.rig.state.all("SELECT state, tries, last_error_code FROM receipt_state"),
                         [("sent", 0, None)])
        self.assertEqual(self.rig.net.calls[0][0], "/v1/leases/%s/settle" % lease_id)
        self.assertEqual(self.rig.state.current_cursor(1)[1], 1)

    def test_zero_spend_with_a_failed_outcome_settles_the_lease_too(self):
        lease_id = self.grant()["lease_id"]
        reporter = self.start([receipt(lease_id, 1, outcome="failed", calls=0, input_tokens=0, output_tokens=0,
                                       actual_micro_eur=0)])
        self.know(lease_id)
        result = reporter.cycle()
        self.assertEqual((result.sent, result.blocked), (1, None))
        self.assertEqual(self.lease_row(lease_id), ("settled", "failed"))
        self.assertEqual(self.receipts(lease_id), [(self.ids["gateways"][GATEWAY], 1, 0, 0, 0, 0, 0, "failed", 1)])

    def test_every_outcome_word_settles(self):
        words = ("completed", "cancelled", "failed", "provider_limit")
        leases = [self.grant()["lease_id"] for _ in words]
        reporter = self.start([receipt(l, n + 1, outcome=word) for n, (l, word) in enumerate(zip(leases, words))])
        self.know(*leases)
        result = reporter.cycle()
        self.assertEqual((result.new_receipts, result.sent, result.blocked), (4, 4, None))
        for lease_id, word in zip(leases, words):
            self.assertEqual(self.lease_row(lease_id), ("settled", word), word)

    def test_a_second_cycle_sends_nothing_new(self):
        lease_id = self.grant()["lease_id"]
        reporter = self.start([receipt(lease_id, 1)])
        self.know(lease_id)
        reporter.cycle()
        calls = len(self.rig.net.calls)
        before = self.rig.lw.rows("SELECT COUNT(*), MAX(received_at) FROM receipts")
        result = reporter.cycle()
        self.assertEqual((result.new_receipts, result.sent, len(self.rig.net.calls)), (0, 0, calls))
        self.assertEqual(self.rig.lw.rows("SELECT COUNT(*), MAX(received_at) FROM receipts"), before)

    def test_a_restart_in_the_middle_loses_and_repeats_nothing(self):
        lease_id = self.grant()["lease_id"]
        reporter = self.start([receipt(lease_id, 1)])
        self.know(lease_id)
        self.rig.net.down = True                                       # Leasewise unreachable
        result = reporter.cycle()
        self.assertEqual(result.blocked, UNREACHABLE)
        self.assertEqual(self.rig.state.all("SELECT state, tries FROM receipt_state"), [("pending", 1)])
        self.assertEqual(self.rig.state.current_cursor(1)[1], 1)    # captured, so never asked for again
        self.assertEqual(self.lease_row(lease_id)[0], "active")
        self.rig.close()                                               # restart
        self.rig.net.down = False
        reporter = self.rig.open()
        self.rig.clock.advance(3600)
        reporter.cycle()
        self.assertEqual(self.rig.state.all("SELECT state FROM receipt_state"), [("sent",)])
        self.assertEqual(self.lease_row(lease_id), ("settled", "completed"))
        self.assertEqual(len(self.receipts(lease_id)), 1)

    def test_a_receipt_for_a_lease_that_is_already_settled_is_a_conflict_for_that_row_only(self):
        lease_id = self.grant()["lease_id"]
        other = self.grant()["lease_id"]
        reporter = self.start([receipt(lease_id, 1)])
        self.know(lease_id, other)
        reporter.cycle()
        self.assertEqual(self.lease_row(lease_id)[0], "settled")
        # a NEW final receipt (another ledger generation) for the same lease, then one for another lease
        write_json(self.fixture, ledger([receipt(lease_id, 1), receipt(other, 2)], generation=GENERATION_B))
        result = reporter.cycle()
        self.assertEqual(result.new_receipts, 2)
        states = self.rig.state.all("SELECT c.seq, s.state, s.last_error_code FROM receipt_capture c"
                                    " JOIN receipt_state s ON s.capture_id=c.id ORDER BY c.id")
        self.assertEqual(states, [(1, "sent", None), (1, "failed_permanent", "RECEIPT_CONFLICT"), (2, "sent", None)])
        self.assertEqual(self.lease_row(other)[0], "settled")            # the next row still flowed
        self.assertEqual(self.rig.state.one("SELECT COUNT(*) FROM agent_state")[0], 0)
        # a conflicting FINAL receipt parks the account on the Leasewise side (the design's own rule)
        self.assertEqual(self.rig.lw.rows("SELECT reason, state FROM blocks"), [("receipt_conflict", "parked")])

    def test_a_receipt_naming_a_lease_without_this_gateway_is_a_conflict(self):
        lease_id = self.grant(seat="s7", gateway=None)["lease_id"]      # a lease with no gateway at all
        reporter = self.start([receipt(lease_id, 1)])
        self.know(lease_id)
        reporter.cycle()
        self.assertEqual(self.rig.state.all("SELECT state, last_error_code FROM receipt_state"),
                         [("failed_permanent", "RECEIPT_CONFLICT")])
        self.assertEqual(self.lease_row(lease_id)[0], "active")

    def test_a_receipt_with_no_binding_is_captured_quarantined_never_sent_and_sent_once_it_is_bound(self):
        lease_id = self.grant()["lease_id"]
        reporter = self.start([receipt(lease_id, 1)])                    # the admission store knows nothing of it yet
        result = reporter.cycle()
        self.assertEqual((result.new_receipts, result.sent), (1, 0))
        self.assertEqual(self.rig.net.calls, [])
        self.assertEqual(self.rig.state.all("SELECT state, reason FROM receipt_state"),
                         [("quarantined", "unknown_reference")])
        self.assertEqual(self.rig.state.one("SELECT COUNT(*) FROM receipt_delivery")[0], 0)
        self.assertEqual(self.rig.state.current_cursor(1)[1], 1)         # captured: its number is passed
        self.assertEqual(self.lease_row(lease_id)[0], "active")
        self.know(lease_id)                                              # the binding appears later
        self.assertEqual(reporter.cycle().sent, 1)
        self.assertEqual(self.lease_row(lease_id), ("settled", "completed"))
        self.assertEqual(self.rig.state.all("SELECT state, reason FROM receipt_state"), [("sent", None)])

    def test_a_receipt_for_a_binding_that_names_another_gateway_is_never_sent(self):
        lease_id = self.grant()["lease_id"]
        reporter = self.start([receipt(lease_id, 1)])
        add_binding(self.store, {"lease_id": lease_id, "gateway_id": "c4a2e9f0-3b7d-4c1e-8a55-0d9b2f6e7a12"})
        reporter.cycle()
        self.assertEqual(self.rig.net.calls, [])
        self.assertEqual(self.rig.state.all("SELECT state, reason FROM receipt_state"),
                         [("quarantined", "wrong_gateway")])
        self.assertEqual(self.lease_row(lease_id)[0], "active")

    def test_spend_over_the_hold_settles_and_leasewise_records_the_overrun(self):
        made = self.grant(seat="s3", metered=True)
        lease_id, hold = made["lease_id"], made["money_hold"]
        self.assertEqual(made["enforcement"], "metered")
        self.assertGreater(hold, 0)
        reporter = self.start([receipt(lease_id, 1, calls=1, actual_micro_eur=hold + 7)])
        self.know(lease_id)
        result = reporter.cycle()
        self.assertEqual((result.sent, result.blocked), (1, None))
        self.assertEqual(self.lease_row(lease_id), ("settled", "completed"))
        self.assertEqual(self.rig.lw.rows("SELECT unattributed_micro_eur, reason FROM adjustments"), [(7, "overrun")])
        self.assertEqual(self.rig.lw.rows("SELECT reason, state FROM blocks"), [("overrun", "parked")])

    def test_spend_under_the_hold_releases_the_rest(self):
        made = self.grant(seat="s3", metered=True)
        lease_id, hold = made["lease_id"], made["money_hold"]
        reporter = self.start([receipt(lease_id, 1, actual_micro_eur=min(450, hold))])
        self.know(lease_id)
        reporter.cycle()
        self.assertEqual(self.lease_row(lease_id), ("settled", "completed"))
        self.assertEqual(self.rig.lw.rows("SELECT COUNT(*) FROM adjustments"), [(0,)])
        self.assertEqual(self.rig.lw.rows("SELECT COUNT(*) FROM blocks"), [(0,)])

    def test_the_status_says_what_happened(self):
        lease_id = self.grant()["lease_id"]
        reporter = self.start([receipt(lease_id, 1)])
        self.know(lease_id)
        reporter.cycle()
        report = reporter.report()["gateway_receipts"]
        self.assertEqual((report["configured"], report["gateway"], report["last_seq"]), (True, GATEWAY, 1))
        self.assertEqual(report["rows"], {"pending": 0, "sent": 1, "failed_permanent": 0, "quarantined": 0})
        self.assertEqual(report["last_read_result"], "ok")


if __name__ == "__main__":
    unittest.main()
