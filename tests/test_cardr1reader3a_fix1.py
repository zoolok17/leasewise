"""Card R1-READER step 3a, fix round 1: a reset keeps EVERY row of a damaged state, and a damaged halt word can never stop the reporter.

* **Reset.** The normal archive keeps one incarnation, one halt and ONE pending report, so it is used only for a state the start-up check calls
  consistent. Every other picture (two pending rows, damaged progress numbers, a damaged halt word, each word the start-up check can return) is kept
  whole in the quarantine, every column of every row of every reader table, or refused with nothing changed.
* **Halt words are closed.** A stored halt that is not one of the three words reads as `state_unavailable` everywhere (status, the send lane,
  start-up); the stored value stays as it is and is never shown.
* **Lane isolation.** An unexpected failure inside the money reader's lane (its send pass, its part of `status`) is caught at that lane only and
  counted as `state_unavailable`; the other uploads go on and `Reporter.run` keeps cycling.

Synthetic values only; no network beyond a fake transport.
"""

import inspect
import json
import re
import unittest
from unittest import mock

from quota_balancer.reporter import gwsend, gwstate, status

from tests import test_cardr1reader2_state as base
from tests.reporter_env import Rig
from tests.test_cardr1reader3a_send import GEN, INC, NOW, LIMIT_ALIAS, LIMIT_NOW, LaneTransport, _Send, doc, error, limit_body

OTHER = "c" * 32

# One forged picture per word `gwstate.inconsistency` can return: statements run with the schema's own checks off.
FORGERY = {
    "no_identity": ("DELETE FROM gwr_identity",),
    "bad_incarnation": ("DROP TRIGGER gwr_identity_incarnation_fixed", "UPDATE gwr_identity SET reader_incarnation='NOT-HEX'"),
    "bad_halt": ("UPDATE gwr_identity SET halt='damaged-halt'",),
    "bad_progress": ("UPDATE gwr_seq SET minted_seq=-1",),
    "numbers": ("UPDATE gwr_seq SET acked_seq=2",),
    "bad_epoch": ("UPDATE gwr_epoch SET ledger_epoch=0",),
    "epoch_mapping": ("DELETE FROM gwr_epoch",),
    "orphan_epoch": ("INSERT INTO gwr_epoch(ledger_epoch, generation) VALUES(2, '" + OTHER + "')",),
    "two_pending": ("INSERT INTO gwr_pending SELECT 2,generation,seq,body,event_id,idempotency_key,made_ms,refusals FROM gwr_pending",),
    "two_unfinished": ("INSERT INTO gwr_seq(generation, minted_seq, acked_seq) VALUES('" + OTHER + "',1,0)",
                       "INSERT INTO gwr_epoch(ledger_epoch, generation) VALUES(2, '" + OTHER + "')",
                       "INSERT INTO gwr_receipts(generation, read_through_seq, no_month) VALUES('" + OTHER + "',0,0)"),
    "unfinished_without_pending": ("DELETE FROM gwr_pending",),
    "orphan_pending": ("UPDATE gwr_pending SET generation='" + OTHER + "'",),
    "pending_number": ("UPDATE gwr_pending SET seq=2",),
    "bad_pending": ("UPDATE gwr_pending SET made_ms=-1",),
    "pending_event_id": ("UPDATE gwr_pending SET event_id='broken'",),
    "pending_bytes": ("UPDATE gwr_pending SET body=x'7b7d'",),
    "bad_receipts": ("UPDATE gwr_receipts SET read_through_seq=-1",),
    "progress_without_receipts": ("DELETE FROM gwr_receipt_months", "DELETE FROM gwr_receipts"),
}


