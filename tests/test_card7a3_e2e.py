"""Card 7a-3: end to end. Hand-made journals go through the reporter to a real,
in-process Leasewise; the stored leases and receipts are compared with the
journal facts.

Rows: e2e_facts, e2e_second_pass, e2e_outage, e2e_restarts, e2e_two_agents.
The server shares the test's simulated clock, so an outage of hours costs no
time. Synthetic data only; loopback only.
"""

import os
import shutil
import tempfile
import unittest

from quota_balancer.reporter import mapper
from tests.journal_fixture import USAGE, Journal
from tests.reporter_env import (Crash, Rig, agent_table, build_store)

_TEMPLATE = {}


def setUpModule():
    base = os.environ.get("AGENTTALK_SCRATCH")
    if base:
        os.makedirs(base, exist_ok=True)
    folder = tempfile.mkdtemp(dir=base, prefix="quota-7a3-tpl-")
    _TEMPLATE["dir"] = folder
    _TEMPLATE["db"] = os.path.join(folder, "template.db")
    build_store(_TEMPLATE["db"], folder)


def tearDownModule():
    shutil.rmtree(_TEMPLATE["dir"], ignore_errors=True)


def make_rig(**kw):
    return Rig(template=_TEMPLATE["db"], **kw)


NOTHING = (None, None, None)
FAILED_USAGE = None                       # the CLI gave no usage figures


def facts(journal, now):
    """One agent's journal: six messages, each a different ending. Returns
    {message_id: expectations}."""
    s = journal.add_stream("g1", now - 900_000)
    t = now - 800_000

    def at(n):
        return t + n * 10_000

    # m1: one launched run, success, completed
    s.dispatch("m1", "m1-a", at(1), at(1) + 1000, usage=USAGE,
               message_at=at(1) - 500)
    s.disposed("m1", "completed", at(2), message_at=at(1) - 500)
    # m2: a failed run with no usage figures, then a good one; delivery failed
    s.dispatch("m2", "m2-a", at(3), at(3) + 1000, outcome="failed",
               usage=FAILED_USAGE, message_at=at(3) - 500)
    s.dispatch("m2", "m2-b", at(4), at(4) + 1000, usage=USAGE,
               message_at=at(3) - 500)
    s.disposed("m2", "delivery_failed", at(5), message_at=at(3) - 500)
    # m3: the only run never launched; dead lettered
    s.dispatch("m3", "m3-a", at(6), at(6) + 1000, launched=False,
               outcome="not_launched", usage=None, message_at=at(6) - 500)
    s.disposed("m3", "dead_lettered", at(7), message_at=at(6) - 500)
    # m4: ended with no known result
    s.dispatch("m4", "m4-a", at(8), at(8) + 1000, usage=USAGE,
               message_at=at(8) - 500)
    s.disposed("m4", "outcome_unknown", at(9), message_at=at(8) - 500)
    # m5: still open (no disposition yet)
    s.dispatch("m5", "m5-a", at(10), at(10) + 1000, usage=USAGE,
               message_at=at(10) - 500)
    # m6: one run, completed, but a numbered event was lost just before
    s.lose()
    s.dispatch("m6", "m6-a", at(11), at(11) + 1000, usage=USAGE,
               message_at=at(11) - 500)
    s.disposed("m6", "completed", at(12), message_at=at(11) - 500)
    return s


# What the server must hold for the journal above, written out by hand:
# message -> (lease state, non-final receipts [(outcome, tokens)], final)
# tokens are (input, cached, output) or None for unknown; a final is
# (outcome, calls, tokens) or None when no final may exist.
GOOD = (170, 50, 10)
EXPECTED = {
    "m1": ("settled", [("completed", GOOD)], ("completed", 1, GOOD)),
    "m2": ("settled", [("failed", None), ("completed", GOOD)],
           ("failed", 2, None)),
    "m3": ("settled", [], ("failed", 0, (0, 0, 0))),
    "m4": ("active", [("completed", GOOD)], None),
    "m5": ("active", [("completed", GOOD)], None),
    "m6": ("settled", [("completed", GOOD)], ("completed", 1, None)),
}
DISPATCHES = {"m1": ["m1-a"], "m2": ["m2-a", "m2-b"], "m3": [],
              "m4": ["m4-a"], "m5": ["m5-a"], "m6": ["m6-a"]}


