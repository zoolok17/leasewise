"""Card 7b, step 7b-4: `reporter status` shows readings (status version 5; DESIGN-7B2.md section 9).

The report gains a top-level `readings` object: closed words and counts, the provider and the Leasewise alias of each
configured row (never the agenttalk account text), the last stored figure of every window (from the window memory, so it
outlives the pruning of its row), and the stated gaps in the words of the design. Synthetic data; a fake transport.
"""

import json
import os
import unittest

from quota_balancer.reporter import readings as readings_mod, status
from tests.card7b2_fixtures import CLAUDE_ACCOUNT, NOW, edited, put
from tests.reporter_env import Rig
from tests.test_card7b2_delivery import ReadingNet, at, reading_source, SEC
from tests.test_card7a3 import cli

GAPS = ("Readings are best effort. A figure is stored only if agenttalk still counted it as current (ten minutes old at most) "
        "when the reporter looked at it. Each window keeps only its latest unsent figure, and Leasewise gets at most one figure "
        "per window every two minutes (a new reset period excepted). It can reach Leasewise later, after a short wait or an "
        "outage. A figure that is replaced by a newer one before its turn, or that is still unsent two hours after its reset "
        "period ended, is never delivered, even if it was the highest of its period, and what was never delivered cannot be "
        "recovered; the reporter's status counts both. An account whose seats are idle shows no current figure rather than a "
        "zero. A refusal that leaves no figure leaves no reading, and the reading never says \"refused\": refusals travel as "
        "limit events, which for now cover Claude only and are also best effort. Leasewise keeps the highest percentage it "
        "received within one reset period, so what it shows can differ from the last figure the reporter stored, which is what "
        "the reporter's own status shows. The reporter cannot see which login is behind an agenttalk account, so the table of "
        "accounts is trusted as the operator wrote it.")
OBJECT_KEYS = {"configured", "sources", "last_scan", "scan", "captured", "superseded", "expired", "pruned", "body_invalid",
               "refused_streak", "rows", "failed", "retrying", "capture_fault", "last_stored", "last_ok", "gaps"}
LOUD = "SECRET-USER-status-4c1d"


