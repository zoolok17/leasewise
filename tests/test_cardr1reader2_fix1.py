"""Card R1-READER step 2, fix round 1 (Codex's read of the first build).

* **Both directions.** Every relationship of the state is checked both ways: an epoch whose generation has no progress row is an orphan (the next mint
  would have numbered that generation from 1 again, under the same event id), and so is a progress row without its receipts row.
* **Exact identity facts.** A saved report's event id, reader, generation, number and epoch are checked for their exact types by ONE checker, used by the
  mint and by the start-up check; the same bytes are shown to go through Leasewise's real receiving path (the HTTP translation and the validator), and every
  malformed variant is refused there too.
* **The largest numbers** cannot be incremented: they halt as `state_unavailable`, nothing is numbered or created.
* **A failure at each real SQL write** of a mint, a replacement mint, an acceptance and a reset leaves the whole logical dump unchanged.

Synthetic values only.
"""

import json
import re
import unittest

from quota_balancer import http_api, store
from quota_balancer.errors import QuotaError
from quota_balancer.reporter import gwstate
from quota_balancer.validate_readings import derive_event_id, validate_readings

from tests import test_cardr1reader2_state as base
from tests.test_card2b1 import _month_bounds_of, _now_ms
from tests.test_cardr13b_ingest import _Store

GEN, GEN2, INC, NOW = base.GEN, base.GEN2, base.INC, base.NOW
MAX = 2 ** 63 - 1
body_for, totals = base.body_for, base.totals


class _S(base._State):
    pass


class Orphans(_S):

    def healthy(self, generation=GEN):
        self.started()
        event_id = self.mint(generation)[1]
        self.assertTrue(self.accepted(event_id))

    def test_an_orphan_epoch_alone_halts_and_nothing_can_be_minted(self):
        self.started()
        self.forced(("INSERT INTO gwr_epoch(ledger_epoch, generation) VALUES(1,?)", (GEN,)))
        self.assertEqual(gwstate.inconsistency(self.state), "orphan_epoch")
        self.assertEqual(self.run_start().halt, gwstate.STATE_UNAVAILABLE)
        self.refused(gwstate.HALTED, self.mint)

    def test_an_orphan_epoch_beside_a_generation_with_healthy_progress_halts(self):
        self.healthy(GEN)
        self.forced(("INSERT INTO gwr_epoch(ledger_epoch, generation) VALUES(2,?)", (GEN2,)))
        self.assertEqual(gwstate.inconsistency(self.state), "orphan_epoch")
        self.assertEqual(self.run_start().halt, gwstate.STATE_UNAVAILABLE)
        self.refused(gwstate.HALTED, self.mint, GEN)
        self.refused(gwstate.HALTED, self.mint, GEN2)

    def test_codexs_case_report_1_accepted_then_progress_and_receipts_removed_never_mints_report_1_again(self):
        self.healthy(GEN)
        self.state.conn.execute("DELETE FROM gwr_receipt_months")
        self.state.conn.execute("DELETE FROM gwr_receipts")
        self.state.conn.execute("DELETE FROM gwr_seq")                                  # only the epoch row survives
        self.assertEqual(self.run_start().halt, gwstate.STATE_UNAVAILABLE)
        before = self.dump()
        self.refused(gwstate.HALTED, self.mint)
        self.assertEqual(self.state.all("SELECT * FROM gwr_pending"), [])               # report 1 was not saved a second time
        self.assertEqual(self.dump(), before)

    def test_progress_without_its_receipts_row_halts(self):
        self.healthy(GEN)
        self.state.conn.execute("DELETE FROM gwr_receipt_months")
        self.state.conn.execute("DELETE FROM gwr_receipts")
        self.assertEqual(gwstate.inconsistency(self.state), "progress_without_receipts")
        self.assertEqual(self.run_start().halt, gwstate.STATE_UNAVAILABLE)

    def test_the_valid_pictures_still_pass(self):
        self.started()
        self.forced(("INSERT INTO gwr_epoch(ledger_epoch, generation) VALUES(1,?)", (GEN,)),
                    ("INSERT INTO gwr_seq(generation, minted_seq, acked_seq) VALUES(?,0,0)", (GEN,)),
                    ("INSERT INTO gwr_receipts(generation, read_through_seq, no_month) VALUES(?,0,0)", (GEN,)))
        self.assertIsNone(gwstate.inconsistency(self.state))                             # 0/0
        self.assertIsNone(self.run_start().halt)
        for generation in (GEN2, GEN):                                                   # and a return to an earlier, fully acknowledged generation
            self.assertTrue(self.accepted(self.mint(generation)[1]))
        self.assertIsNone(self.run_start().halt)
        self.assertEqual([r[1] for r in self.state.all("SELECT ledger_epoch, generation FROM gwr_epoch ORDER BY 1")], [GEN, GEN2, GEN])


