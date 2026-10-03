"""Card L6a-2, fix round 2 (the last): the connection obeys the deadline, and a saved request is recovered after its agent
was removed from the settings.

* D1: with `[admission]` the address of Leasewise must be an IP address or `localhost` (so `admit` never waits on a name
  lookup that its deadline cannot stop); the connection step uses the time that is left, the socket belongs to the
  watchdog from the moment it exists, nothing is sent after the deadline, and the watchdog thread always ends.
* D2: custody before permission. An existing `requested` row is recovered from its saved bodies even when its agent is no
  longer configured: the grant is saved (the owner is the winning saved body's), and the turn is `held` as
  `not_configured`. Only a rebuild of a stale estimate needs today's settings: it is held and nothing is rebuilt.
Synthetic data only: local peers, a patched resolver and the real in-process handler.
"""

import json
import os
import socket
import socketserver
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from quota_balancer.reporter import admission_store as store
from quota_balancer.reporter import admit
from quota_balancer.reporter import admit_wire as wire
from quota_balancer.reporter import config as cfg
from quota_balancer.reporter.client import HttpClient

from tests.l6a2_env import AGENT, FakeClock, Rigged, Script, admission_block, request_bytes
from tests.reporter_env import Crash, agent_table, config_text

SENTENCE = "base_url: with [admission], the host must be an IP address or localhost"
BOUND = 1.0                     # the measured overrun is a few milliseconds; the test allows this much cleanup


