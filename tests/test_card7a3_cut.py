"""Card 7a-3, scope cut after fix round 2: only a JSON object is an answer, and
an identity may not be, or be part of, a public error word.

Synthetic data; a fake transport; identities are obvious fakes.
"""

import unittest

from quota_balancer.reporter import config as cfg
from quota_balancer.reporter import decisions
from quota_balancer.reporter.client import HttpClient
from quota_balancer.reporter.state import Item

from tests.reporter_env import Rig
from tests.test_card7a3 import Script, granted, one_message

NON_OBJECTS = (b"null", b"[]", b"[1, 2]", b"42", b"true", b"false", b'"answer"')


def send(kind, status, raw):
    client = HttpClient("http", "127.0.0.1", 9, "FAKE-IDENTITY-0001",
                        transport=lambda *a: (status, raw))
    item = Item(kind, "alpha", "m", "/v1/leases", {}, "k", ("report", 1), None)
    return decisions.decide(kind, client.send(item))


class OnlyAnObjectIsAnAnswer(unittest.TestCase):
    def test_a_non_object_success_is_a_retry_for_every_kind(self):
        for kind in (decisions.ACQUIRE, decisions.REPORT, decisions.FINAL):
            for raw in NON_OBJECTS:
                for status in (200, 201):
                    with self.subTest(kind=kind, raw=raw, status=status):
                        self.assertEqual(send(kind, status, raw).action,
                                         decisions.RETRY)

    def test_401_429_5xx_stay_retries(self):
        for status in (401, 429, 500, 503):
            for raw in NON_OBJECTS:
                with self.subTest(status=status, raw=raw):
                    self.assertEqual(
                        send(decisions.FINAL, status, raw).action,
                        decisions.RETRY)

    def test_other_4xx_keeps_the_permanent_handling_with_no_loop(self):
        for status in (400, 403, 404, 409, 422):
            for raw in NON_OBJECTS:
                with self.subTest(status=status, raw=raw):
                    self.assertNotEqual(
                        send(decisions.FINAL, status, raw).action,
                        decisions.RETRY)

    def test_an_object_without_the_expected_fields_is_still_accepted(self):
        for kind in (decisions.REPORT, decisions.FINAL):
            self.assertEqual(send(kind, 200, b"{}").action, decisions.DONE)

    def test_a_report_and_a_final_stay_pending_and_resend_the_same_bytes(self):
        for which in ("report", "final"):
            for raw in NON_OBJECTS:
                for status in (200, 201):
                    with self.subTest(which=which, raw=raw, status=status):
                        rig = Rig(server=False)
                        self.addCleanup(rig.cleanup)
                        one_message(rig.journal, rig.clock.ms)
                        answers = [granted(), (200, {}), (200, {})]
                        answers[1 if which == "report" else 2] = (status, raw)
                        script = Script(*answers)
                        rig.open(transport=script)
                        rig.reporter.cycle()
                        state, tries = rig.state.one(
                            "SELECT state, tries FROM %s" % which)
                        self.assertEqual((state, tries), ("pending", 1))
                        rig.clock.advance(3600)
                        again = Script(granted(), (200, {}), (200, {}))
                        rig.restart(transport=again)
                        rig.reporter.cycle()
                        sent = script.sent[1 if which == "report" else 2]
                        resent = [s for s in again.sent
                                  if s["body"] == sent["body"]]
                        self.assertTrue(resent)

    def test_a_final_answered_with_null_is_not_stored_as_sent(self):
        rig = Rig(server=False)
        self.addCleanup(rig.cleanup)
        one_message(rig.journal, rig.clock.ms)
        rig.open(transport=Script(granted(), (200, {}), (200, b"null")))
        rig.reporter.cycle()
        self.assertEqual(rig.state.one("SELECT state FROM final")[0], "pending")


class IdentityVersusPublicWords(unittest.TestCase):
    def refused(self, value):
        with self.assertRaises(Exception) as caught:
            cfg._identity(value)
        self.assertNotIn(value, str(caught.exception))

    def test_an_identity_equal_to_a_closed_code_is_refused(self):
        for word in sorted(decisions.CLOSED_CODES):
            with self.subTest(word=word):
                self.refused(word)
                self.refused(word.lower())

    def test_an_identity_inside_a_closed_code_is_refused(self):
        self.refused("CONTEXT_UNK")
        self.refused("unknown")
        self.refused("MALFORMED")

    def test_a_normal_long_identity_is_accepted(self):
        for value in ("11111111-2222-4333-8444-555555555555",
                      "FAKE-IDENTITY-0001-abcdefghijklmnop"):
            self.assertEqual(cfg._identity(value), value)


if __name__ == "__main__":
    unittest.main()
