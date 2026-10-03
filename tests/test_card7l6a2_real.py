"""Card L6a-2, step 1: `reporter admit` against the REAL in-process Leasewise handler, and as real child processes.

* a metered-advisory acquisition through the real handler: null caps, no money hold, no token; the same request again
  is the same lease; the handler has no shadow fallback (an UNKNOWN context is refused) and judges an old estimate stale;
* two, then many, real processes asking about one message produce one request, one lease and one stored grant, and
  every one of them prints the same answer;
* the program as a command: exact standard output, a fixed sentence on standard error, the exit codes.
Synthetic data only: the in-process handler on a temporary store; no real gateway, provider or key.
"""

import json
import unittest

from quota_balancer.reporter import admission_store as store
from quota_balancer.reporter import admit_wire as wire
from quota_balancer.reporter.client import HttpClient

from tests.l6a2_env import AGENT, Rigged, request_bytes
from tests.reporter_env import FAKE_IDENTITY


class _Real(unittest.TestCase):
    def setUp(self):
        self.rg = Rigged(server=True)
        self.addCleanup(self.rg.cleanup)
        self.lw = self.rg.rig.lw

    def admit(self, raw=None):
        code, out, err = self.rg.run(raw or request_bytes(), None)       # the real HTTP client, to the real handler
        self.assertEqual((code, err), (0, ""))
        return out

    def leases(self):
        return self.lw.rows("SELECT id, enforcement, money_hold, token_hash, gateway_id, state FROM leases")

    def client(self):
        return HttpClient("http", "127.0.0.1", self.lw.port, FAKE_IDENTITY)


class Handler(_Real):

    def test_a_metered_advisory_acquisition_has_null_caps_no_hold_and_no_token(self):
        out = self.admit()
        doc = wire.parse_answer(out)
        self.assertEqual((doc["status"], doc["max_calls"], doc["max_micro_eur"]), ("admitted", None, None))
        rows = self.leases()
        self.assertEqual(len(rows), 1)
        lease_id, enforcement, money_hold, token_hash, gateway_id, state = rows[0]
        self.assertEqual((enforcement, money_hold, token_hash, state), ("metered_advisory", None, None, "active"))
        self.assertEqual(doc["reference"], lease_id)
        self.assertEqual(gateway_id, self.lw.rows("SELECT id FROM gateways WHERE alias='g1'")[0][0])
        self.assertGreaterEqual(doc["ttl_seconds"], 1)
        self.assertLessEqual(doc["ttl_seconds"], 86_400)

    def test_the_same_request_again_is_the_same_lease_from_the_store_and_from_the_handler(self):
        first = self.admit()
        self.assertEqual(self.admit(), first)
        self.assertEqual(len(self.leases()), 1)
        # the stored bytes sent again, exactly: the real handler replays its stored lease (a 200, not a second lease)
        row = self.rg.sql("SELECT request_id, body_current FROM admission")[0]
        status, doc, code = self.client().post_acquisition(row[0], row[1].encode("utf-8"))
        self.assertEqual((status, code, doc["lease_id"]), (200, None, wire.parse_answer(first)["reference"]))
        self.assertEqual(len(self.leases()), 1)

    def test_there_is_no_shadow_fallback_and_an_old_estimate_is_stale(self):
        self.admit()
        row = self.rg.sql("SELECT request_id, body_current FROM admission")[0]
        body = json.loads(row[1])
        unknown = dict(body, request_id="11111111-1111-4111-8111-111111111111",
                       turn_id="22222222-2222-4222-8222-222222222222",
                       context=dict(body["context"], mode="UNKNOWN", initial_tokens=None, dispatch_tokens=None,
                                    source="unknown", growth_tokens_per_call=None))
        status, doc, code = self.client().post_acquisition(
            unknown["request_id"], json.dumps(unknown, sort_keys=True, separators=(",", ":")).encode())
        self.assertEqual((status, code), (400, "CONTEXT_UNKNOWN"))
        old = dict(body, request_id="33333333-3333-4333-8333-333333333333",
                   turn_id="44444444-4444-4444-8444-444444444444",
                   context=dict(body["context"], observed_at=body["context"]["observed_at"] - 300_000))
        status, doc, code = self.client().post_acquisition(
            old["request_id"], json.dumps(old, sort_keys=True, separators=(",", ":")).encode())
        self.assertEqual(code, "ESTIMATE_STALE")
        self.assertEqual(len(self.leases()), 1)

    def test_the_reporter_reads_the_saved_grant_as_a_binding(self):
        doc = wire.parse_answer(self.admit())
        status, found = store.read_bindings(self.rg.store)
        self.assertEqual(status, store.OK)
        self.assertEqual([(b.lease_id, b.agent, b.message_id) for b in found], [(doc["reference"], AGENT, "msg-0001")])
        self.assertEqual(found[0].gateway_id, self.leases()[0][4])


