"""Card 7b, step 7b-3: delivery of readings with the send gate (DESIGN-7B2.md 6.3, 7.2-7.5).

A stored reading is offered only when its own back-off is over and the window's gate is open (or its reset is a new
one); an accepted answer (200, 201, 202) moves the gate by one pace, and nothing else does. A permanent refusal fails
only that row. The readings pass runs after limit events and before receipts and leases, and no pass holds another
back. Synthetic data; a fake transport; the clock is a controlled number.
"""

import json
import os
import sqlite3
import unittest
import uuid

from quota_balancer.reporter import config as cfg
from quota_balancer.reporter import decisions, run as run_mod
from quota_balancer.reporter.state import READING_AGENT, READING_PACE_MS
from tests.card7b2_fixtures import CLAUDE_ACCOUNT, FIVE_RESET_S, NOW, WEEK_RESET_S, edited, put
from tests.reporter_env import Rig
from tests.test_card7a3 import one_message
from tests.test_card7c2_capture import source as limit_source
from tests.test_card7c2_delivery import LIMIT_PATH, Net
from tests.test_card7c2_reader import marker

READINGS_PATH = "/v1/readings"
ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"
PARK_DIR = os.path.join("state", "usage-limit-park")
SEC = 1000


class ReadingNet(Net):
    """A fake Leasewise: readings answer from `reading`, limit events from `limit`, leases from `lease`. Each is a
    list of (status, doc) answers or exceptions; the last one repeats."""

    def __init__(self, reading=None, lease=None, limit=None):
        super().__init__(lease=lease, limit=limit)
        self.reading = list(reading or [(202, {"event_id": "x", "accepted": True})])

    def __call__(self, method, path, headers, raw, timeout):
        if path != READINGS_PATH:
            return super().__call__(method, path, headers, raw, timeout)
        self.calls.append((path, headers["Idempotency-Key"], raw))
        answer = self.reading.pop(0) if len(self.reading) > 1 else self.reading[0]
        if isinstance(answer, BaseException):
            raise answer
        status, doc = answer
        return status, doc if isinstance(doc, bytes) else json.dumps(doc).encode()

    def readings(self):
        return [c for c in self.calls if c[0] == READINGS_PATH]


def reading_source(bus_root, account="claude-main", text=CLAUDE_ACCOUNT):
    row = cfg.ResolvedReadingSource()
    row.bus_root, row.folder, row.provider = bus_root, cfg.folder_key(bus_root), "claude"
    row.agenttalk_account, row.account, row.account_id = text, account, ACCOUNT_ID
    return row


def at(seconds):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW // 1000 + seconds))


class Stop(Exception):
    """A crash, injected between the answer and its marking."""


