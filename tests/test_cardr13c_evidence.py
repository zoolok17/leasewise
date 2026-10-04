"""Card R1-3c: the pure evidence rules (`quota_balancer/evidence.py`) and the transitions written at ingest (DESIGN-R1-3.md 3, 4, 9).

* **The rules are pure** (no database): a table of cases for the order of the checks (retired first, then identity before order, then
  order, then what an in-order report shows), the monotonic rules (`settled` on every report, `unattributed` on quiet reports only),
  the automatic month roll, and the reader bookkeeping.
* **Ingest writes what the rules return**, once, at ingest, never from history: a transition is written for the report that shows it, a
  state kind is latched while one is open, nothing is resolved or overwritten, and a report of a retired reader is counted and creates no
  transition on any path (the accepted-report epoch-mismatch path included).
* **The month roll** writes a `period_roll` adoption in the same transaction and keeps the same reader current.
* **`billing_eval._gateway_usable`** stays a second line of defence: a chain that starts at the adoption report is a whole chain.

Synthetic reports only. No operator command exists yet (card R1-3d), so a test that needs an adoption writes it the way R1-3d will.
"""

import contextlib
import sqlite3
import unittest

from quota_balancer import billing_eval, evidence, store
from quota_balancer.errors import QuotaError
from quota_balancer.evidence import (Adoption, Report, State, assess, content, STANDING_CONFLICT, STANDING_DUPLICATE,
                                     STANDING_IN_ORDER, STANDING_LATE, STANDING_OLD_CHAIN, STANDING_OLD_PERIOD, STANDING_RETIRED,
                                     STANDING_UNCHAINED)

from tests.test_card2b1 import _month_bounds_of, _now_ms
from tests.test_cardr13b_ingest import CAP, GEN, GEN2, INC, INC2, PRICE, _Store, report

INC3 = "f" * 32
GEN3 = "a" * 32
PRICE2 = "e" * 64
CAP2 = "d" * 64


def R(**over):
    base = dict(event_id="ev", incarnation=INC, epoch=1, generation=GEN, seq=2, period_start=1000, period_end=2000, settled=5000,
                reserved=10, uncertain=0, unattributed=7, receipts_seq=4, quiet=True, held_over_reservation=0, price_hash=PRICE,
                child_cap_hash=CAP, flag=False)
    base.update(over)
    return Report(**base)


def A(**over):
    base = dict(incarnation=INC, epoch=1, generation=GEN, period_start=1000, seq=1, settled=5000, unattributed=7,
                price_hash=PRICE, child_cap_hash=CAP)
    base.update(over)
    return Adoption(**base)


def S(**over):
    base = dict(reader="current", adoption=A(), generation=GEN, same_seq=(), max_seq=1, previous=5000, last_quiet=7,
                open_kinds=frozenset(), approved=True)
    base.update(over)
    return State(**base)


def kinds(result):
    return [kind for kind, _ in result.transitions]


class Purity(unittest.TestCase):

    def test_the_module_touches_no_database_clock_or_store(self):
        for name in ("sqlite3", "store", "time", "os"):
            self.assertNotIn(name, vars(evidence), name)

    def test_the_same_inputs_always_give_the_same_answer(self):
        self.assertEqual(assess(R(flag=True), S()), assess(R(flag=True), S()))


class OrderOfTheChecks(unittest.TestCase):

    def test_a_report_with_nothing_new_is_in_order_and_raises_nothing(self):
        result = assess(R(), S())
        self.assertEqual((result.standing, result.transitions, result.counter), (STANDING_IN_ORDER, (), None))
        self.assertTrue(result.remember_quiet)
        self.assertFalse(result.roll)

    def test_a_retired_reader_is_only_counted_whatever_else_the_report_shows(self):
        loud = R(flag=True, epoch=3, price_hash=PRICE2, settled=1)
        result = assess(loud, S(reader="retired", generation=GEN2, same_seq=(content(R(settled=9)),)))
        self.assertEqual((result.standing, result.transitions, result.counter), (STANDING_RETIRED, (), "retired_reader_reports"))
        self.assertFalse(result.create_reader or result.remember_quiet or result.roll)

    def test_identity_comes_before_order_a_late_number_that_collides_is_a_conflict(self):
        # A(seq 2) was stored, then seq 3, then B(seq 2) arrives: B is "late" by number but it collides with A.
        rival = R(seq=2, settled=5001)
        result = assess(rival, S(same_seq=(content(R(seq=2)),), max_seq=3))
        self.assertEqual(result.standing, STANDING_CONFLICT)
        self.assertEqual(result.transitions, ((evidence.KIND_SEQUENCE_CONFLICT, (INC, 1, 2)),))
        self.assertIsNone(result.counter)                                    # not counted as late

    def test_identity_comes_before_order_a_collision_that_arrives_before_the_next_number(self):
        # A(seq 2), then B(seq 2) arrives while 2 is the highest: a conflict. Then seq 3 arrives and is simply in order.
        rival = assess(R(seq=2, settled=5001), S(same_seq=(content(R(seq=2)),), max_seq=2))
        self.assertEqual(kinds(rival), [evidence.KIND_SEQUENCE_CONFLICT])
        after = assess(R(seq=3), S(max_seq=2))
        self.assertEqual((after.standing, after.transitions), (STANDING_IN_ORDER, ()))

    def test_the_same_number_and_the_same_content_is_a_duplicate_not_a_conflict(self):
        result = assess(R(event_id="other-id"), S(same_seq=(content(R()),), max_seq=2))
        self.assertEqual((result.standing, result.counter), (STANDING_DUPLICATE, "duplicate_reports"))
        self.assertEqual(result.transitions, ())

    def test_a_lower_number_in_its_own_epoch_is_late(self):
        result = assess(R(seq=2), S(max_seq=5))
        self.assertEqual((result.standing, result.counter, result.transitions), (STANDING_LATE, "late_reports", ()))

    def test_an_older_epoch_of_the_current_reader_is_old_chain_evidence(self):
        result = assess(R(epoch=1, generation=GEN3, flag=True), S(adoption=A(epoch=2), generation=GEN3))
        self.assertEqual((result.standing, result.counter, result.transitions), (STANDING_OLD_CHAIN, "old_chain_reports", ()))

    def test_an_older_month_is_ignored_and_counted_the_same_way(self):
        result = assess(R(period_start=500, period_end=1000, flag=True), S())
        self.assertEqual((result.standing, result.counter, result.transitions), (STANDING_OLD_PERIOD, "old_chain_reports", ()))
        self.assertFalse(result.roll)

    def test_a_generation_that_differs_from_the_recorded_one_is_an_identity_conflict_and_stops_the_chain_rules(self):
        result = assess(R(generation=GEN2, settled=1), S(previous=None))
        self.assertEqual(kinds(result), [evidence.KIND_EPOCH_GENERATION_MISMATCH])
        self.assertEqual(result.transitions[0][1], (INC, 1, 2))
        self.assertFalse(result.roll)

    def test_a_late_report_still_raises_the_identity_conflict_but_nothing_that_order_would_ignore(self):
        result = assess(R(seq=2, generation=GEN2, flag=True, price_hash=PRICE2), S(max_seq=5))
        self.assertEqual(kinds(result), [evidence.KIND_EPOCH_GENERATION_MISMATCH])
        self.assertEqual(result.standing, STANDING_LATE)


