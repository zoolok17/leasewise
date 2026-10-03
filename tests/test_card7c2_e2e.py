"""Card 7c-2, step 4: the whole path, end to end.

A synthetic, valid, fresh park marker in a temp agenttalk folder; the reporter configured with that
folder and its account map; one `cycle` against the REAL Leasewise HTTP handler (in-process, over
loopback, on a test store, with the test's simulated clock); then the account is parked until the
reset plus 30 seconds. No network beyond localhost, no real provider, no real marker.
"""

import datetime
import json
import os
import unittest

from quota_balancer.reporter.run import UNREACHABLE

from tests.reporter_env import Rig
from tests.test_card7a3 import one_message
from tests.test_card7c2_reader import marker

ALIAS = "meter"                                       # the account alias in the test store


def iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()                                                  # a real in-process Leasewise
        self.addCleanup(self.rig.cleanup)
        self.bus = os.path.join(self.rig.ws.dir, "bus")
        self.folder = os.path.join(self.bus, "state", "usage-limit-park")
        os.makedirs(self.folder)
        extra = ("[[limit_events.sources]]\nbus_root = %s\nos_user = \"operator-desktop\"\nprovider = \"claude\"\n"
                 "account = \"%s\"\n" % (json.dumps(self.bus), ALIAS))
        self.rig.ws.write_config(self.rig.lw.url, self.rig.agents_cfg, extra=extra)
        self.reset_s = self.rig.clock.ms // 1000 + 3600
        self.reporter = self.rig.open()

    def park(self, agent, **over):
        now = self.rig.clock.ms
        fields = {"parked_at": iso(now - 60_000), "reset_epoch": self.reset_s, "wake_epoch": self.reset_s + 30}
        fields.update(over)
        doc = marker(agent=agent, now_ms=now, **fields)
        with open(os.path.join(self.folder, agent + ".json"), "w", encoding="utf-8") as handle:
            json.dump(doc, handle)

    def blocks(self):
        return self.rig.lw.rows("SELECT retry_at, state, reason, model_id FROM blocks")

    def events(self):
        return self.rig.lw.rows("SELECT resets_at, evidence_kind, exhausted_windows FROM limit_events")

    def test_a_park_file_becomes_a_parked_account_until_the_reset_plus_thirty_seconds(self):
        self.assertEqual(self.blocks(), [])
        self.park("agent-one")
        result = self.reporter.cycle()
        self.assertEqual((result.new_limit_events, result.blocked), (1, None))
        # Leasewise: the account is parked, held until the reset plus 30 000 ms
        self.assertEqual(self.blocks(), [(self.reset_s * 1000 + 30_000, "parked", "claude_exhausted", None)])
        # the reset the reporter sent is kept raw as evidence
        self.assertEqual(self.events(), [(self.reset_s * 1000, "claude_exhausted", '["five_hour"]')])
        # the reporter: delivered, nothing waiting
        self.assertEqual(self.rig.state.all("SELECT state, tries FROM limit_event"), [("sent", 0)])

    def test_a_second_cycle_sends_nothing_new(self):
        self.park("agent-one")
        self.reporter.cycle()
        calls = len(self.rig.net.calls)
        before = (self.blocks(), self.events())
        result = self.reporter.cycle()
        self.assertEqual(result.new_limit_events, 0)
        self.assertEqual(len(self.rig.net.calls), calls)
        self.assertEqual((self.blocks(), self.events()), before)

    def test_a_second_seat_with_the_same_limit_adds_no_event(self):
        self.park("agent-one")
        self.reporter.cycle()
        calls = len(self.rig.net.calls)
        self.park("agent-two", message_id="m-two", parked_at=iso(self.rig.clock.ms - 30_000))
        result = self.reporter.cycle()
        self.assertEqual(result.new_limit_events, 0)
        self.assertEqual(len(self.rig.net.calls), calls)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(len(self.blocks()), 1)
        self.assertEqual(self.rig.state.all("SELECT COUNT(*) FROM limit_event"), [(1,)])

    def test_a_later_reset_is_a_new_event_that_moves_the_hold_later(self):
        self.park("agent-one")
        self.reporter.cycle()
        self.reset_s += 3600
        self.park("agent-one")
        self.assertEqual(self.reporter.cycle().new_limit_events, 1)
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(self.blocks()[0][0], self.reset_s * 1000 + 30_000)            # the later reset, margined once
        self.assertEqual(len(self.blocks()), 1)

    def test_the_parked_account_refuses_leases_and_the_limit_event_still_got_through(self):
        one_message(self.rig.journal, self.rig.clock.ms)
        self.park("agent-one")
        result = self.reporter.cycle()
        self.assertEqual(self.blocks()[0][1], "parked")                                  # delivered first, same cycle
        self.assertEqual(self.rig.state.all("SELECT state FROM limit_event"), [("sent",)])
        self.assertEqual(result.blocked, UNREACHABLE)                                    # the lease was refused (429)
        self.assertEqual(self.rig.state.one("SELECT state, last_error_code FROM acquisition")[1], "ACCOUNT_PARKED")
        self.assertEqual(self.rig.net.calls[0][0], "/v1/limit-events")

    def test_a_restart_in_the_middle_loses_and_repeats_nothing(self):
        self.park("agent-one")
        self.rig.net.down = True                                                         # Leasewise unreachable
        result = self.reporter.cycle()
        self.assertEqual(result.blocked, UNREACHABLE)
        self.assertEqual(self.rig.state.all("SELECT state, tries FROM limit_event"), [("pending", 1)])
        self.assertEqual(self.blocks(), [])
        self.rig.close()                                                                  # restart
        self.rig.net.down = False
        reporter = self.rig.open()
        self.rig.clock.advance(60)
        reporter.cycle()
        self.assertEqual(self.rig.state.all("SELECT state FROM limit_event"), [("sent",)])
        self.assertEqual(len(self.blocks()), 1)
        self.assertEqual(len(self.events()), 1)


if __name__ == "__main__":
    unittest.main()
