"""Card R1-READER step 3a: the money reader's send side (`reporter/gwsend.py`, `client.post_reading`, `gwstate` send functions, `reporter resume|reset`).

* **The closed answer reading.** Every answer is exactly one of four classes: accepted (202, `accepted` exactly `true`, the same event id), conflict
  (409 `IDEMPOTENCY_CONFLICT`: the reader halts), definitive refusal (400 `INVALID_CONFIG` or `SCHEMA_UNSUPPORTED`: counted, a run of
  `REFUSAL_THRESHOLD` halts it) and retry (everything else: the same bytes are kept).
* **`resume`** (after `send_refused` only) and **`reset`** (with the quarantine form): local, audited, need a confirm flag.
* **The report's own lane:** its retry never stops another upload, and another upload's retry never stops it; none is minted while one is pending.

Synthetic values only; no network beyond a fake transport.
"""

import json
import unittest

from quota_balancer.reporter import gwsend, gwstate, status
from quota_balancer.reporter.client import HttpClient
from quota_balancer.validate_readings import derive_event_id

from tests import test_cardr1reader2_fix1 as fx
from tests import test_cardr1reader2_state as base
from tests.reporter_env import Rig
from tests.test_card7a3 import cli
from tests.test_card7c2_state import ALIAS as LIMIT_ALIAS, NOW as LIMIT_NOW, body as limit_body

GEN, INC, NOW = base.GEN, base.INC, base.NOW
EVENT = derive_event_id(INC, GEN, 1)
SECRET = "the-identity-value-0001"


class Transport:
    """A fake transport: the next answer to `/v1/readings` is set by the test; every request is recorded."""

    def __init__(self):
        self.answers = []
        self.calls = []
        self.hook = None                  # runs when a request is made (a test may damage the state at that moment)

    def __call__(self, method, path, headers, body, timeout):
        self.calls.append((path, headers.get("Idempotency-Key"), bytes(body)))
        if self.hook:
            self.hook()
        answer = self.answers.pop(0) if self.answers else (503, b"{}")
        if isinstance(answer, Exception):
            raise answer
        return answer


def doc(status_code, **fields):
    return status_code, json.dumps(fields).encode("utf-8")


def error(status_code, code):
    return status_code, json.dumps({"error": {"code": code, "message": "x"}}).encode("utf-8")


class _Send(base._State):

    def setUp(self):
        super().setUp()
        self.transport = Transport()
        self.client = HttpClient("http", "127.0.0.1", 1, SECRET, transport=self.transport)
        self.started()
        self.event_id = self.mint()[1]
        self.assertEqual(self.event_id, EVENT)

    def send(self, answer=None):
        if answer is not None:
            self.transport.answers.append(answer)
        return gwsend.send_pending(self.state, self.client, NOW)

    def counts(self):
        return {name: self.state.counter(gwstate.READER_AGENT, name) for name in status.MONEY_READER_COUNTS}

    def refusals(self):
        return self.state.one("SELECT refusals FROM gwr_pending")[0]

    def accepted_answer(self):
        return doc(202, accepted=True, event_id=self.event_id)