class Monotonic(unittest.TestCase):

    def test_settled_falling_is_figure_fell_on_every_report_quiet_or_not(self):
        for quiet in (True, False):
            with self.subTest(quiet=quiet):
                self.assertEqual(kinds(assess(R(settled=4999, quiet=quiet, unattributed=0), S(previous=5000, last_quiet=0))),
                                 [evidence.KIND_FIGURE_FELL])

    def test_settled_equal_or_rising_and_reserved_or_uncertain_falling_raise_nothing(self):
        self.assertEqual(kinds(assess(R(settled=5000), S(previous=5000))), [])
        self.assertEqual(kinds(assess(R(settled=6000, reserved=0, uncertain=0), S(previous=5000))), [])    # a reservation became spend

    def test_unattributed_falling_between_quiet_reports_is_figure_fell(self):
        self.assertEqual(kinds(assess(R(unattributed=6), S(last_quiet=7))), [evidence.KIND_FIGURE_FELL])

    def test_a_report_that_is_not_quiet_never_raises_or_resolves_an_unattributed_rule_and_is_not_remembered(self):
        for figure in (0, 6, 8, 5000):
            with self.subTest(unattributed=figure):
                result = assess(R(unattributed=figure, quiet=False), S(last_quiet=7))
                self.assertEqual(result.transitions, ())
                self.assertFalse(result.remember_quiet)

    def test_a_non_quiet_report_still_raises_every_other_rule(self):
        result = assess(R(quiet=False, settled=1, flag=True, price_hash=PRICE2, child_cap_hash=CAP2), S())
        self.assertEqual(set(kinds(result)), {evidence.KIND_FIGURE_FELL, evidence.KIND_REPORTED_BY_GATEWAY,
                                              evidence.KIND_PRICE_HASH_CHANGED, evidence.KIND_CHILD_CAP_HASH_CHANGED})

    def test_a_quiet_rise_above_the_adopted_report_is_unattributed_rose_once_while_it_is_open(self):
        self.assertEqual(kinds(assess(R(unattributed=8), S(last_quiet=7))), [evidence.KIND_UNATTRIBUTED_ROSE])
        later = assess(R(unattributed=9), S(last_quiet=8, open_kinds=frozenset({evidence.KIND_UNATTRIBUTED_ROSE})))
        self.assertEqual(later.transitions, ())                              # a second higher quiet report adds nothing

    def test_the_baseline_is_the_adopted_report_not_the_last_quiet_one(self):
        # last quiet figure 9 (it rose earlier and was resolved by an adoption of a report with 9): the adoption says 9 now.
        self.assertEqual(kinds(assess(R(unattributed=9), S(adoption=A(unattributed=9), last_quiet=9))), [])
        self.assertEqual(kinds(assess(R(unattributed=8), S(adoption=A(unattributed=7), last_quiet=7))),
                         [evidence.KIND_UNATTRIBUTED_ROSE])

    def test_a_fall_and_a_settled_fall_on_one_report_are_one_transition(self):
        self.assertEqual(kinds(assess(R(settled=1, unattributed=0), S())), [evidence.KIND_FIGURE_FELL])

    def test_an_unknown_figure_is_never_read_as_zero_and_proves_no_fall(self):
        for bad in (None, True, -1, 1.5, "7", 2 ** 63):
            with self.subTest(previous=bad):
                self.assertEqual(kinds(assess(R(), S(previous=bad, last_quiet=bad))), [])

    def test_a_new_month_is_not_a_fall_and_a_rise_is_not_compared_across_chains(self):
        result = assess(R(period_start=2000, period_end=3000, settled=1, unattributed=900, quiet=False), S(previous=None))
        self.assertEqual(result.transitions, ())                              # the first report of a month has nothing to fall from

    def test_each_state_change_raises_its_own_kind_and_is_latched_while_open(self):
        cases = ((dict(flag=True), evidence.KIND_REPORTED_BY_GATEWAY), (dict(price_hash=PRICE2), evidence.KIND_PRICE_HASH_CHANGED),
                 (dict(child_cap_hash=CAP2), evidence.KIND_CHILD_CAP_HASH_CHANGED))
        for over, kind in cases:
            with self.subTest(kind=kind):
                self.assertEqual(kinds(assess(R(**over), S())), [kind])
                self.assertEqual(kinds(assess(R(**over), S(open_kinds=frozenset({kind})))), [])
        newer = assess(R(epoch=2, generation=GEN3), S(generation=None))
        self.assertEqual(kinds(newer), [evidence.KIND_EPOCH_CHANGED])
        self.assertEqual(kinds(assess(R(epoch=2, generation=GEN3), S(generation=None, open_kinds=frozenset({"epoch_changed"})))), [])


