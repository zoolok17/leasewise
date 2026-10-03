"""Card L6a-2, step 2: the whole hook on our side, against the REAL in-process Leasewise handler.

`admit` asks Leasewise for an advisory paid lease and saves the grant; `recall` gives the same answer back; `pending`
lists the message until `closed` acknowledges it; and a gateway receipt page for that lease is then captured, released
through the binding the reporter copied from the SAME store, and settles the lease (with the L6a-1 chain). Synthetic data
only: the in-process handler on a temporary store, a fixture ledger, no real gateway, provider or key.
"""

import os
import unittest

from quota_balancer.reporter import admission_store as store
from quota_balancer.reporter import admit_wire as wire

from tests.l6a1_env import block, ledger, receipt, write_json
from tests.l6a2_env import AGENT, Rigged, request_bytes


class EndToEnd(unittest.TestCase):

    def setUp(self):
        self.rg = Rigged(server=True)
        self.addCleanup(self.rg.cleanup)
        self.lw = self.rg.rig.lw
        self.fixture = os.path.join(self.rg.ws.dir, "receipts.json")
        write_json(self.fixture, ledger([]))
        self.rg.configure(extra="\n" + block(fixture=self.fixture, gateway="g1", store=self.rg.store))

    def op(self, raw):
        code, out, err = self.rg.run(raw, None)                    # the real HTTP client, to the real handler
        self.assertEqual((code, err), (0, ""))
        return out

    def test_admit_recall_pending_closed_and_then_a_receipt_settles_the_lease(self):
        # 1. admit: an advisory paid lease with null caps
        first = self.op(request_bytes(message_id="turn-1"))
        admitted = wire.parse_answer(first)
        self.assertEqual((admitted["status"], admitted["max_calls"], admitted["max_micro_eur"]), ("admitted", None, None))
        lease_id = admitted["reference"]
        rows = self.lw.rows("SELECT id, enforcement, money_hold, token_hash, state FROM leases")
        self.assertEqual(rows, [(lease_id, "metered_advisory", None, None, "active")])
        # 2. recall: the same answer, byte for byte, and no second lease
        self.assertEqual(self.op(wire.encode_op(wire.OP_RECALL, agent=AGENT, message_id="turn-1")), first)
        self.assertEqual(self.op(request_bytes(message_id="turn-1")), first)
        self.assertEqual(len(self.lw.rows("SELECT id FROM leases")), 1)
        # 3. pending lists it until closed acknowledges it
        listed = wire.parse_op_answer(wire.OP_PENDING, self.op(wire.encode_op(wire.OP_PENDING, agent=AGENT, limit=10)))
        self.assertEqual(listed["messages"], [{"message_id": "turn-1", "reference": lease_id}])
        self.assertEqual(self.op(wire.encode_op(wire.OP_CLOSED, agent=AGENT, message_id="turn-1")),
                         b'{"status":"ok","v":1}')
        self.assertEqual(self.op(wire.encode_op(wire.OP_PENDING, agent=AGENT, limit=10)),
                         b'{"messages":[],"status":"ok","v":1}')
        # 4. a gateway receipt page for that lease: captured, released through the copied binding, and settled
        write_json(self.fixture, ledger([receipt(lease_id, 1, calls=2, input_tokens=None, output_tokens=300,
                                                 actual_micro_eur=450)]))
        reporter = self.rg.rig.open()
        result = reporter.cycle()
        self.assertEqual((result.new_receipts, result.sent, result.blocked), (1, 1, None))
        self.assertEqual(self.lw.rows("SELECT state, outcome FROM leases WHERE id=?", (lease_id,)),
                         [("settled", "completed")])
        self.assertEqual(self.rg.rig.state.all("SELECT state FROM receipt_state"), [("sent",)])
        stored = self.lw.rows("SELECT input_tokens, output_tokens, actual_micro_eur, final FROM receipts")
        self.assertEqual(stored, [(None, 300, 450, 1)])                           # the null stays null (the L6a-0 rule)
        status = reporter.report()["gateway_receipts"]
        self.assertEqual((status["admission_store"]["state"], status["rows"]["sent"]), ("ok", 1))
        self.assertEqual(status["awaiting_receipt"]["count"], 0)

    def test_a_closed_message_keeps_its_binding_so_a_late_receipt_still_settles_it(self):
        first = self.op(request_bytes(message_id="turn-2"))
        lease_id = wire.parse_answer(first)["reference"]
        self.op(wire.encode_op(wire.OP_CLOSED, agent=AGENT, message_id="turn-2"))        # closed long before the receipt
        reporter = self.rg.rig.open()
        reporter.cycle()                                                                    # the binding is copied: no receipt yet
        self.assertEqual(reporter.report()["gateway_receipts"]["awaiting_receipt"]["count"], 1)
        write_json(self.fixture, ledger([receipt(lease_id, 1)]))
        self.assertEqual(reporter.cycle().sent, 1)
        self.assertEqual(self.lw.rows("SELECT state FROM leases WHERE id=?", (lease_id,)), [("settled",)])

    def test_the_reporter_and_the_admission_program_read_and_write_the_one_store(self):
        self.op(request_bytes(message_id="turn-3"))
        status, found = store.read_bindings(self.rg.store)
        self.assertEqual((status, [(b.agent, b.message_id) for b in found]), (store.OK, [(AGENT, "turn-3")]))

    def test_the_journal_pipeline_stays_out_of_a_declared_agents_messages(self):
        from tests.test_card7a3 import one_message
        self.op(request_bytes(message_id="turn-4"))
        reporter = self.rg.rig.open()
        one_message(self.rg.rig.journal, self.rg.rig.clock.ms, mid="turn-4")
        reporter.cycle()
        for table in ("acquisition", "report", "final"):
            self.assertEqual(self.rg.rig.state.one("SELECT COUNT(*) FROM %s" % table)[0], 0, table)
        self.assertEqual(len(self.lw.rows("SELECT id FROM leases")), 1)                    # only the one admit made


if __name__ == "__main__":
    unittest.main()