class Classes(_Send):

    def test_the_exact_acceptance_clears_the_pending_report(self):
        out = self.send(self.accepted_answer())
        self.assertEqual((out.word, out.sent), (gwsend.ACCEPTED, 1))
        self.assertEqual(self.state.all("SELECT * FROM gwr_pending"), [])
        self.assertEqual(self.state.all("SELECT minted_seq, acked_seq FROM gwr_seq"), [(1, 1)])
        self.assertEqual(len(self.transport.calls), 1)
        path, key, sent = self.transport.calls[0]
        self.assertEqual((path, key), ("/v1/readings", EVENT))                   # the stored bytes, under the event id as the idempotency key
        self.assertEqual(sent, base.body_for()(INC, 1, 1))

    def test_an_answer_that_is_not_the_exact_acceptance_keeps_the_report(self):
        for label, answer in (("text true", doc(202, accepted="true", event_id=EVENT)), ("one", doc(202, accepted=1, event_id=EVENT)),
                              ("false", doc(202, accepted=False, event_id=EVENT)), ("another id", doc(202, accepted=True, event_id=derive_event_id(INC, GEN, 2))),
                              ("no id", doc(202, accepted=True)), ("null id", doc(202, accepted=True, event_id=None)),
                              ("no accepted", doc(202, event_id=EVENT)), ("200", doc(200, accepted=True, event_id=EVENT)),
                              ("201", doc(201, accepted=True, event_id=EVENT)), ("203", doc(203, accepted=True, event_id=EVENT))):
            with self.subTest(label):
                before = self.dump()
                out = self.send(answer)
                self.assertEqual((out.word, out.sent), (gwsend.SEND_FAILED, 0))
                self.assertEqual(self.refusals(), 0)
                self.assertNotEqual(self.dump(), before)                          # only the send_failed count moved...
                self.assertEqual(self.state.all("SELECT minted_seq, acked_seq FROM gwr_seq"), [(1, 0)])
                self.assertEqual(len(self.state.all("SELECT * FROM gwr_pending")), 1)

    def test_a_collision_halts_the_reader_for_good(self):
        out = self.send(error(409, "IDEMPOTENCY_CONFLICT"))
        self.assertEqual(out.word, gwsend.CONFLICT)
        self.assertEqual(self.state.one("SELECT halt FROM gwr_identity")[0], gwstate.CONTINUITY_CONFLICT)
        self.assertEqual(self.counts()["continuity_conflict"], 1)
        calls = len(self.transport.calls)
        again = self.send(self.accepted_answer())                                  # nothing is sent while halted
        self.assertEqual((again.word, len(self.transport.calls)), (gwsend.READER_HALTED, calls))
        self.assertEqual(self.counts()["reader_halted"], 1)
        self.assertEqual(len(self.state.all("SELECT * FROM gwr_pending")), 1)       # and the report is kept as it was

    def test_a_409_with_any_other_code_is_a_retry(self):
        for code in ("INVALID_REQUEST", "RECEIPT_CONFLICT", "NOT_A_WORD"):
            with self.subTest(code):
                self.assertEqual(self.send(error(409, code)).word, gwsend.SEND_FAILED)
        self.assertEqual(self.send(doc(409)).word, gwsend.SEND_FAILED)
        self.assertIsNone(self.state.one("SELECT halt FROM gwr_identity")[0])

    def test_each_definitive_code_is_a_refusal_and_any_other_400_is_a_retry(self):
        for code in ("INVALID_CONFIG", "SCHEMA_UNSUPPORTED"):
            with self.subTest(code):
                self.assertEqual(self.send(error(400, code)).word, gwsend.REFUSED)
        self.assertEqual(self.refusals(), 2)
        for code in ("INVALID_REQUEST", "NOT_A_WORD", "IDEMPOTENCY_CONFLICT"):
            with self.subTest(other=code):
                self.assertEqual(self.send(error(400, code)).word, gwsend.SEND_FAILED)
        self.assertEqual(self.refusals(), 2)
        self.assertEqual(self.send(error(422, "INVALID_CONFIG")).word, gwsend.SEND_FAILED)    # the status matters too

    def test_everything_else_is_a_retry_and_keeps_the_same_bytes(self):
        answers = [("401", error(401, "UNAUTHENTICATED")), ("403", error(403, "FORBIDDEN")), ("429", error(429, "ACCOUNT_PARKED")),
                   ("500", doc(500)), ("503", error(503, "SERVICE_UNAVAILABLE")), ("no answer", ConnectionRefusedError("x")),
                   ("a timeout", TimeoutError()), ("unreadable JSON", (503, b"not json")), ("an unreadable success", (202, b"<html>")),
                   ("empty", (202, b"")), ("a list", (202, b"[1]")), ("the identity echoed", doc(202, accepted=True, event_id=EVENT, note=SECRET))]
        for label, answer in answers:
            with self.subTest(label):
                out = self.send(answer)
                self.assertEqual((out.word, out.sent), (gwsend.SEND_FAILED, 0))
        self.assertEqual(self.refusals(), 0)
        self.assertEqual({call[2] for call in self.transport.calls}, {base.body_for()(INC, 1, 1)})        # never rebuilt, never renumbered
        self.assertEqual(self.counts()["send_failed"], len(answers))
        self.assertTrue(self.send(error(401, "UNAUTHENTICATED")).unauthorized)
        self.assertFalse(self.send(doc(503)).unauthorized)

    def test_with_nothing_pending_nothing_is_sent_and_nothing_changes(self):
        self.send(self.accepted_answer())
        before, calls = self.dump(), len(self.transport.calls)
        out = self.send()
        self.assertEqual((out.word, out.sent, len(self.transport.calls)), (None, 0, calls))
        self.assertEqual(self.dump(), before)

    def test_a_pending_report_beside_a_damaged_state_is_not_sent(self):
        self.forced("UPDATE gwr_pending SET seq=2")
        calls = len(self.transport.calls)
        out = self.send(self.accepted_answer())
        self.assertEqual((out.word, len(self.transport.calls)), (gwsend.STATE_UNAVAILABLE, calls))
        self.assertEqual(self.state.one("SELECT halt FROM gwr_identity")[0], gwstate.STATE_UNAVAILABLE)
        self.assertEqual(self.counts()["state_unavailable"], 1)

    def test_the_acceptance_the_state_will_not_take_halts_instead_of_clearing(self):
        self.transport.hook = lambda: self.forced("UPDATE gwr_seq SET acked_seq=1")   # damage that appears while the request is out
        out = self.send(self.accepted_answer())
        self.assertEqual((out.word, out.sent), (gwsend.STATE_UNAVAILABLE, 0))
        self.assertEqual(len(self.state.all("SELECT * FROM gwr_pending")), 1)           # the report is kept
        self.assertEqual(self.state.one("SELECT halt FROM gwr_identity")[0], gwstate.STATE_UNAVAILABLE)
        self.assertEqual(self.counts()["state_unavailable"], 1)


