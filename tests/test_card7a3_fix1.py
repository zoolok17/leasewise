"""Card 7a-3, fix round 1: the closed error words, an unreadable answer, loud
permanent failures, and a credential fault that is history once it is over.

Rows: closed_words_f2, unreadable_answer_f3, permanent_failures_f4,
credential_history_f5. (The Windows access-list rows, F1, are in
test_card7a3.py: ConfigPermissions.) Synthetic data; a fake transport.
"""

import json
import os
import threading
import unittest

from quota_balancer import errors
from quota_balancer.reporter import decisions
from quota_balancer.reporter import status as status_mod

from tests.reporter_env import FAKE_IDENTITY, Rig
from tests.test_card7a3 import Script, cli, error, granted, one_message


def everything(rig):
    """Every byte the reporter keeps or shows after a cycle: the state, its
    folder (the saved status copy), and the status report."""
    rig.close()
    blob = b""
    folder = os.path.dirname(rig.ws.state)
    for name in sorted(os.listdir(folder)):
        with open(os.path.join(folder, name), "rb") as handle:
            blob += handle.read()
    code, out, err = cli(["status", "--config", rig.ws.config, "--json"])
    return blob.decode("latin-1") + out + err


class ClosedWordsF2(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        one_message(self.rig.journal, self.rig.clock.ms)

    def test_the_closed_words_are_leasewises_own_plus_three(self):
        every = {value for name, value in vars(errors).items()
                 if name.startswith("CODE_") and isinstance(value, str)}
        self.assertEqual(decisions.LEASEWISE_CODES, every)
        self.assertGreaterEqual(len(every), 20)
        for word in every:
            self.assertEqual(decisions.closed_code(word), word)
        for odd in ("whatever", "", FAKE_IDENTITY, 5, ["X"], {"a": 1}, b"E"):
            self.assertEqual(decisions.closed_code(odd), "UNKNOWN_CODE", odd)
        self.assertIsNone(decisions.closed_code(None))

    def test_an_identity_echoed_in_an_error_code_never_reaches_a_record(self):
        for status in (401, 400, 403, 429, 500, 409):
            with self.subTest(status):
                rig = Rig(server=False)
                self.addCleanup(rig.cleanup)
                one_message(rig.journal, rig.clock.ms)
                rig.open(transport=Script(error(status, FAKE_IDENTITY)))
                rig.reporter.cycle()
                self.assertNotIn(FAKE_IDENTITY, everything(rig))

    def test_the_stored_and_shown_word_is_unknown_code(self):
        self.rig.open(transport=Script(error(401, FAKE_IDENTITY)))
        self.rig.reporter.cycle()
        self.assertEqual(self.rig.state.one(
            "SELECT last_error_code FROM acquisition")[0], "UNKNOWN_CODE")
        self.assertEqual(self.rig.reporter.report()["last_401"]["code"],
                         "UNKNOWN_CODE")

    def test_a_real_leasewise_code_is_kept_as_it_is(self):
        self.rig.open(transport=Script(error(401, "UNAUTHENTICATED")))
        self.rig.reporter.cycle()
        self.assertEqual(self.rig.reporter.report()["last_401"]["code"],
                         "UNAUTHENTICATED")

    def test_old_state_with_an_odd_word_is_still_shown_as_one_fixed_word(self):
        self.rig.open(transport=Script(error(400, "INVALID_REQUEST")))
        self.rig.reporter.cycle()
        self.rig.state.conn.execute(
            "UPDATE acquisition SET last_error_code=?", (FAKE_IDENTITY,))
        self.rig.state.conn.execute(
            "INSERT INTO agent_state(agent, failed_code) VALUES('alpha', ?)",
            (FAKE_IDENTITY,))
        self.assertNotIn(FAKE_IDENTITY,
                         json.dumps(self.rig.reporter.report()))


class UnreadableAnswerF3(unittest.TestCase):
    DEEP = b"[" * 20000 + b"0" + b"]" * 20000

    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        one_message(self.rig.journal, self.rig.clock.ms)

    def acq(self):
        return self.rig.state.one(
            "SELECT state, tries, last_error_code FROM acquisition")

    def test_an_answer_that_cannot_be_decoded_is_a_retry_on_every_status(self):
        answers = {
            "500 deeply nested": (500, self.DEEP),
            "401 deeply nested": (401, self.DEEP),
            "200 deeply nested": (200, self.DEEP),
            "201 deeply nested": (201, self.DEEP),
            "200 not json": (200, b"{not json"),
            "200 not utf-8": (200, b"\xff\xfe\x00"),
            "503 not utf-8": (503, b"\xff\xfe"),
            "502 html": (502, b"<html>"),
        }
        for label, answer in answers.items():
            with self.subTest(label):
                rig = Rig(server=False)
                self.addCleanup(rig.cleanup)
                one_message(rig.journal, rig.clock.ms)
                rig.open(transport=Script(answer))
                result = rig.reporter.cycle()
                state, tries, code = rig.state.one(
                    "SELECT state, tries, last_error_code FROM acquisition")
                self.assertEqual((state, tries), ("pending", 1), label)
                self.assertIn(result.blocked, ("unreachable", "credential"))
                self.assertIn(code, (None, "MALFORMED_RESPONSE",
                                     "UNAUTHENTICATED"), label)

    def test_a_malformed_success_is_retried_for_a_report_and_a_final_too(self):
        script = Script(granted(), (200, b"{not json"), (200, b"[[["))
        self.rig.open(transport=script)
        self.rig.reporter.cycle()
        self.assertEqual(self.rig.state.one(
            "SELECT state, tries FROM report"), ("pending", 1))
        self.rig.state.conn.execute("UPDATE report SET not_before_ms=0")
        script.answers = [(200, {})]            # the answer is readable now
        self.rig.reporter.cycle()
        self.assertEqual(self.rig.state.one("SELECT state FROM report")[0],
                         "sent")

    def test_the_command_keeps_going_and_never_calls_it_an_internal_fault(self):
        for answer in ((500, self.DEEP), (200, self.DEEP), (200, b"{x")):
            with self.subTest(answer[0]):
                rig = Rig(server=False)
                self.addCleanup(rig.cleanup)
                one_message(rig.journal, rig.clock.ms)
                code, out, err = cli(["drain", "--config", rig.ws.config],
                                     transport=Script(answer))
                self.assertEqual(code, 3, err)
                self.assertEqual(json.loads(out)["blocked"], "unreachable")
                self.assertEqual(err, "")

    def test_run_keeps_retrying_and_does_not_exit(self):
        waits = []
        rig = self.rig

        class Stop:
            flag = False

            def is_set(self):
                return self.flag

            def set(self):
                self.flag = True

            def wait(self, seconds):
                waits.append(seconds)
                rig.clock.advance(seconds)
                if len(waits) >= 4:
                    self.flag = True

        reporter = rig.open(transport=Script((500, self.DEEP)), stop=Stop())
        reporter.run()                          # returns only because of the stop
        self.assertEqual(len(waits), 4)
        self.assertGreaterEqual(self.acq()[1], 3)


class PermanentFailuresF4(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        one_message(self.rig.journal, self.rig.clock.ms)

    def test_a_final_that_failed_for_good_is_listed_and_warned_about(self):
        self.rig.open(transport=Script(granted(), (200, {}),
                                       error(400, "INVALID_REQUEST")))
        self.rig.reporter.cycle()
        report = self.rig.reporter.report()
        alpha = report["agents"]["alpha"]
        self.assertEqual(alpha["finals"]["failed"], 1)
        self.assertEqual(alpha["permanent_failures_total"], 1)
        item = alpha["permanent_failures"][0]
        self.assertEqual((item["kind"], item["code"], item["message_id"]),
                         ("final", "INVALID_REQUEST", "m1"))
        # a complete observation that was never delivered, said as such
        self.assertEqual((item["observation_complete"], item["delivered"]),
                         (True, False))
        self.assertTrue(any("failed for good" in a
                            for a in report["operator_actions"]))
        text = status_mod.render_text(report)
        self.assertIn("failed for good: final of message m1, INVALID_REQUEST",
                      text)
        self.assertIn("observation complete, NOT delivered", text)

    def test_all_three_kinds_are_listed_with_their_codes(self):
        # an acquisition, a report and a final, each failing on its own
        rig = Rig(server=False)
        self.addCleanup(rig.cleanup)
        s = one_message(rig.journal, rig.clock.ms)
        s.dispatch("m2", "t-m2", rig.clock.ms - 20_000,
                   message_at=rig.clock.ms - 21_000)
        s.disposed("m2", "completed", rig.clock.ms - 10_000,
                   message_at=rig.clock.ms - 21_000)
        s.dispatch("m3", "t-m3", rig.clock.ms - 5_000,
                   message_at=rig.clock.ms - 6_000)
        s.disposed("m3", "completed", rig.clock.ms - 4_000,
                   message_at=rig.clock.ms - 6_000)
        answers = {
            # m1: acquisition refused; m2: report refused; m3: final refused
            "m1": [error(400, "INVALID_REQUEST")],
            "m2": [granted(), error(400, "INVALID_REQUEST"), (200, {})],
            "m3": [granted(), (200, {}), error(400, "RECEIPT_CONFLICT")]}
        order = [a for m in ("m1", "m2", "m3") for a in answers[m]]
        rig.open(transport=Script(*order))
        rig.reporter.cycle()
        found = {(i["kind"], i["message_id"]): i["code"] for i in
                 rig.reporter.report()["agents"]["alpha"]
                 ["permanent_failures"]}
        self.assertEqual(found[("acquisition", "m1")], "INVALID_REQUEST")
        self.assertEqual(found[("report", "m2")], "INVALID_REQUEST")
        self.assertEqual(found[("final", "m3")], "RECEIPT_CONFLICT")
        alpha = rig.reporter.report()["agents"]["alpha"]
        self.assertEqual(alpha["permanent_failures_total"], len(found))

    def test_the_json_and_the_text_status_both_say_it(self):
        self.rig.open(transport=Script(granted(), (200, {}),
                                       error(400, "INVALID_REQUEST")))
        self.rig.reporter.cycle()
        self.rig.close()
        code, out, _ = cli(["status", "--config", self.rig.ws.config, "--json"])
        report = json.loads(out)
        self.assertEqual(report["agents"]["alpha"]["permanent_failures"][0]
                         ["code"], "INVALID_REQUEST")
        code, text, _ = cli(["status", "--config", self.rig.ws.config])
        self.assertIn("failed for good", text)

    def test_a_clean_run_has_no_failures_and_no_warning(self):
        self.rig.open(transport=Script(granted(), (200, {}), (200, {})))
        self.rig.reporter.cycle()
        report = self.rig.reporter.report()
        self.assertEqual(report["agents"]["alpha"]["permanent_failures"], [])
        self.assertFalse(any("failed for good" in a
                             for a in report["operator_actions"]))

    def test_the_list_is_capped_but_the_total_is_not(self):
        self.rig.open(transport=Script(error(400, "INVALID_REQUEST")))
        self.rig.reporter.cycle()
        conn = self.rig.state.conn
        for n in range(status_mod.MAX_LISTED_FAILURES + 5):
            conn.execute(
                "INSERT INTO report(event_id, agent, message_id, turn_id, body,"
                " state, last_error_code) VALUES(?, 'alpha', 'm1', 't', '{}', "
                "'failed_permanent', 'INVALID_REQUEST')", ("e%d" % n,))
        alpha = self.rig.reporter.report()["agents"]["alpha"]
        self.assertEqual(len(alpha["permanent_failures"]),
                         status_mod.MAX_LISTED_FAILURES)
        self.assertEqual(alpha["permanent_failures_total"],
                         status_mod.MAX_LISTED_FAILURES + 6)


class CredentialHistoryF5(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        one_message(self.rig.journal, self.rig.clock.ms)

    def advice(self, report):
        return [a for a in report["operator_actions"]
                if "check the identity" in a]

    def test_the_advice_is_given_only_while_the_refusal_is_unresolved(self):
        script = Script(error(401, "UNAUTHENTICATED"))
        reporter = self.rig.open(transport=script)
        reporter.cycle()
        report = reporter.report()
        self.assertTrue(report["last_401"]["active"])
        self.assertEqual(len(self.advice(report)), 1)
        self.assertIn("!!", status_mod.render_text(report))
        # the identity is fixed: acquisition, report and final are accepted
        script.answers = [granted(), (200, {}), (200, {})]
        self.rig.clock.advance(3600)
        result = reporter.cycle()
        self.assertEqual(result.sent, 3)
        report = reporter.report()
        self.assertIsNotNone(report["last_401"])           # the history stays
        self.assertEqual(report["last_401"]["code"], "UNAUTHENTICATED")
        self.assertFalse(report["last_401"]["active"])
        self.assertEqual(self.advice(report), [])
        text = status_mod.render_text(report)
        self.assertIn("(history)", text)
        self.assertIn("accepted again since", text)

    def test_a_refused_request_that_is_not_a_401_does_not_clear_it(self):
        script = Script(error(401, "UNAUTHENTICATED"))
        reporter = self.rig.open(transport=script)
        reporter.cycle()
        script.answers = [error(400, "INVALID_REQUEST")]
        self.rig.clock.advance(3600)
        reporter.cycle()
        self.assertTrue(reporter.report()["last_401"]["active"])

    def test_it_survives_a_restart_in_either_state(self):
        script = Script(error(401, "UNAUTHENTICATED"))
        self.rig.open(transport=script)
        self.rig.reporter.cycle()
        self.rig.restart(transport=script)
        self.assertTrue(self.rig.reporter.report()["last_401"]["active"])
        script.answers = [granted(), (200, {}), (200, {})]
        self.rig.clock.advance(3600)
        self.rig.reporter.cycle()
        self.rig.restart(transport=script)
        self.assertFalse(self.rig.reporter.report()["last_401"]["active"])


if __name__ == "__main__":
    unittest.main()