class Peer:
    """A local HTTP peer that records the requests it receives and answers `{}`."""

    def __init__(self):
        outer = self
        self.received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                outer.received.append(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class Bytes:
    """A local TCP peer that only counts what it is sent."""

    def __init__(self):
        outer = self
        self.data = b""
        self.connections = 0

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                outer.connections += 1
                self.request.settimeout(1.5)
                try:
                    while True:
                        chunk = self.request.recv(4096)
                        if not chunk:
                            return
                        outer.data += chunk
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
        self.thread.join(2)


def slow_resolver(seconds=2.4):
    """A resolver that takes `seconds` for any host that is not a number, and records every name it was asked."""
    real = socket.getaddrinfo
    asked = []

    def lookup(host, *args, **kwargs):
        text = host.decode() if isinstance(host, bytes) else host
        try:
            socket.inet_pton(socket.AF_INET, text)
        except (OSError, TypeError):
            try:
                socket.inet_pton(socket.AF_INET6, text)
            except (OSError, TypeError):
                asked.append(text)
                time.sleep(seconds)
        return real(host, *args, **kwargs)
    return lookup, asked


class Address(unittest.TestCase):
    """D1: the address with `[admission]`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AGENTTALK_SCRATCH") or None, prefix="l6a2fix2-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def parse(self, url, admission=True):
        extra = admission_block(os.path.join(self.dir, "a.db")) if admission else ""
        return cfg.parse_config(config_text(url, os.path.join(self.dir, "journal"), os.path.join(self.dir, "s", "r.db"),
                                            os.path.join(self.dir, "p.json"), {AGENT: agent_table()}, extra=extra))

    def test_a_host_name_is_refused_at_start_up_with_the_exact_sentence(self):
        for url in ("https://synthetic.example", "https://leasewise.example:8443", "https://localhost.localdomain",
                    "https://127.0.0.1.example", "https://example.com/", "https://1.2.3", "https://999.1.1.1"):
            with self.subTest(url=url):
                with self.assertRaises(cfg.ConfigError) as caught:
                    self.parse(url)
                self.assertEqual((caught.exception.code, caught.exception.message), (cfg.CONFIG_INVALID, SENTENCE))

    def test_an_ip_address_or_localhost_is_accepted_with_admission(self):
        for url in ("http://127.0.0.1:8443", "http://localhost:9", "https://localhost", "https://LOCALHOST:1",
                    "https://127.0.0.1", "https://10.1.2.3:8443", "https://[::1]:8443", "https://[2001:db8::1]"):
            with self.subTest(url=url):
                self.assertEqual(self.parse(url).admission.gateway_agents, (AGENT,))

    def test_without_admission_a_host_name_is_still_accepted(self):
        for url in ("https://synthetic.example", "https://leasewise.example:8443"):
            with self.subTest(url=url):
                self.assertEqual(self.parse(url, admission=False).host, url.split("//")[1].split(":")[0])


class NoLookup(unittest.TestCase):
    """D1: a slow resolver does not delay `admit`, because it is never asked."""

    def run_admit(self, host):
        rg = Rigged(server=True)
        self.addCleanup(rg.cleanup)
        url = rg.rig.lw.url.replace("127.0.0.1", host)
        rg.ws.write_config(url, rg.rig.agents_cfg, extra=admission_block(rg.store))
        lookup, asked = slow_resolver()
        with mock.patch.object(socket, "getaddrinfo", lookup):
            started = time.monotonic()
            code, out, err = rg.run(request_bytes(), None)
            elapsed = time.monotonic() - started
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(wire.parse_answer(out)["status"], "admitted")
        self.assertEqual(asked, [])                                      # no name was ever looked up
        self.assertLess(elapsed, 1.5)                                    # and nothing waited for the 2.4 s resolver

    def test_an_ip_literal_base_url_is_not_delayed(self):
        self.run_admit("127.0.0.1")

    def test_localhost_is_connected_as_127_0_0_1_without_asking_the_resolver(self):
        self.run_admit("localhost")

    def test_a_host_name_on_the_run_path_still_uses_the_resolver(self):
        """The `run` path has no hook promise: a name is looked up, and that lookup is not bounded."""
        peer = Peer()
        self.addCleanup(peer.close)
        asked = []
        client = HttpClient("http", "synthetic.invalid", peer.port, "x")
        _real_getaddrinfo = socket.getaddrinfo

        def named(host, port, *args, **kwargs):
            asked.append(host)
            time.sleep(0.2)
            return _real_getaddrinfo("127.0.0.1", peer.port, *args, **kwargs)
        with mock.patch.object(socket, "getaddrinfo", named):
            status, _raw = client._http("POST", "/v1/leases", {"Content-Length": "2"}, b"{}", 5.0)
        self.assertEqual((status, asked), (200, ["synthetic.invalid"]))


class Connect(unittest.TestCase):
    """D1: the connection step obeys the deadline."""

    def test_a_connection_that_uses_up_the_budget_is_stopped_and_nothing_is_sent(self):
        peer = Bytes()
        self.addCleanup(peer.close)
        client = HttpClient("http", "127.0.0.1", peer.port, "x")
        real = socket.socket.connect

        def slow(self, address):
            time.sleep(0.4)                                              # the whole budget goes on the connection
            return real(self, address)
        with mock.patch.object(socket.socket, "connect", slow):
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                client._http("POST", "/v1/leases", {"Content-Length": "2"}, b'{"x":1}', 0.4)
            elapsed = time.monotonic() - started
        time.sleep(0.3)
        self.assertLess(elapsed, 0.4 + BOUND)
        self.assertEqual(peer.data, b"")                                 # the peer never saw a request

    def test_localhost_is_the_same(self):
        peer = Bytes()
        self.addCleanup(peer.close)
        client = HttpClient("http", "localhost", peer.port, "x")
        real = socket.socket.connect

        def slow(self, address):
            time.sleep(0.4)
            return real(self, address)
        with mock.patch.object(socket.socket, "connect", slow):
            with self.assertRaises(TimeoutError):
                client._http("POST", "/v1/leases", {"Content-Length": "2"}, b'{"x":1}', 0.4)
        time.sleep(0.3)
        self.assertEqual(peer.data, b"")

    def test_the_run_ends_within_the_budget_when_the_connection_is_slow_and_the_peer_sees_no_post(self):
        rg = Rigged(server=True)
        self.addCleanup(rg.cleanup)
        real = socket.socket.connect

        def slow(self, address):
            time.sleep(1.2)
            return real(self, address)
        with mock.patch.object(admit, "TOTAL_SECONDS", 1.2), mock.patch.object(socket.socket, "connect", slow):
            started = time.monotonic()
            code, out, _err = rg.run(request_bytes(), None)
            elapsed = time.monotonic() - started
        self.assertEqual((code, wire.parse_answer(out)["reason"]), (0, "store_busy"))
        self.assertLess(elapsed, 1.2 + BOUND)
        self.assertEqual(rg.rig.lw.rows("SELECT id FROM leases"), [])    # Leasewise never got the request
        self.assertEqual(rg.sql("SELECT state FROM admission"), [("requested",)])


class Watchdog(unittest.TestCase):
    """D1: no timer thread is left running, whatever happens."""

    def timers(self):
        made = []
        real = threading.Timer

        def remember(*args, **kwargs):
            timer = real(*args, **kwargs)
            made.append(timer)
            return timer
        patcher = mock.patch.object(threading, "Timer", remember)
        patcher.start()
        self.addCleanup(patcher.stop)
        return made

    def ended(self, made):
        self.assertTrue(made)
        for timer in made:
            timer.join(2)
            self.assertFalse(timer.is_alive())

    def test_after_a_good_answer(self):
        made = self.timers()
        peer = Peer()
        self.addCleanup(peer.close)
        self.assertEqual(HttpClient("http", "127.0.0.1", peer.port, "x")._http(
            "POST", "/v1/leases", {"Content-Length": "2"}, b"{}", 5.0)[0], 200)
        self.ended(made)

    def test_after_a_refused_connection(self):
        made = self.timers()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]                                # nobody listens here any more
        with self.assertRaises(OSError):
            HttpClient("http", "127.0.0.1", port, "x")._http("POST", "/v1/leases", {"Content-Length": "2"}, b"{}", 2.0)
        self.ended(made)

    def test_after_an_expiry_during_connect(self):
        made = self.timers()
        peer = Bytes()
        self.addCleanup(peer.close)
        real = socket.socket.connect

        def slow(self, address):
            time.sleep(0.3)
            return real(self, address)
        with mock.patch.object(socket.socket, "connect", slow):
            with self.assertRaises(TimeoutError):
                HttpClient("http", "127.0.0.1", peer.port, "x")._http(
                    "POST", "/v1/leases", {"Content-Length": "2"}, b"{}", 0.3)
        self.ended(made)

    def test_after_a_blocked_answer(self):
        from tests.test_card7l6a2_fix1 import SlowPeer
        made = self.timers()
        peer = SlowPeer(gap=0.05)
        self.addCleanup(peer.close)
        started = time.monotonic()
        with self.assertRaises(Exception):                               # noqa: B017 - which error depends on the timing
            HttpClient("http", "127.0.0.1", peer.port, "x")._http(
                "POST", "/v1/leases", {"Content-Length": "2"}, b"{}", 0.3)
        self.assertLess(time.monotonic() - started, 0.3 + BOUND)
        self.ended(made)


class Removed(unittest.TestCase):
    """D2: custody before permission."""

    def remove_alpha(self, rg, url):
        """A valid configuration in which `alpha` is gone and `beta` is the only agent the gateway admits."""
        rg.ws.write_config(url, {"beta": rg.rig.agents_cfg[AGENT] if AGENT in rg.rig.agents_cfg else agent_table()},
                           extra=admission_block(rg.store, agents=("beta",)))

    def test_a_committed_lease_is_recovered_after_its_agent_was_removed(self):
        rg = Rigged(server=True)
        self.addCleanup(rg.cleanup)

        def stop(stage):
            if stage == "granted":
                raise Crash()
        with self.assertRaises(Crash):
            rg.run(request_bytes(), None, fault=stop)                    # Leasewise made the lease; nothing was saved
        lease_id = rg.rig.lw.rows("SELECT id FROM leases")[0][0]
        self.assertEqual(rg.sql("SELECT state FROM admission"), [("requested",)])
        self.remove_alpha(rg, rg.rig.lw.url)
        code, out, err = rg.run(request_bytes(), None)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(wire.parse_answer(out), {"reason": "not_configured", "status": "held", "v": 1})
        self.assertEqual(rg.sql("SELECT state, reference, agent FROM admission"), [("granted", lease_id, AGENT)])
        status, found = store.read_bindings(rg.store)
        self.assertEqual((status, [(one.lease_id, one.agent) for one in found]), (store.OK, [(lease_id, AGENT)]))
        recalled = rg.run(wire.encode_op(wire.OP_RECALL, agent=AGENT, message_id="msg-0001"), None)[1]
        self.assertEqual(wire.parse_op_answer(wire.OP_RECALL, recalled)["reference"], lease_id)
        pending = rg.run(wire.encode_op(wire.OP_PENDING, agent=AGENT, limit=5), None)[1]
        self.assertEqual(len(wire.parse_op_answer(wire.OP_PENDING, pending)["messages"]), 1)
        self.assertEqual(rg.run(wire.encode_op(wire.OP_CLOSED, agent=AGENT, message_id="msg-0001"), None)[1],
                         b'{"status":"ok","v":1}')
        again = rg.run(request_bytes(), None)[1]                         # and the stored grant is held from now on too
        self.assertEqual(wire.parse_answer(again)["reason"], "not_configured")
        self.assertEqual(len(rg.rig.lw.rows("SELECT id FROM leases")), 1)

    def test_the_winning_grant_is_found_by_replay_after_the_agent_was_removed(self):
        rg = Rigged()
        self.addCleanup(rg.cleanup)
        clock = FakeClock()
        landed = {}
        stale = (409, {"error": {"code": "ESTIMATE_STALE", "message": "x"}})

        def late(index, key, body):
            if index == 0:
                landed["first"] = body
                clock.ms += 130_000                     # the estimate is old, so the rebuilt body differs
            if index == 2:                              # the very first body is the one Leasewise granted
                script.granted[key] = ("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", landed["first"])
        script = Script(stale, Exception("no answer"), "GRANT", "GRANT", on_call=late)
        script.now_ms = clock.now_ms
        first = rg.run(request_bytes(), script, **clock.seams())
        self.assertEqual(wire.parse_answer(first[1])["reason"], "coordinator_unreachable")
        self.remove_alpha(rg, "http://127.0.0.1:9")
        clock.advance(61)
        code, out, _err = rg.run(request_bytes(), script, **clock.seams())
        self.assertEqual((code, wire.parse_answer(out)["reason"]), (0, "not_configured"))
        self.assertEqual(rg.sql("SELECT state, reference FROM admission"),
                         [("granted", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")])
        self.assertEqual(rg.sql("SELECT gateway_id FROM admission"), [(json.loads(landed["first"])["gateway_id"],)])
        self.assertEqual(script.calls[2][1] != script.calls[3][1], True)  # the rebuilt body conflicted, the first won

    def test_a_rebuild_needs_the_agent_so_it_is_held_and_the_row_stays_requested(self):
        rg = Rigged()
        self.addCleanup(rg.cleanup)
        clock = FakeClock()
        stale = (409, {"error": {"code": "ESTIMATE_STALE", "message": "x"}})
        script = Script(Exception("no answer"), stale, "GRANT")
        script.now_ms = clock.now_ms
        rg.run(request_bytes(), script, **clock.seams())
        self.remove_alpha(rg, "http://127.0.0.1:9")
        clock.advance(61)
        code, out, _err = rg.run(request_bytes(), script, **clock.seams())
        self.assertEqual((code, wire.parse_answer(out)["reason"]), (0, "not_configured"))
        self.assertEqual(len(script.calls), 2)                           # the same stored bytes were sent; nothing rebuilt
        self.assertEqual(script.calls[0][1], script.calls[1][1])
        row = rg.sql("SELECT state, rebuilds, last_error_code, not_before_ms FROM admission")
        self.assertEqual(row, [("requested", 0, "NOT_CONFIGURED", clock.ms)])     # no back-off: nothing to wait for
        self.assertEqual(rg.sql("SELECT COUNT(*) FROM admission_attempt"), [(1,)])  # nothing was rebuilt or deleted
        rg.configure()                                                   # the operator puts alpha back
        code, out, _err = rg.run(request_bytes(), script, **clock.seams())
        self.assertEqual(wire.parse_answer(out)["status"], "admitted")   # and the same message goes on from where it was

    def test_a_new_message_of_a_removed_agent_is_still_not_configured(self):
        rg = Rigged()
        self.addCleanup(rg.cleanup)
        self.remove_alpha(rg, "http://127.0.0.1:9")
        script = Script("GRANT")
        out = rg.run(request_bytes(), script)[1]
        self.assertEqual(wire.parse_answer(out)["reason"], "not_configured")
        self.assertEqual(script.calls, [])
        self.assertFalse(os.path.exists(rg.store))                        # nothing was even created


if __name__ == "__main__":
    unittest.main()