class Budget(_Send):

    def refuse(self, times):
        for _ in range(times):
            self.assertEqual(self.send(error(400, "INVALID_CONFIG")).word, gwsend.REFUSED)

    def test_the_threshold_is_a_documented_small_positive_named_constant(self):
        self.assertIs(type(gwstate.REFUSAL_THRESHOLD), int)
        self.assertTrue(1 <= gwstate.REFUSAL_THRESHOLD <= 10)
        self.assertEqual(self.state.one("SELECT refusals FROM gwr_pending")[0], 0)

    def test_one_below_keeps_it_and_the_nth_halts(self):
        n = gwstate.REFUSAL_THRESHOLD
        self.refuse(n - 1)
        self.assertEqual((self.refusals(), self.state.one("SELECT halt FROM gwr_identity")[0]), (n - 1, None))
        self.refuse(1)
        self.assertEqual((self.refusals(), self.state.one("SELECT halt FROM gwr_identity")[0]), (n, gwstate.SEND_REFUSED))
        self.assertEqual(self.counts()["send_refused"], 1)
        calls = len(self.transport.calls)
        self.assertEqual(self.send(self.accepted_answer()).word, gwsend.READER_HALTED)           # and it sends no more
        self.assertEqual(len(self.transport.calls), calls)

    def test_a_retry_in_between_does_not_reset_the_count(self):
        n = gwstate.REFUSAL_THRESHOLD
        self.refuse(n - 2)
        for answer in (doc(503), error(429, "ACCOUNT_PARKED"), ConnectionRefusedError("x"), error(401, "UNAUTHENTICATED")):
            self.send(answer)
        self.assertEqual(self.refusals(), n - 2)
        self.refuse(2)
        self.assertEqual(self.state.one("SELECT halt FROM gwr_identity")[0], gwstate.SEND_REFUSED)

    def test_the_count_is_kept_across_a_restart_and_an_acceptance_clears_it_with_the_row(self):
        self.refuse(2)
        self.state.close()
        self.state = base.State(self.path)
        self.addCleanup(self.state.close)
        self.assertEqual(self.refusals(), 2)
        self.assertEqual(self.send(self.accepted_answer()).word, gwsend.ACCEPTED)
        self.assertEqual(self.state.all("SELECT * FROM gwr_pending"), [])
        second = self.mint()
        self.assertEqual(self.refusals(), 0)                                        # the next report has its own budget
        self.assertEqual(second[0], 2)


