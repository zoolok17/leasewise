"""Card 7c-2, step 2: limit events are delivered by their own pass, run FIRST.

A lease answer of "retry" or 429 (a parked account refuses every lease request) must never delay a
limit row, and a refused limit row must never stop an agent or the reporter. Everything is
synthetic: a fake transport and generated ids; no server, no real provider.
"""

import json
import uuid
import unittest

from quota_balancer.reporter import decisions, status
from quota_balancer.reporter.run import CREDENTIAL, UNREACHABLE

from tests.reporter_env import Rig
from tests.test_card7a3 import one_message

LIMIT_PATH = "/v1/limit-events"
ALIAS = "claude-main"
MARKER = "LOUD-MARKER-DO-NOT-LEAK"


def body(now_ms, **over):
    value = {"event_id": str(uuid.uuid4()), "account_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()),
             "observed_at": now_ms - 1_000, "evidence_kind": "claude_exhausted",
             "resets_at": now_ms + 3_600_000, "exhausted_windows": ["five_hour"]}
    value.update(over)
    return value


class Net:
    """A fake Leasewise: lease paths answer `lease`, the limit path answers `limit`. Both are lists
    of (status, doc-or-bytes-or-exception); the last repeats."""

    def __init__(self, lease=None, limit=None):
        self.lease = list(lease or [(429, {"error": {"code": "ACCOUNT_PARKED", "message": "x"}})])
        self.limit = list(limit or [(202, {"block_id": str(uuid.uuid4()), "retry_at": None, "state": "parked"})])
        self.calls = []

    def __call__(self, method, path, headers, raw, timeout):
        self.calls.append((path, headers["Idempotency-Key"], raw))
        script = self.limit if path == LIMIT_PATH else self.lease
        answer = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(answer, BaseException):
            raise answer
        status_code, doc = answer
        return status_code, doc if isinstance(doc, bytes) else json.dumps(doc).encode()

    def paths(self):
        return [c[0] for c in self.calls]


class _Rig(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)

    def start(self, net, message=True):
        if message:
            one_message(self.rig.journal, self.rig.clock.ms)
        self.net = net
        reporter = self.rig.open(transport=net)
        return reporter

    def put(self, **over):
        doc = body(self.rig.clock.ms, **over)
        self.assertTrue(self.rig.state.add_limit_event(ALIAS, doc, self.rig.clock.ms))
        return doc

    def row(self, event_id):
        return self.rig.state.one("SELECT state, tries, last_error_code, not_before_ms FROM limit_event "
                                  "WHERE event_id=?", (event_id,))

    def acquisition(self):
        return self.rig.state.one("SELECT state, tries, last_error_code FROM acquisition")


class Order(_Rig):

    def test_a_parked_account_refusing_every_lease_never_delays_a_limit_row(self):
        reporter = self.start(Net())                              # every lease request: 429 ACCOUNT_PARKED
        doc = self.put()
        result = reporter.cycle()
        self.assertEqual(self.row(doc["event_id"])[0], "sent")    # delivered in the SAME cycle
        self.assertEqual(self.net.paths()[0], LIMIT_PATH)         # the limit pass ran first
        self.assertEqual(result.blocked, UNREACHABLE)             # the lease pass was refused, as before
        self.assertEqual(self.acquisition()[:2], ("pending", 1))

    def test_a_row_stored_while_the_lease_queue_is_backing_off_is_still_delivered(self):
        reporter = self.start(Net())
        reporter.cycle()                                          # the lease is refused and backs off
        first = self.acquisition()
        doc = self.put()
        reporter.cycle()
        self.assertEqual(self.row(doc["event_id"])[0], "sent")
        self.assertEqual(self.acquisition(), first)               # the lease item was not due: untouched

    def test_a_denied_lease_acquisition_that_stops_its_agent_never_blocks_a_limit_row(self):
        reporter = self.start(Net(lease=[(403, {"error": {"code": "FORBIDDEN", "message": "x"}})]))
        doc = self.put()
        reporter.cycle()
        self.assertEqual(self.row(doc["event_id"])[0], "sent")
        self.assertEqual(self.rig.state.one("SELECT COUNT(*) FROM agent_state")[0], 1)    # the AGENT stopped
        later = self.put()
        reporter.cycle()
        self.assertEqual(self.row(later["event_id"])[0], "sent")                           # the limit still flows

    def test_several_rows_go_out_oldest_first_in_one_cycle(self):
        reporter = self.start(Net(), message=False)
        docs = [self.put() for _ in range(3)]
        result = reporter.cycle()
        self.assertEqual(result.sent, 3)
        self.assertEqual([c[1] for c in self.net.calls], [d["event_id"] for d in docs])

    def test_a_refused_row_fails_alone_and_the_next_one_still_goes(self):
        net = Net(limit=[(400, {"error": {"code": "INVALID_REQUEST", "message": "x"}}), (202, {})])
        reporter = self.start(net, message=False)
        first, second = self.put(), self.put()
        result = reporter.cycle()
        self.assertEqual(self.row(first["event_id"])[:3], ("failed_permanent", 0, "INVALID_REQUEST"))
        self.assertEqual(self.row(second["event_id"])[0], "sent")
        self.assertIsNone(result.blocked)
        self.assertEqual(self.rig.state.one("SELECT COUNT(*) FROM agent_state")[0], 0)

    def test_a_limit_row_that_gets_no_answer_stops_the_cycle_before_the_lease_pass(self):
        net = Net(limit=[(None, {})])
        reporter = self.start(net)
        doc = self.put()
        net.limit = [OSError("down")]
        result = reporter.cycle()
        self.assertEqual(result.blocked, UNREACHABLE)
        self.assertEqual(self.net.paths(), [LIMIT_PATH])          # the lease pass met the same wall: not tried
        self.assertEqual(self.row(doc["event_id"])[:2], ("pending", 1))
        self.assertEqual(self.acquisition()[:2], ("pending", 0))

    def test_a_row_waiting_out_its_own_back_off_is_not_resent_but_the_lease_pass_runs(self):
        net = Net(limit=[(503, {})])
        reporter = self.start(net)
        doc = self.put()
        reporter.cycle()                                          # 503: back-off, and the lease pass was skipped
        calls = len(net.calls)
        net.limit = [(202, {})]
        result = reporter.cycle()                                 # still inside its back-off
        self.assertEqual(self.row(doc["event_id"])[0], "pending")
        self.assertNotIn(LIMIT_PATH, net.paths()[calls:])
        self.assertIn("/v1/leases", net.paths()[calls:])          # the lease pass was not held by it
        self.assertEqual(result.blocked, UNREACHABLE)
        self.rig.clock.advance(1_000)
        reporter.cycle()
        self.assertEqual(self.row(doc["event_id"])[0], "sent")

    def test_a_credential_refusal_is_a_credential_block_and_loses_nothing(self):
        net = Net(limit=[(401, {"error": {"code": "UNAUTHENTICATED", "message": "x"}})])
        reporter = self.start(net, message=False)
        doc = self.put()
        result = reporter.cycle()
        self.assertEqual(result.blocked, CREDENTIAL)
        self.assertEqual(self.row(doc["event_id"])[0], "pending")
        self.assertIsNotNone(self.rig.state.note("credential_active"))
        net.limit = [(202, {})]
        self.rig.clock.advance(900_000)
        reporter.cycle()
        self.assertEqual(self.row(doc["event_id"])[0], "sent")
        self.assertIsNone(self.rig.state.note("credential_active"))