def expected_receipts(seat_id, messages=EXPECTED):
    """event_id -> (final, outcome, calls, input, cached, output)."""
    out = {}
    for mid, (_state, reports, final) in messages.items():
        turn = mapper.turn_uuid(seat_id, mid)
        for turn_id, (outcome, tokens) in zip(DISPATCHES[mid], reports):
            event = mapper.dispatch_event_id(turn, turn_id)
            out[event] = (0, outcome, 1) + (tokens or (None, None, None))
        if final:
            outcome, calls, tokens = final
            out[mapper.final_event_id(turn)] = (
                (1, outcome, calls) + (tokens or (None, None, None)))
    return out


def server_receipts(rig):
    return {row[0]: row[1:] for row in rig.lw.rows(
        "SELECT event_id, final, outcome, calls, input_tokens,"
        " cached_input_tokens, output_tokens FROM receipts")}


def server_leases(rig):
    return {row[0]: row[1:] for row in rig.lw.rows(
        "SELECT turn_id, state, enforcement, request_id, money_hold "
        "FROM leases")}


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.rig = make_rig()
        self.addCleanup(self.rig.cleanup)
        self.stream = facts(self.rig.journal, self.rig.clock.ms)
        self.seat = None

    def run_all(self):
        self.rig.open()
        self.seat = self.rig.reporter.agents["alpha"].seat_id
        self.rig.drain_all()

    def test_the_stored_leases_and_receipts_match_the_journal_facts(self):
        self.run_all()
        leases = server_leases(self.rig)
        self.assertEqual(len(leases), 6)                   # one per message
        for mid, (state, _r, _f) in EXPECTED.items():
            turn = mapper.turn_uuid(self.seat, mid)
            self.assertIn(turn, leases, mid)
            lease_state, enforcement, request, hold = leases[turn]
            self.assertEqual(lease_state, state, mid)
            self.assertEqual(enforcement, "shadow", mid)   # observe-only
            self.assertIsNone(hold, mid)                   # no money held
            self.assertEqual(request, mapper.request_id(turn), mid)
        self.assertEqual(server_receipts(self.rig),
                         expected_receipts(self.seat))

    def test_nothing_is_stored_for_what_must_not_be_sent(self):
        self.run_all()
        receipts = server_receipts(self.rig)
        # no final for the open message and for the unknown outcome
        for mid in ("m4", "m5"):
            turn = mapper.turn_uuid(self.seat, mid)
            self.assertNotIn(mapper.final_event_id(turn), receipts)
        # a not-launched run is no call and no receipt
        turn = mapper.turn_uuid(self.seat, "m3")
        self.assertNotIn(mapper.dispatch_event_id(turn, "m3-a"), receipts)
        # observe-only: no money, no sequence number, no gateway
        self.assertEqual(self.rig.lw.rows(
            "SELECT COUNT(*) FROM receipts WHERE actual_micro_eur IS NOT NULL"
            " OR seq IS NOT NULL OR gateway_id IS NOT NULL"), [(0,)])
        self.assertEqual(self.rig.lw.rows("SELECT COUNT(*) FROM holds"),
                         [(0,)])
        # exactly one final per settled lease
        self.assertEqual(self.rig.lw.rows(
            "SELECT COUNT(*) FROM receipts WHERE final=1"), [(4,)])

    def test_unknown_tokens_are_null_never_zero(self):
        self.run_all()
        receipts = server_receipts(self.rig)
        m2 = mapper.final_event_id(mapper.turn_uuid(self.seat, "m2"))
        m6 = mapper.final_event_id(mapper.turn_uuid(self.seat, "m6"))
        self.assertEqual(receipts[m2][3:], (None, None, None))
        self.assertEqual(receipts[m6][3:], (None, None, None))
        self.assertEqual(receipts[m6][2], 1)             # calls still counted
        first = mapper.dispatch_event_id(
            mapper.turn_uuid(self.seat, "m2"), "m2-a")
        self.assertEqual(receipts[first][3:], (None, None, None))

    def test_a_second_pass_and_a_rebuilt_reporter_change_nothing(self):
        self.run_all()
        before = (server_receipts(self.rig), server_leases(self.rig))
        calls = len(self.rig.net.calls)
        result = self.rig.reporter.cycle()
        self.assertEqual((result.sent, result.new_events), (0, 0))
        self.rig.restart()
        result = self.rig.reporter.cycle()
        self.assertEqual((result.sent, result.new_events), (0, 0))
        self.assertEqual(len(self.rig.net.calls), calls)
        self.assertEqual((server_receipts(self.rig), server_leases(self.rig)),
                         before)

    def test_a_late_event_for_a_finished_message_is_counted_not_sent(self):
        self.run_all()
        calls = len(self.rig.net.calls)
        self.stream.dispatch("m1", "m1-late", self.rig.clock.ms - 10_000,
                             message_at=self.rig.clock.ms - 20_000)
        self.rig.drain_all()
        self.assertEqual(len(self.rig.net.calls), calls)
        alpha = self.rig.reporter.report()["agents"]["alpha"]
        self.assertEqual(alpha["late_observations"], 2)

    def test_the_status_agrees_with_the_server(self):
        self.run_all()
        alpha = self.rig.reporter.report()["agents"]["alpha"]
        self.assertEqual(alpha["finals"], {"pending": 0, "sent": 4,
                                           "failed": 0})
        self.assertEqual(alpha["reports"]["sent"], 6)
        self.assertEqual(alpha["acquisitions"]["acquired"], 6)
        self.assertEqual(alpha["messages"]["open"], 1)
        self.assertEqual(alpha["messages"]["outcome_unknown"], 1)
        # m6 lost a numbered event; the open m5 may still lose one (its
        # window has no end), so both are incomplete for that reason
        self.assertEqual(alpha["messages"]["incomplete_reasons"],
                         {"seq_gap": 2})
        self.assertEqual(len(self.rig.reporter.report()["operator_actions"]),
                         1)                          # the expire-sweep hint


