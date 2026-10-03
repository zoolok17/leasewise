"""Card L6a-1, step 4: gateway receipts are delivered by their own pass, between the limit pass and the
lease pass, to `/v1/leases/{id}/settle` and nowhere else.

Answers: 200 is done; LEASE_CLOSED is done; RECEIPT_CONFLICT and every other refusal fail THAT row for
good with a closed word (never an agent stop, never a rebuild or a drop); 401, 429, 5xx and no answer
leave the row as it is and retry the same bytes. The cycle summary keeps the worse of the passes.
Everything is synthetic: a fake transport and generated ids; no server, no real gateway.
"""

import json
import unittest
import uuid

from quota_balancer.reporter import client, decisions, mapper, state as state_mod
from quota_balancer.reporter.run import CREDENTIAL, UNREACHABLE, _worse

from tests.l6a1_env import GATEWAY_ID, configure, lease, receipt
from tests.reporter_env import Rig
from tests.test_card7a3 import one_message
from tests.test_card7c2_delivery import LIMIT_PATH, body as limit_body

LOUD = "loud-server-word-l6a1"
OK = (200, {"state": "settled", "released_micro_eur": 1, "retained_micro_eur": 2})


class Net:
    """A fake Leasewise: `/v1/leases` answers `acquire`, the limit path `limit`, and every settle path
    `receipt`. Each is a list of (status, doc-or-bytes-or-exception); the last repeats."""

    def __init__(self, receipt=None, acquire=None, limit=None):
        self.receipt = list(receipt or [OK])
        self.acquire = list(acquire or [(429, {"error": {"code": "ACCOUNT_PARKED", "message": "x"}})])
        self.limit = list(limit or [(202, {"block_id": str(uuid.uuid4()), "retry_at": None, "state": "parked"})])
        self.calls = []

    def __call__(self, method, path, headers, raw, timeout):
        self.calls.append((path, headers["Idempotency-Key"], raw))
        script = self.limit if path == LIMIT_PATH else self.acquire if path == "/v1/leases" else self.receipt
        answer = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(answer, BaseException):
            raise answer
        status_code, doc = answer
        return status_code, doc if isinstance(doc, bytes) else json.dumps(doc).encode()

    def paths(self):
        return [c[0] for c in self.calls]

    def settles(self):
        return [c for c in self.calls if c[0].endswith("/settle")]


def error(code, status_code):
    return (status_code, {"error": {"code": code, "message": "x"}})