class ExactFacts(_S):

    BAD = {
        "seq true": {"seq": True}, "seq 1.0": {"seq": 1.0}, "seq text": {"seq": "1"}, "seq zero": {"seq": 0}, "seq above": {"seq": MAX + 1},
        "epoch true": {"ledger_epoch": True}, "epoch 1.0": {"ledger_epoch": 1.0}, "epoch list": {"ledger_epoch": []},
        "epoch object": {"ledger_epoch": {}}, "epoch text": {"ledger_epoch": "1"}, "epoch zero": {"ledger_epoch": 0}, "epoch null": {"ledger_epoch": None},
        "event id upper": {"event_id": derive_event_id(INC, GEN, 1).upper()}, "event id not an id": {"event_id": "alice-the-agent"},
        "event id number": {"event_id": 5}, "incarnation upper": {"reader_incarnation": INC.upper()}, "incarnation short": {"reader_incarnation": "d" * 31},
        "generation list": {"generation": [GEN]}, "generation upper": {"generation": GEN.upper()},
    }

    def test_a_malformed_report_is_bad_report_and_nothing_is_written(self):
        self.started()
        for label, over in self.BAD.items():
            with self.subTest(label):
                self.refused(gwstate.BAD_REPORT, self.mint, GEN, None, body_for(**over))

    def test_malformed_stored_bytes_halt_the_start_up_with_no_exception(self):
        for label, over in self.BAD.items():
            with self.subTest(label):
                self.setUp()
                self.started()
                event_id = derive_event_id(INC, GEN, 1)
                good = body_for()(INC, 1, 1)
                bad = body_for(**over)(INC, 1, 1)
                self.assertNotEqual(good, bad)
                self.forced(("INSERT INTO gwr_epoch(ledger_epoch, generation) VALUES(1,?)", (GEN,)),
                            ("INSERT INTO gwr_seq VALUES(?,1,0)", (GEN,)),
                            ("INSERT INTO gwr_receipts VALUES(?,0,0)", (GEN,)),
                            ("INSERT INTO gwr_pending(id, generation, seq, body, event_id, idempotency_key, made_ms) VALUES(1,?,1,?,?,?,1)",
                             (GEN, bad, event_id, event_id)))
                got = self.run_start()                                                    # must not raise
                self.assertEqual(got.halt, gwstate.STATE_UNAVAILABLE)
                self.assertEqual(gwstate.inconsistency(self.state), "pending_bytes")
                self.refused(gwstate.HALTED, self.mint)

    def test_the_good_bytes_pass_both_checks(self):
        self.started()
        seq, event_id, body = self.mint()
        self.assertEqual(gwstate.report_facts(body), (event_id, INC, GEN, seq, 1))
        self.assertIsNone(gwstate.inconsistency(self.state))


