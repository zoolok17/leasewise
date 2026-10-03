"""Card L6a-2, fix round 1 (Codex's read of the first build): the real deadline, the owner taken from the saved
request, custody of a lease that expired while the program was down, and the grant order of the store.

* F1: ONE absolute deadline holds through the actual HTTP exchange (a peer that answers a few bytes at a time cannot
  stretch it), through the waits for the store, and through the open retry; at the deadline the answer is `held` with
  `store_busy` and the progress is saved, and a grant that Leasewise committed but we did not save is recovered by
  resending the same stored bytes.
* F2: the binding's gateway comes from the exact saved body that won, never from today's configuration; a request that is
  already saved is recovered before any check that reads today's settings.
* F3: a replayed paid grant whose lease is `uncertain` (expired) or `settled` is still saved (recall, pending, close and
  receipts need it); `admit` answers `held` because the turn must not run.
Synthetic data only: a local slow peer, a scripted fake Leasewise and the real in-process handler.
"""

import json
import socketserver
import sqlite3
import threading
import time
import unittest
from http.client import HTTPException
from unittest import mock

from quota_balancer import lease as lease_mod
from quota_balancer.reporter import admit
from quota_balancer.reporter import admit_wire as wire
from quota_balancer.reporter.client import HttpClient

from tests.l6a2_env import AGENT, GATEWAY_ID, FakeClock, Rigged, Script, admission_block, request_bytes
from tests.reporter_env import Crash, FAKE_IDENTITY

OTHER_GATEWAY = "c4a2e9f0-3b7d-4c1e-8a55-0d9b2f6e7a12"