class Interruption(_Send):

    def test_a_failure_at_each_write_of_the_acceptance_leaves_the_old_state_and_the_answer_can_be_applied_again(self):
        real = self.state.conn
        counter = fx._Writes(real)
        self.state.conn = counter
        try:
            self.assertEqual(self.send(self.accepted_answer()).word, gwsend.ACCEPTED)
        finally:
            self.state.conn = real
        total = counter.writes
        self.assertEqual(total, 2)                                                      # the pending row is deleted, acked_seq moves
        for after in (False, True):
            for number in range(1, total + 1):
                self.setUp()
                before = self.dump()
                real = self.state.conn
                self.state.conn = fx._Writes(real, number, after)
                try:
                    with self.assertRaises(RuntimeError):
                        self.send(self.accepted_answer())
                finally:
                    self.state.conn = real
                with self.subTest(write=number, after=after):
                    self.assertEqual(self.dump(), before)                                # the old state: there is no third
                self.assertEqual(self.send(self.accepted_answer()).word, gwsend.ACCEPTED)


class Resume(_Send):

    def halt(self):
        for _ in range(gwstate.REFUSAL_THRESHOLD):
            self.send(error(400, "SCHEMA_UNSUPPORTED"))

    def test_it_is_refused_unless_the_reader_stopped_by_send_refused(self):
        before = self.dump()
        self.refused(gwstate.NOT_RESUMABLE, gwstate.resume, self.state, NOW)                  # no halt at all
        for word in (gwstate.CONTINUITY_CONFLICT, gwstate.STATE_UNAVAILABLE):
            with self.subTest(word):
                self.forced(("UPDATE gwr_identity SET halt=?", (word,)))
                held = self.dump()
                self.refused(gwstate.NOT_RESUMABLE, gwstate.resume, self.state, NOW)
                self.assertEqual(self.dump(), held)
                self.forced("UPDATE gwr_identity SET halt=NULL")
        self.assertEqual(self.dump(), before)

    def test_it_clears_the_halt_renews_the_budget_keeps_the_totals_and_is_audited(self):
        self.halt()
        total = self.counts()["refused_answers"]
        self.assertEqual(total, gwstate.REFUSAL_THRESHOLD)
        gwstate.resume(self.state, NOW + 7)
        self.assertIsNone(self.state.one("SELECT halt FROM gwr_identity")[0])
        self.assertEqual(self.refusals(), 0)
        self.assertEqual(self.counts()["refused_answers"], total)                          # cumulative diagnostics are kept
        self.assertEqual(self.counts()["resumed"], 1)
        self.assertEqual(self.state.all("SELECT act, at_ms, quarantined, refusals_total FROM gwr_audit"), [("resume", NOW + 7, 0, total)])

    def test_the_same_saved_bytes_are_resent_and_the_new_budget_is_a_full_one(self):
        self.halt()
        first = {call[2] for call in self.transport.calls}
        gwstate.resume(self.state, NOW)
        n = gwstate.REFUSAL_THRESHOLD
        for _ in range(n - 1):
            self.send(error(400, "INVALID_CONFIG"))
        self.assertIsNone(self.state.one("SELECT halt FROM gwr_identity")[0])
        self.assertEqual(self.counts()["refused_answers"], 2 * n - 1)                      # the total keeps growing across resumes
        self.assertEqual({call[2] for call in self.transport.calls}, first)                # never rebuilt
        self.assertEqual(self.send(self.accepted_answer()).word, gwsend.ACCEPTED)

    def test_a_failure_at_each_write_of_a_resume_leaves_the_old_state(self):
        self.halt()
        steps = self.count_steps(lambda: gwstate.resume(self.state, NOW))
        self.assertEqual(steps, ["halt_cleared", "budget_renewed", "audited"])
        for number in (1, 2, 3):
            self.setUp()
            self.halt()
            before = self.dump()
            with self.fault_at(number):
                with self.assertRaises(RuntimeError):
                    gwstate.resume(self.state, NOW)
            self.assertEqual(self.dump(), before)


