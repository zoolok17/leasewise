"""Card 7c-2, step 2: what the reporter does with Leasewise's answer to a LIMIT event.

A limit event is account-level: it belongs to no seat and no message. So a refusal of it can only
fail that one row. It never stops an agent and never stops the reporter. 200, 201 and 202 all mean
"recorded" for a limit event; 202 is still NOT success for the lease kinds (those routes never
answer 202). The credential rules (401) and the retry rules (no answer, 5xx, 429) are the same for
every kind. Synthetic data only.
"""

import unittest

from quota_balancer.reporter import decisions
from quota_balancer.reporter.decisions import Response, decide


def answer(status, code=None):
    return Response(status, code)


class LimitKind(unittest.TestCase):

    def test_the_kind_word_exists_and_is_distinct(self):
        self.assertEqual(decisions.LIMIT, "limit")
        self.assertEqual(len({decisions.ACQUIRE, decisions.REPORT, decisions.FINAL, decisions.LIMIT}), 4)

    def test_200_201_and_202_are_done(self):
        for status in (200, 201, 202):
            with self.subTest(status=status):
                self.assertEqual(decide(decisions.LIMIT, answer(status)), decisions.Decision(decisions.DONE))

    def test_202_is_still_not_success_for_the_lease_kinds(self):
        for kind in (decisions.ACQUIRE, decisions.REPORT, decisions.FINAL):
            with self.subTest(kind=kind):
                self.assertNotEqual(decide(kind, answer(202)).action, decisions.DONE)

    def test_the_old_success_statuses_are_unchanged_for_the_lease_kinds(self):
        for kind in (decisions.ACQUIRE, decisions.REPORT, decisions.FINAL):
            for status in (200, 201):
                with self.subTest(kind=kind, status=status):
                    self.assertEqual(decide(kind, answer(status)).action, decisions.DONE)

    def test_no_answer_5xx_and_429_are_retried(self):
        for status in (None, 500, 502, 503, 429):
            with self.subTest(status=status):
                self.assertEqual(decide(decisions.LIMIT, answer(status)).action, decisions.RETRY)

    def test_401_keeps_the_credential_rule(self):
        self.assertEqual(decide(decisions.LIMIT, answer(401)), decisions.Decision(decisions.RETRY, "UNAUTHENTICATED"))
        self.assertEqual(decide(decisions.LIMIT, answer(401, "UNAUTHENTICATED")),
                         decisions.Decision(decisions.RETRY, "UNAUTHENTICATED"))

    def test_other_4xx_fail_that_row_only_and_keep_the_closed_code(self):
        cases = ((400, "INVALID_REQUEST"), (403, "FORBIDDEN"), (404, "NOT_FOUND"),
                 (409, "IDEMPOTENCY_CONFLICT"), (422, None))
        for status, code in cases:
            with self.subTest(status=status):
                decision = decide(decisions.LIMIT, answer(status, code))
                self.assertEqual(decision.action, decisions.FAIL)
                self.assertEqual(decision.code, code)

    def test_a_limit_row_is_never_an_agent_stop_a_rebuild_or_a_drop(self):
        # FORBIDDEN, NOT_FOUND and CONTEXT_UNKNOWN stop an AGENT for a lease; a limit event has no agent.
        for code in ("FORBIDDEN", "NOT_FOUND", "CONTEXT_UNKNOWN", "POLICY_DENIED", "ESTIMATE_STALE",
                     "LEASE_EXPIRED", "RECEIPT_CONFLICT", "LEASE_CLOSED"):
            with self.subTest(code=code):
                self.assertEqual(decide(decisions.LIMIT, answer(400, code)).action, decisions.FAIL)
        # and the lease kinds keep their agent stops
        self.assertEqual(decide(decisions.ACQUIRE, answer(403, "FORBIDDEN")).action, decisions.FAIL_AGENT)

    def test_an_unknown_error_word_becomes_the_unknown_code(self):
        decision = decide(decisions.LIMIT, answer(400, "SOMETHING-THE-SERVER-ECHOED"))
        self.assertEqual((decision.action, decision.code), (decisions.FAIL, decisions.UNKNOWN_CODE))


if __name__ == "__main__":
    unittest.main()