class _Status(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        self.rig.clock.ms = NOW
        self.bus = os.path.join(self.rig.ws.dir, "bus")
        self.folder = os.path.join(self.bus, "state")
        os.makedirs(self.folder)
        self.mono = [1000.0]
        self.figure()

    def figure(self, name="claude_stream_both", stem="seat-a", **over):
        put(self.folder, edited(name, source_agent=stem, **over))

    def reporter(self, net=None, readings_on=True):
        reporter = self.rig.open(transport=net or ReadingNet())
        reporter.monotonic = lambda: self.mono[0]
        if readings_on:
            reporter.reading_sources = (reading_source(self.bus),)
        return reporter

    def cycle(self, reporter, ms=0):
        self.rig.clock.ms += ms
        self.mono[0] += max(ms / 1000.0, 31.0)
        return reporter.cycle()


class Report(_Status):
    def test_the_status_version_is_5_and_the_object_has_a_closed_shape(self):
        report = self.reporter().report()
        self.assertEqual(report["status_version"], 5)
        self.assertEqual(set(report["readings"]), OBJECT_KEYS)
        self.assertEqual(set(report["readings"]["scan"]), set(readings_mod.SCAN_COUNTS))

    def test_without_sources_it_says_not_configured_and_carries_nothing(self):
        report = self.reporter(readings_on=False).report()
        reading = report["readings"]
        self.assertFalse(reading["configured"])
        self.assertEqual((reading["sources"], reading["captured"], reading["gaps"], reading["last_stored"]), ([], 0, None, []))
        self.assertEqual(reading["rows"], dict.fromkeys(("pending", "sent", "failed_permanent", "expired", "superseded"), 0))
        self.assertEqual(status.render_text(report).count("readings: off"), 1)
        self.assertNotIn("readings (best effort)", status.render_text(report))

    def test_configured_it_lists_provider_and_alias_only_and_the_gaps_verbatim(self):
        reading = self.reporter().report()["readings"]
        self.assertTrue(reading["configured"])
        self.assertEqual(reading["sources"], [{"provider": "claude", "account": "claude-main"}])
        self.assertEqual(reading["gaps"], GAPS)
        self.assertEqual(status.READING_GAPS, GAPS)

    def test_the_text_has_exactly_one_line_with_the_sentence(self):
        text = status.render_text(self.reporter().report())
        lines = [line for line in text.splitlines() if line.startswith("readings")]
        self.assertEqual(lines, ["readings (best effort): " + GAPS])

    def test_the_last_stored_figures_come_from_the_window_memory_and_outlive_pruning(self):
        reporter = self.reporter()
        self.cycle(reporter)
        reading = reporter.report()["readings"]
        self.assertEqual(reading["rows"]["sent"], 2)
        expected = [{"account": "claude-main", "window": "five_hour", "used_percent": 2.0, "observed_at": status.iso(NOW),
                     "resets_at": "2026-10-08T16:00:00.000Z"},
                    {"account": "claude-main", "window": "seven_day", "used_percent": 14.0, "observed_at": status.iso(NOW),
                     "resets_at": "2026-10-12T00:00:00.000Z"}]
        self.assertEqual(reading["last_stored"], expected)
        self.assertEqual(reading["last_ok"], status.iso(NOW))
        self.assertEqual(reading["captured"], 2)
        self.rig.state.housekeep_readings(NOW + 400 * 86_400_000)
        after = reporter.report()["readings"]
        self.assertEqual(after["rows"]["sent"], 0)
        self.assertEqual(after["pruned"], 2)
        self.assertEqual(after["last_stored"], expected)                          # the memory outlives its rows

    def test_the_counters_replaced_and_expired_are_visible(self):
        reporter = self.reporter(ReadingNet(reading=[OSError("down")]))
        self.cycle(reporter)
        self.figure(primary_observed_at=at(40), primary_used_percent=60.0, secondary_observed_at=at(0))
        self.cycle(reporter, 41 * SEC)
        reading = reporter.report()["readings"]
        self.assertEqual(reading["superseded"], 1)
        self.rig.clock.ms = NOW + 6 * 3_600_000
        self.mono[0] += 100
        reporter.cycle()
        self.assertGreaterEqual(reporter.report()["readings"]["expired"], 1)

    def test_a_retry_is_visible_with_its_word_and_a_garbled_202_is_repeated_as_a_retry(self):
        reporter = self.reporter(ReadingNet(reading=[(202, b"not json")]))
        self.cycle(reporter)
        self.cycle(reporter, 100 * SEC)
        reading = reporter.report()["readings"]
        self.assertEqual(reading["retrying"], {"MALFORMED_RESPONSE": 1})                 # the pass stops at the first retry
        self.assertEqual(self.rig.state.one("SELECT tries FROM reading WHERE tries>0")[0], 2)      # the same bytes, tried again
        self.assertEqual(reading["rows"]["sent"], 0)
        self.assertEqual(reading["failed"], {})

    def test_a_refusal_is_counted_by_its_closed_word_and_shown_to_the_operator(self):
        for status_code, code, expected in ((403, "FORBIDDEN", "FORBIDDEN"), (409, "IDEMPOTENCY_CONFLICT", "IDEMPOTENCY_CONFLICT"),
                                            (422, LOUD, "UNKNOWN_CODE")):
            with self.subTest(code=code):
                self.rig.close()
                for suffix in ("", "-wal", "-shm"):
                    if os.path.exists(self.rig.ws.state + suffix):
                        os.remove(self.rig.ws.state + suffix)
                reporter = self.reporter(ReadingNet(reading=[(status_code, {"error": {"code": code, "message": LOUD}})]))
                self.cycle(reporter)
                report = reporter.report()
                self.assertEqual(report["readings"]["failed"], {expected: 2})
                joined = " ".join(report["operator_actions"])
                if expected == "FORBIDDEN":
                    self.assertIn("this machine is not an authorized reporter for the account: ask whoever manages Leasewise", joined)
                else:
                    self.assertIn("2 reading(s) failed for good", joined)
                self.assertNotIn(LOUD, json.dumps(report))

    def test_an_unmapped_file_gets_the_operator_line_without_naming_anything(self):
        self.figure(account="claude:%s:home-deadbeef" % LOUD)
        reporter = self.reporter()
        self.cycle(reporter)
        report = reporter.report()
        self.assertEqual(report["readings"]["scan"]["unmapped"], 1)
        line = [x for x in report["operator_actions"] if "not in readings.sources" in x]
        self.assertEqual(line, [status.UNMAPPED_ACTION % 1])
        self.assertIn('Run "agenttalk capacity" to see the account names, then add a row, or ignore this', line[0])
        self.assertNotIn(LOUD, json.dumps(report) + status.render_text(report))

    def test_a_format_refusal_seen_for_many_scans_in_a_row_gets_a_line_and_a_clean_scan_clears_it(self):
        put(self.folder, edited("claude_stream_both", source_agent="seat-z", scope="seat"))
        reporter = self.reporter()
        for _ in range(status.REFUSED_SCANS_IN_A_ROW - 1):
            self.cycle(reporter)
        self.assertFalse([x for x in reporter.report()["operator_actions"] if "documented capacity format" in x])
        self.cycle(reporter)
        report = reporter.report()
        self.assertEqual(report["readings"]["refused_streak"], status.REFUSED_SCANS_IN_A_ROW)
        self.assertEqual([x for x in report["operator_actions"] if "documented capacity format" in x],
                         [status.FORMAT_ACTION % status.REFUSED_SCANS_IN_A_ROW])
        os.remove(os.path.join(self.folder, "seat-z.capacity.json"))
        self.cycle(reporter)
        self.assertEqual(reporter.report()["readings"]["refused_streak"], 0)

    def test_many_scans_in_a_row_means_ten_about_five_minutes_at_the_scan_spacing(self):
        self.assertEqual(status.REFUSED_SCANS_IN_A_ROW, 10)

    def test_empty_files_never_raise_the_format_line(self):
        for number, name in enumerate(("unknown_unbound", "unknown_bound", "claude_stream_stale", "claude_stream_status_only")):
            put(self.folder, edited(name, source_agent="seat-%d" % number))
        reporter = self.reporter()
        for _ in range(status.REFUSED_SCANS_IN_A_ROW + 2):
            self.cycle(reporter)
        report = reporter.report()
        self.assertEqual(report["readings"]["refused_streak"], 0)
        self.assertFalse([x for x in report["operator_actions"] if "capacity format" in x])

    def test_a_capture_fault_and_a_damaged_stored_body_are_shown(self):
        reporter = self.reporter(ReadingNet(reading=[(503, {})]))
        self.cycle(reporter)
        self.rig.state.conn.execute("DROP TRIGGER reading_immutable")
        self.rig.state.conn.execute("UPDATE reading SET body='{}' WHERE window_key='five_hour'")
        self.cycle(reporter, 30 * SEC)
        self.rig.state.note_readings_fault(NOW)
        report = reporter.report()
        self.assertEqual(report["readings"]["body_invalid"], 1)
        self.assertEqual(report["readings"]["failed"], {"INVALID_REQUEST": 1})
        self.assertEqual(report["readings"]["capture_fault"], {"count": 1, "last": status.iso(NOW)})
        joined = " ".join(report["operator_actions"])
        self.assertIn("no longer fitted the allowed shape", joined)
        self.assertIn("reading capture failed 1 time(s)", joined)

    def test_nothing_private_is_in_the_report_or_the_text(self):
        self.figure(plan_type=LOUD, reason=LOUD, last_status_window=LOUD, account=CLAUDE_ACCOUNT)
        reporter = self.reporter()
        self.cycle(reporter)
        report = reporter.report()
        everything = json.dumps(report) + status.render_text(report)
        for word in (LOUD, CLAUDE_ACCOUNT, "<os-user>", "home-0123abcd", "seat-a", self.bus, "claude_stream"):
            self.assertNotIn(word, everything)

    def test_the_snapshot_a_running_reporter_leaves_is_ascii_and_carries_the_object(self):
        reporter = self.reporter()
        self.cycle(reporter)
        path = status.snapshot_path(self.rig.ws.state)
        with open(path, "rb") as handle:
            raw = handle.read()
        raw.decode("ascii")
        snapshot = json.loads(raw)
        self.assertEqual(snapshot["status_version"], 5)
        self.assertEqual(set(snapshot["readings"]), OBJECT_KEYS)


class Command(_Status):
    def test_the_status_command_reads_the_same_object(self):
        reporter = self.reporter()
        self.cycle(reporter)
        self.rig.close()
        code, out, err = cli(["status", "--config", self.rig.ws.config, "--json"], permissions=lambda p: None)
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["status_version"], 5)
        self.assertFalse(report["readings"]["configured"])                 # this configuration file has no readings block
        self.assertEqual(report["readings"]["rows"]["sent"], 2)

    def test_the_text_view_through_the_command_says_off_when_not_configured(self):
        self.rig.close()
        code, out, err = cli(["status", "--config", self.rig.ws.config], permissions=lambda p: None)
        self.assertEqual(code, 0, err)
        self.assertIn("readings: off", out)


if __name__ == "__main__":
    unittest.main()