class _Rig(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        self.rig.clock.ms = NOW
        self.bus = os.path.join(self.rig.ws.dir, "bus")
        self.folder = os.path.join(self.bus, "state")
        os.makedirs(os.path.join(self.bus, PARK_DIR))
        self.mono = [1000.0]
        self.figure()

    def figure(self, seen=0, five=2.0, week=14.0, five_reset=FIVE_RESET_S, week_reset=WEEK_RESET_S, week_seen=None):
        """The one capacity file, as of `seen` seconds after the fixtures' moment (both windows; `week_seen` keeps the
        weekly figure at an earlier observation, which is then the same fact as before)."""
        week_seen = seen if week_seen is None else week_seen
        put(self.folder, edited("claude_stream_both", source_agent="seat-a", observed_at=at(seen), primary_observed_at=at(seen),
                                secondary_observed_at=at(week_seen), primary_used_percent=five, secondary_used_percent=week,
                                primary_resets_at=five_reset, secondary_resets_at=week_reset))

    def start(self, net, message=False, limits=False, fault=None):
        if message:
            one_message(self.rig.journal, self.rig.clock.ms)
        reporter = self.rig.open(transport=net, fault=fault)
        reporter.monotonic = lambda: self.mono[0]
        reporter.reading_sources = (reading_source(self.bus),)
        if limits:
            reporter.limit_sources = (limit_source(self.bus),)
        return reporter

    def restart(self, net, **kw):
        self.rig.close()
        return self.start(net, **kw)

    def advance(self, ms):
        self.rig.clock.ms += ms

    def cycle(self, reporter, ms=0):
        """Move the reporter's clock by `ms` (it may go back) and run one cycle. The monotonic clock only goes forward and
        always by at least the scan spacing, so every cycle looks at the files (the spacing itself is tested in the wiring tests)."""
        self.advance(ms)
        self.mono[0] += max(ms / 1000.0, 31.0)
        return reporter.cycle()

    def rows(self, where=""):
        return self.rig.state.all("SELECT window_key, state, tries, not_before_ms, last_error_code, observed_ms FROM reading %s "
                                  "ORDER BY id" % where)

    def gate(self, window="five_hour"):
        return self.rig.state.one("SELECT gate_ms, last_sent_reset_ms FROM reading_window WHERE window_key=?", (window,))

    def park(self):
        now = self.rig.clock.ms
        doc = marker(agent="agent-one", now_ms=now, parked_at="2026-10-08T11:59:00.000000Z",
                     reset_epoch=now // 1000 + 3600, wake_epoch=now // 1000 + 3630)
        with open(os.path.join(self.bus, PARK_DIR, "agent-one.json"), "w", encoding="utf-8") as handle:
            json.dump(doc, handle)


class Answers(_Rig):
    def test_an_accepted_answer_marks_the_row_sent_and_moves_the_gate(self):
        for status in (200, 201, 202):
            with self.subTest(status=status):
                self.rig.close()
                self.rig.state = None
                for suffix in ("", "-wal", "-shm"):
                    if os.path.exists(self.rig.ws.state + suffix):
                        os.remove(self.rig.ws.state + suffix)
                self.rig.clock.ms = NOW
                net = ReadingNet(reading=[(status, {"event_id": "x", "accepted": True})])
                reporter = self.start(net)
                result = self.cycle(reporter)
                self.assertEqual(result.sent, 2)
                self.assertEqual([r[1] for r in self.rows()], ["sent", "sent"])
                self.assertEqual(self.gate(), (NOW + READING_PACE_MS, FIVE_RESET_S * 1000))
                self.assertEqual(self.gate("seven_day"), (NOW + READING_PACE_MS, WEEK_RESET_S * 1000))

    def test_the_wire_carries_the_stored_bytes_the_event_id_and_the_right_path(self):
        net = ReadingNet()
        self.cycle(self.start(net))
        stored = {r[0]: r[1] for r in self.rig.state.all("SELECT event_id, body FROM reading")}
        calls = net.readings()
        self.assertEqual(len(calls), 2)
        for path, key, raw in calls:
            self.assertEqual(path, READINGS_PATH)
            self.assertEqual(raw.decode(), stored[key])
            self.assertEqual(json.loads(raw)["event_id"], key)

    def test_a_refusal_fails_only_that_row_with_its_closed_word_and_leaves_the_gate_alone(self):
        for status, code in ((400, "INVALID_REQUEST"), (403, "FORBIDDEN"), (404, "NOT_FOUND"), (409, "IDEMPOTENCY_CONFLICT"),
                             (422, "SOMETHING_ELSE")):
            with self.subTest(status=status):
                self.rig.close()
                for suffix in ("", "-wal", "-shm"):
                    if os.path.exists(self.rig.ws.state + suffix):
                        os.remove(self.rig.ws.state + suffix)
                self.rig.clock.ms = NOW
                expected = code if code in decisions.CLOSED_CODES else decisions.UNKNOWN_CODE
                net = ReadingNet(reading=[(status, {"error": {"code": code, "message": "x"}})])
                reporter = self.start(net)
                result = self.cycle(reporter)
                self.assertEqual(result.blocked, None)
                self.assertEqual([(r[1], r[4]) for r in self.rows()], [("failed_permanent", expected)] * 2)
                self.assertEqual(self.gate(), (0, None))
                self.assertEqual(self.rig.state.all("SELECT * FROM agent_state"), [])
                self.assertEqual(len(net.readings()), 2)                       # each failed row once; none is tried again
                self.cycle(reporter, 10 * SEC)
                self.assertEqual(len(net.readings()), 2)

    def test_a_401_is_retried_with_its_word_and_the_credential_note(self):
        net = ReadingNet(reading=[(401, {"error": {"code": "UNAUTHENTICATED", "message": "x"}})])
        reporter = self.start(net)
        result = self.cycle(reporter)
        self.assertEqual(result.blocked, run_mod.CREDENTIAL)
        self.assertEqual([(r[1], r[2], r[4]) for r in self.rows()], [("pending", 1, "UNAUTHENTICATED"), ("pending", 0, None)])   # the pass stops at the first
        self.assertEqual(self.gate(), (0, None))
        self.assertEqual(len(net.readings()), 1)
        self.assertIsNotNone(self.rig.state.note("credential_active"))

    def test_no_answer_429_and_5xx_are_retried_and_leave_the_gate_alone(self):
        answers = {"no answer": OSError("down"), "429": (429, {"error": {"code": "RATE_LIMITED", "message": "x"}}),
                   "500": (500, {}), "503": (503, {"error": {"code": "UNAVAILABLE", "message": "x"}}),
                   "garbled 202": (202, b"not json"), "empty 202": (202, b"")}
        for label, answer in answers.items():
            with self.subTest(label):
                self.rig.close()
                for suffix in ("", "-wal", "-shm"):
                    if os.path.exists(self.rig.ws.state + suffix):
                        os.remove(self.rig.ws.state + suffix)
                self.rig.clock.ms = NOW
                net = ReadingNet(reading=[answer])
                reporter = self.start(net)
                result = self.cycle(reporter)
                self.assertEqual(result.blocked, run_mod.UNREACHABLE)
                self.assertEqual([(r[1], r[2]) for r in self.rows()], [("pending", 1), ("pending", 0)])     # the pass stops at the first
                self.assertGreater(self.rows()[0][3], NOW)
                self.assertEqual(self.gate(), (0, None))

    def test_the_answer_body_is_not_kept_or_shown(self):
        marker_text = "LOUD-ACCEPTED-BODY-77"
        net = ReadingNet(reading=[(202, {"event_id": marker_text, "accepted": marker_text, "state": marker_text})])
        self.cycle(self.start(net))
        dump = json.dumps([self.rig.state.all("SELECT * FROM %s" % t) for t in ("reading", "reading_window", "counter", "note")])
        self.assertNotIn(marker_text, dump)

    def test_a_reading_never_stops_an_agent_or_rebuilds(self):
        for status, code in ((403, "FORBIDDEN"), (404, "NOT_FOUND"), (400, "CONTEXT_UNKNOWN"), (409, "ESTIMATE_STALE")):
            self.assertEqual(decisions.decide(decisions.READING, decisions.Response(status, code)), decisions.Decision(decisions.FAIL, code))


class Gate(_Rig):
    def sent(self, net):
        return len(net.readings())

    def test_the_first_figure_of_a_window_is_sent_at_once(self):
        net = ReadingNet()
        self.assertEqual(self.cycle(self.start(net)).sent, 2)

    def test_the_gate_holds_the_next_figure_for_one_pace_then_opens(self):
        net = ReadingNet()
        reporter = self.start(net)
        self.cycle(reporter)
        self.figure(seen=30, five=50.0)
        self.cycle(reporter, 31 * SEC)                                                  # stored at once ...
        self.assertEqual(self.sent(net), 2)                                             # ... held by the gate
        self.assertEqual(self.rows("WHERE state='pending'")[0][0], "five_hour")
        self.cycle(reporter, 88 * SEC)                                                  # 119 s after the first send
        self.assertEqual(self.sent(net), 2)
        self.cycle(reporter, 1 * SEC)                                                   # exactly one pace
        self.assertEqual(self.sent(net), 4)
        self.assertEqual(self.gate(), (self.rig.clock.ms + READING_PACE_MS, FIVE_RESET_S * 1000))

    def test_a_retry_does_not_move_the_gate_and_the_row_waits_only_its_own_back_off(self):
        net = ReadingNet()
        reporter = self.start(net)
        self.cycle(reporter)
        gate_after_first = self.gate()
        self.figure(seen=130, five=50.0, week_seen=0)
        net.reading[:] = [(503, {})]
        self.cycle(reporter, 131 * SEC)                                                 # the gate is open; the answer is a 5xx
        self.assertEqual(self.gate(), gate_after_first)                                  # unchanged by the retry
        row = self.rows("WHERE state='pending'")[0]
        self.assertEqual(row[2], 1)
        net.reading[:] = [(202, {})]
        before = self.sent(net)
        self.cycle(reporter, 1 * SEC)                                                   # still inside the row's back-off
        self.assertEqual(self.sent(net), before)
        self.cycle(reporter, 20 * SEC)                                                  # back-off over (5 s to 15 min; the first is under 5 s x 1)
        self.assertGreater(self.sent(net), before)
        self.assertGreater(self.gate()[0], gate_after_first[0])

    def test_a_new_reset_period_passes_the_gate(self):
        net = ReadingNet()
        reporter = self.start(net)
        self.cycle(reporter)
        self.figure(seen=10, five=3.0, five_reset=FIVE_RESET_S + 5 * 3600, week_seen=0)   # the 5-hour period turned over
        self.cycle(reporter, 11 * SEC)
        self.assertEqual(self.sent(net), 3)                                              # sent at once, gate still shut
        self.assertEqual(self.gate()[1], (FIVE_RESET_S + 5 * 3600) * 1000)
        self.figure(seen=20, five=4.0, five_reset=FIVE_RESET_S + 5 * 3600, week_seen=0)  # the next figure of the new period waits
        self.cycle(reporter, 10 * SEC)
        self.assertEqual(self.sent(net), 3)

    def test_the_other_window_is_unaffected_by_a_gate(self):
        net = ReadingNet()
        reporter = self.start(net)
        self.cycle(reporter)
        self.figure(seen=30, five=50.0, week=60.0)
        self.cycle(reporter, 31 * SEC)
        self.assertEqual(self.sent(net), 2)                                              # both held
        self.rig.state.conn.execute("UPDATE reading_window SET gate_ms=0 WHERE window_key='seven_day'")
        self.cycle(reporter, 1 * SEC)
        self.assertEqual(self.sent(net), 3)                                              # only the weekly one goes
        self.assertEqual([r[0] for r in self.rows("WHERE state='pending'")], ["five_hour"])

    def test_two_sends_of_one_window_are_at_least_one_pace_apart(self):
        net = ReadingNet()
        reporter = self.start(net)
        times = []
        for step in range(40):
            self.figure(seen=step * 10, five=2.0 + step)
            before = self.sent(net)
            self.cycle(reporter, 10 * SEC if step else 0)
            if self.sent(net) > before:
                times.append(self.rig.clock.ms)
        gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
        self.assertTrue(gaps)
        self.assertTrue(all(g >= READING_PACE_MS for g in gaps), gaps)

    def test_after_an_outage_and_a_restart_exactly_one_row_per_window_is_sent_and_then_the_gate_holds(self):
        down = ReadingNet(reading=[OSError("down")])
        reporter = self.start(down)
        for step in range(30):                                                           # figures keep arriving for hours
            self.figure(seen=step * 600, five=2.0 + step % 50)
            self.cycle(reporter, 600 * SEC)
        self.assertEqual(len(self.rows("WHERE state='pending'")), 2)
        self.assertGreater(len(self.rows("WHERE state='superseded'")), 40)
        up = ReadingNet()
        self.advance(20 * 60 * SEC)                                                      # past any back-off
        self.figure(seen=30 * 600 + 20 * 60 - 10)
        reporter = self.restart(up)
        self.cycle(reporter)
        self.assertEqual(len(up.readings()), 2)                                          # exactly one per window
        self.figure(seen=30 * 600 + 20 * 60 + 5, five=77.0)
        self.cycle(reporter, 20 * SEC)
        self.assertEqual(len(up.readings()), 2)                                          # the gate holds the next
        self.cycle(reporter, 100 * SEC)
        self.assertEqual(len(up.readings()), 3)

    def test_the_restart_keeps_the_gate(self):
        net = ReadingNet()
        reporter = self.start(net)
        self.cycle(reporter)
        gate = self.gate()
        self.figure(seen=30, five=50.0)
        reporter = self.restart(net)
        self.cycle(reporter, 31 * SEC)
        self.assertEqual(self.gate(), gate)
        self.assertEqual(len(net.readings()), 2)

    def test_a_crash_between_the_answer_and_the_marking_resends_the_same_bytes_once_more(self):
        net = ReadingNet()

        def crash(stage):
            if stage == "answered":
                raise Stop()
        reporter = self.start(net, fault=crash)
        with self.assertRaises(Stop):
            self.cycle(reporter)
        first = net.readings()[0]
        self.assertEqual([r[1] for r in self.rows()], ["pending", "pending"])
        self.assertEqual(self.gate(), (0, None))
        reporter = self.restart(net)
        self.cycle(reporter, 10 * SEC)
        again = [c for c in net.readings() if c[1] == first[1]]
        self.assertEqual(len(again), 2)
        self.assertEqual(again[0][2], again[1][2])                                       # the same bytes under the same key
        self.assertEqual([r[1] for r in self.rows()], ["sent", "sent"])
        self.assertEqual(self.rig.state.one("SELECT COUNT(*) FROM reading")[0], 2)       # no duplicate row
        self.assertGreater(self.gate()[0], 0)


class Clock(_Rig):
    def test_a_clock_that_moved_back_an_hour_is_clamped_to_one_pace_and_one_back_off(self):
        net = ReadingNet()
        reporter = self.start(net)
        self.cycle(reporter)
        self.figure(seen=130, five=50.0)
        net.reading[:] = [(503, {})]
        self.cycle(reporter, 131 * SEC)                                                  # a retry is pending with a deadline
        self.figure(seen=300, five=61.0)
        net.reading[:] = [(202, {})]
        self.rig.state.conn.execute("UPDATE reading_window SET gate_ms=? WHERE window_key='five_hour'", (self.rig.clock.ms + 7_200_000,))
        self.rig.state.conn.execute("UPDATE reading SET not_before_ms=? WHERE state='pending' AND window_key='five_hour'",
                                    (self.rig.clock.ms + 7_200_000,))
        self.advance(-3_600_000)                                                         # the clock jumps back one hour
        now = self.rig.clock.ms
        self.assertIsNone(self.rig.state.next_reading_item(now))
        self.assertEqual(self.gate()[0], now + READING_PACE_MS)                          # stored clamped
        self.assertEqual(self.rows("WHERE state='pending'")[0][3], now + 900_000)
        self.rig.close()
        self.rig.open(transport=net)
        self.assertEqual(self.gate()[0], now + READING_PACE_MS)                          # a restart keeps the clamped values
        self.assertEqual(self.rig.state.one("SELECT not_before_ms FROM reading WHERE state='pending'")[0], now + 900_000)

    def test_after_the_clock_moved_back_the_next_send_comes_within_one_pace(self):
        net = ReadingNet()
        reporter = self.start(net)
        self.cycle(reporter)
        self.figure(seen=40, five=50.0, week_seen=0)
        self.cycle(reporter, 41 * SEC)                                                   # stored, held by the gate
        self.assertEqual(len(net.readings()), 2)
        self.cycle(reporter, -3_600_000)                                                 # the clock jumps back one hour: gate clamped, held
        before = len(net.readings())
        self.cycle(reporter, READING_PACE_MS - 1)
        self.assertEqual(len(net.readings()), before)
        self.cycle(reporter, 1)
        self.assertGreater(len(net.readings()), before)

    def test_the_clamp_only_lowers_it_never_raises_a_gate_or_a_deadline(self):
        net = ReadingNet()
        reporter = self.start(net)
        self.cycle(reporter)
        gate = self.gate()
        self.rig.state.next_reading_item(self.rig.clock.ms)
        self.assertEqual(self.gate(), gate)


class Edges(_Rig):
    """The exact edges of the clamps and of the offer rule, on the stored rows."""

    def store(self):
        reporter = self.start(ReadingNet(reading=[OSError("down")]))
        self.cycle(reporter)                                                              # two pending rows, the first tried
        self.rig.state.conn.execute("UPDATE reading SET not_before_ms=0, tries=0")
        return self.rig.clock.ms

    def test_a_gate_is_clamped_only_when_it_is_more_than_one_pace_ahead(self):
        now = self.store()
        state = self.rig.state
        for ahead, expected in ((READING_PACE_MS - 1, READING_PACE_MS - 1), (READING_PACE_MS, READING_PACE_MS),
                                (READING_PACE_MS + 1, READING_PACE_MS), (7_200_000, READING_PACE_MS)):
            state.conn.execute("UPDATE reading_window SET gate_ms=?", (now + ahead,))
            state.next_reading_item(now)
            self.assertEqual(state.one("SELECT gate_ms FROM reading_window WHERE window_key='five_hour'")[0], now + expected, ahead)

    def test_a_retry_deadline_is_clamped_only_when_it_is_more_than_the_longest_back_off_ahead(self):
        now = self.store()
        state = self.rig.state
        state.conn.execute("UPDATE reading_window SET gate_ms=?", (now + 10 * READING_PACE_MS,))     # keep every row from being offered
        for ahead, expected in ((899_999, 899_999), (900_000, 900_000), (900_001, 900_000), (7_200_000, 900_000)):
            state.conn.execute("UPDATE reading SET not_before_ms=?", (now + ahead,))
            state.next_reading_item(now)
            self.assertEqual(state.one("SELECT not_before_ms FROM reading WHERE window_key='five_hour'")[0], now + expected, ahead)

    def test_a_row_is_offered_exactly_when_its_back_off_is_over(self):
        now = self.store()
        state = self.rig.state
        state.conn.execute("UPDATE reading_window SET gate_ms=0")
        for not_before, offered in ((now - 1, True), (now, True), (now + 1, False)):
            state.conn.execute("UPDATE reading SET not_before_ms=?", (not_before,))
            self.assertEqual(state.next_reading_item(now) is not None, offered, not_before - now)

    def test_a_row_is_offered_exactly_when_the_gate_opens_or_its_reset_is_new(self):
        now = self.store()
        state = self.rig.state
        reset = FIVE_RESET_S * 1000
        for gate, last_sent, offered in ((now - 1, reset, True), (now, reset, True), (now + 1, reset, False),
                                         (now + 1, reset - 1, True), (now + 1, None, True)):
            state.conn.execute("UPDATE reading_window SET gate_ms=?, last_sent_reset_ms=? WHERE window_key='five_hour'", (gate, last_sent))
            state.conn.execute("UPDATE reading_window SET gate_ms=? WHERE window_key='seven_day'", (now + 99,))
            state.conn.execute("UPDATE reading_window SET last_sent_reset_ms=? WHERE window_key='seven_day'", (WEEK_RESET_S * 1000,))
            item = state.next_reading_item(now)
            self.assertEqual(item is not None, offered, (gate - now, last_sent))

    def test_the_oldest_row_is_offered_first(self):
        now = self.store()
        item = self.rig.state.next_reading_item(now)
        first = self.rig.state.one("SELECT event_id FROM reading ORDER BY id LIMIT 1")[0]
        self.assertEqual(item.key, first)
        self.assertEqual((item.kind, item.path, item.agent, item.message_id), (decisions.READING, READINGS_PATH, READING_AGENT, ""))
        self.assertEqual(item.ref[0], "reading")


class Isolation(_Rig):
    def test_a_parked_lease_account_does_not_block_a_reading(self):
        net = ReadingNet()                                                               # leases: 429 ACCOUNT_PARKED
        reporter = self.start(net, message=True)
        result = self.cycle(reporter)
        self.assertEqual(len(net.readings()), 2)
        self.assertEqual(self.rig.state.one("SELECT state FROM acquisition")[0], "pending")
        self.assertEqual(result.blocked, run_mod.UNREACHABLE)

    def test_a_reading_retry_does_not_block_leases(self):
        lease = str(uuid.uuid4())
        net = ReadingNet(reading=[(503, {})], lease=[(201, {"lease_id": lease, "state": "active"}), (200, {})])
        reporter = self.start(net, message=True)
        self.cycle(reporter)
        self.assertEqual(self.rig.state.one("SELECT state FROM acquisition")[0], "acquired")
        self.assertEqual(self.rows("WHERE state='pending'")[0][2], 1)

    def test_a_reading_retry_does_not_block_limit_events_and_the_reverse(self):
        net = ReadingNet(reading=[(503, {})])
        reporter = self.start(net, limits=True)
        self.park()
        self.cycle(reporter)
        self.assertEqual(self.rig.state.one("SELECT state FROM limit_event")[0], "sent")
        net2 = ReadingNet(limit=[(503, {})])
        self.rig.close()
        reporter = self.rig.open(transport=net2)
        reporter.monotonic = lambda: self.mono[0]
        reporter.reading_sources = (reading_source(self.bus),)
        reporter.limit_sources = (limit_source(self.bus),)
        self.figure(seen=1)
        self.cycle(reporter, 1 * SEC)
        self.assertEqual(self.rig.state.one("SELECT COUNT(*) FROM reading WHERE state='sent'")[0], 2)

    def test_the_passes_run_in_order_limit_events_readings_then_leases(self):
        net = ReadingNet(lease=[(201, {"lease_id": str(uuid.uuid4()), "state": "active"}), (200, {})])
        reporter = self.start(net, message=True, limits=True)
        self.park()
        self.cycle(reporter)
        paths = net.paths()
        self.assertEqual(paths[0], LIMIT_PATH)
        self.assertEqual(paths[1:3], [READINGS_PATH, READINGS_PATH])
        self.assertTrue(all(p.startswith("/v1/leases") for p in paths[3:]))

    def test_there_is_no_per_cycle_cap_but_a_window_has_one_row(self):
        net = ReadingNet()
        reporter = self.start(net)
        reporter.reading_sources = tuple(reading_source(self.bus, "alias-%d" % n, "claude:<os-user>:home-%08x" % n) for n in range(8))
        for n in range(8):
            put(self.folder, edited("claude_stream_both", source_agent="seat-%d" % n, account="claude:<os-user>:home-%08x" % n))
        self.cycle(reporter)
        self.assertEqual(len(net.readings()), 16)
        self.assertEqual(self.rig.state.one("SELECT COUNT(*) FROM reading WHERE state='pending'")[0], 0)

    def test_the_wait_between_cycles_is_capped_at_60_seconds_while_readings_are_on(self):
        import threading

        class Stopper(threading.Event):
            def __init__(self):
                super().__init__()
                self.waits = []

            def wait(self, timeout=None):
                self.waits.append(timeout)
                self.set()
                return True
        for readings_on, capped in ((True, True), (False, False)):
            stop = Stopper()
            self.rig.close()
            reporter = self.rig.open(stop=stop, transport=ReadingNet(lease=[(503, {})], reading=[(503, {})]))
            reporter.reading_sources = (reading_source(self.bus),) if readings_on else ()
            reporter.config.poll_seconds = 5
            reporter.failures = 8
            one_message(self.rig.journal, self.rig.clock.ms)
            reporter.run()
            self.assertEqual(stop.waits[0] <= run_mod.LIMIT_CAPTURE_MAX_WAIT_SECONDS, capped)


class NoReturn(_Rig):
    def test_a_settled_reading_never_becomes_pending_again(self):
        net = ReadingNet()
        reporter = self.start(net)
        self.cycle(reporter)
        for state in ("sent", "expired", "superseded", "failed_permanent"):
            self.rig.state.conn.execute("UPDATE reading SET state=? WHERE window_key='five_hour'", (state,))
            with self.assertRaises(sqlite3.IntegrityError) as caught:
                self.rig.state.conn.execute("UPDATE reading SET state='pending' WHERE window_key='five_hour'")
            self.assertIn("never becomes pending", str(caught.exception))
            self.assertEqual(self.rows("WHERE window_key='five_hour'")[0][1], state)

    def test_a_pending_row_can_still_move_on_and_a_new_pending_row_can_be_stored(self):
        net = ReadingNet(reading=[(503, {})])
        reporter = self.start(net)
        self.cycle(reporter)
        self.rig.state.conn.execute("UPDATE reading SET state='superseded' WHERE window_key='five_hour'")
        self.figure(seen=5, five=9.0, week_seen=0)
        self.cycle(reporter, 6 * SEC)
        self.assertIn("pending", [r[1] for r in self.rows("WHERE window_key='five_hour'")])


class BodyCheck(_Rig):
    def test_a_stored_body_that_no_longer_fits_is_not_sent_and_fails_once(self):
        net = ReadingNet()
        net = ReadingNet(reading=[(503, {})])
        reporter = self.start(net)
        self.cycle(reporter)                                                              # both rows are stored; the first is tried
        net.reading[:] = [(202, {})]
        self.rig.state.conn.execute("DROP TRIGGER reading_immutable")
        self.rig.state.conn.execute("UPDATE reading SET body=? WHERE state='pending' AND window_key='five_hour'", ('{"event_id": 1}',))
        sent = len(net.readings())
        self.cycle(reporter, 20 * SEC)
        self.assertEqual(len([c for c in net.readings()[sent:] if c[1] == self.rig.state.one(
            "SELECT event_id FROM reading WHERE window_key='five_hour'")[0]]), 0)
        last = self.rows("WHERE window_key='five_hour'")[-1]
        self.assertEqual((last[1], last[4]), ("failed_permanent", "INVALID_REQUEST"))
        self.assertEqual(self.rig.state.counter(READING_AGENT, "body_invalid"), 1)
        self.cycle(reporter, 10 * SEC)
        self.assertEqual(self.rig.state.counter(READING_AGENT, "body_invalid"), 1)


class ShadowAdmission(unittest.TestCase):
    """The seats run in shadow: a reading changes what `status` shows and nothing a seat experiences. The OUTCOME of a
    shadow lease (granted, or refused with the same code) is the same with and without readings; the answer may differ in
    its annotations, so it is not compared byte for byte."""

    def run_rig(self, with_readings):
        rig = Rig()
        self.addCleanup(rig.cleanup)
        bus = os.path.join(rig.ws.dir, "bus")
        folder = os.path.join(bus, "state")
        os.makedirs(folder)
        one_message(rig.journal, rig.clock.ms)
        reporter = rig.open()
        if with_readings:
            accounts = cfg.load_snapshot(rig.ws.snapshot)["accounts"]
            alias, account_id = accounts[0]
            row = cfg.ResolvedReadingSource()
            row.bus_root, row.folder, row.provider = bus, cfg.folder_key(bus), "claude"
            row.agenttalk_account, row.account, row.account_id = CLAUDE_ACCOUNT, alias, account_id
            reporter.reading_sources = (row,)
            import time
            seen = lambda seconds: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rig.clock.ms / 1000 + seconds))      # noqa: E731
            put(folder, edited("claude_stream_both", source_agent="seat-a", observed_at=seen(-5), primary_observed_at=seen(-5),
                               secondary_observed_at=seen(-5), primary_used_percent=99.0, secondary_used_percent=99.0,
                               primary_resets_at=int(rig.clock.ms / 1000) + 3600, secondary_resets_at=int(rig.clock.ms / 1000) + 86_400))
        reporter.cycle()
        outcome = rig.state.one("SELECT state, last_error_code FROM acquisition")
        readings = rig.state.all("SELECT state, last_error_code FROM reading")
        rig.close()
        return outcome, readings

    def test_the_lease_outcome_is_the_same_with_and_without_readings(self):
        plain, none = self.run_rig(False)
        with_readings, sent = self.run_rig(True)
        self.assertEqual(none, [])
        self.assertEqual(with_readings, plain)
        self.assertEqual(len(sent), 2)


if __name__ == "__main__":
    unittest.main()
