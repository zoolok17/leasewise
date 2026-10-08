"""Card 7b, step 7b-4: readings from a fake bus folder to the REAL Leasewise reading handler and store reducer.

The reporter runs against the in-process loopback server the suite already uses (a real store, the real `handle_readings`
and `reduce`), with writer-made synthetic capacity files. It proves what DESIGN-7B2.md 6.6 says Leasewise does with the
stored bytes: one window leaves the other alone, a late reading is accepted, the highest figure of a reset period is kept,
a larger reset starts a new period only when the observation is at or after the stored reset, a replay is a no-op, a changed
body is a 409, and a machine that is not an authorized reporter for the account gets 403. Synthetic data only.
"""

import json
import os
import sqlite3
import time
import unittest

from quota_balancer.reporter import config as cfg, decisions
from quota_balancer.reporter.client import HttpClient
from quota_balancer.reporter.state import READING_AGENT, READING_PATH, Item
from tests.card7b2_fixtures import CLAUDE_ACCOUNT, edited, put
from tests.reporter_env import Rig

HOUR_S = 3600
DAY_S = 86_400


class _E2E(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.addCleanup(self.rig.cleanup)
        self.accounts = {a["alias"]: a["id"] for a in json.load(open(self.rig.ws.snapshot, encoding="utf-8"))["accounts"]}
        self.bus = os.path.join(self.rig.ws.dir, "bus")
        self.folder = os.path.join(self.bus, "state")
        os.makedirs(self.folder)
        self.mono = [1000.0]
        # The store refuses an observation more than 30 seconds ahead of the REAL time, so the test clock starts ten hours
        # in the past and has room to move forward (the server's own clock is the same controlled one).
        self.rig.clock.ms -= 10 * 3_600_000
        self.reporter = self.rig.open()
        self.reporter.monotonic = lambda: self.mono[0]
        self.use("meter")

    def use(self, alias, text=CLAUDE_ACCOUNT):
        row = cfg.ResolvedReadingSource()
        row.bus_root, row.folder, row.provider = self.bus, cfg.folder_key(self.bus), "claude"
        row.agenttalk_account, row.account, row.account_id = text, alias, self.accounts[alias]
        self.reporter.reading_sources = (row,)
        self.alias = alias

    def stamp(self, seconds):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.rig.clock.ms / 1000 + seconds))

    def figure(self, five=None, week=None, five_seen=-5, week_seen=-5, five_reset=HOUR_S, week_reset=3 * DAY_S, account=CLAUDE_ACCOUNT):
        """One capacity file as of now; a window given as None is left out (a figure with nothing in it)."""
        now_s = int(self.rig.clock.ms / 1000)
        over = dict(source_agent="seat-a", account=account, observed_at=self.stamp(max(five_seen, week_seen)),
                    primary_observed_at=self.stamp(five_seen), secondary_observed_at=self.stamp(week_seen),
                    primary_used_percent=five, secondary_used_percent=week,
                    primary_resets_at=None if five is None else now_s + five_reset,
                    secondary_resets_at=None if week is None else now_s + week_reset)
        if five is None:
            over.update(primary_observed_at=None, primary_window_minutes=None, primary_window_basis=None)
        if week is None:
            over.update(secondary_observed_at=None, secondary_window_minutes=None, secondary_window_basis=None)
        put(self.folder, edited("claude_stream_both", **over))

    def later(self, ms):
        """Move the clock (the reporter's and the server's) BEFORE a new file is written, so its times follow the clock."""
        self.rig.clock.ms += ms

    def cycle(self, ms=0):
        self.rig.clock.ms += ms
        self.mono[0] += max(ms / 1000.0, 31.0)
        return self.reporter.cycle()

    def windows(self, alias=None):
        conn = sqlite3.connect(self.rig.lw.db)
        try:
            return {r[0]: r[1:] for r in conn.execute(
                "SELECT window_key, used_bps, resets_at, observed_at FROM windows WHERE account_id=? ORDER BY window_key",
                (self.accounts[alias or self.alias],))}
        finally:
            conn.close()

    def rows(self):
        return self.rig.state.all("SELECT window_key, state, last_error_code FROM reading ORDER BY id")

    def item(self, window="five_hour", body=None):
        row = self.rig.state.one("SELECT id, event_id, body FROM reading WHERE window_key=? ORDER BY id DESC LIMIT 1", (window,))
        return Item(decisions.READING, READING_AGENT, "", READING_PATH, body or json.loads(row[2]), row[1], ("reading", row[0]), None)

    def client(self):
        config = cfg.load_config(self.rig.ws.config)
        return HttpClient(config.scheme, config.host, config.port, config.identity)