class Reset(_Send):

    def damaged(self):
        """Codex's case: a pending event id that is not an id (the schema's own checks switched off, as a damaged file would be)."""
        self.forced("UPDATE gwr_pending SET event_id='broken'")

    def test_the_normal_case_archives_everything_and_starts_fresh_without_the_old_sequence(self):
        self.halt_it()
        old = self.state.all("SELECT generation, seq, event_id, body FROM gwr_pending")[0]
        fresh = gwstate.reset(self.state, NOW + 3, "e" * 32)
        self.assertEqual(self.state.all("SELECT reader_incarnation, halt, pending_generation, pending_seq, pending_event_id, pending_body, quarantine, "
                                        "archived_ms FROM gwr_archive"),
                         [(INC, gwstate.SEND_REFUSED, old[0], old[1], old[2], old[3], None, NOW + 3)])
        self.assertEqual(self.state.all("SELECT reader_incarnation, halt FROM gwr_identity"), [(fresh, None)])
        for table in ("gwr_epoch", "gwr_seq", "gwr_pending", "gwr_receipts", "gwr_receipt_months"):
            self.assertEqual(self.state.all("SELECT * FROM " + table), [], table)               # fixed names
        self.assertEqual(self.state.all("SELECT act, at_ms, quarantined FROM gwr_audit"), [("reset", NOW + 3, 0)])
        self.assertEqual(self.mint()[0], 1)                                                     # numbering starts again under the new identity
        self.assertNotEqual(derive_event_id(fresh, GEN, 1), EVENT)

    def halt_it(self):
        for _ in range(gwstate.REFUSAL_THRESHOLD):
            self.send(error(400, "INVALID_CONFIG"))

    def test_a_damaged_pending_event_id_goes_to_the_quarantine_and_nothing_is_discarded(self):
        self.damaged()
        raw_body = self.state.one("SELECT body FROM gwr_pending")[0]
        fresh = gwstate.reset(self.state, NOW, "e" * 32)
        (row,) = self.state.all("SELECT reader_incarnation, halt, pending_event_id, pending_body, quarantine, archived_ms FROM gwr_archive")
        self.assertEqual(row[:4], (None, None, None, None))
        evidence = json.loads(row[4])
        self.assertEqual(evidence["pending"][0][4], "broken")                                       # the raw damaged value is kept
        self.assertEqual(bytes.fromhex(evidence["pending"][0][3]["hex"]), raw_body)                 # and the raw bytes
        self.assertEqual(evidence["identity"][0][1], INC)
        self.assertEqual(evidence["seq"], [[GEN, 1, 0]])
        self.assertEqual(self.state.all("SELECT quarantined FROM gwr_audit"), [(1,)])
        self.assertEqual(self.state.all("SELECT reader_incarnation FROM gwr_identity"), [(fresh,)])
        self.assertEqual(self.state.all("SELECT * FROM gwr_pending"), [])

    def test_a_damaged_incarnation_is_quarantined_too(self):
        self.state.conn.execute("DROP TRIGGER gwr_identity_incarnation_fixed")                      # damage that bypasses the trigger too
        self.forced("UPDATE gwr_identity SET reader_incarnation='NOT-HEX'")
        gwstate.reset(self.state, NOW, "e" * 32)
        evidence = json.loads(self.state.one("SELECT quarantine FROM gwr_archive")[0])
        self.assertEqual(evidence["identity"][0][1], "NOT-HEX")

    def test_a_state_too_large_for_the_quarantine_refuses_with_a_closed_word_and_changes_nothing(self):
        self.forced("UPDATE gwr_pending SET event_id='broken'", ("UPDATE gwr_pending SET body=?", (b"{" + b" " * (gwstate.QUARANTINE_MAX_BYTES + 10) + b"}",)))
        before = self.dump()
        self.refused(gwstate.RESET_TOO_LARGE, gwstate.reset, self.state, NOW)
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.state.all("SELECT * FROM gwr_archive"), [])

    def test_a_large_but_healthy_pending_report_archives_normally(self):
        padded = bytes(self.state.one("SELECT body FROM gwr_pending")[0]) + b" " * (gwstate.QUARANTINE_MAX_BYTES + 10)      # a VALID report, with real identity facts
        self.forced(("UPDATE gwr_pending SET body=?", (padded,)))
        self.assertIsNone(gwstate.inconsistency(self.state))
        gwstate.reset(self.state, NOW, "e" * 32)
        self.assertEqual(self.state.one("SELECT quarantine IS NULL, pending_body IS NOT NULL FROM gwr_archive"), (1, 1))

    def test_a_failure_at_each_write_of_a_reset_through_the_quarantine_leaves_the_old_state(self):
        self.damaged()
        steps = self.count_steps(lambda: gwstate.reset(self.state, NOW, "e" * 32))
        self.assertGreaterEqual(len(steps), 9)
        for number in range(1, len(steps) + 1):
            self.setUp()
            self.damaged()
            before = self.dump()
            with self.fault_at(number):
                with self.assertRaises(RuntimeError):
                    gwstate.reset(self.state, NOW, "e" * 32)
            self.assertEqual(self.dump(), before)