class TwoAgents(unittest.TestCase):
    def test_each_agent_reports_on_its_own_seat(self):
        agents = {"alpha": agent_table("s6"), "beta": agent_table("s7")}
        rig = make_rig(agents=agents)
        self.addCleanup(rig.cleanup)
        facts(rig.journal, rig.clock.ms)
        beta = Journal(rig.ws.journal, "beta")
        s = beta.add_stream("b1", rig.clock.ms - 100_000)
        s.dispatch("b1", "b1-a", rig.clock.ms - 50_000,
                   message_at=rig.clock.ms - 51_000)
        s.disposed("b1", "completed", rig.clock.ms - 40_000,
                   message_at=rig.clock.ms - 51_000)
        rig.open()
        rig.drain_all()
        alpha_seat = rig.reporter.agents["alpha"].seat_id
        beta_seat = rig.reporter.agents["beta"].seat_id
        self.assertNotEqual(alpha_seat, beta_seat)
        self.assertEqual(rig.lw.rows(
            "SELECT COUNT(*) FROM leases WHERE seat_id=?", (beta_seat,)),
            [(1,)])
        self.assertEqual(rig.lw.rows(
            "SELECT COUNT(*) FROM leases WHERE seat_id=?", (alpha_seat,)),
            [(6,)])
        turn = mapper.turn_uuid(beta_seat, "b1")
        expected = expected_receipts(alpha_seat)
        expected[mapper.dispatch_event_id(turn, "b1-a")] = (
            0, "completed", 1, 170, 50, 10)
        expected[mapper.final_event_id(turn)] = (1, "completed", 1, 170, 50,
                                                 10)
        self.assertEqual(server_receipts(rig), expected)


# --- an outage of hours, then recovery ---------------------------------------------------