class MonthRoll(unittest.TestCase):

    def new_month(self, **over):
        return R(period_start=2000, period_end=3000, **over)

    def test_the_first_quiet_report_of_a_new_month_rolls(self):
        result = assess(self.new_month(), S())
        self.assertEqual((result.standing, result.transitions, result.roll), (STANDING_IN_ORDER, (), True))

    def test_a_new_month_rolls_even_when_its_figures_are_lower_or_higher(self):
        self.assertTrue(assess(self.new_month(settled=1, unattributed=0), S(previous=None)).roll)
        self.assertTrue(assess(self.new_month(settled=90000, unattributed=80000), S()).roll)

    def test_a_fall_of_settled_in_a_new_month_is_caught_before_the_roll_and_blocks_it(self):
        # The store supplies the highest `settled` already seen in the new month (100) before this month's adoption exists.
        result = assess(self.new_month(settled=50), S(previous=100))
        self.assertEqual((kinds(result), result.roll), ([evidence.KIND_FIGURE_FELL], False))
        self.assertTrue(assess(self.new_month(settled=100), S(previous=100)).roll)       # equal is not a fall

    def test_each_condition_of_4_4_is_needed(self):
        state_over = {
            "not quiet": (dict(quiet=False), {}),
            "another epoch": (dict(epoch=2, generation=GEN3), dict(generation=None)),
            "another generation": (dict(generation=GEN2), {}),
            "another price fingerprint": (dict(price_hash=PRICE2), {}),
            "another child-cap fingerprint": (dict(child_cap_hash=CAP2), {}),
            "pair not approved": ({}, dict(approved=False)),
            "something is open": ({}, dict(open_kinds=frozenset({"figure_fell"}))),
            "an earlier month": (dict(period_start=500, period_end=1000), {}),
            "the same month": (dict(period_start=1000, period_end=2000), {}),
            "another reader": (dict(incarnation=INC2), dict(reader=None)),
        }
        for name, (report_over, state_kw) in state_over.items():
            with self.subTest(name):
                report = self.new_month(**report_over) if "period_start" not in report_over else R(**report_over)
                self.assertFalse(assess(report, S(**state_kw)).roll)

    def test_reported_by_gateway_is_decided_before_any_roll_a_quiet_report_can_still_say_hold(self):
        result = assess(self.new_month(flag=True), S())
        self.assertEqual(kinds(result), [evidence.KIND_REPORTED_BY_GATEWAY])
        self.assertFalse(result.roll)

    def test_a_report_that_raises_anything_does_not_roll(self):
        self.assertFalse(assess(self.new_month(price_hash=PRICE2), S()).roll)
        self.assertFalse(assess(self.new_month(), S(open_kinds=frozenset({"reader_changed"}))).roll)


class Readers(unittest.TestCase):

    def test_the_first_incarnation_with_no_adoption_is_pending_and_raises_nothing(self):
        result = assess(R(flag=True, price_hash=PRICE2), S(reader=None, adoption=None, generation=None, max_seq=None,
                                                          previous=None, last_quiet=None))
        self.assertTrue(result.create_reader)
        self.assertEqual(result.transitions, ())
        self.assertEqual(result.standing, STANDING_UNCHAINED)

    def test_a_new_incarnation_after_an_adoption_raises_one_reader_changed_and_nothing_about_the_other_chain(self):
        result = assess(R(incarnation=INC2, flag=True, price_hash=PRICE2, settled=1), S(reader=None, generation=None, max_seq=None))
        self.assertTrue(result.create_reader)
        self.assertEqual(kinds(result), [evidence.KIND_READER_CHANGED])
        self.assertEqual(result.standing, STANDING_UNCHAINED)
        self.assertFalse(result.roll)

    def test_a_reader_already_known_raises_no_second_reader_changed(self):
        result = assess(R(incarnation=INC2), S(reader="pending", generation=None, max_seq=None))
        self.assertFalse(result.create_reader)
        self.assertEqual(result.transitions, ())

    def test_the_adopted_reader_is_never_a_new_reader(self):
        result = assess(R(), S(reader=None))
        self.assertFalse(result.create_reader)
        self.assertEqual(result.transitions, ())

    def test_the_reports_of_a_pending_reader_are_judged_by_no_chain(self):
        result = assess(R(incarnation=INC2, flag=True, settled=1, quiet=True), S(reader="pending", generation=None, max_seq=None))
        self.assertEqual((result.standing, result.transitions, result.remember_quiet, result.roll), (STANDING_UNCHAINED, (), False, False))