class RealHandler(_E2E):
    def test_both_windows_arrive_and_are_reduced_into_the_store(self):
        self.figure(five=20.0, week=50.0)
        self.cycle()
        self.assertEqual(self.rows(), [("five_hour", "sent", None), ("seven_day", "sent", None)])
        windows = self.windows()
        self.assertEqual(sorted(windows), ["five_hour", "seven_day"])
        self.assertEqual((windows["five_hour"][0], windows["seven_day"][0]), (2000, 5000))
        self.assertEqual(self.rig.state.counter(READING_AGENT, "capture_fault"), 0)

    def test_a_reading_of_one_window_leaves_the_other_window_as_it_was(self):
        self.figure(five=20.0, week=50.0)
        self.cycle()
        before = self.windows()
        self.later(130_000)
        self.figure(five=None, week=55.0, week_seen=-5)                    # only the weekly figure is new
        self.cycle()
        after = self.windows()
        self.assertEqual(after["five_hour"], before["five_hour"])          # untouched
        self.assertEqual(after["seven_day"][0], 5500)
        self.assertGreater(after["seven_day"][2], before["seven_day"][2])

    def test_a_late_reading_is_accepted_and_keeps_its_own_observation_time(self):
        self.figure(five=30.0, week=40.0)
        working = self.reporter.client._transport

        def down(*args, **kwargs):
            raise OSError("down")
        self.reporter.client._transport = down
        self.cycle()
        self.assertEqual({r[1] for r in self.rows()}, {"pending"})
        observed = self.rig.state.one("SELECT observed_ms FROM reading WHERE window_key='five_hour'")[0]
        self.reporter.client._transport = working
        self.cycle(5 * 60_000)                                             # five minutes later: delivered late
        self.assertEqual({r[1] for r in self.rows()}, {"sent"})
        self.assertEqual(self.windows()["five_hour"][2], observed)         # the window carries the reading's own old time

    def test_the_highest_figure_of_a_reset_period_is_kept_and_the_time_moves_on(self):
        self.figure(five=97.0, week=10.0)
        self.cycle()
        first = self.windows()["five_hour"]
        self.later(130_000)
        self.figure(five=20.0, week=10.0, five_seen=-1, five_reset=HOUR_S - 130)       # the same period, a lower figure, a newer time
        self.cycle()
        window = self.windows()["five_hour"]
        self.assertEqual(window[0], 9700)                                  # the high-water: 97 percent stays
        self.assertGreater(window[2], first[2])                            # with the newer observation time

    def test_a_larger_reset_before_the_stored_reset_is_jitter_in_the_same_period(self):
        self.figure(five=60.0, week=10.0, five_reset=HOUR_S)
        self.cycle()
        stored_reset = self.windows()["five_hour"][1]
        self.later(130_000)
        self.figure(five=5.0, week=10.0, five_seen=-1, five_reset=HOUR_S - 130 + 600)  # the clock is still before the stored reset
        self.cycle()
        window = self.windows()["five_hour"]
        self.assertEqual(window[0], 6000)                                 # high-water applies
        self.assertEqual(window[1], stored_reset)                         # the stored reset stays

    def test_a_larger_reset_at_or_after_the_stored_reset_starts_a_new_period(self):
        self.figure(five=60.0, week=10.0, five_reset=HOUR_S)
        self.cycle()
        stored_reset = self.windows()["five_hour"][1]
        self.rig.clock.ms = stored_reset                                    # the observation is exactly AT the stored reset
        self.figure(five=5.0, week=10.0, five_seen=0, five_reset=5 * HOUR_S)
        self.cycle()
        window = self.windows()["five_hour"]
        self.assertEqual(window[0], 500)                                  # a new period may lower the figure
        self.assertGreater(window[1], stored_reset)

    def test_the_epoch_rule_has_two_sides(self):
        """One millisecond before the stored reset the larger reset is jitter; at the reset it is a new period."""
        self.figure(five=60.0, week=10.0, five_reset=HOUR_S)
        self.cycle()
        stored_reset = self.windows()["five_hour"][1]
        self.rig.clock.ms = stored_reset - 2000
        self.figure(five=5.0, week=10.0, five_seen=-1, five_reset=5 * HOUR_S)
        self.cycle()
        self.assertEqual(self.windows()["five_hour"][0:2], (6000, stored_reset))

    def test_a_replay_of_the_stored_bytes_is_a_no_op_and_a_changed_body_is_a_409(self):
        self.figure(five=20.0, week=50.0)
        self.cycle()
        before = self.windows()
        client = self.client()
        replay = client.send(self.item())
        self.assertEqual((replay.status, replay.code), (202, None))
        self.assertEqual(self.windows(), before)
        body = json.loads(self.rig.state.one("SELECT body FROM reading WHERE window_key='five_hour'")[0])
        body["payload"]["windows"][0]["used_bps"] = 9999
        changed = client.send(self.item(body=body))
        self.assertEqual((changed.status, changed.code), (409, "IDEMPOTENCY_CONFLICT"))
        self.assertEqual(self.windows(), before)

    def test_a_machine_that_is_not_an_authorized_reporter_for_the_account_gets_a_403(self):
        conn = sqlite3.connect(self.rig.lw.db)
        conn.execute("DELETE FROM project_accounts WHERE account_id=?", (self.accounts["other"],))
        conn.commit()
        conn.close()
        self.use("other")
        self.figure(five=20.0, week=50.0)
        self.cycle()
        self.assertEqual({r[1:] for r in self.rows()}, {("failed_permanent", "FORBIDDEN")})
        self.assertEqual(self.windows("other"), {})                        # nothing was stored for the account
        report = self.reporter.report()
        self.assertEqual(report["readings"]["failed"], {"FORBIDDEN": 2})
        self.assertTrue([x for x in report["operator_actions"] if "not an authorized reporter" in x])

    def test_the_stored_bytes_are_what_the_handler_accepted(self):
        self.figure(five=20.0, week=50.0)
        self.cycle()
        for event_id, body in self.rig.state.all("SELECT event_id, body FROM reading"):
            doc = json.loads(body)
            self.assertEqual(doc["event_id"], event_id)
            self.assertEqual(doc["account_id"], self.accounts["meter"])
            self.assertEqual(set(doc), {"event_id", "account_id", "observed_at", "kind", "payload"})


if __name__ == "__main__":
    unittest.main()