class Outage(unittest.TestCase):
    def test_three_simulated_hours_without_leasewise_lose_nothing(self):
        rig = make_rig()
        self.addCleanup(rig.cleanup)
        now = rig.clock.ms
        s = rig.journal.add_stream("g1", now - 100_000)
        # m1 starts while Leasewise is up: its lease is granted
        s.dispatch("m1", "m1-a", now - 50_000, ended=False,
                   message_at=now - 51_000)
        rig.open()
        rig.reporter.cycle()
        self.assertEqual(rig.lw.rows(
            "SELECT COUNT(*) FROM leases WHERE state='active'"), [(1,)])
        # ...then the network goes away. m1 finishes, m2 runs and finishes.
        rig.net.down = True
        s.dispatch("m1", "m1-a", rig.clock.ms, started=False,
                   message_at=now - 51_000)
        s.disposed("m1", "completed", rig.clock.ms + 1_000,
                   message_at=now - 51_000)
        s.dispatch("m2", "m2-a", rig.clock.ms + 2_000,
                   message_at=rig.clock.ms + 1_500)
        s.disposed("m2", "completed", rig.clock.ms + 3_000,
                   message_at=rig.clock.ms + 1_500)
        requests = 0
        for _ in range(36):                          # 36 x 5 min = 3 hours
            rig.clock.advance(300)
            before = len(rig.net.calls)
            result = rig.reporter.cycle()
            requests += len(rig.net.calls) - before
            self.assertLessEqual(len(rig.net.calls) - before, 1)
            self.assertIn(result.blocked, (None, "unreachable"))
            waiting = rig.state.one(
                "SELECT MAX(not_before_ms) FROM (SELECT not_before_ms FROM "
                "acquisition UNION ALL SELECT not_before_ms FROM report "
                "UNION ALL SELECT not_before_ms FROM final)")[0]
            self.assertLessEqual(waiting - rig.clock.ms, 900_000)  # 15 min
        self.assertGreater(requests, 3)
        self.assertLess(requests, 36)                # it backed off
        self.assertEqual(rig.lw.rows("SELECT COUNT(*) FROM receipts"), [(0,)])
        status = rig.reporter.report()["agents"]["alpha"]
        self.assertEqual(status["acquisitions"]["pending"], 1)      # m2
        self.assertEqual(status["finals"]["pending"], 2)
        self.assertEqual(status["finals"]["sent"], 0)
        # recovery
        rig.net.down = False
        rig.clock.advance(1000)
        rig.drain_all()
        seat = rig.reporter.agents["alpha"].seat_id
        self.assertEqual(rig.lw.rows(
            "SELECT COUNT(*) FROM leases WHERE state='settled'"), [(2,)])
        receipts = server_receipts(rig)
        t1, t2 = (mapper.turn_uuid(seat, m) for m in ("m1", "m2"))
        # m1's lease ran out during the outage: its report is dropped, its
        # final is accepted all the same
        self.assertNotIn(mapper.dispatch_event_id(t1, "m1-a"), receipts)
        self.assertEqual(receipts[mapper.final_event_id(t1)],
                         (1, "completed", 1, 170, 50, 10))
        # m2's stale estimate was rebuilt, then everything arrived
        self.assertEqual(receipts[mapper.dispatch_event_id(t2, "m2-a")],
                         (0, "completed", 1, 170, 50, 10))
        self.assertEqual(receipts[mapper.final_event_id(t2)],
                         (1, "completed", 1, 170, 50, 10))
        status = rig.reporter.report()["agents"]["alpha"]
        self.assertEqual(status["reports"]["dropped"], 1)
        self.assertEqual(status["finals"]["sent"], 2)
        self.assertGreaterEqual(rig.state.one(
            "SELECT rebuilds FROM acquisition WHERE message_id='m2'")[0], 1)
        # the original request bytes are kept for audit
        row = rig.state.one("SELECT body_original, body_current FROM "
                            "acquisition WHERE message_id='m2'")
        self.assertNotEqual(row[0], row[1])
        self.assertEqual(rig.lw.rows("SELECT COUNT(*) FROM leases"), [(2,)])

    def test_a_server_that_comes_back_after_each_kind_of_failure(self):
        """5xx and no answer for hours, then the same bytes get through."""
        rig = make_rig()
        self.addCleanup(rig.cleanup)
        facts(rig.journal, rig.clock.ms)
        rig.net.down = True
        rig.open()
        for _ in range(12):
            rig.clock.advance(1200)
            rig.reporter.cycle()
        rig.net.down = False
        rig.clock.advance(3600)
        rig.drain_all()
        seat = rig.reporter.agents["alpha"].seat_id
        # every acquisition was stale after hours; all were rebuilt and sent
        self.assertEqual(rig.lw.rows("SELECT COUNT(*) FROM leases"), [(6,)])
        receipts = server_receipts(rig)
        for event, row in expected_receipts(seat).items():
            if row[0] == 1:                       # the finals always arrive
                self.assertEqual(receipts[event], row, event)


# --- a restart at every step -----------------------------------------------------------------

