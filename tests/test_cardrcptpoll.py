"""Card RCPT-POLL (lead decision D-234): the receipt lane starts the gateway's receipts command at two speeds, not on every cycle.

* `waiting_interval_seconds` (default 15, 5 to 60) while a lease waits for its receipt, `idle_interval_seconds` (default 300, 60 to 900) when none
  waits, both counted from the last ATTEMPT, on the reporter's monotonic clock kept in memory. A restart reads at once. A fixture file starts no
  program: it is read every cycle, and the two keys are refused for it.
* A skipped read only delays: nothing is lost or counted twice, and every other duty of the cycle (the grant copy, delivering stored receipts, the
  uploads, the money reader) runs every time.
Synthetic values only; the clock is driven by the test, never slept on; fake commands are small scripts run by the base Python.
"""

import json
import unittest

from quota_balancer.reporter import config as cfg
from quota_balancer.reporter import runner

from tests import test_card7l6a1b_config as config_tests
from tests import test_card7l6a1b_wired as wired
from tests import test_cardr1reader3b_cycle as money
from tests.l6a1_env import add_binding, configure, ledger, lease, receipt, write_json
from tests.reporter_env import Rig
from tests.test_card7l6a1_delivery import Net
from tests.test_card7a3 import one_message
from tests.test_card7l6a1b_runner import PYTHON
from tests.test_cardr1reader3b_cycle import setUpModule, tearDownModule      # noqa: F401 - the money reader's env needs its store template

COMMAND = [PYTHON, "-c", "pass"]
LOUD = "loud-value-rcptpoll-4c1e"


class Settings(config_tests._Case):

    def source(self, keys=""):
        return self.parse(self.block(COMMAND, keys=keys)).gateway_receipts

    def test_the_defaults_are_15_and_300_for_a_command(self):
        one = self.source()
        self.assertEqual((one.waiting_interval_seconds, one.idle_interval_seconds), (15, 300))
        self.assertEqual((cfg.DEFAULT_WAITING_INTERVAL, cfg.DEFAULT_IDLE_INTERVAL), (15, 300))

    def test_the_bounds_are_5_to_60_and_60_to_900_and_each_end_is_taken(self):
        for low, high in ((5, 60), (60, 900)):
            self.assertEqual(low, cfg.WAITING_INTERVAL_RANGE[0] if high == 60 else cfg.IDLE_INTERVAL_RANGE[0])
        for value in (5, 60):
            self.assertEqual(self.source("waiting_interval_seconds = %d\n" % value).waiting_interval_seconds, value)
        for value in (60, 900):
            self.assertEqual(self.source("idle_interval_seconds = %d\n" % value).idle_interval_seconds, value)

    def test_a_value_outside_the_bounds_a_bool_a_text_or_a_float_is_refused_with_one_fixed_sentence(self):
        for key, bad in (("waiting_interval_seconds", ["4", "61", "0", "-5", "true", '"15"', "15.0", "[15]"]),
                         ("idle_interval_seconds", ["59", "901", "0", "-300", "false", '"300"', "300.0", "[300]"])):
            for value in bad:
                with self.subTest(key=key, value=value):
                    message = self.message(self.block(COMMAND, keys="%s = %s\n" % (key, value)))
                    self.assertEqual(message, "gateway_receipts: %s must be a whole number from %d to %d" % (
                        key, *(cfg.WAITING_INTERVAL_RANGE if key.startswith("waiting") else cfg.IDLE_INTERVAL_RANGE)))

    def test_the_sentence_never_holds_text_from_the_file(self):
        self.assertNotIn(LOUD, self.message(self.block(COMMAND, keys='idle_interval_seconds = "%s"\n' % LOUD)))

    def test_a_fixture_source_refuses_the_keys_and_has_none(self):
        for key in ("waiting_interval_seconds", "idle_interval_seconds"):
            message = self.message(self.block(fixture=True, keys="%s = 30\n" % key))
            self.assertEqual(message, "gateway_receipts: waiting_interval_seconds and idle_interval_seconds belong to a command source")
        one = self.parse(self.block(fixture=True)).gateway_receipts
        self.assertEqual((one.waiting_interval_seconds, one.idle_interval_seconds), (None, None))

    def test_the_resolved_source_keeps_the_intervals(self):
        one = self.source("waiting_interval_seconds = 20\nidle_interval_seconds = 120\n")
        snapshot = {"gateways": [(one.gateway, "7b1e3c52-0d4a-4a8e-9c61-5f2a8d3b9e01")]}
        resolved = cfg.resolve_gateway_receipts(self.parse(self.block(COMMAND, keys="waiting_interval_seconds = 20\nidle_interval_seconds = 120\n")), snapshot)
        self.assertEqual((resolved.waiting_interval_seconds, resolved.idle_interval_seconds), (20, 120))


