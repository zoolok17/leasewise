"""Card 7c-2, step 2: the client sends a limit event only to /v1/limit-events, and only a limit row can.

The path and the kind are checked as a PAIR. Every other path stays refused as before; the limit
path is refused for every lease kind. A limit answer is read for its status and its error word
only: no field of a 202 body is kept. Synthetic data; a recording fake transport.
"""

import json
import os
import tempfile
import unittest
import uuid

from quota_balancer.reporter import client as client_mod
from quota_balancer.reporter import decisions
from quota_balancer.reporter import state as state_mod
from quota_balancer.reporter.state import State

IDENTITY = "FAKE-IDENTITY-reporter-test-0001"
NOW = 1_788_224_400_000
LIMIT_PATH = "/v1/limit-events"


class Recorder:
    """A fake transport: records the raw bytes it was given, answers from a script."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, path, headers, body, timeout):
        self.calls.append({"method": method, "path": path, "headers": dict(headers), "body": body})
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        status, doc = answer
        if status is None:
            raise OSError("no answer")
        return status, doc if isinstance(doc, bytes) else json.dumps(doc).encode()


def make_client(transport):
    return client_mod.HttpClient("http", "127.0.0.1", 9, IDENTITY, transport=transport)


def limit_item(path=LIMIT_PATH, kind=decisions.LIMIT, body=None, key=None):
    body = body if body is not None else {"event_id": key or str(uuid.uuid4())}
    return state_mod.Item(kind, state_mod.LIMIT_AGENT, "", path, body, key or body.get("event_id", "k"),
                          ("limit_event", 1), None)


def lease_item(kind, path):
    return state_mod.Item(kind, "alpha", "m1", path, {}, "k", ("report", 1), None)


class PathPair(unittest.TestCase):

    def test_a_limit_row_is_sent_to_exactly_the_limit_path(self):
        rec = Recorder((202, {"block_id": "x", "retry_at": None, "state": "parked"}))
        answer = make_client(rec).send(limit_item())
        self.assertEqual(answer.status, 202)
        self.assertEqual([c["path"] for c in rec.calls], [LIMIT_PATH])
        self.assertEqual(rec.calls[0]["method"], "POST")

    def test_a_limit_row_to_any_other_path_is_refused_and_nothing_is_sent(self):
        settle = "/v1/leases/%s/settle" % uuid.uuid4()
        for path in ("/v1/leases", settle, "/v1/readings", "/v1/status", LIMIT_PATH + "/", LIMIT_PATH + "/x",
                     LIMIT_PATH + "?a=1", LIMIT_PATH + "#x", "//v1/limit-events", "/v1/limit-events/../leases",
                     "/V1/limit-events", "v1/limit-events", "http://evil.invalid/v1/limit-events", ""):
            with self.subTest(path=path):
                rec = Recorder((202, {}))
                answer = make_client(rec).send(limit_item(path=path))
                self.assertEqual((answer.status, answer.code), (None, client_mod.BAD_ANSWER))
                self.assertEqual(rec.calls, [])

    def test_no_lease_kind_can_reach_the_limit_path(self):
        for kind in (decisions.ACQUIRE, decisions.REPORT, decisions.FINAL):
            for path in (LIMIT_PATH, LIMIT_PATH + "/", "/v1/limit-events/x"):
                with self.subTest(kind=kind, path=path):
                    rec = Recorder((202, {}))
                    answer = make_client(rec).send(lease_item(kind, path))
                    self.assertEqual((answer.status, answer.code), (None, client_mod.BAD_ANSWER))
                    self.assertEqual(rec.calls, [])

    def test_the_lease_paths_still_work_for_the_lease_kinds_and_not_for_limit_rows(self):
        lease = str(uuid.uuid4())
        for kind, path in ((decisions.ACQUIRE, "/v1/leases"), (decisions.REPORT, "/v1/leases/%s/settle" % lease),
                           (decisions.FINAL, "/v1/leases/%s/settle" % lease)):
            rec = Recorder((200, {"lease_id": lease}))
            make_client(rec).send(lease_item(kind, path))
            self.assertEqual(len(rec.calls), 1, kind)


class Bytes(unittest.TestCase):

    def test_the_wire_bytes_are_the_stored_bytes_on_every_try(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("AGENTTALK_SCRATCH") or None) as tmp:
            state = State(os.path.join(tmp, "r.db"))
            try:
                body = {"event_id": str(uuid.uuid4()), "account_id": str(uuid.uuid4()),
                        "turn_id": str(uuid.uuid4()), "observed_at": NOW, "evidence_kind": "claude_exhausted",
                        "resets_at": NOW + 5_000, "exhausted_windows": ["five_hour"]}
                state.add_limit_event("claude-main", body, NOW)
                stored = state.one("SELECT body FROM limit_event")[0].encode("utf-8")
                rec = Recorder((503, {}), (None, {}), (202, {"state": "parked"}))
                client = make_client(rec)
                answers = []
                for _ in range(3):
                    item = state.next_limit_item(NOW)
                    answer = client.send(item)
                    answers.append(answer.status)
                    state.apply(item, answer, NOW, 0.0)
                    state.conn.execute("UPDATE limit_event SET not_before_ms=0 WHERE state='pending'")
                self.assertEqual(answers, [503, None, 202])
                self.assertEqual(state.one("SELECT state, tries FROM limit_event"), ("sent", 2))
                self.assertEqual([c["body"] for c in rec.calls], [stored] * 3)
                self.assertEqual({c["headers"]["Idempotency-Key"] for c in rec.calls}, {body["event_id"]})
                self.assertEqual({c["headers"]["X-Machine-Identity"] for c in rec.calls}, {IDENTITY})
            finally:
                state.close()


class Answers(unittest.TestCase):

    def send(self, answer, **kw):
        return make_client(Recorder(answer)).send(limit_item(**kw))

    def test_a_202_is_read_for_its_status_only(self):
        answer = self.send((202, {"block_id": str(uuid.uuid4()), "retry_at": NOW, "state": "parked",
                                  "lease_id": str(uuid.uuid4())}))
        self.assertEqual((answer.status, answer.code, answer.lease_id), (202, None, None))

    def test_a_202_with_an_unusable_body_is_no_answer_and_is_retried(self):
        for raw in (b"", b"not json", b"[]", b"null", b"\xff\xfe", b"x" * (client_mod.MAX_ANSWER_BYTES + 1)):
            with self.subTest(raw=raw[:8]):
                answer = self.send((202, raw))
                self.assertEqual((answer.status, answer.code), (None, client_mod.MALFORMED_RESPONSE))
                self.assertEqual(decisions.decide(decisions.LIMIT, answer).action, decisions.RETRY)

    def test_an_error_answer_keeps_only_the_closed_word(self):
        answer = self.send((400, {"error": {"code": "INVALID_REQUEST", "message": "x"}}))
        self.assertEqual((answer.status, answer.code), (400, "INVALID_REQUEST"))
        marker = "SERVER-ECHOED-THE-EVENT-ID-123"
        answer = self.send((400, {"error": {"code": marker, "message": marker}}))
        self.assertEqual((answer.status, answer.code), (400, decisions.UNKNOWN_CODE))
        self.assertNotIn(marker, repr(vars(answer)) if hasattr(answer, "__dict__") else str(
            (answer.status, answer.code, answer.lease_id)))

    def test_401_and_429_and_5xx_keep_their_old_reading(self):
        self.assertEqual(self.send((401, {"error": {"code": "UNAUTHENTICATED"}})).status, 401)
        self.assertEqual(self.send((429, b"")).status, 429)
        self.assertEqual(self.send((503, b"")).code, client_mod.MALFORMED_RESPONSE)

    def test_a_transport_failure_keeps_no_text(self):
        marker = "EXCEPTION-TEXT-WITH-BODY-VALUE"
        answer = make_client(Recorder(RuntimeError(marker))).send(limit_item())
        self.assertEqual((answer.status, answer.code, answer.lease_id), (None, None, None))

    def test_a_lease_kind_still_reads_its_lease_id_and_202_is_not_a_grant(self):
        lease = str(uuid.uuid4())
        rec = Recorder((200, {"lease_id": lease}))
        answer = make_client(rec).send(lease_item(decisions.ACQUIRE, "/v1/leases"))
        self.assertEqual((answer.status, answer.lease_id), (200, lease))
        rec = Recorder((202, {"lease_id": lease}))
        answer = make_client(rec).send(lease_item(decisions.ACQUIRE, "/v1/leases"))
        self.assertNotEqual(decisions.decide(decisions.ACQUIRE, answer).action, decisions.DONE)


READINGS_PATH = "/v1/readings"


def reading_item(path=READINGS_PATH, kind=decisions.READING, key=None):
    body = {"event_id": key or str(uuid.uuid4())}
    return state_mod.Item(kind, state_mod.READING_AGENT, "", path, body, body["event_id"], ("reading", 1), None)


class ReadingPair(unittest.TestCase):
    """Card 7b: a reading row goes to exactly /v1/readings and nothing else can; the answer is its status and word only."""

    def test_a_reading_row_is_sent_to_exactly_the_readings_path(self):
        rec = Recorder((202, {"event_id": "x", "accepted": True}))
        answer = make_client(rec).send(reading_item())
        self.assertEqual(answer.status, 202)
        self.assertEqual([c["path"] for c in rec.calls], [READINGS_PATH])
        self.assertEqual(rec.calls[0]["method"], "POST")

    def test_a_reading_row_to_any_other_path_is_refused_and_nothing_is_sent(self):
        settle = "/v1/leases/%s/settle" % uuid.uuid4()
        for path in ("/v1/leases", settle, LIMIT_PATH, "/v1/status", READINGS_PATH + "/", READINGS_PATH + "/x",
                     READINGS_PATH + "?x", READINGS_PATH + "?a=1", READINGS_PATH + "#x", "//v1/readings", "/v1/readings/../leases",
                     "/V1/readings", "v1/readings", "http://evil.invalid/v1/readings", ""):
            with self.subTest(path=path):
                rec = Recorder((202, {}))
                answer = make_client(rec).send(reading_item(path=path))
                self.assertEqual((answer.status, answer.code), (None, client_mod.BAD_ANSWER))
                self.assertEqual(rec.calls, [])

    def test_no_other_kind_can_reach_the_readings_path(self):
        for kind in (decisions.ACQUIRE, decisions.REPORT, decisions.FINAL, decisions.RECEIPT, decisions.LIMIT):
            for path in (READINGS_PATH, READINGS_PATH + "/x", READINGS_PATH + "?x"):
                with self.subTest(kind=kind, path=path):
                    rec = Recorder((202, {}))
                    answer = make_client(rec).send(lease_item(kind, path))
                    self.assertEqual((answer.status, answer.code), (None, client_mod.BAD_ANSWER))
                    self.assertEqual(rec.calls, [])

    def test_a_reading_row_cannot_use_a_lease_or_limit_path(self):
        lease = str(uuid.uuid4())
        for path in ("/v1/leases", "/v1/leases/%s/settle" % lease, LIMIT_PATH):
            rec = Recorder((200, {"lease_id": lease}))
            self.assertEqual(make_client(rec).send(reading_item(path=path)).code, client_mod.BAD_ANSWER)
            self.assertEqual(rec.calls, [])

    def test_a_202_is_read_for_its_status_only(self):
        answer = make_client(Recorder((202, {"event_id": str(uuid.uuid4()), "accepted": True, "lease_id": str(uuid.uuid4()),
                                             "state": "x"}))).send(reading_item())
        self.assertEqual((answer.status, answer.code, answer.lease_id), (202, None, None))

    def test_a_202_with_an_unusable_body_is_no_answer_and_is_retried(self):
        for raw in (b"", b"not json", b"[]", b"null", b"\xff\xfe", b"x" * (client_mod.MAX_ANSWER_BYTES + 1)):
            with self.subTest(raw=raw[:8]):
                answer = make_client(Recorder((202, raw))).send(reading_item())
                self.assertEqual((answer.status, answer.code), (None, client_mod.MALFORMED_RESPONSE))
                self.assertEqual(decisions.decide(decisions.READING, answer).action, decisions.RETRY)

    def test_an_error_answer_keeps_only_the_closed_word(self):
        answer = make_client(Recorder((403, {"error": {"code": "FORBIDDEN", "message": "LOUD"}}))).send(reading_item())
        self.assertEqual((answer.status, answer.code), (403, "FORBIDDEN"))
        answer = make_client(Recorder((403, {"error": {"code": IDENTITY, "message": "x"}}))).send(reading_item())
        self.assertEqual(answer.code, decisions.UNKNOWN_CODE)

    def test_the_wire_bytes_and_headers_are_the_stored_ones(self):
        rec = Recorder((202, {}))
        item = reading_item(key="11111111-1111-4111-8111-111111111111")
        make_client(rec).send(item)
        call = rec.calls[0]
        self.assertEqual(call["headers"]["Idempotency-Key"], item.key)
        self.assertEqual(call["headers"]["X-Machine-Identity"], IDENTITY)
        self.assertEqual(json.loads(call["body"]), item.body)

    def test_the_money_readers_own_lane_is_untouched(self):
        rec = Recorder((202, {"x": 1}))
        status, doc, code = make_client(rec).post_reading("k", b"{}")
        self.assertEqual((status, doc, code), (202, {"x": 1}, None))
        self.assertEqual(rec.calls[0]["path"], READINGS_PATH)


if __name__ == "__main__":
    unittest.main()
