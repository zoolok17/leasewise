"""Card 7a-3, optional: the REAL agenttalk journal writer feeds the reporter.

Skipped unless QUOTA_REPORTER_AGENTTALK_SRC names a folder that holds
`agenttalk/turn_events.py` (for example the `src` folder of the working copy
that carries the turn journal). The suite never needs it. The test imports the
writer, drives it with a fake turn sequence on a simulated clock, lets the
reporter read the files it wrote, and compares the stored leases and receipts
with what was fed in. Nothing in agenttalk is changed.
"""

import importlib
import os
import sys
import unittest

from quota_balancer.reporter import mapper
from tests.reporter_env import Rig

SOURCE = os.environ.get("QUOTA_REPORTER_AGENTTALK_SRC", "")
HAS_WRITER = bool(SOURCE) and os.path.isfile(
    os.path.join(SOURCE, "agenttalk", "turn_events.py"))

USAGE = {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 50,
         "cache_write_tokens": 20}
RAW_DONE = {"consumed": True, "landed": True, "compliance_success": True,
            "dead_lettered": False, "terminal_failure": False}
RAW_NONE = {"consumed": None, "landed": None, "compliance_success": None,
            "dead_lettered": None, "terminal_failure": None}


@unittest.skipUnless(HAS_WRITER, "set QUOTA_REPORTER_AGENTTALK_SRC to an "
                     "agenttalk source folder with turn_events")
class RealWriter(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls._path = sys.path[:]
        cls._dont = sys.dont_write_bytecode
        cls._before = set(sys.modules)
        sys.dont_write_bytecode = True       # leave their folder untouched
        sys.path.insert(0, SOURCE)
        cls.te = importlib.import_module("agenttalk.turn_events")

    @classmethod
    def tearDownClass(cls):
        sys.path[:] = cls._path
        sys.dont_write_bytecode = cls._dont
        for name in set(sys.modules) - cls._before:
            if name == "agenttalk" or name.startswith("agenttalk."):
                del sys.modules[name]

    def setUp(self):
        self.rig = Rig(template=None)
        self.addCleanup(self.rig.cleanup)
        self.clock = self.rig.clock

    def sink(self):
        sink = self.te.TurnEventSink(
            self.rig.ws.journal, "alpha", agent_version="test-1",
            clock=self.clock.time)
        self.assertTrue(sink.start())
        return sink

    def at(self, seconds_ago):
        return self.te.format_time(self.clock.ms - int(seconds_ago * 1000))

    def turn(self, sink, mid, runs, disposition, raw=RAW_DONE, arrived=5):
        """One message: `runs` is [(turn id, outcome, usage)]; a disposition of
        None leaves the message open."""
        arrival = self.at(arrived)      # the message record's time: ONE value
        for turn_id, outcome, usage in runs:
            self.clock.advance(1)
            sink.emit("dispatch_started", message_id=mid, turn_id=turn_id,
                      cli="claude", cli_session="fresh",
                      message_at=arrival)
            self.clock.advance(1)
            launched = outcome != "not_launched"
            sink.emit("dispatch_ended", message_id=mid, turn_id=turn_id,
                      launched=launched, outcome=outcome,
                      exit="normal" if launched else "spawn_error",
                      duration_ms=1000, usage=usage)
        if disposition:
            self.clock.advance(1)
            sink.emit("message_disposed", message_id=mid,
                      disposition=disposition, message_at=arrival,
                      **raw)

    def settle(self, sink):
        """Wait for the writer's thread to put everything on disk."""
        sink.close()

    def test_a_real_journal_through_the_reporter_to_leasewise(self):
        # The agenttalk process runs, then restarts (a second stream).
        first = self.sink()
        self.turn(first, "m1", [("m1-a", "success", USAGE)], "completed")
        self.turn(first, "m2", [("m2-a", "failed", None),
                                ("m2-b", "success", USAGE)],
                  "delivery_failed",
                  raw=dict(RAW_NONE, consumed=True, terminal_failure=True))
        self.settle(first)
        second = self.sink()
        self.turn(second, "m3", [("m3-a", "success", USAGE)],
                  "outcome_unknown",
                  raw=dict(RAW_NONE, consumed=True, compliance_success=True,
                           landed=False))
        self.turn(second, "m4", [("m4-a", "success", USAGE)], None)
        self.turn(second, "m5", [("m5-a", "success", USAGE)], "completed")
        self.settle(second)

        self.rig.open()
        self.rig.drain_all()
        seat = self.rig.reporter.agents["alpha"].seat_id
        good = (170, 50, 10)
        ids = {mid: mapper.turn_uuid(seat, mid)
               for mid in ("m1", "m2", "m3", "m4", "m5")}
        expected = {
            mapper.dispatch_event_id(ids["m1"], "m1-a"): (0, "completed", 1)
            + good,
            mapper.final_event_id(ids["m1"]): (1, "completed", 1) + good,
            mapper.dispatch_event_id(ids["m2"], "m2-a"): (
                0, "failed", 1, None, None, None),
            mapper.dispatch_event_id(ids["m2"], "m2-b"): (0, "completed", 1)
            + good,
            mapper.final_event_id(ids["m2"]): (1, "failed", 2, None, None,
                                               None),
            mapper.dispatch_event_id(ids["m3"], "m3-a"): (0, "completed", 1)
            + good,                                  # outcome unknown: no final
            mapper.dispatch_event_id(ids["m4"], "m4-a"): (0, "completed", 1)
            + good,                                  # still open: no final
            mapper.dispatch_event_id(ids["m5"], "m5-a"): (0, "completed", 1)
            + good,
            mapper.final_event_id(ids["m5"]): (1, "completed", 1) + good,
        }
        got = {row[0]: row[1:] for row in self.rig.lw.rows(
            "SELECT event_id, final, outcome, calls, input_tokens,"
            " cached_input_tokens, output_tokens FROM receipts")}
        self.assertEqual(got, expected)
        self.assertEqual(self.rig.lw.rows(
            "SELECT enforcement, COUNT(*) FROM leases GROUP BY 1"),
            [("shadow", 5)])
        status = self.rig.reporter.report()["agents"]["alpha"]
        self.assertEqual(status["streams"]["seen"], 2)
        self.assertEqual(status["streams"]["missing"], 0)
        self.assertEqual(status["messages"]["open"], 1)
        self.assertEqual(status["messages"]["outcome_unknown"], 1)
        self.assertEqual(status["dropped_total"], 0)
        self.assertEqual(status["damaged_lines"], 0)
        # the writer's own files (status, streams) did not confuse the reader
        self.assertEqual(status["damaged_stream_records"], 0)


if __name__ == "__main__":
    unittest.main()