class ResetKeepsEverything(_Send):

    def evidence_expected(self):
        """What the quarantine must hold: every row of every reader table, as read now (bytes as hex)."""
        return {name: [[gwstate._raw_value(value) for value in row] for row in self.state.all(statement)] for name, statement in gwstate._RAW}

    def quarantine_after_reset(self):
        gwstate.reset(self.state, NOW + 5, "e" * 32)
        (row,) = self.state.all("SELECT reader_incarnation, halt, pending_generation, pending_seq, pending_event_id, pending_body, quarantine "
                                "FROM gwr_archive")
        self.assertEqual(row[:6], (None,) * 6)                                    # the normal columns are empty: the whole state is in the quarantine
        self.assertEqual(self.state.all("SELECT quarantined FROM gwr_audit"), [(1,)])
        return json.loads(row[6])

    def test_two_pending_rows_with_different_bodies_both_survive(self):
        self.forced("INSERT INTO gwr_pending SELECT 2,generation,seq,body,event_id,idempotency_key,made_ms,refusals FROM gwr_pending",
                    ("UPDATE gwr_pending SET body=? WHERE id=2", (b'{"second_report":true}',)))
        first, second = [bytes(row[0]) for row in self.state.all("SELECT body FROM gwr_pending ORDER BY id")]
        self.assertNotEqual(first, second)
        kept = self.quarantine_after_reset()
        self.assertEqual([bytes.fromhex(row[3]["hex"]) for row in kept["pending"]], [first, second])
        self.assertEqual(self.state.all("SELECT * FROM gwr_pending"), [])

    def test_damaged_progress_numbers_survive(self):
        self.forced("UPDATE gwr_seq SET acked_seq=2")
        self.assertEqual(self.quarantine_after_reset()["seq"], [[GEN, 1, 2]])

    def test_a_damaged_halt_word_survives_and_is_never_shown(self):
        self.forced("UPDATE gwr_identity SET halt='damaged-halt'")
        self.assertEqual(self.quarantine_after_reset()["identity"][0][2], "damaged-halt")

    def test_every_word_the_start_up_check_can_return_leads_to_the_quarantine_of_the_whole_state(self):
        for word, statements in FORGERY.items():
            with self.subTest(word):
                self.setUp()
                self.forced(*statements)
                self.assertEqual(gwstate.inconsistency(self.state), word)
                expected = self.evidence_expected()
                self.assertEqual(self.quarantine_after_reset(), expected)               # every row of every reader table, exactly as it was
                for name in ("gwr_epoch", "gwr_seq", "gwr_pending", "gwr_receipts", "gwr_receipt_months"):
                    self.assertEqual(self.state.all("SELECT * FROM " + name), [], name)    # fixed names

    def test_the_forgery_table_names_every_word_the_check_can_return(self):
        source = inspect.getsource(gwstate.inconsistency) + inspect.getsource(gwstate._receipts_inconsistency)
        words = set(re.findall(r'return "([a-z_]+)"', source)) | {"unfinished_without_pending"}
        self.assertEqual(words, set(FORGERY))

    def test_a_consistent_state_still_uses_the_normal_archive(self):
        gwstate.reset(self.state, NOW, "e" * 32)
        self.assertEqual(self.state.one("SELECT quarantine IS NULL, pending_body IS NOT NULL, reader_incarnation FROM gwr_archive"), (1, 1, INC))
        self.assertEqual(self.state.all("SELECT quarantined FROM gwr_audit"), [(0,)])

    def test_a_consistent_halted_state_uses_the_normal_archive(self):
        for _ in range(gwstate.REFUSAL_THRESHOLD):
            self.send(error(400, "INVALID_CONFIG"))
        self.assertEqual(self.state.one("SELECT halt FROM gwr_identity")[0], gwstate.SEND_REFUSED)
        self.assertIsNone(gwstate.inconsistency(self.state))
        gwstate.reset(self.state, NOW, "e" * 32)
        self.assertEqual(self.state.one("SELECT quarantine IS NULL, halt FROM gwr_archive"), (1, gwstate.SEND_REFUSED))

    def test_a_large_but_valid_pending_report_archives_normally(self):
        padded = bytes(self.state.one("SELECT body FROM gwr_pending")[0]) + b" " * (gwstate.QUARANTINE_MAX_BYTES + 10)    # trailing space is still a report
        self.forced(("UPDATE gwr_pending SET body=?", (padded,)))
        self.assertIsNone(gwstate.inconsistency(self.state))
        gwstate.reset(self.state, NOW, "e" * 32)
        self.assertEqual(self.state.one("SELECT quarantine IS NULL, pending_body IS NOT NULL FROM gwr_archive"), (1, 1))

    def test_a_damaged_state_over_the_bound_is_refused_with_nothing_changed(self):
        self.forced("INSERT INTO gwr_pending SELECT 2,generation,seq,body,event_id,idempotency_key,made_ms,refusals FROM gwr_pending",
                    ("UPDATE gwr_pending SET body=? WHERE id=2", (b"{" + b" " * (gwstate.QUARANTINE_MAX_BYTES + 10) + b"}",)))
        before = self.dump()
        self.refused(gwstate.RESET_TOO_LARGE, gwstate.reset, self.state, NOW)
        self.assertEqual(self.dump(), before)