class _Rig(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        self.leases = []

    def start(self, net, rows=None, count=1, message=False):
        """A reporter whose fixture holds `count` receipts (or `rows`), one per fresh lease it knows."""
        if message:
            one_message(self.rig.journal, self.rig.clock.ms)
        self.net = net
        if rows is None:
            self.leases = [lease() for _ in range(count)]
            rows = [receipt(l, n + 1) for n, l in enumerate(self.leases)]
        configure(self.rig, rows)
        reporter = self.rig.open(transport=net)
        with self.rig.state.txn():
            for lease_id in self.leases:
                self.rig.state.remember_lease(lease_id)
        return reporter

    def row(self, n=1):
        return self.rig.state.one("SELECT state, tries, last_error_code, not_before_ms FROM gateway_receipt "
                                  "WHERE seq=?", (n,))

    def agent_stops(self):
        return self.rig.state.one("SELECT COUNT(*) FROM agent_state")[0]


class Answers(_Rig):

    def test_a_200_settles_the_row_and_the_request_is_exactly_the_stored_one(self):
        reporter = self.start(Net())
        result = reporter.cycle()
        self.assertEqual((result.new_receipts, result.sent, result.blocked), (1, 1, None))
        self.assertEqual(self.row()[0], "sent")
        (path, key, raw), = self.net.settles()
        self.assertEqual(path, "/v1/leases/%s/settle" % self.leases[0])
        stored = self.rig.state.one("SELECT event_id, body FROM gateway_receipt")
        self.assertEqual(key, stored[0])                                       # Idempotency-Key = event_id
        self.assertEqual(raw.decode(), stored[1])                              # the stored bytes, canonical
        sent = json.loads(raw)
        self.assertEqual(set(sent), mapper.RECEIPT_BODY_KEYS)
        self.assertEqual((sent["gateway_id"], sent["cached_input_tokens"], sent["final"], sent["seq"]),
                         (GATEWAY_ID, 0, True, 1))

    def test_lease_closed_is_done(self):
        reporter = self.start(Net(receipt=[error("LEASE_CLOSED", 410)]))
        result = reporter.cycle()
        self.assertEqual((self.row()[0], self.row()[2], result.blocked), ("sent", None, None))
        self.assertEqual(result.sent, 1)

    def test_a_receipt_conflict_fails_that_row_for_good_with_its_word_and_is_never_resent(self):
        reporter = self.start(Net(receipt=[error("RECEIPT_CONFLICT", 409)]), count=2)
        result = reporter.cycle()
        self.assertEqual(self.row(1)[:3], ("failed_permanent", 0, "RECEIPT_CONFLICT"))
        self.assertEqual(self.row(2)[0], "failed_permanent")                    # the same answer for the second
        self.assertEqual((result.blocked, self.agent_stops()), (None, 0))
        calls = len(self.net.calls)
        reporter.cycle()
        self.assertEqual(len(self.net.calls), calls)                           # nothing is sent again

    def test_a_refusal_fails_only_its_row_and_the_next_one_still_goes_out(self):
        reporter = self.start(Net(receipt=[error("INVALID_REQUEST", 400), OK]), count=2)
        reporter.cycle()
        self.assertEqual(self.row(1)[::2], ("failed_permanent", "INVALID_REQUEST"))
        self.assertEqual(self.row(2)[0], "sent")

    def test_the_words_that_stop_an_agent_for_a_lease_only_fail_a_receipt_row(self):
        for code, status_code in (("FORBIDDEN", 403), ("NOT_FOUND", 404), ("POLICY_DENIED", 403),
                                  ("CONTEXT_UNKNOWN", 422), ("ESTIMATE_STALE", 409), ("LEASE_EXPIRED", 410)):
            with self.subTest(code=code):
                rig = Rig(server=False)
                self.addCleanup(rig.cleanup)
                self.rig = rig
                reporter = self.start(Net(receipt=[error(code, status_code)]))
                reporter.cycle()
                self.assertEqual(self.row()[::2], ("failed_permanent", code))
                self.assertEqual(self.agent_stops(), 0)
                self.assertEqual(self.rig.state.one("SELECT COUNT(*) FROM report")[0], 0)      # nothing dropped or built

    def test_an_unknown_word_is_stored_as_unknown_code_never_as_sent(self):
        reporter = self.start(Net(receipt=[error(LOUD, 400)]))
        reporter.cycle()
        self.assertEqual(self.row()[::2], ("failed_permanent", decisions.UNKNOWN_CODE))
        self.assertNotIn(LOUD, json.dumps(self.rig.state.all("SELECT * FROM gateway_receipt")))

    def test_no_answer_5xx_and_429_keep_the_row_and_retry_the_same_bytes(self):
        for answer in (ConnectionRefusedError("down"), (503, {}), (500, b"not json"), error("CONCURRENCY_LIMIT", 429)):
            with self.subTest(answer=repr(answer)[:30]):
                rig = Rig(server=False)
                self.addCleanup(rig.cleanup)
                self.rig = rig
                reporter = self.start(Net(receipt=[answer, OK]))
                result = reporter.cycle()
                self.assertEqual(result.blocked, UNREACHABLE)
                state, tries, code, not_before = self.row()
                self.assertEqual((state, tries), ("pending", 1))
                self.assertGreater(not_before, self.rig.clock.ms)
                self.rig.clock.advance(3600)
                result = reporter.cycle()
                self.assertEqual((result.blocked, self.row()[0]), (None, "sent"))
                first, second = self.net.settles()
                self.assertEqual((first[1], first[2]), (second[1], second[2]))      # the same key and bytes

    def test_a_401_keeps_the_row_then_recovery_sends_it(self):
        reporter = self.start(Net(receipt=[error("UNAUTHENTICATED", 401), OK]))
        result = reporter.cycle()
        self.assertEqual(result.blocked, CREDENTIAL)
        self.assertEqual(self.row()[:2], ("pending", 1))
        self.assertIsNotNone(self.rig.state.note(state_mod.NOTE_CREDENTIAL_ACTIVE))
        self.rig.clock.advance(3600)
        result = reporter.cycle()
        self.assertEqual((result.blocked, self.row()[0]), (None, "sent"))
        self.assertIsNone(self.rig.state.note(state_mod.NOTE_CREDENTIAL_ACTIVE))   # the fault ended with the answer
        self.assertIsNotNone(self.rig.state.note(state_mod.NOTE_LAST_401))          # and the history stays

    def test_a_second_cycle_sends_nothing_new(self):
        reporter = self.start(Net())
        reporter.cycle()
        calls = len(self.net.calls)
        result = reporter.cycle()
        self.assertEqual((result.new_receipts, result.sent, len(self.net.calls)), (0, 0, calls))


class Order(_Rig):

    def test_the_passes_run_limit_then_receipt_then_lease(self):
        reporter = self.start(Net(), message=True)
        self.rig.state.add_limit_event("claude-main", limit_body(self.rig.clock.ms), self.rig.clock.ms)
        reporter.cycle()
        paths = self.net.paths()
        self.assertEqual(paths[0], LIMIT_PATH)
        self.assertTrue(paths[1].endswith("/settle"))
        self.assertEqual(paths[2], "/v1/leases")

    def test_a_lease_that_is_refused_never_delays_a_receipt(self):
        reporter = self.start(Net(), message=True)                 # every lease request: 429 ACCOUNT_PARKED
        result = reporter.cycle()
        self.assertEqual(self.row()[0], "sent")                    # delivered in the SAME cycle
        self.assertEqual(result.blocked, UNREACHABLE)              # the lease pass was refused, as before

    def test_a_receipt_that_is_retried_does_not_stop_the_lease_pass_or_the_limit_pass(self):
        reporter = self.start(Net(receipt=[(503, {})]), message=True)
        self.rig.state.add_limit_event("claude-main", limit_body(self.rig.clock.ms), self.rig.clock.ms)
        reporter.cycle()
        self.assertEqual(self.rig.state.one("SELECT state FROM limit_event")[0], "sent")
        self.assertIn("/v1/leases", self.net.paths())                          # the lease pass still ran
        self.assertEqual(self.row()[0], "pending")

    def test_a_refused_limit_row_or_a_stopped_agent_never_holds_a_receipt_back(self):
        net = Net(limit=[error("FORBIDDEN", 403)], acquire=[error("FORBIDDEN", 403)])
        reporter = self.start(net, message=True)
        self.rig.state.add_limit_event("claude-main", limit_body(self.rig.clock.ms), self.rig.clock.ms)
        reporter.cycle()
        self.assertEqual(self.row()[0], "sent")
        self.assertEqual(self.agent_stops(), 1)                               # the lease agent stopped; the receipt did not

    def test_the_summary_keeps_the_worse_outcome_whichever_pass_ran_last(self):
        cases = (
            (error("UNAUTHENTICATED", 401), [(503, {})], CREDENTIAL),       # receipt 401, lease down
            ([(503, {})], error("UNAUTHENTICATED", 401), CREDENTIAL),        # receipt down, lease 401
            ([(503, {})], [(503, {})], UNREACHABLE),
            (OK, [(503, {})], UNREACHABLE),
            (OK, error("UNAUTHENTICATED", 401), CREDENTIAL),
        )
        for receipt_answer, lease_answer, expected in cases:
            with self.subTest(expected=expected):
                rig = Rig(server=False)
                self.addCleanup(rig.cleanup)
                self.rig = rig
                receipt_script = receipt_answer if isinstance(receipt_answer, list) else [receipt_answer]
                lease_script = lease_answer if isinstance(lease_answer, list) else [lease_answer]
                reporter = self.start(Net(receipt=receipt_script, acquire=lease_script), message=True)
                self.assertEqual(reporter.cycle().blocked, expected)

    def test_the_ranking_is_complete(self):
        for a in (None, UNREACHABLE, CREDENTIAL):
            for b in (None, UNREACHABLE, CREDENTIAL):
                expected = CREDENTIAL if CREDENTIAL in (a, b) else UNREACHABLE if UNREACHABLE in (a, b) else None
                self.assertEqual(_worse(a, b), expected, (a, b))


class Table(unittest.TestCase):
    """`decisions.decide` for a receipt, answer by answer."""

    def decide(self, status_code, code=None):
        return decisions.decide(decisions.RECEIPT, decisions.Response(status_code, code))

    def test_the_table(self):
        self.assertEqual(self.decide(200), decisions.Decision(decisions.DONE))
        self.assertEqual(self.decide(201), decisions.Decision(decisions.DONE))
        self.assertEqual(self.decide(410, "LEASE_CLOSED"), decisions.Decision(decisions.DONE))
        self.assertEqual(self.decide(409, "RECEIPT_CONFLICT"), decisions.Decision(decisions.FAIL, "RECEIPT_CONFLICT"))
        self.assertEqual(self.decide(400, "INVALID_REQUEST"), decisions.Decision(decisions.FAIL, "INVALID_REQUEST"))
        self.assertEqual(self.decide(404, None), decisions.Decision(decisions.FAIL, decisions.UNKNOWN_CODE))
        self.assertEqual(self.decide(202), decisions.Decision(decisions.FAIL, decisions.UNKNOWN_CODE))
        self.assertEqual(self.decide(401), decisions.Decision(decisions.RETRY, "UNAUTHENTICATED"))
        self.assertEqual(self.decide(429).action, decisions.RETRY)
        self.assertEqual(self.decide(503).action, decisions.RETRY)
        self.assertEqual(self.decide(None).action, decisions.RETRY)

    def test_no_answer_to_a_receipt_ever_stops_an_agent_or_rebuilds_or_drops(self):
        for status_code in (400, 403, 404, 409, 410, 422):
            for code in decisions.CLOSED_CODES:
                action = self.decide(status_code, code).action
                self.assertIn(action, (decisions.DONE, decisions.FAIL), (status_code, code))


class Pair(unittest.TestCase):
    """The client checks the kind and the path as a PAIR: a receipt goes to one settle path only."""

    def test_the_receipt_pair(self):
        good = "/v1/leases/%s/settle" % uuid.uuid4()
        self.assertTrue(client._allowed(decisions.RECEIPT, good))
        for bad in (LIMIT_PATH, "/v1/leases", "/v1/leases/", "/v1/leases/x/settle", good + "/", good + "x",
                    "/v1/leases/%s" % uuid.uuid4(), "/v1/leases/%s/check" % uuid.uuid4(),
                    "/v1/leases/%s/settle" % str(uuid.uuid4()).upper(), "//v1/leases/%s/settle" % uuid.uuid4(),
                    "/v1/limit-events/%s/settle" % uuid.uuid4(), ""):
            self.assertFalse(client._allowed(decisions.RECEIPT, bad), bad)

    def test_nothing_else_reaches_the_limit_path_and_a_limit_row_never_reaches_a_settle_path(self):
        good = "/v1/leases/%s/settle" % uuid.uuid4()
        for kind in (decisions.ACQUIRE, decisions.REPORT, decisions.FINAL, decisions.RECEIPT):
            self.assertFalse(client._allowed(kind, LIMIT_PATH), kind)
        self.assertFalse(client._allowed(decisions.LIMIT, good))
        self.assertTrue(client._allowed(decisions.LIMIT, LIMIT_PATH))
        self.assertTrue(client._allowed(decisions.REPORT, good))                       # as before

    def test_a_refused_pair_sends_nothing(self):
        called = []
        http = client.HttpClient("http", "127.0.0.1", 9, "FAKE-IDENTITY-pair", transport=lambda *a: called.append(a))
        item = state_mod.Item(decisions.RECEIPT, state_mod.RECEIPT_AGENT, "", LIMIT_PATH, {}, "k", ("gateway_receipt", 1), None)
        answer = http.send(item)
        self.assertEqual((answer.status, answer.code, called), (None, decisions.BAD_ANSWER, []))

    def test_a_receipt_answer_is_its_status_and_its_word_only(self):
        for doc in ({"lease_id": str(uuid.uuid4()), "state": "settled"}, {"error": {"code": "RECEIPT_CONFLICT"}}):
            answer = client._response(decisions.RECEIPT, 200 if "state" in doc else 409, json.dumps(doc).encode())
            self.assertIsNone(answer.lease_id)
        self.assertEqual(client._response(decisions.RECEIPT, 409, json.dumps({"error": {"code": "RECEIPT_CONFLICT"}})
                                          .encode()).code, "RECEIPT_CONFLICT")
        self.assertEqual(client._response(decisions.RECEIPT, 400, json.dumps({"error": {"code": LOUD}}).encode()).code,
                         decisions.UNKNOWN_CODE)


if __name__ == "__main__":
    unittest.main()