class _Paced(wired._Wired):
    """The command source over the fake pager, with a driven clock (`later`). `start(count)` makes a ledger of `count` receipts, each with a grant."""

    def reads(self):
        return len(self.calls())

    def store_path(self):
        import os
        return os.path.join(self.rig.ws.dir, "admission.db")

    def grow(self, how_many=1, grant=True):
        """New receipts at the end of the ledger, with their grants (or without)."""
        for _ in range(how_many):
            one = lease()
            self.leases.append(one)
            self.rows.append(receipt(one, len(self.rows) + 1))
            if grant:
                add_binding(self.store_path(), one)
        write_json(self.ledger, ledger(self.rows))

    def waiting(self, reporter):
        """A grant whose receipt has not been written yet: a lease that waits."""
        one = lease()
        self.leases.append(one)
        add_binding(self.store_path(), one)
        return one

    def settled_ids(self):
        return [json.loads(call[2])["event_id"] for call in self.net.settles()]


class Speeds(_Paced):

    def test_idle_means_one_read_per_300_seconds_across_many_cycles(self):
        reporter = self.start(count=3)
        for _ in range(130):                                           # 5-second cycles for 645 seconds
            reporter.cycle()
            self.later(5)
        self.assertEqual(self.reads(), 3)                              # at 0, 300 and 600 seconds
        self.assertEqual(self.count("receipt_capture"), 3)

    def test_the_first_cycle_after_a_start_reads_at_once(self):
        reporter = self.start(count=1)
        self.assertIsNone(reporter.receipt_attempted)
        reporter.cycle()
        self.assertEqual(self.reads(), 1)

    def test_a_restart_reads_at_once(self):
        reporter = self.start(count=1)
        reporter.cycle()
        reporter.cycle()
        self.assertEqual(self.reads(), 1)
        again = self.rig.restart()
        again.monotonic = lambda: self.now
        again.cycle()                                                  # a new process: no memory of the last attempt
        self.assertEqual(self.reads(), 2)

    def test_a_lease_that_waits_is_read_every_15_seconds(self):
        reporter = self.start(count=1)
        reporter.cycle()                                               # read 1 at 0 s; nothing waits now
        self.later(5)
        self.waiting(reporter)
        for _ in range(6):                                             # cycles at 5, 10, ..., 30 seconds
            reporter.cycle()
            self.later(5)
        self.assertEqual(self.reads(), 3)                              # 0, then 15 and 30 (15 s after the attempt at 0 is 15)

    def test_a_grant_that_arrives_while_idle_turns_the_speed_up_at_once(self):
        reporter = self.start(count=1)
        reporter.cycle()
        self.later(100)                                                # 100 s idle: not due at 300
        reporter.cycle()
        self.assertEqual(self.reads(), 1)
        self.waiting(reporter)
        reporter.cycle()                                               # the grant is copied this cycle; 100 s >= 15 s since the attempt
        self.assertEqual(self.reads(), 2)

    def test_when_the_waiting_ends_the_speed_goes_back_to_300(self):
        reporter = self.start(count=1)
        reporter.cycle()
        self.later(5)
        one = self.waiting(reporter)
        self.later(10)
        reporter.cycle()                                               # read 2 at 15 s: the receipt is still not there
        self.assertEqual(self.reads(), 2)
        self.rows.append(receipt(one, len(self.rows) + 1))
        write_json(self.ledger, ledger(self.rows))
        self.later(15)
        reporter.cycle()                                               # read 3 at 30 s: it arrives, nothing waits any more
        self.assertEqual((self.reads(), self.count("receipt_capture")), (3, 2))
        for _ in range(10):                                            # 50 more seconds of 5-second cycles: idle speed
            self.later(5)
            reporter.cycle()
        self.assertEqual(self.reads(), 3)
        self.later(300)
        reporter.cycle()
        self.assertEqual(self.reads(), 4)

    def test_a_grant_past_its_own_expiry_does_not_keep_the_fast_speed(self):
        reporter = self.start(count=1)
        reporter.cycle()
        self.later(5)
        one = lease()
        add_binding(self.store_path(), {"lease_id": one, "expires_ms": 1})          # long past
        for _ in range(20):
            reporter.cycle()
            self.later(5)
        self.assertEqual(self.reads(), 1)                              # idle speed: 0..100 s, nothing is read again before 300 s

    def test_a_failed_read_counts_as_an_attempt(self):
        reporter = self.start(count=1)
        reporter.cycle()
        self.later(5)
        self.waiting(reporter)
        self.set_mode("exit1")
        for _ in range(7):                                             # cycles at 5, 10, ..., 35 seconds
            reporter.cycle()
            self.later(5)
        self.assertEqual(self.reads(), 3)                              # 0, 15, 30: not every cycle, although each failed
        self.assertEqual(self.receipt_status(reporter)["read_later"]["exit_code"], 2)

    def test_a_backlog_is_read_through_at_once_not_one_slice_per_interval(self):
        reporter = self.start(count=8)
        reporter.gateway_receipts.page_limit = 1
        reporter.cycle()                                               # six pages
        self.assertEqual(self.count("receipt_capture"), 6)
        self.later(1)
        reporter.cycle()                                               # the rest, although 1 s < 15 s
        self.assertEqual(self.count("receipt_capture"), 8)
        self.later(1)
        reporter.cycle()
        self.assertEqual(self.reads(), 8)                              # 6 + 2 pages; the last page said it had no more, so the pace holds again

    def test_a_skipped_cycle_writes_nothing_for_the_pace(self):
        reporter = self.start(count=2)
        reporter.cycle()
        before = (self.rig.state.all("SELECT * FROM receipt_capture"), self.rig.state.all("SELECT * FROM receipt_cursor"))
        self.later(5)
        reporter.cycle()
        self.assertEqual(before, (self.rig.state.all("SELECT * FROM receipt_capture"), self.rig.state.all("SELECT * FROM receipt_cursor")))
        self.assertEqual(self.reads(), 1)