class Processes(_Real):

    def many(self, count, message_id):
        raw = request_bytes(message_id=message_id)
        children = [self.rg.child(raw) for _ in range(count)]
        results = []
        for child in children:
            out, err = child.communicate(timeout=120)
            results.append((child.returncode, out, err))
        return results

    def test_two_real_processes_for_one_message_give_one_request_one_lease_and_one_answer(self):
        results = self.many(2, "race-2")
        self.assertEqual({(code, err) for code, _out, err in results}, {(0, b"")})
        self.assertEqual(len({out for _code, out, _err in results}), 1)
        self.assertEqual(wire.parse_answer(results[0][1])["status"], "admitted")
        self.check_one("race-2")

    def test_many_real_processes_for_one_message_give_one_request_one_lease_and_one_answer(self):
        results = self.many(8, "race-8")
        self.assertEqual({(code, err) for code, _out, err in results}, {(0, b"")})
        self.assertEqual(len({out for _code, out, _err in results}), 1)
        self.check_one("race-8")

    def check_one(self, message_id):
        rows = self.rg.sql("SELECT message_id, state FROM admission")
        self.assertEqual(rows, [(message_id, "granted")])                              # one request, one stored grant
        self.assertEqual(self.rg.sql("SELECT COUNT(*) FROM admission_attempt"), [(1,)])       # one body, never rebuilt
        self.assertEqual(len(self.leases()), 1)                                         # one lease in Leasewise

    def test_a_real_process_waits_for_the_key_while_another_holds_it_and_goes_on_when_it_is_released(self):
        import time
        from quota_balancer.reporter import admission_lock
        held = admission_lock.KeyLock(self.rg.store, AGENT, "msg-0001")
        self.assertTrue(held.acquire(time.monotonic() + 5))
        child = self.rg.child(request_bytes())
        try:
            time.sleep(2.0)
            self.assertIsNone(child.poll())                                             # still waiting for the key
            self.assertEqual(self.leases(), [])                                         # and has asked Leasewise nothing
            held.release()
            out, err = child.communicate(timeout=60)
        finally:
            held.release()
            if child.poll() is None:
                child.kill()                                                            # this child, by its process id
                child.communicate()
        self.assertEqual((child.returncode, err), (0, b""))
        self.assertEqual(wire.parse_answer(out)["status"], "admitted")
        self.assertEqual(len(self.leases()), 1)

    def test_the_command_prints_exactly_the_answer_and_nothing_else(self):
        child = self.rg.child(request_bytes(message_id="cmd-1"))
        out, err = child.communicate(timeout=120)
        self.assertEqual((child.returncode, err), (0, b""))
        self.assertEqual(out, json.dumps(json.loads(out), sort_keys=True, separators=(",", ":")).encode("ascii"))
        self.assertEqual(wire.parse_answer(out)["status"], "admitted")

    def test_a_bad_request_is_exit_2_with_a_fixed_sentence_and_no_standard_output(self):
        child = self.rg.child(b'{"v":1,"op":"admit","agent":"loud-private-input"}')
        out, err = child.communicate(timeout=120)
        self.assertEqual((child.returncode, out), (2, b""))
        self.assertIn(b"the request is not allowed", err)
        self.assertNotIn(b"loud-private-input", err)
        self.assertEqual(self.leases(), [])

    def test_a_configuration_without_the_admission_block_is_exit_2(self):
        self.rg.rig.ws.write_config(self.lw.url, self.rg.rig.agents_cfg)
        child = self.rg.child(request_bytes())
        out, err = child.communicate(timeout=120)
        self.assertEqual((child.returncode, out), (2, b""))
        self.assertIn(b"no admission block", err)


if __name__ == "__main__":
    unittest.main()