def small(rig):
    """Three messages: a clean one, a failed pair, and an unknown outcome."""
    now = rig.clock.ms
    s = rig.journal.add_stream("g1", now - 900_000)
    s.dispatch("m1", "m1-a", now - 800_000, now - 799_000, usage=USAGE,
               message_at=now - 801_000)
    s.disposed("m1", "completed", now - 790_000, message_at=now - 801_000)
    s.dispatch("m2", "m2-a", now - 700_000, now - 699_000, outcome="failed",
               usage=None, message_at=now - 701_000)
    s.dispatch("m2", "m2-b", now - 690_000, now - 689_000, usage=USAGE,
               message_at=now - 701_000)
    s.disposed("m2", "delivery_failed", now - 680_000,
               message_at=now - 701_000)
    s.dispatch("m4", "m4-a", now - 600_000, now - 599_000, usage=USAGE,
               message_at=now - 601_000)
    s.disposed("m4", "outcome_unknown", now - 590_000,
               message_at=now - 601_000)


def final_bodies(rig):
    return rig.state.all("SELECT message_id, body FROM final "
                         "ORDER BY message_id")


class Restarts(unittest.TestCase):
    """Drop the reporter at every step; a new one on the same state must end
    with exactly what a reporter that never stopped produced."""

    @classmethod
    def setUpClass(cls):
        rig = make_rig()
        try:
            small(rig)
            rig.open()
            rig.drain_all()
            seat = rig.reporter.agents["alpha"].seat_id
            cls.baseline = (server_receipts(rig), set(
                (k, v[0], v[1], v[2]) for k, v in server_leases(rig).items()))
            cls.requests = len(rig.net.calls)
            cls.finals = final_bodies(rig)
            cls.seat = seat
            cls.sent_keys = sorted(c[1] for c in rig.net.calls)
        finally:
            rig.cleanup()

    def crashed_run(self, fault=None, before=None):
        rig = make_rig()
        self.addCleanup(rig.cleanup)
        small(rig)
        rig.net.before = before
        rig.open(fault=fault)
        with self.assertRaises(Crash):
            for _ in range(5):
                rig.reporter.cycle()
        rig.net.before = None
        rig.restart()                         # a new process, the same files
        rig.drain_all()
        return rig

    def assertSameEnd(self, rig):
        self.assertEqual(server_receipts(rig), self.baseline[0])
        got = set((k, v[0], v[1], v[2]) for k, v in server_leases(rig).items())
        self.assertEqual(got, self.baseline[1])
        # a final is never recomputed: the stored bytes are the ones sent
        self.assertEqual(final_bodies(rig), self.finals)

    def test_the_baseline_has_the_expected_size(self):
        self.assertEqual(self.requests, 3 + 4 + 2)      # leases, reports, finals
        self.assertEqual(len(self.baseline[0]), 6)

    def test_a_crash_before_each_request(self):
        for step in range(self.requests):
            with self.subTest(before_request=step):
                def die(index, step=step):
                    if index == step:
                        raise Crash()
                self.assertSameEnd(self.crashed_run(before=die))

    def test_a_crash_after_each_answer_before_it_is_recorded(self):
        for step in range(self.requests):
            with self.subTest(after_answer=step):
                seen = []

                def die(stage, step=step, seen=seen):
                    if stage == "answered":
                        seen.append(stage)
                        if len(seen) == step + 1:
                            raise Crash()
                rig = self.crashed_run(fault=die)
                self.assertSameEnd(rig)
                # the answered request was sent twice with the SAME key
                keys = [c[1] for c in rig.net.calls]
                self.assertEqual(len(keys) - len(set(keys)), 1)

    def test_a_crash_inside_a_round(self):
        for stage in ("data", "cursor", "final"):
            with self.subTest(stage=stage):
                def die(where, stage=stage):
                    if where == stage:
                        raise Crash()
                self.assertSameEnd(self.crashed_run(fault=die))

    def test_a_crash_on_every_cycle_still_gets_there(self):
        """A reporter that dies after each single request makes progress."""
        rig = make_rig()
        self.addCleanup(rig.cleanup)
        small(rig)
        for _ in range(self.requests + 3):
            state = {"n": 0}

            def die(index):
                state["n"] += 1
                if state["n"] == 2:
                    raise Crash()
            rig.net.before = die
            rig.restart()
            try:
                rig.reporter.cycle()
            except Crash:
                pass
        rig.net.before = None
        rig.restart()
        rig.drain_all()
        self.assertSameEnd(rig)


if __name__ == "__main__":
    unittest.main()