class OtherDuties(_Paced):

    def test_a_skipped_cycle_still_copies_grants_and_delivers_stored_receipts(self):
        reporter = self.start(count=2)
        late = lease()                                                 # its receipt is in the ledger, its grant is not copied yet
        self.rows.append(receipt(late, 3))
        write_json(self.ledger, ledger(self.rows))
        reporter.cycle()                                               # read: three captured, two released, the third quarantined
        self.assertEqual((self.count("receipt_capture"), len(self.net.settles())), (3, 2))
        self.later(5)
        add_binding(self.store_path(), late)
        result = reporter.cycle()                                      # no read (idle), but the grant is copied and the receipt is released
        self.assertEqual(self.reads(), 1)
        self.assertEqual((result.sent, len(self.net.settles())), (1, 3))

    def test_a_skipped_cycle_still_reads_the_journal_and_sends_the_uploads_that_are_due(self):
        """The other lanes get real work AFTER the first cycle: a message in the journal (its acquisition is an upload that is due). On the skipped cycle
        the journal is read (`new_events`), the upload goes out (`/v1/leases`), and the receipts command is not started again."""
        reporter = self.start(count=1)
        reporter.cycle()                                               # read 1; the journal is still empty
        paths_before = len(self.net.paths())
        one_message(self.rig.journal, self.rig.clock.ms)               # new work for the journal lane and the upload lane
        self.later(5)
        result = reporter.cycle()                                      # a skipped read for the receipt lane
        self.assertEqual(self.reads(), 1)                              # the receipts command was not started again
        self.assertEqual(result.new_events, 3)                         # the journal was read: three new events
        self.assertIn("/v1/leases", self.net.paths()[paths_before:])   # the acquisition upload went out
        self.assertEqual(reporter.report()["gateway_receipts"]["last_seq"], 1)


