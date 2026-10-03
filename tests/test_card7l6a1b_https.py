"""Card L6a-1b, follow-up: the HTTPS handshake obeys the admission deadline (Codex's limit L1 on L6a-2).

Before, the connection and the sending obeyed the deadline but the secure-connection setup (the TLS handshake) did not: each
read of the handshake was bounded by the time left when it started, so a peer that answers a little at a time could stretch it
(Codex measured 3.6 s for a 2 s budget). Now the socket is handed to the watchdog before the handshake, so the watchdog can end it.

The peers here speak no real TLS and need no certificate: one accepts late and never answers the ClientHello (Codex's case), one
answers it a byte at a time. Nothing leaves this machine.
"""

import socket
import socketserver
import threading
import time
import unittest
from unittest import mock

from quota_balancer.reporter import admit
from quota_balancer.reporter import admit_wire as wire
from quota_balancer.reporter.client import HttpClient

from tests.l6a2_env import Rigged, admission_block, request_bytes

BOUND = 1.0                      # the measured overrun is a few milliseconds; the test allows this much cleanup


class TlsPeer:
    """A local peer that is not a TLS server. `delay` is how long it waits before it accepts; then it reads what the client
    sends and either stays silent (`drip` is None) or answers with the start of a TLS record and then one byte every `drip`
    seconds, so that every read of the handshake succeeds but the handshake never finishes."""

    def __init__(self, delay=0.0, drip=None):
        outer = self
        self.received = b""

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                time.sleep(delay)
                self.request.settimeout(10)
                try:
                    outer.received += self.request.recv(4096)               # the ClientHello
                    if drip is not None:
                        self.request.sendall(b"\x16\x03\x03\x00\xff")      # a record header: 255 bytes will follow
                        for _ in range(200):
                            time.sleep(drip)
                            self.request.sendall(b"x")
                    else:
                        while self.request.recv(4096):
                            pass
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


class Handshake(unittest.TestCase):

    def peer(self, **kwargs):
        peer = TlsPeer(**kwargs)
        self.addCleanup(peer.close)
        return peer

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

    def call(self, port, budget):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            HttpClient("https", "127.0.0.1", port, "x")._http("POST", "/v1/leases", {"Content-Length": "2"}, b"{}", budget)
        return time.monotonic() - started

    def slow_connect(self, seconds):
        """The TCP connection itself takes `seconds` (the time the handshake must not be given again)."""
        real = socket.socket.connect

        def slow(sock, address):
            time.sleep(seconds)
            return real(sock, address)
        patcher = mock.patch.object(socket.socket, "connect", slow)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_connection_that_takes_1_6_s_leaves_the_handshake_only_what_is_left_of_the_budget(self):
        """Codex's case: a 2 s budget took 3.6 s, because the handshake was given the whole budget again after the connection
        had used 1.6 s of it. The peer accepts and then never answers the ClientHello."""
        peer = self.peer()
        self.slow_connect(1.6)
        made = self.timers()
        elapsed = self.call(peer.port, 2.0)
        self.assertGreaterEqual(elapsed, 1.9)
        self.assertLess(elapsed, 2.0 + BOUND)
        self.assertNotIn(b"POST", peer.received)                         # no request was sent
        for timer in made:
            timer.join(2)
            self.assertFalse(timer.is_alive())

    def test_a_peer_that_accepts_late_and_never_answers_the_client_hello_ends_at_the_deadline(self):
        peer = self.peer(delay=1.6)
        made = self.timers()
        elapsed = self.call(peer.port, 2.0)
        self.assertGreaterEqual(elapsed, 1.9)
        self.assertLess(elapsed, 2.0 + BOUND)
        self.assertNotIn(b"POST", peer.received)
        for timer in made:
            timer.join(2)
            self.assertFalse(timer.is_alive())

    def test_a_peer_that_answers_the_handshake_a_byte_at_a_time_cannot_stretch_the_budget(self):
        """Each read succeeds in time, so a timeout per read never fires: only the watchdog can end this handshake."""
        peer = self.peer(drip=0.3)
        made = self.timers()
        elapsed = self.call(peer.port, 2.0)
        self.assertGreaterEqual(elapsed, 1.9)
        self.assertLess(elapsed, 2.0 + BOUND)
        self.assertNotIn(b"POST", peer.received)
        for timer in made:
            timer.join(2)
            self.assertFalse(timer.is_alive())

    def test_a_deadline_that_has_already_passed_sends_nothing_and_does_not_start_the_handshake(self):
        peer = self.peer()
        real = socket.socket.connect

        def slow(sock, address):
            time.sleep(0.4)                                              # the whole budget goes on the connection
            return real(sock, address)
        with mock.patch.object(socket.socket, "connect", slow):
            self.call(peer.port, 0.4)
        time.sleep(0.2)
        self.assertEqual(peer.received, b"")                             # not even a ClientHello

    def test_the_whole_admit_route_answers_store_busy_and_keeps_the_request(self):
        peer = self.peer()
        self.slow_connect(1.6)
        rg = Rigged()
        self.addCleanup(rg.cleanup)
        rg.ws.write_config("https://127.0.0.1:%d" % peer.port, rg.rig.agents_cfg, extra=admission_block(rg.store))
        with mock.patch.object(admit, "TOTAL_SECONDS", 2.0):
            started = time.monotonic()
            code, out, err = rg.run(request_bytes(), None)
            elapsed = time.monotonic() - started
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(wire.parse_answer(out), {"reason": "store_busy", "status": "held", "v": 1})
        self.assertLess(elapsed, 2.0 + BOUND)
        self.assertEqual(rg.sql("SELECT state FROM admission"), [("requested",)])   # saved before it was sent; recoverable
        self.assertNotIn(b"POST", peer.received)


if __name__ == "__main__":
    unittest.main()