class SlowPeer:
    """A local HTTP peer that accepts a request and then answers a byte at a time, `gap` seconds apart, for far longer
    than any deadline: the 'small pieces' case. It never speaks to anything but this machine."""

    def __init__(self, gap):
        self.gap = gap
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                data = b""
                self.request.settimeout(10)
                try:
                    while b"\r\n\r\n" not in data:
                        data += self.request.recv(4096)
                    head, _sep, rest = data.partition(b"\r\n\r\n")
                    length = 0
                    for line in head.split(b"\r\n"):
                        if line.lower().startswith(b"content-length:"):
                            length = int(line.split(b":")[1])
                    while len(rest) < length:
                        rest += self.request.recv(4096)
                    self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                         b"Content-Length: 100000\r\nConnection: close\r\n\r\n")
                    for _ in range(300):
                        time.sleep(outer.gap)
                        self.request.sendall(b" ")
                except OSError:
                    return

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class Deadline(unittest.TestCase):

    def setUp(self):
        self.rg = Rigged()
        self.addCleanup(self.rg.cleanup)

    def point_at(self, port):
        self.rg.ws.write_config("http://127.0.0.1:%d" % port, self.rg.rig.agents_cfg,
                                extra=admission_block(self.rg.store))

    def test_a_peer_that_answers_a_little_at_a_time_cannot_stretch_one_request(self):
        peer = SlowPeer(gap=1.0)
        self.addCleanup(peer.close)
        client = HttpClient("http", "127.0.0.1", peer.port, FAKE_IDENTITY)
        started = time.monotonic()
        with self.assertRaises((OSError, HTTPException)):
            client._http("POST", "/v1/leases", {"Content-Length": "2"}, b"{}", 3.0)
        self.assertLess(time.monotonic() - started, 3.0 + 1.5)                         # the budget plus a cleanup bound

    def test_the_whole_run_ends_within_its_budget_when_leasewise_drips_its_answer(self):
        """Codex measured 24 s for a 20 s budget with a peer that sent a chunk every few seconds."""
        peer = SlowPeer(gap=2.0)
        self.addCleanup(peer.close)
        self.point_at(peer.port)
        with mock.patch.object(admit, "TOTAL_SECONDS", 4.0):
            started = time.monotonic()
            code, out, err = self.rg.run(request_bytes(), None)
            elapsed = time.monotonic() - started
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(wire.parse_answer(out), {"reason": "store_busy", "status": "held", "v": 1})
        self.assertLess(elapsed, 4.0 + 2.5)
        row = self.rg.row()
        self.assertEqual(row[0], "requested")                                           # the request was saved first
        self.assertEqual(self.rg.sql("SELECT COUNT(*) FROM admission_attempt"), [(1,)])

    def test_waiting_for_the_store_near_the_deadline_ends_within_the_budget(self):
        self.rg.run(request_bytes(message_id="seed"), Script("GRANT"))                  # the file exists
        holder = sqlite3.connect(self.rg.store, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")                                               # a writer holds the file
        with mock.patch.object(admit, "TOTAL_SECONDS", 2.0):
            started = time.monotonic()
            code, out, _err = self.rg.run(request_bytes(message_id="late"), Script("GRANT"))
            elapsed = time.monotonic() - started
        holder.execute("ROLLBACK")
        self.assertEqual((code, wire.parse_answer(out)["reason"]), (0, "store_busy"))
        self.assertLess(elapsed, 2.0 + 1.5)
        self.assertEqual(self.rg.sql("SELECT COUNT(*) FROM admission WHERE message_id='late'"), [(0,)])

    def test_an_open_that_cannot_finish_is_not_started(self):
        clock = FakeClock()
        env = admit.Environment(mock.Mock(store=self.rg.store), {}, mock.Mock(), **clock.seams())
        with self.assertRaises(admit.store.StoreBusy):
            admit._open(env, clock.mono + admit.MIN_OPEN_SECONDS / 2)                   # no time left to try

    def test_a_grant_committed_but_not_saved_at_the_deadline_is_recovered_by_the_same_bytes(self):
        script = Script("GRANT")
        clock = FakeClock()
        script.now_ms = clock.now_ms
        self.rg.run(request_bytes(message_id="seed"), Script("GRANT"), **clock.seams())
        holder = sqlite3.connect(self.rg.store, isolation_level=None)
        self.addCleanup(holder.close)

        def lock_the_file(stage):
            if stage == "granted":                                                      # Leasewise has granted; not saved
                holder.execute("BEGIN IMMEDIATE")
        with mock.patch.object(admit, "TOTAL_SECONDS", 3.0):
            started = time.monotonic()
            code, out, _err = self.rg.run(request_bytes(), script, **clock.seams(fault=lock_the_file))
            elapsed = time.monotonic() - started
        holder.execute("ROLLBACK")
        self.assertEqual((code, wire.parse_answer(out)["reason"]), (0, "store_busy"))
        self.assertLess(elapsed, 3.0 + 1.5)
        self.assertEqual(self.rg.sql("SELECT state FROM admission WHERE message_id='msg-0001'"), [("requested",)])
        self.assertEqual(len(script.granted), 1)                                        # but Leasewise made the lease
        code, out, _err = self.rg.run(request_bytes(), script, **clock.seams())         # the next run resends the same bytes
        self.assertEqual(wire.parse_answer(out)["status"], "admitted")
        self.assertEqual(len(script.granted), 1)                                        # replayed, never a second lease
        self.assertEqual(script.calls[0][1], script.calls[1][1])
        self.assertEqual(self.rg.sql("SELECT state FROM admission WHERE message_id='msg-0001'"), [("granted",)])


class Owner(unittest.TestCase):

    def setUp(self):
        self.rg = Rigged()
        self.addCleanup(self.rg.cleanup)
        self.clock = FakeClock()

    def remap(self):
        """The alias now names ANOTHER gateway, and the bounds are gone."""
        self.rg.write_snapshot()
        with open(self.rg.ws.snapshot, "r", encoding="utf-8") as handle:
            snap = json.load(handle)
        snap["gateways"][0]["id"] = OTHER_GATEWAY
        with open(self.rg.ws.snapshot, "w", encoding="utf-8") as handle:
            json.dump(snap, handle)

    def test_the_binding_names_the_gateway_of_the_saved_request_after_a_remap_and_a_crash(self):
        script = Script("GRANT")
        script.now_ms = self.clock.now_ms
        with self.assertRaises(Crash):
            def stop(stage):
                if stage == "posted":
                    raise Crash()                                                       # Leasewise answered; nothing saved
            self.rg.run(request_bytes(), script, **self.clock.seams(fault=stop))
        saved = json.loads(self.rg.sql("SELECT body_current FROM admission")[0][0])
        self.assertEqual(saved["gateway_id"], GATEWAY_ID)
        self.remap()                                                                    # the alias now means gateway B
        self.rg.configure(fresh_overhead_bound_tokens=None, growth_bound_tokens=None)    # and today's bounds are gone
        code, out, _err = self.rg.run(request_bytes(), script, **self.clock.seams())
        self.assertEqual((code, wire.parse_answer(out)["status"]), (0, "admitted"))     # recovered from what was saved
        self.assertEqual(self.rg.sql("SELECT gateway_id FROM admission"), [(GATEWAY_ID,)])   # A, never B
        self.assertEqual(len(script.granted), 1)

    def test_a_saved_request_is_recovered_even_when_the_agent_is_no_longer_unprovable_or_declared_the_same_way(self):
        script = Script(Exception("no answer"), "GRANT")
        script.now_ms = self.clock.now_ms
        first = self.rg.run(request_bytes(), script, **self.clock.seams())
        self.assertEqual(wire.parse_answer(first[1])["reason"], "coordinator_unreachable")
        self.rg.configure(resume_history_bound_tokens=None, context_ceiling_tokens=1200)    # today it would be unprovable
        self.clock.advance(11)
        code, out, _err = self.rg.run(request_bytes(), script, **self.clock.seams())
        self.assertEqual(wire.parse_answer(out)["status"], "admitted")

    def test_a_new_message_is_still_judged_by_todays_settings(self):
        self.rg.configure(context_ceiling_tokens=1200)
        code, out, _err = self.rg.run(request_bytes(message_id="new-one"), Script("GRANT"), **self.clock.seams())
        self.assertEqual(wire.parse_answer(out)["reason"], "context_unprovable")


class Custody(unittest.TestCase):
    """F3: custody is separate from permission to run."""

    def setUp(self):
        self.rg = Rigged()
        self.addCleanup(self.rg.cleanup)
        self.clock = FakeClock()

    def doc(self, state, expires_in):
        return (200, {"lease_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "state": state, "enforcement": "metered_advisory",
                      "money_hold": None, "caps": {}, "expires_at": self.clock.ms + expires_in})

    def test_an_expired_or_settled_replay_is_saved_as_the_binding_and_the_turn_is_held(self):
        for state, expires_in in (("uncertain", -5_000), ("settled", 600_000)):
            with self.subTest(state=state):
                self.rg.cleanup()
                self.setUp()
                out = self.rg.run(request_bytes(), Script(self.doc(state, expires_in)), **self.clock.seams())[1]
                self.assertEqual(wire.parse_answer(out), {"reason": "refused", "status": "held", "v": 1})
                self.assertEqual(self.rg.row()[0], "granted")                           # the binding exists
                self.assertEqual(self.rg.sql("SELECT ttl_seconds, reference FROM admission"),
                                 [(0, "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")])
                recalled = self.rg.run(wire.encode_op(wire.OP_RECALL, agent=AGENT, message_id="msg-0001"), None,
                                       **self.clock.seams())[1]
                self.assertEqual(wire.parse_op_answer(wire.OP_RECALL, recalled)["reference"],
                                 "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
                pending = self.rg.run(wire.encode_op(wire.OP_PENDING, agent=AGENT, limit=5), None, **self.clock.seams())[1]
                self.assertEqual(len(wire.parse_op_answer(wire.OP_PENDING, pending)["messages"]), 1)
                self.assertEqual(self.rg.run(wire.encode_op(wire.OP_CLOSED, agent=AGENT, message_id="msg-0001"), None,
                                             **self.clock.seams())[1], b'{"status":"ok","v":1}')
                again = self.rg.run(request_bytes(), None, **self.clock.seams())[1]    # held every time, from the store
                self.assertEqual(wire.parse_answer(again)["reason"], "refused")

    def test_a_lease_that_is_not_the_message_s_kind_is_still_not_saved(self):
        bad = {"lease_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "state": "active", "enforcement": "shadow",
               "money_hold": None, "caps": {}, "expires_at": self.clock.ms + 600_000}
        self.rg.run(request_bytes(), Script((200, bad)), **self.clock.seams())
        self.assertEqual(self.rg.row()[0], "requested")


class RealHandler(unittest.TestCase):

    def test_grant_crash_before_the_save_expire_sweep_and_retry_saves_the_binding_and_holds_the_turn(self):
        rg = Rigged(server=True)
        self.addCleanup(rg.cleanup)
        lw = rg.rig.lw

        def stop(stage):
            if stage == "granted":
                raise Crash()
        with self.assertRaises(Crash):
            rg.run(request_bytes(), None, fault=stop)                                    # the lease exists; nothing saved
        self.assertEqual(rg.row()[0], "requested")
        lease_id = lw.rows("SELECT id FROM leases")[0][0]
        lease_mod.expire_sweep(lw.db, int(time.time() * 1000) + 7_200_000)               # the lease expires meanwhile
        self.assertEqual(lw.rows("SELECT state FROM leases"), [("uncertain",)])
        code, out, err = rg.run(request_bytes(), None)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(wire.parse_answer(out)["reason"], "refused")                    # the turn must not run
        self.assertEqual(rg.sql("SELECT state, reference FROM admission"), [("granted", lease_id)])
        recalled = rg.run(wire.encode_op(wire.OP_RECALL, agent=AGENT, message_id="msg-0001"), None)[1]
        self.assertEqual(wire.parse_op_answer(wire.OP_RECALL, recalled)["reference"], lease_id)
        self.assertEqual(len(lw.rows("SELECT id FROM leases")), 1)


if __name__ == "__main__":
    unittest.main()