class Commands(unittest.TestCase):
    """`reporter resume --confirm` and `reporter reset --confirm`: local, audited, fixed sentences."""

    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        self.state = base.State(self.rig.ws.state)
        gwstate.start(self.state, NOW, INC)
        gwstate.mint(self.state, GEN, base.body_for(), base.totals(), NOW)
        self.state.close()

    def read(self, sql):
        state = base.State(self.rig.ws.state)
        try:
            return state.all(sql)
        finally:
            state.close()

    def halt(self, word):
        state = base.State(self.rig.ws.state)
        try:
            with state.txn():
                state.conn.execute("UPDATE gwr_identity SET halt=?", (word,))
        finally:
            state.close()

    def test_without_confirm_nothing_is_opened_or_changed(self):
        self.halt(gwstate.SEND_REFUSED)
        for verb in ("resume", "reset"):
            code, out, err = cli([verb, "--config", self.rig.ws.config])
            self.assertEqual((code, out), (2, ""))
            self.assertEqual(json.loads(err), {"error": {"code": "CONFIRM_REQUIRED", "message": cli_module().MSG_CONFIRM}})
        self.assertEqual(self.read("SELECT halt FROM gwr_identity"), [(gwstate.SEND_REFUSED,)])
        self.assertEqual(self.read("SELECT * FROM gwr_audit"), [])

    def test_resume_after_send_refused_clears_it_and_prints_one_line(self):
        self.halt(gwstate.SEND_REFUSED)
        code, out, err = cli(["resume", "--config", self.rig.ws.config, "--confirm"])
        self.assertEqual((code, json.loads(out), err), (0, {"resumed": True}, ""))
        self.assertEqual(self.read("SELECT halt FROM gwr_identity"), [(None,)])
        self.assertEqual(self.read("SELECT act FROM gwr_audit"), [("resume",)])

    def test_resume_is_not_offered_after_a_collision_or_a_damaged_state(self):
        for word in (gwstate.CONTINUITY_CONFLICT, gwstate.STATE_UNAVAILABLE):
            with self.subTest(word):
                self.halt(word)
                code, out, err = cli(["resume", "--config", self.rig.ws.config, "--confirm"])
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(json.loads(err)["error"]["code"], "NOT_RESUMABLE")
                self.assertEqual(self.read("SELECT halt FROM gwr_identity"), [(word,)])

    def test_reset_archives_and_prints_the_new_identity(self):
        self.halt(gwstate.CONTINUITY_CONFLICT)
        old = self.read("SELECT reader_incarnation FROM gwr_identity")[0][0]
        code, out, err = cli(["reset", "--config", self.rig.ws.config, "--confirm"])
        shown = json.loads(out)
        self.assertEqual((code, err, shown["reset"]), (0, "", True))
        self.assertNotEqual(shown["reader_incarnation"], old)
        self.assertEqual(self.read("SELECT reader_incarnation, halt FROM gwr_identity"), [(shown["reader_incarnation"], None)])
        self.assertEqual(self.read("SELECT reader_incarnation, halt FROM gwr_archive"), [(old, gwstate.CONTINUITY_CONFLICT)])
        self.assertEqual(self.read("SELECT act FROM gwr_audit"), [("reset",)])

    def test_a_state_owned_by_a_running_reporter_is_refused(self):
        owner = base.State(self.rig.ws.state)
        self.addCleanup(owner.close)
        code, out, err = cli(["reset", "--config", self.rig.ws.config, "--confirm"])
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(json.loads(err)["error"]["code"], "STATE_OWNED")


def cli_module():
    from quota_balancer.reporter import cli as module
    return module


