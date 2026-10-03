"""Card L6a-2, step 2: one authority per paid message (DESIGN-6A 8.5).

For a message bound to a paid lease, the gateway's final receipt is the only thing that settles it. The journal pipeline
never acquires for it, never sends a non-final report and never sends a final; its observations are kept for diagnosis.

* an agent on `gateway_agents` never acquires or reports through the journal pipeline at all;
* a message bound in the admission store creates no journal acquisition, report or final (counter `suppressed`);
* journal rows queued BEFORE the binding appeared are set aside with a `superseded` record, never sent, never deleted;
* an acquisition already SENT is counted as `superseded_after_send`;
* a missing or busy admission store is never proof that a message is unbound.
Synthetic data only: a fake Leasewise transport, journal fixtures and generated ids.
"""

import os
import unittest
import uuid

from tests.l6a1_env import GATEWAY_ALIAS, add_binding, configure, lease, make_store
from tests.l6a2_env import admission_block
from tests.reporter_env import Rig, agent_table
from tests.test_card7a3 import one_message
from tests.test_card7l6a1_delivery import Net

JOURNAL = "alpha"           # the agent whose journal the fixture writes
OTHER = "gamma"             # a second declared agent
PARKED = (429, {"error": {"code": "ACCOUNT_PARKED", "message": "x"}})
ACQUIRED = (201, {"lease_id": str(uuid.uuid4()), "state": "active"})


class _Case(unittest.TestCase):
    listed = (OTHER,)

    def setUp(self):
        self.rig = Rig(server=False, agents={JOURNAL: agent_table(), OTHER: agent_table()})
        self.addCleanup(self.rig.cleanup)
        self.store = os.path.join(self.rig.ws.dir, "admission.db")

    def start(self, net, listed=None, make=True):
        if make:
            make_store(self.store, [])
        configure(self.rig, [], store=self.store,
                  extra_block=admission_block(self.store, agents=self.listed if listed is None else listed,
                                              gateway=GATEWAY_ALIAS))
        self.net = net
        return self.rig.open(transport=net)

    def bind(self, message_id="m1", agent=JOURNAL):
        add_binding(self.store, {"lease_id": lease(), "agent": agent, "message_id": message_id})

    def journal(self, message_id="m1"):
        one_message(self.rig.journal, self.rig.clock.ms, mid=message_id)

    def count(self, table):
        return self.rig.state.one("SELECT COUNT(*) FROM %s" % table)[0]

    def rows(self):
        return {name: self.count(name) for name in ("acquisition", "report", "final", "superseded")}

    def sent_paths(self):
        return [call[0] for call in self.net.calls]

    def observations(self):
        return {name: self.count(name) for name in ("event", "dispatch", "disposal", "message_status")}

    def suppression(self, reporter):
        return reporter.report()["gateway_receipts"]["journal_suppression"]


class Declared(_Case):
    """An agent on `gateway_agents`: nothing is acquired, reported or finalised for it, whatever the store says."""

    def test_a_declared_agent_never_acquires_reports_or_finalises_and_its_observations_are_kept(self):
        net = Net()
        reporter = self.start(net, listed=(JOURNAL,))
        self.journal()
        reporter.cycle()
        self.assertEqual(self.rows(), {"acquisition": 0, "report": 0, "final": 0, "superseded": 0})
        self.assertEqual(net.calls, [])                                                    # nothing was sent at all
        seen = self.observations()
        self.assertTrue(all(seen.values()), seen)                                          # the journal is still read and kept
        self.assertGreater(self.rig.state.counter(JOURNAL, "suppressed"), 0)
        self.assertEqual(self.suppression(reporter)["gateway_agents"], 1)
        self.assertGreater(self.suppression(reporter)["suppressed"], 0)

    def test_a_declared_agent_with_no_binding_is_still_not_acquired_for(self):
        net = Net()
        reporter = self.start(net, listed=(JOURNAL,))
        for number in range(3):
            self.journal("m%d" % number)
            reporter.cycle()
        self.assertEqual(self.rows()["acquisition"], 0)
        self.assertEqual(net.calls, [])

    def test_the_status_marks_the_agent_and_does_not_ask_for_a_sweep_on_its_account(self):
        reporter = self.start(Net(), listed=(JOURNAL,))
        self.journal()
        reporter.cycle()
        report = reporter.report()
        self.assertNotIn("gateway_agent", report["agents"][JOURNAL])                          # the shape is unchanged
        self.assertFalse([line for line in report["operator_actions"] if "expire-sweep" in line])

    def test_an_agent_that_is_not_declared_is_journalled_as_before(self):
        net = Net(acquire=[ACQUIRED])
        reporter = self.start(net, listed=(OTHER,))
        self.journal()
        reporter.cycle()
        self.assertEqual(self.rows()["acquisition"], 1)
        self.assertIn("/v1/leases", self.sent_paths())