class DamagedHaltWord(_Send):

    def test_start_reads_it_as_state_unavailable_and_leaves_the_stored_value(self):
        self.forced("UPDATE gwr_identity SET halt='damaged-halt'")
        self.assertEqual(gwstate.start(self.state, NOW).halt, gwstate.STATE_UNAVAILABLE)
        self.assertEqual(gwstate.halt_word(self.state), gwstate.STATE_UNAVAILABLE)
        self.assertEqual(self.state.one("SELECT halt FROM gwr_identity")[0], "damaged-halt")

    def test_nothing_is_sent_and_the_count_is_state_unavailable(self):
        self.forced("UPDATE gwr_identity SET halt='damaged-halt'")
        out = self.send(self.accepted_answer())
        self.assertEqual((out.word, out.sent, len(self.transport.calls)), (gwsend.STATE_UNAVAILABLE, 0, 0))
        self.assertEqual(self.counts()["state_unavailable"], 1)
        self.assertEqual(self.counts()["reader_halted"], 0)

    def test_nothing_is_minted_while_the_word_is_damaged(self):
        self.send(self.accepted_answer())                                          # so no report is pending
        self.forced("UPDATE gwr_identity SET halt='damaged-halt'")
        self.refused(gwstate.HALTED, gwstate.mint, self.state, GEN, base.body_for(), base.totals(), NOW + 1)

    def test_resume_is_not_offered(self):
        self.forced("UPDATE gwr_identity SET halt='damaged-halt'")
        self.refused(gwstate.NOT_RESUMABLE, gwstate.resume, self.state, NOW)

    def test_status_shows_the_closed_word_and_its_sentence_and_does_not_raise(self):
        self.forced("UPDATE gwr_identity SET halt='damaged-halt'")
        report = status.build_status(self.state, set(), NOW, 60)
        self.assertEqual(report["money_reader"]["halt"], gwstate.STATE_UNAVAILABLE)
        self.assertIn(status.MONEY_READER_ACTIONS[gwstate.STATE_UNAVAILABLE], report["operator_actions"])
        self.assertNotIn("damaged-halt", json.dumps(report))


class LaneIsolation(unittest.TestCase):

    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        self.transport = LaneTransport()
        self.calls = []
        inner = self.transport

        def recording(method, path, headers, body, timeout):
            self.calls.append(path)
            return inner(method, path, headers, body, timeout)
        self.reporter = self.rig.open(transport=recording)
        gwstate.start(self.rig.state, NOW, INC)
        self.event_id = gwstate.mint(self.rig.state, GEN, base.body_for(), base.totals(), NOW)[1]
        self.rig.state.add_limit_event(LIMIT_ALIAS, limit_body(), LIMIT_NOW)
        self.transport.limit = doc(202, accepted=True)

    def count(self):
        return self.rig.state.counter(gwstate.READER_AGENT, gwstate.STATE_UNAVAILABLE)

    def test_a_real_cycle_with_a_damaged_halt_sends_no_report_and_still_uploads_the_rest(self):
        self.rig.state.conn.execute("PRAGMA ignore_check_constraints=ON")
        self.rig.state.conn.execute("UPDATE gwr_identity SET halt='damaged-halt'")
        self.rig.state.conn.execute("PRAGMA ignore_check_constraints=OFF")
        result = self.reporter.cycle()
        self.assertNotIn("/v1/readings", self.calls)
        self.assertIn("/v1/limit-events", self.calls)
        self.assertEqual((result.sent, self.rig.state.all("SELECT state FROM limit_event")), (1, [("sent",)]))
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.reporter.report()["money_reader"]["halt"], gwstate.STATE_UNAVAILABLE)

    def test_an_unexpected_failure_in_the_report_pass_is_caught_at_the_lane_and_the_cycle_goes_on(self):
        with mock.patch.object(gwsend, "send_pending", side_effect=RuntimeError("secret detail 123")):
            result = self.reporter.cycle()
        self.assertEqual((result.sent, result.blocked), (1, None))
        self.assertEqual(self.rig.state.all("SELECT state FROM limit_event"), [("sent",)])
        self.assertEqual(self.count(), 1)
        self.assertNotIn("secret detail", json.dumps(self.reporter.report()))

    def test_run_keeps_cycling_when_the_report_pass_fails_every_time(self):
        cycles = []

        def on_cycle(result):
            cycles.append(result)
            if len(cycles) == 3:
                self.reporter.stop.set()
        with mock.patch.object(gwsend, "send_pending", side_effect=RuntimeError("x")):
            self.reporter.run(on_cycle=on_cycle)
        self.assertEqual(len(cycles), 3)
        self.assertEqual(self.count(), 3)

    def test_even_a_failed_count_does_not_end_the_cycle(self):
        with mock.patch.object(gwsend, "send_pending", side_effect=RuntimeError("x")), \
\
                mock.patch.object(type(self.rig.state), "bump", side_effect=RuntimeError("no write")):
            result = self.reporter.cycle()
        self.assertEqual(result.sent, 1)

    def test_a_failure_in_the_money_readers_part_of_status_leaves_the_rest_of_status(self):
        with mock.patch.object(gwstate, "closed_halt", side_effect=RuntimeError("x")):
            report = self.reporter.report()
        reader = report["money_reader"]
        self.assertEqual((reader["halt"], reader["pending"], reader["consecutive_refusals"]), (gwstate.STATE_UNAVAILABLE, 0, 0))
        self.assertIn(status.MONEY_READER_ACTIONS[gwstate.STATE_UNAVAILABLE], report["operator_actions"])
        self.assertIn("agents", report)
        self.assertEqual(report["status_version"], status.STATUS_VERSION)


if __name__ == "__main__":
    unittest.main()