class NothingLost(_Paced):

    def test_a_growing_ledger_read_at_two_speeds_gives_the_same_receipts_once_each(self):
        reporter = self.start(count=1)
        for step in range(60):                                         # 300 seconds of 5-second cycles; a receipt (with its grant) every 20 seconds
            if step % 4 == 0:
                self.grow()
            reporter.cycle()
            self.later(5)
        self.later(300)
        reporter.cycle()
        total = len(self.rows)
        self.assertGreater(total, 10)
        self.assertLess(self.reads(), 60)                              # far fewer starts than cycles
        self.assertEqual([row[0] for row in self.rig.state.all("SELECT seq FROM receipt_capture ORDER BY seq")], list(range(1, total + 1)))
        ids = self.settled_ids()
        self.assertEqual(len(ids), total)
        self.assertEqual(len(set(ids)), total)                         # delivered once each
        self.assertEqual(self.rig.state.all("SELECT COUNT(*) FROM receipt_delivery"), [(total,)])

    def test_an_unpaced_run_of_the_same_ledger_stores_exactly_the_same(self):
        paced = self.start(count=1)
        for step in range(40):
            if step % 4 == 0:
                self.grow()
            paced.cycle()
            self.later(5)
        self.later(300)
        paced.cycle()
        mine = self.rig.state.all("SELECT seq, ref_sha256, outcome, calls, input_tokens, output_tokens, actual_micro_eur, closed_at"
                                  " FROM receipt_capture ORDER BY seq")
        other = Rig(server=False)
        self.addCleanup(other.cleanup)
        configure(other, self.rows, bindings=self.leases)
        fixture_reporter = other.open(transport=Net())
        fixture_reporter.cycle()                                       # a fixture source is read whole every cycle
        theirs = other.state.all("SELECT seq, ref_sha256, outcome, calls, input_tokens, output_tokens, actual_micro_eur, closed_at"
                                 " FROM receipt_capture ORDER BY seq")
        self.assertEqual(mine, theirs)

    def test_a_ledger_that_changes_generation_inside_a_skipped_stretch_is_followed(self):
        reporter = self.start(count=2)
        reporter.cycle()
        self.later(5)
        new = [receipt(one, n + 1) for n, one in enumerate(self.leases)]
        write_json(self.ledger, ledger(new, generation="fedcba9876543210fedcba9876543210"))
        reporter.cycle()                                               # skipped
        self.assertEqual(self.reads(), 1)
        self.later(300)
        reporter.cycle()
        self.assertEqual([one["count"] for one in self.receipt_status(reporter)["generation_changes"]], [1])
        self.assertEqual(self.count("receipt_capture"), 4)             # the new ledger's two receipts are kept apart from the old two


class Fixture(unittest.TestCase):

    def test_a_fixture_source_is_read_on_every_cycle_without_any_clock(self):
        rig = Rig(server=False)
        self.addCleanup(rig.cleanup)
        one, two = lease(), lease()
        path = configure(rig, [receipt(one, 1)], bindings=[one, two])
        reporter = rig.open(transport=Net())
        reporter.monotonic = lambda: 0.0                               # the clock never moves
        reporter.cycle()
        write_json(path, ledger([receipt(one, 1), receipt(two, 2)]))
        reporter.cycle()
        self.assertEqual(rig.state.one("SELECT COUNT(*) FROM receipt_capture")[0], 2)
        self.assertIsNone(reporter.receipt_attempted)


class MoneyReader(money.Env):
    """The money reader reads its own receipts by its own position; pacing the receipt lane changes nothing for it."""

    def receipt_calls(self):
        return [call for call in self.gateway.calls if call[0] == runner.MODE_RECEIPTS]

    def test_every_report_is_still_made_while_the_receipt_lane_waits_for_its_interval(self):
        for _ in range(5):                                             # 70 s apart: each report is due, the receipt lane is not
            self.lane(wait=70)
        self.assertEqual(len(self.gateway.reports()), 5)
        self.assertEqual(self.reader("SELECT minted_seq FROM gwr_seq"), [(5,)])
        first = len(self.receipt_calls())                              # the lane's first read, and the money reader's catch-up to N
        self.assertGreaterEqual(first, 1)
        self.mono[0] = 400.0
        self.lane(wait=0)
        self.assertEqual(len(self.receipt_calls()), first + 1)         # the lane is due again after 300 s: one read


if __name__ == "__main__":
    unittest.main()