class Bytes(_Rig):

    def test_every_try_resends_the_stored_bytes_and_a_restart_changes_nothing(self):
        net = Net(limit=[OSError("down"), (503, {}), (202, {})])
        reporter = self.start(net, message=False)
        doc = self.put()
        stored = self.rig.state.one("SELECT body FROM limit_event")[0].encode("utf-8")
        reporter.cycle()
        self.rig.clock.advance(900_000)
        reporter.cycle()
        self.rig.close()                                          # a restart in the middle
        reporter = self.rig.open(transport=net)
        self.rig.clock.advance(900_000)
        reporter.cycle()
        sent = [c for c in net.calls if c[0] == LIMIT_PATH]
        self.assertEqual(len(sent), 3)
        self.assertEqual({c[2] for c in sent}, {stored})          # the same bytes, byte for byte
        self.assertEqual({c[1] for c in sent}, {doc["event_id"]})  # under the same Idempotency-Key
        self.assertEqual(self.row(doc["event_id"])[0], "sent")

    def test_a_sent_row_is_never_sent_again(self):
        net = Net()
        reporter = self.start(net, message=False)
        self.put()
        reporter.cycle()
        reporter.cycle()
        self.assertEqual(net.paths().count(LIMIT_PATH), 1)

    def test_starting_the_reporter_forgives_agent_stops_but_keeps_limit_rows(self):
        net = Net(limit=[(503, {})])
        reporter = self.start(net, message=False)
        doc = self.put()
        reporter.cycle()
        self.rig.close()
        self.rig.open(transport=net)
        self.assertEqual(self.row(doc["event_id"])[:2], ("pending", 1))


class Privacy(_Rig):

    def test_a_failing_limit_row_leaks_no_body_value_into_errors_notes_counters_or_status(self):
        net = Net(limit=[(400, {"error": {"code": MARKER, "message": MARKER}})])
        reporter = self.start(net, message=False)
        doc = self.put()
        result = reporter.cycle()
        values = [str(v) for v in doc.values() if not isinstance(v, (int, list))] + [MARKER]
        dumps = [json.dumps(result.as_dict()),
                 json.dumps(reporter.report(), sort_keys=True),
                 status.render_text(reporter.report())]
        for table in ("note", "counter", "agent_state"):
            dumps.append(json.dumps(self.rig.state.all("SELECT * FROM %s" % table)))
        dumps.append(json.dumps(self.rig.state.all("SELECT last_error_code, state, tries FROM limit_event")))
        for text in dumps:
            for value in values:
                self.assertNotIn(value, text)
        self.assertEqual(self.row(doc["event_id"])[2], decisions.UNKNOWN_CODE)

    def test_a_transport_exception_text_is_never_kept(self):
        net = Net(limit=[RuntimeError(MARKER)])
        reporter = self.start(net, message=False)
        self.put()
        reporter.cycle()
        everything = json.dumps([self.rig.state.all("SELECT * FROM %s" % t) for t in
                                 ("note", "counter", "limit_event")])
        self.assertNotIn(MARKER, everything)
        self.assertNotIn(MARKER, json.dumps(reporter.report()))


if __name__ == "__main__":
    unittest.main()