# ---------------------------------------------------------------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------------------------------------------------------------

class _Ingest(_Store):

    def connect(self):
        return contextlib.closing(sqlite3.connect(self.db))

    def adopt(self, item, operator="op"):
        """Adopt a stored report the way R1-3d will: the baseline row, the reader made current (the old one retired first), and the
        chain state pointed at it. Returns the adoption id."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT gateway_id, reader_incarnation, ledger_epoch, generation, period_start, seq, settled_micro_eur, "
                "reserved_micro_eur, uncertain_micro_eur, unattributed_micro_eur, price_hash, child_cap_hash FROM gateway_totals "
                "WHERE event_id=?", (item["event_id"],)).fetchone()
            previous = conn.execute("SELECT current_adoption_id FROM gateway_chain_state WHERE gateway_id=?", (row[0],)).fetchone()
            adoption = conn.execute(
                "INSERT INTO gateway_adoptions(gateway_id, event_id, reader_incarnation, ledger_epoch, generation, period_start, "
                "seq, settled_micro_eur, reserved_micro_eur, uncertain_micro_eur, unattributed_micro_eur, price_hash, "
                "child_cap_hash, adopted_at, operator, kind, prev_adoption_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row[0], item["event_id"]) + tuple(row[1:]) + (1000, operator, "operator", None if previous is None else previous[0])
            ).lastrowid
            if conn.execute("SELECT 1 FROM gateway_readers WHERE gateway_id=? AND reader_incarnation=?", (row[0], row[1])).fetchone() is None:
                conn.execute("INSERT INTO gateway_readers(gateway_id, reader_incarnation, first_event_id, first_seen_at, state) "
                             "VALUES(?,?,?,?, 'pending')", (row[0], row[1], item["event_id"], 1))
            store._promote_reader(conn, row[0], row[1], adoption, 2000)
            conn.execute("INSERT OR IGNORE INTO gateway_chain_state(gateway_id) VALUES(?)", (row[0],))
            conn.execute("UPDATE gateway_chain_state SET current_adoption_id=?, last_quiet_event_id=?, last_quiet_unattributed=? "
                         "WHERE gateway_id=?", (adoption, item["event_id"], row[9], row[0]))
            conn.commit()
            return adoption

    def approve(self, price=PRICE, cap=CAP):
        self.run_sql("INSERT INTO gateway_approvals(gateway_id, price_hash, child_cap_hash, approved_at, operator) VALUES(?,?,?,?,?)",
                     (self.gateway_id(), price, cap, 1, "op"))

    def first(self, **over):
        """Ingest a first quiet report and adopt it. Later reports are numbered from 2."""
        item = report(seq=1, **over)
        self.ingest(item)
        self.adopt(item)
        return item

    def send(self, name, **over):
        item = report(**over)
        self.ingest(item, name=name + ".json")
        return item

    def counters(self):
        return self.rows("SELECT late_reports, old_chain_reports, duplicate_reports, retired_reader_reports FROM gateway_chain_state")

    def readers(self):
        return self.rows("SELECT reader_incarnation, state, decided_by_adoption FROM gateway_readers ORDER BY first_seen_at, reader_incarnation")

    def kinds(self):
        return [row[0] for row in self.transitions()]


class ReaderBookkeeping(_Ingest):

    def test_the_first_incarnation_with_no_adoption_is_pending_with_no_transition(self):
        first = report(seq=1)
        self.ingest(first)
        self.send("two", seq=2)
        self.assertEqual(self.rows("SELECT reader_incarnation, first_event_id, state, decided_by_adoption, retired_at "
                                   "FROM gateway_readers"), [(INC, first["event_id"], "pending", None, None)])
        self.assertEqual(self.transitions(), [])

    def test_one_reader_changed_per_incarnation_however_many_reports_it_sends(self):
        self.first()
        for seq in (1, 2, 3):
            self.send("b%d" % seq, seq=seq, reader_incarnation=INC2)
        self.assertEqual(self.kinds(), ["reader_changed"])
        self.assertEqual(self.rows("SELECT target_incarnation, event_id FROM gateway_transitions"),
                         [(INC2, derive(INC2, GEN, 1))])
        self.send("c1", seq=1, reader_incarnation=INC3)
        self.assertEqual(self.rows("SELECT target_incarnation FROM gateway_transitions ORDER BY id"), [(INC2,), (INC3,)])
        self.assertEqual(self.readers(), [(INC, "current", 1), (INC2, "pending", None), (INC3, "pending", None)])

    def test_a_new_reader_adds_no_other_transition_even_when_its_report_differs_in_every_way(self):
        self.first()
        self.send("b", seq=1, reader_incarnation=INC2, reconciliation_required=True, settled_micro_eur=7,
                  policy_hashes={"price": PRICE2, "child_cap": CAP2})
        self.assertEqual(self.kinds(), ["reader_changed"])

    def test_promoting_a_reader_retires_the_old_current_one_first_so_the_unique_index_never_trips(self):
        self.first()
        self.send("b", seq=1, reader_incarnation=INC2)
        with self.connect() as conn:
            conn.execute("INSERT INTO gateway_adoptions(gateway_id, event_id, reader_incarnation, ledger_epoch, generation, "
                         "period_start, seq, settled_micro_eur, reserved_micro_eur, uncertain_micro_eur, unattributed_micro_eur, "
                         "price_hash, child_cap_hash, adopted_at, operator, kind) "
                         "SELECT gateway_id, event_id, reader_incarnation, ledger_epoch, generation, period_start, seq, "
                         "settled_micro_eur, reserved_micro_eur, uncertain_micro_eur, unattributed_micro_eur, price_hash, "
                         "child_cap_hash, 1, 'op', 'operator' FROM gateway_totals WHERE reader_incarnation=?", (INC2,))
            gateway = conn.execute("SELECT id FROM gateways WHERE alias='g1'").fetchone()[0]
            store._promote_reader(conn, gateway, INC2, 2, 7000)
            conn.commit()
        self.assertEqual(self.rows("SELECT reader_incarnation, state, decided_by_adoption, retired_at FROM gateway_readers "
                                   "ORDER BY reader_incarnation"), [(INC, "retired", 2, 7000), (INC2, "current", 2, None)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM gateway_readers WHERE state='current'"), [(1,)])

    def test_a_retired_reader_is_never_promoted_again(self):
        self.first()
        self.send("b", seq=1, reader_incarnation=INC2)
        gateway = self.gateway_id()
        with self.connect() as conn:
            conn.execute("UPDATE gateway_readers SET state='retired', retired_at=5, decided_by_adoption=1 WHERE reader_incarnation=?",
                         (INC2,))
            with self.assertRaises(QuotaError):
                store._promote_reader(conn, gateway, INC2, 1, 6000)


def derive(incarnation, generation, seq):
    from quota_balancer.validate_readings import derive_event_id
    return derive_event_id(incarnation, generation, seq)


class RetiredReaders(_Ingest):
    """A report from a retired incarnation is kept as history and counted and creates no transition of any kind, on every path."""

    def retire(self, incarnation):
        self.run_sql("UPDATE gateway_readers SET state='retired', retired_at=5, decided_by_adoption=1 WHERE reader_incarnation=?",
                     (incarnation,))

    def setUp(self):
        super().setUp()
        self.first()
        self.send("seed", seq=1, reader_incarnation=INC2)          # INC2 is pending (and has its one reader_changed)
        self.retire(INC2)
        self.run_sql("DELETE FROM gateway_transitions")
        self.run_sql("UPDATE gateway_chain_state SET retired_reader_reports=0")

    def assert_counted_only(self, expected):
        self.assertEqual(self.transitions(), [])
        self.assertEqual(self.counters()[0][3], expected)

    def test_an_accepted_report_that_shows_an_epoch_generation_mismatch_creates_no_transition(self):
        self.send("m", seq=2, reader_incarnation=INC2, generation=GEN2)         # the same (incarnation, epoch) with another generation
        self.assert_counted_only(1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM gateway_totals WHERE reader_incarnation=?", (INC2,)), [(2,)])   # kept as history

    def test_an_accepted_report_that_says_hold_or_changes_a_fingerprint_or_a_figure_creates_no_transition(self):
        self.send("flag", seq=2, reader_incarnation=INC2, reconciliation_required=True)
        self.send("hash", seq=3, reader_incarnation=INC2, policy_hashes={"price": PRICE2, "child_cap": CAP2})
        self.send("fall", seq=4, reader_incarnation=INC2, settled_micro_eur=1, unattributed_micro_eur=0)
        self.assert_counted_only(3)

    def test_an_accepted_report_that_collides_by_content_creates_no_transition(self):
        self.send("same", seq=1, reader_incarnation=INC2, generation=GEN2, settled_micro_eur=6000)    # same (inc, epoch, seq), other content
        self.assert_counted_only(1)

    def test_a_refused_same_event_id_collision_is_counted_and_creates_no_transition(self):
        rival = report(seq=1, reader_incarnation=INC2, settled_micro_eur=6000)       # the same id as the seeded report, other content
        with self.assertRaises(QuotaError):
            self.ingest(rival, name="rival.json")
        self.assert_counted_only(1)

    def test_a_retired_reader_never_becomes_current_again_through_the_roll(self):
        self.approve()
        nxt = _month_bounds_of(_now_ms() - 1000)[1]
        start, end = _month_bounds_of(nxt)
        self.send("roll", seq=2, reader_incarnation=INC2, period_start=start, period_end=end)
        self.assert_counted_only(1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM gateway_adoptions"), [(1,)])
        self.assertEqual(self.readers()[0][1], "current")


class TransitionsAtIngest(_Ingest):

    def test_each_change_is_written_once_for_the_report_that_shows_it_and_nothing_resolves_or_overwrites_one(self):
        self.first()
        flagged = self.send("flag", seq=2, reconciliation_required=True)
        changed = self.send("hash", seq=3, policy_hashes={"price": PRICE2, "child_cap": CAP})
        fell = self.send("fall", seq=4, settled_micro_eur=10, unattributed_micro_eur=0)
        self.assertEqual(self.rows("SELECT kind, event_id, resolved_by_adoption FROM gateway_transitions ORDER BY id"), [
            ("reported_by_gateway", flagged["event_id"], None),
            ("price_hash_changed", changed["event_id"], None),
            ("figure_fell", fell["event_id"], None)])
        before = self.transitions()
        self.send("more", seq=5, reconciliation_required=True, policy_hashes={"price": PRICE2, "child_cap": CAP})
        self.assertEqual(self.transitions()[:3], before)              # untouched
        self.assertEqual(len(self.transitions()), 3)                  # the held and changed states are latched while their first row is open

    def test_a_quiet_fall_and_a_quiet_rise_and_a_report_that_is_not_quiet(self):
        self.first()                                                                     # adopted: settled 5000, unattributed 7, quiet
        self.send("busy", seq=2, quiet=False, unattributed_micro_eur=5000)              # not quiet: never raises or resolves
        self.send("busy2", seq=3, quiet=False, unattributed_micro_eur=0)
        self.assertEqual(self.transitions(), [])
        rise = self.send("rise", seq=4, unattributed_micro_eur=9)                       # quiet, above the adopted 7
        self.send("rise2", seq=5, unattributed_micro_eur=11)                            # one open rise: no second row
        self.assertEqual(self.rows("SELECT kind, event_id FROM gateway_transitions"), [("unattributed_rose", rise["event_id"])])
        fall = self.send("fall", seq=6, unattributed_micro_eur=3)                       # quiet, below the previous quiet report (11)
        self.assertEqual(self.kinds(), ["unattributed_rose", "figure_fell"])
        self.assertEqual(self.transitions()[1][1], fall["event_id"])

    def test_a_new_epoch_of_the_current_reader_raises_epoch_changed_once_while_open(self):
        self.first()
        self.send("e2a", seq=2, ledger_epoch=2, generation=GEN2)
        self.send("e2b", seq=3, ledger_epoch=2, generation=GEN2)
        self.assertEqual(self.kinds(), ["epoch_changed"])


class IdentityBeforeOrder(_Ingest):
    """A(seq 2), seq 3, then B(seq 2), and A, B, then seq 3: each gives exactly one `sequence_conflict`, whatever the arrival order."""

    def conflicts(self):
        return self.rows("SELECT event_id, conflict_incarnation, conflict_epoch, conflict_seq FROM gateway_transitions "
                         "WHERE kind='sequence_conflict'")

    def a_b(self):
        a = report(seq=2)
        b = report(seq=2, generation=GEN2, settled_micro_eur=5001)       # the same (incarnation, epoch, seq), other generation and content
        return a, b

    def test_a_then_a_later_number_then_b_is_one_conflict_and_b_is_not_merely_late(self):
        self.first()
        a, b = self.a_b()
        self.ingest(a, name="a.json")
        self.send("three", seq=3, settled_micro_eur=5200)
        self.ingest(b, name="b.json")
        self.assertEqual(self.conflicts(), [(b["event_id"], INC, 1, 2)])
        self.assertEqual(self.counters()[0][0], 0)                                  # not counted as late

    def test_a_then_b_then_a_later_number_is_one_conflict(self):
        self.first()
        a, b = self.a_b()
        self.ingest(a, name="a.json")
        self.ingest(b, name="b.json")
        self.send("three", seq=3, settled_micro_eur=5200)
        self.assertEqual(self.conflicts(), [(b["event_id"], INC, 1, 2)])

    def test_a_refused_same_event_id_report_still_gives_exactly_one_conflict_in_either_order(self):
        for order in ("before", "after"):
            with self.subTest(order=order):
                self.setUp()
                self.first()
                a = report(seq=2)
                self.ingest(a, name="a.json")
                if order == "after":
                    self.send("three", seq=3, settled_micro_eur=5200)
                rival = report(seq=2, settled_micro_eur=5001)                      # the same id as A, other content: refused
                for attempt in range(2):
                    with self.assertRaises(QuotaError):
                        self.ingest(rival, name="rival%d.json" % attempt)
                if order == "before":
                    self.send("three", seq=3, settled_micro_eur=5200)
                self.assertEqual(self.conflicts(), [(a["event_id"], INC, 1, 2)])

    def test_a_resolved_conflict_replayed_after_an_adoption_never_re_opens_anything(self):
        first = self.first()
        a, b = self.a_b()
        self.ingest(a, name="a.json")
        self.ingest(b, name="b.json")
        late = self.send("three", seq=3, settled_micro_eur=5200)
        self.adopt(late)                                                           # the uncontested report is adopted
        self.run_sql("UPDATE gateway_transitions SET resolved_by_adoption=(SELECT MAX(id) FROM gateway_adoptions)")
        resolved = self.transitions()
        for name, item in (("ra", a), ("rb", b), ("rfirst", first), ("rlate", late)):
            self.ingest(item, name=name + ".json")                                 # a replay of a stored report is a no-op
        self.assertEqual(self.transitions(), resolved)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM gateway_transitions WHERE resolved_by_adoption IS NULL"), [(0,)])
        rival = report(seq=2, settled_micro_eur=5001)                              # a refused same-id collision with A: a NEW event
        with self.assertRaises(QuotaError):
            self.ingest(rival, name="r.json")
        self.assertEqual(self.transitions()[:len(resolved)], resolved)             # the resolved rows are never touched


class OrderAtIngest(_Ingest):

    def test_late_old_chain_and_old_period_reports_are_kept_and_counted_and_raise_nothing(self):
        self.first()
        self.send("high", seq=5, settled_micro_eur=5100)
        self.send("late", seq=3, settled_micro_eur=7, reconciliation_required=True)          # lower number: late, ignored
        self.assertEqual(self.counters(), [(1, 0, 0, 0)])
        self.send("old-epoch", seq=1, ledger_epoch=0, generation=GEN3, reconciliation_required=True)
        self.assertEqual(self.counters(), [(1, 1, 0, 0)])
        start, end = _month_bounds_of(_month_bounds_of(_now_ms() - 1000)[0] - 1)
        self.send("old-month", seq=6, period_start=start, period_end=end, reconciliation_required=True)
        self.assertEqual(self.counters(), [(1, 2, 0, 0)])
        self.assertEqual(self.transitions(), [])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM gateway_totals"), [(5,)])             # every one is kept as history

    def test_the_same_number_and_content_under_another_event_id_is_a_duplicate(self):
        self.first()
        self.send("a", seq=2)
        self.send("dup", seq=2, generation=GEN2)                      # another generation, so another id; the same figures
        self.assertEqual(self.counters(), [(0, 0, 1, 0)])
        self.assertEqual(self.kinds(), ["epoch_generation_mismatch"])


class MonthRollAtIngest(_Ingest):

    def next_month(self):
        return _month_bounds_of(_month_bounds_of(_now_ms() - 1000)[1])

    def rolled(self):
        return self.rows("SELECT kind, operator, reader_incarnation, seq, prev_adoption_id FROM gateway_adoptions ORDER BY id")

    def test_the_first_quiet_report_of_a_new_month_rolls_and_the_same_reader_stays_current(self):
        self.first()
        self.approve()
        start, end = self.next_month()
        roll = self.send("roll", seq=2, period_start=start, period_end=end, settled_micro_eur=40, unattributed_micro_eur=3)
        self.assertEqual(self.rolled(), [("operator", "op", INC, 1, None), ("period_roll", None, INC, 2, 1)])
        self.assertEqual(self.rows("SELECT current_adoption_id, last_quiet_event_id, last_quiet_unattributed "
                                   "FROM gateway_chain_state"), [(2, roll["event_id"], 3)])
        self.assertEqual(self.readers(), [(INC, "current", 2)])
        self.assertEqual(self.rows("SELECT event_id, period_start, settled_micro_eur FROM gateway_adoptions WHERE id=2"),
                         [(roll["event_id"], start, 40)])
        self.assertEqual(self.transitions(), [])                                 # a new month is no fall
        self.assertEqual(self.rows("SELECT actor_alias, action, object_id FROM audit WHERE action='gateway_period_roll'"),
                         [("coordinator", "gateway_period_roll", roll["event_id"])])
        self.send("after", seq=3, period_start=start, period_end=end, settled_micro_eur=41, unattributed_micro_eur=3)
        self.assertEqual(len(self.rolled()), 2)                                  # the next report of the month rolls nothing more
        self.assertEqual(self.transitions(), [])

    def test_a_report_that_is_not_quiet_waits_and_the_next_quiet_one_rolls(self):
        self.first()
        self.approve()
        start, end = self.next_month()
        self.send("busy", seq=2, period_start=start, period_end=end, quiet=False)
        self.assertEqual(len(self.rolled()), 1)
        self.send("quiet", seq=3, period_start=start, period_end=end)
        self.assertEqual([row[0] for row in self.rolled()], ["operator", "period_roll"])

    def test_a_quiet_report_that_says_hold_raises_reported_by_gateway_and_does_not_roll(self):
        self.first()
        self.approve()
        start, end = self.next_month()
        held = self.send("held", seq=2, period_start=start, period_end=end, reconciliation_required=True)
        self.assertEqual(self.rows("SELECT kind, event_id FROM gateway_transitions"), [("reported_by_gateway", held["event_id"])])
        self.assertEqual(len(self.rolled()), 1)
        self.send("held2", seq=3, period_start=start, period_end=end)            # quiet and not held, but the last one is still open
        self.assertEqual(len(self.rolled()), 1)

    def test_no_roll_without_an_approved_pair_or_with_another_pair_or_another_epoch_or_anything_open(self):
        start, end = self.next_month()
        cases = {
            "not approved": (lambda: None, {}),
            "another pair": (self.approve, dict(policy_hashes={"price": PRICE2, "child_cap": CAP})),
            "another epoch": (self.approve, dict(ledger_epoch=2, generation=GEN2)),
            "another generation": (self.approve, dict(generation=GEN2)),
        }
        for name, (prepare, over) in cases.items():
            with self.subTest(name):
                self.setUp()
                self.first()
                prepare()
                self.send("n", seq=2, period_start=start, period_end=end, **over)
                self.assertEqual(len(self.rolled()), 1)
        self.setUp()
        self.first()
        self.approve()
        self.send("open", seq=2, settled_micro_eur=7)                             # a fall: open
        self.send("n", seq=3, period_start=start, period_end=end)
        self.assertEqual(len(self.rolled()), 1)

    def new_month_sends(self, *sends):
        """Adopt a quiet report of this month, approve the pair, then send (seq, settled, quiet) reports of the NEXT month."""
        self.first()
        self.approve()
        start, end = self.next_month()
        return [self.send("m%d" % seq, seq=seq, period_start=start, period_end=end, settled_micro_eur=settled, quiet=quiet)
                for seq, settled, quiet in sends]

    def test_a_fall_of_settled_in_a_new_month_before_its_first_quiet_report_is_caught_and_blocks_the_roll(self):
        busy, quiet = self.new_month_sends((2, 100, False), (3, 50, True))
        self.assertEqual(self.rows("SELECT kind, event_id FROM gateway_transitions"), [("figure_fell", quiet["event_id"])])
        self.assertEqual(len(self.rolled()), 1)                                    # the lower figure is not adopted

    def test_a_fall_between_two_busy_reports_is_raised_there_and_the_later_quiet_report_does_not_roll(self):
        _, fell, later = self.new_month_sends((2, 100, False), (3, 50, False), (4, 60, True))
        # The baseline is the HIGHEST earlier figure (100), so 60 is also below it: one `figure_fell` for each of the two reports.
        self.assertEqual(self.rows("SELECT kind, event_id FROM gateway_transitions ORDER BY id"),
                         [("figure_fell", fell["event_id"]), ("figure_fell", later["event_id"])])
        self.assertEqual(len(self.rolled()), 1)

    def test_the_highest_figure_is_the_baseline_so_a_missing_report_in_between_hides_nothing(self):
        # seq 3 never arrives; the quiet report 4 is compared with the highest earlier figure (100), not only with a neighbour.
        _, fell = self.new_month_sends((2, 100, False), (4, 90, True))
        self.assertEqual(self.kinds(), ["figure_fell"])
        self.assertEqual(len(self.rolled()), 1)

    def test_the_first_report_of_a_new_month_is_never_compared_with_the_old_month(self):
        self.new_month_sends((2, 1000, False), (3, 1200, True))                    # last month ended at 5000
        self.assertEqual(self.transitions(), [])
        self.assertEqual([row[0] for row in self.rolled()], ["operator", "period_roll"])

    def test_a_late_lower_numbered_report_in_the_new_month_is_counted_and_ignored_not_a_fall(self):
        self.new_month_sends((3, 100, False), (2, 50, False))                      # seq 2 arrives after seq 3
        self.assertEqual(self.transitions(), [])
        self.assertEqual(self.counters()[0][0], 1)

    def test_an_earlier_month_arriving_late_is_ignored_and_never_rolls(self):
        self.first()
        self.approve()
        start, end = _month_bounds_of(_month_bounds_of(_now_ms() - 1000)[0] - 1)
        self.send("old", seq=2, period_start=start, period_end=end)
        self.assertEqual(len(self.rolled()), 1)
        self.assertEqual(self.counters(), [(0, 1, 0, 0)])


# ---------------------------------------------------------------------------------------------------------------------------------
# The evaluator, a second line of defence
# ---------------------------------------------------------------------------------------------------------------------------------

class GatewayUsable(unittest.TestCase):
    """`billing_eval._gateway_usable` over a hand-made context: the chain starts at the adoption report, and a mixed chain is refused."""

    NOW = 10_000_000

    def ctx(self, rows, **over):
        readings, chain = [], []
        for seq, settled, extra in rows:
            event = "ev%d" % seq
            payload = {"gateway": "g1", "ledger_epoch": 1, "policy_hashes": {"price": PRICE, "child_cap": CAP}, "seq": seq,
                       "settled_micro_eur": settled, "reserved_micro_eur": 0, "uncertain_micro_eur": 0,
                       "unattributed_micro_eur": 0, "period_start": 1000, "reader_incarnation": INC, "generation": GEN}
            payload.update(extra)
            readings.append({"event_id": event, "machine": "desktop", "kind": "gateway_snapshot", "observed_at": self.NOW - 1000,
                             "payload": payload})
            chain.append((seq, self.NOW - 1000, settled, 0, 0, event))
        ctx = {"readings": readings, "unresolved_event_ids": [],
               "gateways": [{"gateway": "g1", "current_chain_event_ids": [row[5] for row in chain]}],
               "chains_by_gateway": {"g1": chain}}
        ctx.update(over)
        return ctx

    def test_the_adoption_report_alone_is_a_whole_usable_chain(self):
        self.assertTrue(billing_eval._gateway_usable(self.ctx([(4, 5000, {})]), self.NOW))

    def test_the_chain_from_the_adoption_report_on_is_usable_and_a_fall_inside_it_is_not(self):
        self.assertTrue(billing_eval._gateway_usable(self.ctx([(4, 5000, {}), (5, 5100, {})]), self.NOW))
        self.assertFalse(billing_eval._gateway_usable(self.ctx([(4, 5000, {}), (5, 4999, {})]), self.NOW))

    def test_a_chain_that_mixes_readers_generations_or_months_is_refused(self):
        for name, extra in (("reader", {"reader_incarnation": INC2}), ("generation", {"generation": GEN2}),
                            ("month", {"period_start": 2000}), ("epoch", {"ledger_epoch": 2})):
            with self.subTest(name):
                self.assertFalse(billing_eval._gateway_usable(self.ctx([(4, 5000, {}), (5, 5100, extra)]), self.NOW))

    def test_an_older_chain_without_the_version_4_keys_is_judged_as_before(self):
        ctx = self.ctx([(4, 5000, {}), (5, 5100, {})])
        for reading in ctx["readings"]:
            for key in ("reader_incarnation", "generation"):
                reading["payload"].pop(key)
        self.assertTrue(billing_eval._gateway_usable(ctx, self.NOW))

    def test_an_unresolved_event_of_the_gateway_still_makes_it_unusable(self):
        self.assertFalse(billing_eval._gateway_usable(self.ctx([(4, 5000, {})], unresolved_event_ids=["ev4"]), self.NOW))


if __name__ == "__main__":
    unittest.main()
