"""Card L6a-2, fix round 1, second part (Codex's read of the first build):

* F4: nothing unsafe is ever SENT. The exclusion is made where the next request is chosen, for every acquisition,
  non-final and final row of a declared agent or of a message with a known binding, whatever the `superseded` markers say.
* F5: the reporter's state file is version 4; the upgrade from L6a-1's final version 3 (a file written by the real State
  of that card) is one transaction that deletes nothing, and an older reporter refuses version 4.
* F6: the count the status reads is an index lookup, never a scan of the bodies table.
* F7: the admission summary is refreshed whenever `[admission]` is configured, with or without receipt intake, and a
  failed read never shows as a fresh zero.
Synthetic data only.
"""

import os
import shutil
import sqlite3
import tempfile
import unittest
import uuid
from unittest import mock

from quota_balancer.reporter import admission_store as store
from quota_balancer.reporter import gwstate
from quota_balancer.reporter import state as state_mod
from quota_balancer.reporter import status
from quota_balancer.reporter.state import State, StateError

from tests.l6a1_env import GATEWAY_ALIAS, GATEWAY_ID
from tests.test_card7l6a2_suppress import ACQUIRED, JOURNAL, PARKED, _Case
from tests.test_card7l6a1_delivery import Net
from tests.l6a2_env import Rigged, admission_block
from tests.reporter_env import Rig, agent_table

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "reporter-state-v3-l6a1.sqlite")
NOW = 1_788_224_400_000


def queue(state, agent, message_id, bound=False):
    """One pending acquisition (and, when `bound`, a known binding), written straight into the state."""
    with state.txn():
        state.conn.execute("INSERT INTO acquisition(agent, message_id, turn_uuid, request_id, body_original, body_current)"
                           " VALUES(?,?,?,?, '{}', '{}')", (agent, message_id, str(uuid.uuid4()), str(uuid.uuid4())))
        if bound:
            lease = str(uuid.uuid4())
            state.conn.execute("INSERT INTO receipt_binding VALUES(?,?,?,?,?,?,?)",
                               (uuid.uuid4().hex + uuid.uuid4().hex, lease, GATEWAY_ID, agent, message_id, NOW, NOW))