class AgreesWithLeasewise(_Store):
    """The reader's check and Leasewise's receiving path agree: the SAME saved bytes are accepted by the real HTTP translation and the validator, and every
    malformed variant is refused there too."""

    def wire(self, **over):
        gateway = self.rows("SELECT g.id, a.id, m.id FROM gateways g JOIN accounts a ON a.id=g.account_id JOIN machines m ON m.id=g.machine_id "
                            "WHERE g.alias='g1'")[0]
        now = _now_ms()
        start, end = _month_bounds_of(now)
        inc, generation = "d" * 32, "c" * 32
        payload = {"gateway_id": gateway[0], "ledger_epoch": 1, "seq": 1, "period_start": start, "period_end": end,
                   "policy_hashes": {"price": "a" * 64, "child_cap": "9" * 64}, "sized_ready": False, "reconciliation_required": False,
                   "settled_micro_eur": 5000, "reserved_micro_eur": 200, "uncertain_micro_eur": 30, "unattributed_micro_eur": 7,
                   "generation": generation, "receipts_seq": 4, "quiet": True, "held_over_reservation": 0, "reader_incarnation": inc}
        doc = {"event_id": derive_event_id(inc, generation, 1), "account_id": gateway[1], "observed_at": now - 1000, "kind": "gateway_snapshot",
               "payload": payload}
        for key, value in over.items():
            if key in doc:
                doc[key] = value
            else:
                payload[key] = value
        return doc, (gateway[2], "desktop"), now

    def leasewise_accepts(self, doc, identity, now):
        conn = store._connect(self.db, must_exist=True)
        try:
            _account, translated = http_api._wire_to_reading(doc, identity, conn)
        finally:
            conn.close()
        return validate_readings(translated, now)

    def test_a_complete_valid_report_is_accepted_by_both(self):
        doc, identity, now = self.wire()
        raw = json.dumps(doc).encode("utf-8")
        self.assertEqual(gwstate.report_facts(raw), (doc["event_id"], "d" * 32, "c" * 32, 1, 1))
        self.assertEqual(len(self.leasewise_accepts(doc, identity, now)), 1)

    def test_every_malformed_variant_is_refused_by_both(self):
        variants = {"seq true": {"seq": True}, "seq 1.0": {"seq": 1.0}, "epoch true": {"ledger_epoch": True}, "epoch 1.0": {"ledger_epoch": 1.0},
                    "epoch list": {"ledger_epoch": []}, "epoch object": {"ledger_epoch": {}}, "epoch text": {"ledger_epoch": "1"},
                    "incarnation upper": {"reader_incarnation": "D" * 32}, "generation short": {"generation": "c" * 31}}
        for label, over in variants.items():
            with self.subTest(label):
                doc, identity, now = self.wire(**over)
                self.assertIsNone(gwstate.report_facts(json.dumps(doc).encode("utf-8")))
                with self.assertRaises(QuotaError):
                    self.leasewise_accepts(doc, identity, now)

    def test_the_event_id_must_be_the_derived_one_for_leasewise_too(self):
        doc, identity, now = self.wire(event_id=derive_event_id("e" * 32, "c" * 32, 1))
        with self.assertRaises(QuotaError):
            self.leasewise_accepts(doc, identity, now)


class Exhaustion(_S):

    def seed(self, minted, epoch=1, generation=GEN):
        self.started()
        self.forced(("INSERT INTO gwr_epoch(ledger_epoch, generation) VALUES(?,?)", (epoch, generation)),
                    ("INSERT INTO gwr_seq VALUES(?,?,?)", (generation, minted, minted)),
                    ("INSERT INTO gwr_receipts VALUES(?,0,0)", (generation,)))

    def test_one_below_the_largest_number_still_mints_and_the_largest_halts(self):
        self.seed(MAX - 2)
        self.assertEqual(self.mint()[0], MAX - 1)
        self.assertEqual(self.accepted(derive_event_id(INC, GEN, MAX - 1)), True)
        self.assertEqual(self.mint()[0], MAX)                                           # the largest number itself can be minted
        self.assertTrue(self.accepted(derive_event_id(INC, GEN, MAX)))
        before = self.dump()
        with self.assertRaises(gwstate.Refused) as caught:
            self.mint()
        self.assertEqual(caught.exception.word, gwstate.HALTED)
        self.assertEqual(self.state.one("SELECT halt FROM gwr_identity")[0], gwstate.STATE_UNAVAILABLE)
        self.state.conn.execute("UPDATE gwr_identity SET halt=NULL")                    # compare everything else with the halt put back
        self.assertEqual(self.dump(), before)
        self.state.conn.execute("UPDATE gwr_identity SET halt='state_unavailable'")
        self.assertEqual(self.state.all("SELECT * FROM gwr_pending"), [])               # nothing was numbered or saved

    def test_an_epoch_at_the_largest_number_halts_when_a_new_epoch_is_needed_and_only_then(self):
        self.seed(1, epoch=MAX)
        seq, _event, body = self.mint(GEN)                                              # same generation: no new epoch, the largest epoch is reused
        self.assertEqual((seq, gwstate.report_facts(body)[4]), (2, MAX))
        self.assertTrue(self.accepted(derive_event_id(INC, GEN, 2)))
        before = self.dump()
        with self.assertRaises(gwstate.Refused) as caught:
            self.mint(GEN2)                                                             # a change of generation needs epoch MAX + 1
        self.assertEqual(caught.exception.word, gwstate.HALTED)
        self.assertEqual(self.state.one("SELECT halt FROM gwr_identity")[0], gwstate.STATE_UNAVAILABLE)
        self.assertEqual(self.state.all("SELECT COUNT(*) FROM gwr_epoch"), [(1,)])
        self.assertEqual(self.state.all("SELECT COUNT(*) FROM gwr_seq"), [(1,)])
        self.assertNotEqual(self.dump(), before)                                        # only the halt differs
        self.assertEqual(self.state.all("SELECT * FROM gwr_pending"), [])

    def test_reset_recovers_from_an_exhausted_state(self):
        self.seed(MAX)
        with self.assertRaises(gwstate.Refused):
            self.mint()
        gwstate.reset(self.state, NOW, "e" * 32)
        self.assertIsNone(gwstate.start(self.state, NOW).halt)
        self.assertEqual(self.mint()[0], 1)