class Bound(_Case):
    """An agent that is not declared, with a message bound in the admission store."""

    def test_binding_first_creates_no_journal_acquisition_report_or_final(self):
        net = Net(acquire=[ACQUIRED])
        reporter = self.start(net)
        self.bind("m1")
        reporter.cycle()                                                                   # the binding is copied
        self.journal("m1")
        reporter.cycle()
        self.assertEqual(self.rows(), {"acquisition": 0, "report": 0, "final": 0, "superseded": 0})
        self.assertEqual(net.calls, [])
        self.assertGreater(self.rig.state.counter(JOURNAL, "suppressed"), 0)
        self.assertTrue(all(self.observations().values()))                                # still observed

    def test_an_unbound_message_of_the_same_agent_is_journalled_normally(self):
        net = Net(acquire=[ACQUIRED])
        reporter = self.start(net)
        self.bind("m1")
        reporter.cycle()
        self.journal("m2")                                                                 # another message: not bound
        reporter.cycle()
        self.assertEqual(self.rows()["acquisition"], 1)
        self.assertEqual(self.rig.state.one("SELECT message_id FROM acquisition")[0], "m2")

    def test_journal_first_binding_second_the_queued_rows_are_set_aside_and_never_sent(self):
        net = Net(acquire=[PARKED])                                                        # the acquisition cannot go yet
        reporter = self.start(net)
        self.journal("m1")
        reporter.cycle()
        self.assertEqual((self.rows()["acquisition"], self.rows()["superseded"]), (1, 0))
        before = len(net.calls)
        self.assertGreaterEqual(before, 1)
        self.bind("m1")
        reporter.cycle()
        self.assertEqual(self.rows()["superseded"], 1)
        self.assertEqual(self.rig.state.one("SELECT after_send FROM superseded")[0], 0)
        self.assertEqual(self.rig.state.counter(JOURNAL, "superseded"), 1)
        self.assertEqual(self.rig.state.counter(JOURNAL, "superseded_after_send"), 0)
        self.assertEqual(len(net.calls), before)                                           # nothing more was sent
        self.rig.clock.advance(7200)
        reporter.cycle()
        reporter.cycle()
        self.assertEqual(len(net.calls), before)
        self.assertEqual(self.rows()["acquisition"], 1)                                    # set aside, not deleted
        self.assertEqual(reporter.report()["agents"][JOURNAL]["acquisitions"]["pending"], 0)
        self.assertEqual(self.suppression(reporter)["superseded"], 1)

    def test_an_acquisition_that_was_already_sent_is_counted_and_its_later_rows_are_never_sent(self):
        net = Net(acquire=[ACQUIRED], receipt=[(503, {})])                                 # the lease exists; settles wait
        reporter = self.start(net)
        self.journal("m1")
        reporter.cycle()
        self.assertEqual(self.rig.state.one("SELECT state FROM acquisition")[0], "acquired")
        queued = self.rows()
        self.assertGreaterEqual(queued["report"] + queued["final"], 1)
        settles = len(net.settles())
        self.bind("m1")
        reporter.cycle()
        self.assertEqual(self.rig.state.counter(JOURNAL, "superseded_after_send"), 1)
        self.assertEqual(self.rig.state.one("SELECT after_send FROM superseded")[0], 1)
        self.rig.clock.advance(7200)
        reporter.cycle()
        self.assertEqual(len(net.settles()), settles)                                      # the queued settles never go
        after = self.rows()
        self.assertEqual((after["report"], after["final"]), (queued["report"], queued["final"]))   # nothing deleted
        agent = reporter.report()["agents"][JOURNAL]
        self.assertEqual((agent["reports"]["pending"], agent["finals"]["pending"]), (0, 0))
        self.assertEqual(self.suppression(reporter)["superseded_after_send"], 1)

    def test_a_missing_store_is_never_proof_that_a_message_is_unbound(self):
        net = Net(acquire=[PARKED])
        reporter = self.start(net, make=False)
        os.remove(self.store)                                                              # no store file (yet)
        self.assertFalse(os.path.exists(self.store))
        self.journal("m1")
        reporter.cycle()
        self.assertEqual(self.rows()["acquisition"], 1)                                    # journalled as usual
        make_store(self.store, [])                                                         # the store appears, with the binding
        self.bind("m1")
        reporter.cycle()
        self.assertEqual(self.rows()["superseded"], 1)

    def test_a_busy_store_changes_nothing_and_the_binding_is_taken_when_it_can_be_read(self):
        import sqlite3
        net = Net(acquire=[PARKED])
        reporter = self.start(net)
        self.bind("m1")
        writer = sqlite3.connect(self.store, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN EXCLUSIVE")                                                  # a writer holds it (WAL: readers go on)
        self.journal("m1")
        reporter.cycle()
        writer.execute("ROLLBACK")
        reporter.cycle()
        self.assertEqual(self.rig.state.one("SELECT COUNT(*) FROM receipt_binding")[0], 1)
        self.assertEqual(self.rows()["acquisition"] + self.rows()["superseded"] >= 1, True)

    def test_the_binding_of_a_declared_agents_message_is_just_copied(self):
        reporter = self.start(Net(), listed=(JOURNAL,))
        self.bind("m1")
        reporter.cycle()
        self.assertEqual(self.rig.state.one("SELECT COUNT(*) FROM receipt_binding")[0], 1)
        self.assertEqual(self.rows()["superseded"], 0)


class Records(_Case):

    def test_a_superseded_record_is_never_changed_or_deleted(self):
        net = Net(acquire=[PARKED])
        reporter = self.start(net)
        self.journal("m1")
        reporter.cycle()
        self.bind("m1")
        reporter.cycle()
        import sqlite3
        for sql in ("UPDATE superseded SET after_send=1", "DELETE FROM superseded"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.rig.state.conn.execute(sql)

    def test_the_set_aside_happens_once_per_message_however_many_cycles_pass(self):
        net = Net(acquire=[PARKED])
        reporter = self.start(net)
        self.journal("m1")
        reporter.cycle()
        self.bind("m1")
        for _ in range(4):
            reporter.cycle()
        self.assertEqual(self.rig.state.counter(JOURNAL, "superseded"), 1)
        self.assertEqual(self.count("superseded"), 1)


class BodyCapStatus(_Case):

    def test_a_message_held_at_the_body_cap_is_counted_in_the_status_and_asks_for_attention(self):
        from quota_balancer.reporter import admission_store as store
        from quota_balancer.reporter import status
        writer = store.Writer(self.store)
        row = writer.begin_request("alpha", "m9", "req-9", "{}", 1)
        for number in range(store.MAX_BODIES - 1):
            writer.rebuild(row["id"], "body-%d" % number, number + 2)
        writer.close()
        reporter = self.start(Net(), make=False)
        reporter.cycle()
        report = reporter.report()
        self.assertEqual(report["gateway_receipts"]["admission_store"]["at_body_cap"], 1)
        self.assertIn(status.BODY_CAP_ACTION % 1, report["operator_actions"])
        shown = str(report) + status.render_text(report)
        for private in ("m9", "req-9", "body-1", self.store):
            self.assertNotIn(private, shown)

    def test_below_the_cap_nothing_is_shown(self):
        from quota_balancer.reporter import admission_store as store
        writer = store.Writer(self.store)
        row = writer.begin_request("alpha", "m9", "req-9", "{}", 1)
        for number in range(store.MAX_BODIES - 2):
            writer.rebuild(row["id"], "body-%d" % number, number + 2)
        writer.close()
        reporter = self.start(Net(), make=False)
        reporter.cycle()
        self.assertEqual(reporter.report()["gateway_receipts"]["admission_store"]["at_body_cap"], 0)


if __name__ == "__main__":
    unittest.main()