class Exclusion(_Case):
    """F4. These rows are never offered, with or without a `superseded` marker."""

    def test_a_thousand_and_one_known_bound_queued_rows_are_none_of_them_offered(self):
        reporter = self.start(Net())
        for number in range(1001):
            queue(self.rig.state, JOURNAL, "bound-%d" % number, bound=True)
        queue(self.rig.state, JOURNAL, "free", bound=False)
        self.assertEqual(self.count("superseded"), 0)                       # no marker was ever written
        offered = self.rig.state.next_item(NOW, ())
        self.assertEqual(offered.message_id, "free")                        # the unbound one goes; the 1,001 do not
        reporter.cycle()
        sent = [json_body for _p, _k, json_body in self.net.calls]
        self.assertEqual(len(sent), 1)
        self.assertTrue(all(b"bound-" not in raw for raw in sent))

    def test_an_agent_declared_after_its_rows_were_queued_gets_nothing_sent(self):
        net = Net()
        reporter = self.start(net)
        self.journal("m1")
        reporter.cycle()                                                    # not declared yet: queued and sent
        queued = self.rows()["acquisition"]
        self.assertGreaterEqual(queued, 1)
        sent_before = len(net.calls)
        queue(self.rig.state, JOURNAL, "late-one")
        self.reporter_with(JOURNAL).cycle()
        self.assertEqual(len(net.calls), sent_before)                       # declared now: neither row is sent

    def reporter_with(self, *declared):
        self.rig.close()
        return self.start(self.net, listed=tuple(declared), make=False)

    def test_a_failure_between_copying_bindings_and_writing_markers_sends_nothing_unsafe(self):
        net = Net(acquire=[ACQUIRED], receipt=[(503, {})])                  # the lease exists; its settles are queued
        reporter = self.start(net)
        self.journal("m1")
        reporter.cycle()
        queued = self.rows()
        self.assertGreaterEqual(queued["report"] + queued["final"], 1)
        settles = len(net.settles())
        sent = len(net.calls)
        self.bind("m1")
        with mock.patch.object(type(self.rig.state), "supersede_bound", side_effect=RuntimeError("crash")):
            self.rig.clock.advance(7200)
            reporter.cycle()
        self.assertEqual(self.count("superseded"), 0)                       # the markers were never written
        self.assertEqual(self.count("receipt_binding"), 1)                  # the binding was copied
        self.assertEqual((len(net.settles()), len(net.calls)), (settles, sent))   # and nothing queued went out

    def test_a_queued_acquisition_of_a_bound_message_is_not_sent_even_if_the_marker_step_fails(self):
        net = Net(acquire=[PARKED])
        reporter = self.start(net)
        self.journal("m1")
        reporter.cycle()
        sent = len(net.calls)
        self.bind("m1")
        with mock.patch.object(type(self.rig.state), "supersede_bound", side_effect=RuntimeError("crash")):
            self.rig.clock.advance(7200)
            reporter.cycle()
        self.assertEqual(len(net.calls), sent)


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="state-v4-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "r.db")

    def raw(self, sql):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def tables(self):
        return [name for (name,) in self.raw("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

    def contents(self):
        return {name: self.raw("SELECT * FROM %s ORDER BY 1" % name) for name in self.tables()}


class Upgrade(_Tmp):
    """F5. The fixture is a file written by the real State of the final `card-l6a-1` head (merged as main 55b229f)."""

    def setUp(self):
        super().setUp()
        shutil.copy(FIXTURE, self.path)

    def test_the_fixture_is_the_real_version_3_layout_without_the_superseded_table(self):
        self.assertEqual(self.raw("PRAGMA user_version"), [(3,)])
        self.assertNotIn("superseded", self.tables())
        self.assertIn("receipt_binding", self.tables())
        self.assertEqual(len(self.raw("SELECT * FROM receipt_binding")), 1)

    def test_version_3_upgrades_to_4_in_one_step_and_nothing_is_deleted(self):
        before = self.contents()
        State(self.path).close()
        self.assertEqual(self.raw("PRAGMA user_version"), [(5,)])
        after = self.contents()
        self.assertEqual(after.pop("superseded"), [])
        for table in gwstate.TABLES:                                        # the money reader's tables (card R1-READER step 2) arrive empty
            self.assertEqual(after.pop(table), [], table)
        self.assertEqual(after, before)                                     # every old row is as it was
        triggers = {name for (name,) in self.raw("SELECT name FROM sqlite_master WHERE type='trigger'")}
        self.assertTrue({"superseded_immutable", "superseded_keep"} <= triggers)

    def test_an_upgrade_that_fails_leaves_the_file_at_version_3_whole(self):
        before = self.contents()
        with mock.patch.object(state_mod, "_SUPERSEDED_DDL", state_mod._SUPERSEDED_DDL + ("THIS IS NOT SQL",)):
            with self.assertRaises(StateError):
                State(self.path)
        self.assertEqual(self.raw("PRAGMA user_version"), [(3,)])
        self.assertEqual(self.contents(), before)

    def test_a_second_open_changes_nothing(self):
        State(self.path).close()
        schema = self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name")
        State(self.path).close()
        self.assertEqual(self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name"), schema)

    def test_an_older_reporter_refuses_version_4_and_deletes_nothing(self):
        State(self.path).close()
        before = self.contents()
        with mock.patch.object(state_mod, "SCHEMA_VERSION", 3):             # the reporter of card L6a-1
            with self.assertRaisesRegex(StateError, "version is not supported"):
                State(self.path)
        self.assertEqual(self.raw("PRAGMA user_version"), [(5,)])
        self.assertEqual(self.contents(), before)

    def test_versions_1_and_2_reach_4_with_the_superseded_table(self):
        for version in (1, 2):
            with self.subTest(version=version):
                path = os.path.join(self.dir, "old%d.db" % version)
                original = (state_mod.SCHEMA_VERSION, state_mod._DDL)
                state_mod.SCHEMA_VERSION = version
                state_mod._DDL = state_mod._DDL_V1 + (state_mod._LIMIT_DDL if version == 2 else ())
                try:
                    State(path).close()
                finally:
                    state_mod.SCHEMA_VERSION, state_mod._DDL = original
                State(path).close()
                conn = sqlite3.connect(path)
                try:
                    self.assertEqual(conn.execute("PRAGMA user_version").fetchone(), (5,))
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN"
                                                  " ('superseded','receipt_binding','receipt_state','limit_event')")
                                     .fetchone(), (4,))
                finally:
                    conn.close()


class Plan(unittest.TestCase):
    """F6. The count behind `at_body_cap` is looked up through an index."""

    def test_the_correlated_count_never_scans_the_bodies_table(self):
        directory = tempfile.mkdtemp(prefix="plan-")
        self.addCleanup(shutil.rmtree, directory, True)
        conn = sqlite3.connect(os.path.join(directory, "a.db"), isolation_level=None)
        self.addCleanup(conn.close)
        store.create(conn)
        sql = ("SELECT COUNT(*) FROM admission a WHERE a.state='requested' AND"
               " (SELECT COUNT(*) FROM admission_attempt t WHERE t.admission_id=a.id)>=?")
        plan = " | ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, (store.MAX_BODIES,)))
        self.assertIn("SEARCH t USING", plan)
        self.assertNotIn("SCAN t", plan)

    def test_the_summary_query_in_the_store_is_the_one_that_was_planned(self):
        import inspect
        self.assertIn("t.admission_id=a.id", inspect.getsource(store.read_summary))


class Summary(unittest.TestCase):
    """F7. `[admission]` alone (no receipt source) still keeps the store state and the body-cap count."""

    def setUp(self):
        self.rg = Rigged(agents={JOURNAL: agent_table()})
        self.addCleanup(self.rg.cleanup)
        self.rg.ws.write_config("http://127.0.0.1:9", self.rg.rig.agents_cfg,
                                extra=admission_block(self.rg.store, agents=(JOURNAL,), gateway=GATEWAY_ALIAS))
        self.reporter = self.rg.rig.open(transport=Net())

    def fill(self, bodies):
        writer = store.Writer(self.rg.store)
        row = writer.begin_request(JOURNAL, "m9", "req-9", "{}", 1)
        for number in range(bodies - 1):
            writer.rebuild(row["id"], "body-%d" % number, number + 2)
        writer.close()

    def summary(self):
        return self.reporter.report()["gateway_receipts"]["admission_store"]

    def test_with_only_an_admission_block_the_summary_is_still_refreshed(self):
        self.assertFalse(self.reporter.report()["gateway_receipts"]["configured"])
        self.fill(store.MAX_BODIES)
        self.reporter.cycle()
        shown = self.summary()
        self.assertEqual((shown["state"], shown["at_body_cap"]), ("ok", 1))
        report = self.reporter.report()
        self.assertIn(status.BODY_CAP_ACTION % 1, report["operator_actions"])
        self.assertIn("admission store: ok, 1 at the body cap", status.render_text(report))

    def test_an_unreadable_summary_shows_unknown_never_a_fresh_zero_and_recovers(self):
        self.fill(store.MAX_BODIES)
        self.reporter.cycle()
        self.assertEqual(self.summary()["at_body_cap"], 1)
        real = store.read_summary
        with mock.patch.object(store, "read_summary", return_value=(store.LATER, store.STORE_UNREADABLE)):
            self.reporter.cycle()
        shown = self.summary()
        self.assertEqual((shown["state"], shown["at_body_cap"]), ("unreadable", None))     # unknown, not 0
        self.assertIn(status.STORE_ACTIONS["unreadable"], self.reporter.report()["operator_actions"])
        self.assertIs(store.read_summary, real)
        self.reporter.cycle()                                                              # the store reads again
        shown = self.summary()
        self.assertEqual((shown["state"], shown["at_body_cap"]), ("ok", 1))

    def test_before_the_first_read_the_count_is_unknown(self):
        self.assertIsNone(self.summary()["at_body_cap"])
        self.assertIsNone(self.summary()["state"])

    def test_a_missing_store_is_unknown_not_zero(self):
        self.reporter.cycle()
        shown = self.summary()
        self.assertEqual((shown["state"], shown["at_body_cap"]), ("missing", None))


class OneCheckedPath(unittest.TestCase):
    """F7. With receipts AND admission configured, the one checked store path does the summary: one read a cycle."""

    def test_the_summary_is_read_once_per_cycle_when_both_are_configured(self):
        from tests.l6a1_env import configure, make_store
        rig = Rig(server=False, agents={JOURNAL: agent_table()})
        self.addCleanup(rig.cleanup)
        path = os.path.join(rig.ws.dir, "admission.db")
        make_store(path, [])
        configure(rig, [], store=path, extra_block=admission_block(path, agents=(JOURNAL,), gateway=GATEWAY_ALIAS))
        reporter = rig.open(transport=Net())
        with mock.patch.object(store, "read_summary", wraps=store.read_summary) as spy:
            reporter.cycle()
        self.assertEqual(spy.call_count, 1)


if __name__ == "__main__":
    unittest.main()