class Lane(unittest.TestCase):
    """The report's own lane, in a whole cycle of the reporter."""

    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        self.transport = LaneTransport()
        self.reporter = self.rig.open(transport=self.transport)
        gwstate.start(self.rig.state, NOW, INC)
        self.event_id = gwstate.mint(self.rig.state, GEN, base.body_for(), base.totals(), NOW)[1]
        self.rig.state.add_limit_event(LIMIT_ALIAS, limit_body(), LIMIT_NOW)

    def pending(self):
        return len(self.rig.state.all("SELECT * FROM gwr_pending"))

    def limit_state(self):
        return self.rig.state.all("SELECT state FROM limit_event")

    def test_a_report_that_needs_a_retry_does_not_stop_a_limit_upload_in_the_same_cycle(self):
        self.transport.report = doc(503)
        self.transport.limit = doc(202, accepted=True)
        result = self.reporter.cycle()
        self.assertEqual(self.pending(), 1)
        self.assertEqual(self.limit_state(), [("sent",)])
        self.assertEqual((result.sent, result.blocked), (1, "unreachable"))

    def test_a_limit_upload_that_needs_a_retry_does_not_stop_the_report_in_the_same_cycle(self):
        self.transport.report = doc(202, accepted=True, event_id=self.event_id)
        self.transport.limit = doc(503)
        result = self.reporter.cycle()
        self.assertEqual(self.pending(), 0)                                           # the report was accepted
        self.assertEqual(self.limit_state(), [("pending",)])
        self.assertEqual((result.sent, result.blocked), (1, "unreachable"))
        self.assertEqual(self.rig.state.all("SELECT minted_seq, acked_seq FROM gwr_seq"), [(1, 1)])

    def test_a_credential_refusal_of_the_report_is_ranked_above_unreachable(self):
        self.transport.report = error(401, "UNAUTHENTICATED")
        self.transport.limit = doc(503)
        self.assertEqual(self.reporter.cycle().blocked, "credential")

    def test_a_halted_report_does_not_stop_the_other_uploads(self):
        with self.rig.state.txn():
            self.rig.state.conn.execute("UPDATE gwr_identity SET halt='continuity_conflict'")
        self.transport.limit = doc(202, accepted=True)
        result = self.reporter.cycle()
        self.assertEqual((self.limit_state(), result.sent), ([("sent",)], 1))
        self.assertEqual(self.pending(), 1)

    def test_no_report_is_minted_while_one_is_pending(self):
        self.transport.report = doc(503)
        self.reporter.cycle()
        before = self.rig.state.all("SELECT generation, seq, body, event_id FROM gwr_pending")
        with self.assertRaises(gwstate.Refused) as caught:
            gwstate.mint(self.rig.state, GEN, base.body_for(), base.totals(), NOW + 1)
        self.assertEqual(caught.exception.word, gwstate.PENDING_EXISTS)
        self.assertEqual(self.rig.state.all("SELECT generation, seq, body, event_id FROM gwr_pending"), before)

    def test_the_status_shows_numbers_and_closed_words_only(self):
        self.transport.report = error(400, "INVALID_CONFIG")
        for _ in range(gwstate.REFUSAL_THRESHOLD):
            self.reporter.cycle()
        report = self.reporter.report()
        reader = report["money_reader"]
        self.assertEqual(report["status_version"], status.STATUS_VERSION)
        self.assertEqual((reader["pending"], reader["halt"], reader["consecutive_refusals"], reader["refusal_threshold"]),
                         (1, "send_refused", gwstate.REFUSAL_THRESHOLD, gwstate.REFUSAL_THRESHOLD))
        self.assertEqual(reader["counts"]["send_refused"], 1)
        self.assertEqual(reader["counts"]["refused_answers"], gwstate.REFUSAL_THRESHOLD)
        self.assertIn(status.MONEY_READER_ACTIONS["send_refused"], report["operator_actions"])
        text = json.dumps(report)
        for secret in (EVENT, INC, GEN, SECRET):
            self.assertNotIn(secret, text)

    def test_a_reporter_with_no_money_reader_is_unchanged(self):
        rig = Rig(server=False)
        self.addCleanup(rig.cleanup)
        reporter = rig.open(transport=LaneTransport())
        result = reporter.cycle()
        self.assertEqual((result.sent, result.blocked), (0, None))
        self.assertEqual(rig.state.all("SELECT * FROM gwr_identity"), [])                # nothing was created by looking
        self.assertEqual(reporter.report()["money_reader"]["halt"], None)


class LaneTransport:
    """Answers by path: `/v1/readings` (the report) and `/v1/limit-events`; anything else is a 503."""

    def __init__(self):
        self.report = doc(503)
        self.limit = doc(503)

    def __call__(self, method, path, headers, body, timeout):
        if path == "/v1/readings":
            return self.report
        if path == "/v1/limit-events":
            return self.limit
        return doc(503)


if __name__ == "__main__":
    unittest.main()