class _Writes:
    """A stand-in for the state's connection that counts the INSERT, UPDATE and DELETE statements and fails the k-th (before or after it runs)."""

    WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.I)

    def __init__(self, real, fail_at=None, after=False):
        self._real, self.fail_at, self.after, self.writes = real, fail_at, after, 0

    def execute(self, sql, *args):
        if self.WRITE.match(sql):
            self.writes += 1
            if self.writes == self.fail_at:
                if not self.after:
                    raise RuntimeError("fault before write %d" % self.writes)
                self._real.execute(sql, *args)
                raise RuntimeError("fault after write %d" % self.writes)
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


class EveryWrite(_S):
    """A failure injected at each ACTUAL SQL write (not at a hook): the whole logical dump is unchanged each time, and the same call then succeeds."""

    MONTHS = (("2026-09", 2, 30), ("2026-10", 1, 5))

    def run_scenario(self, action, prepare):
        """Count the writes of `action` on a clean run, then fail each one, before and after it runs."""
        self.setUp()
        prepare()
        counter = _Writes(self.state.conn)
        real = self.state.conn
        self.state.conn = counter
        try:
            action()
        finally:
            self.state.conn = real
        total = counter.writes
        self.assertGreaterEqual(total, 2)
        for after in (False, True):
            for number in range(1, total + 1):
                self.setUp()
                prepare()
                before = self.dump()
                real = self.state.conn
                self.state.conn = _Writes(real, number, after)
                try:
                    with self.assertRaises(RuntimeError):
                        action()
                finally:
                    self.state.conn = real
                with self.subTest(write=number, after=after):
                    self.assertEqual(self.dump(), before)
                action()                                                                 # and, with no fault, the same call succeeds
        return total

    def test_a_first_mint_with_two_receipt_months(self):
        total = self.run_scenario(lambda: self.mint(tot=totals(through=3, months=self.MONTHS, no_month=1)), self.started)
        self.assertGreaterEqual(total, 7)                                                # seq, epoch, pending, receipts, delete, two months, minted

    def test_a_replacement_mint(self):
        def prepare():
            self.started()
            self.accepted(self.mint(tot=totals(through=1, months=(("2026-10", 1, 4),)))[1])
        total = self.run_scenario(lambda: self.mint(tot=totals(through=3, months=self.MONTHS, no_month=1)), prepare)
        self.assertGreaterEqual(total, 5)

    def test_an_acceptance(self):
        def prepare():
            self.started()
            self.mint(tot=totals(through=2, months=self.MONTHS))
        total = self.run_scenario(lambda: self.accepted(derive_event_id(INC, GEN, 1)), prepare)
        self.assertEqual(total, 2)

    def test_a_reset(self):
        def prepare():
            self.started()
            self.mint(tot=totals(through=2, months=self.MONTHS))
        total = self.run_scenario(lambda: gwstate.reset(self.state, NOW, "e" * 32), prepare)
        self.assertGreaterEqual(total, 8)


if __name__ == "__main__":
    unittest.main()
