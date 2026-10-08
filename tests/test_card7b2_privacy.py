"""Card 7b, step 7b-4: `privacy_7b2` (DESIGN-7B2.md section 8).

The capacity files carry loud private markers: an account text holding an OS user name, a seat name, a path-like value, a
plan, a reason, a status word and an exception text. None of them may appear in any body (stored or on the wire), any
outbox row, the window memory, any counter name or value, any note, any status text (JSON and plain), any log line or the
state file itself; and every body has only the allow-listed keys. Synthetic data; a fake transport.
"""

import contextlib
import io
import json
import os
import unittest
from unittest import mock

from quota_balancer.reporter import mapper, readings, status
from tests.card7b2_fixtures import NOW, edited, put
from tests.reporter_env import Rig
from tests.test_card7a3 import cli
from tests.test_card7b2_delivery import ReadingNet, reading_source

USER = "LOUD-OS-USER-7731"
SEAT = "loud-seat-name-7731"
PATH = "C:/Loud/Private/Path-7731/config"
PLAN = "loud-plan-7731"
REASON = "loud-reason-7731"
STATUS_WORD = "loud-window-7731"
EXCEPTION = "loud-exception-text-7731"
ACCOUNT = "claude:%s:home-0123abcd" % USER
MARKERS = (USER, SEAT, PATH, PLAN, REASON, STATUS_WORD, EXCEPTION, "home-0123abcd", "7731")
ALLOWED_BODY = {"event_id", "account_id", "observed_at", "kind", "payload"}
ALLOWED_PAYLOAD = {"windows", "confidence", "source_stale", "covered_seq"}
ALLOWED_WINDOW = {"key", "used_bps", "resets_at"}


class Privacy(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        self.rig.clock.ms = NOW
        self.bus = os.path.join(self.rig.ws.dir, "bus-" + "folder")
        self.folder = os.path.join(self.bus, "state")
        os.makedirs(self.folder)
        self.mono = [1000.0]
        put(self.folder, edited("claude_stream_both", source_agent=SEAT, account=ACCOUNT, plan_type=PLAN, limit_id=PATH, reason=REASON,
                                last_status_window=STATUS_WORD, rate_limit_reached_type=STATUS_WORD, extra_field=PATH))
        put(self.folder, edited("unknown_unbound", source_agent="idle-seat", reason=REASON, plan_type=PLAN))
        put(self.folder, edited("claude_stream_both", source_agent="other-seat", account="claude:%s:home-deadbeef" % USER))
        put(self.folder, edited("claude_stream_both", source_agent="bad-seat", scope=REASON))

    def reporter(self, net):
        reporter = self.rig.open(transport=net)
        reporter.monotonic = lambda: self.mono[0]
        row = reading_source(self.bus, text=ACCOUNT)
        reporter.reading_sources = (row,)
        return reporter

    def cycle(self, reporter, ms=0):
        self.rig.clock.ms += ms
        self.mono[0] += max(ms / 1000.0, 31.0)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            reporter.cycle()
        return out.getvalue() + err.getvalue()

    def everything(self, reporter):
        state = self.rig.state
        state.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dump = [json.dumps(state.all("SELECT * FROM %s" % table)) for table in ("reading", "reading_window", "counter", "note", "limit_event")]
        report = reporter.report()
        dump.append(json.dumps(report))
        dump.append(status.render_text(report))
        with open(self.rig.ws.state, "rb") as handle:
            dump.append(handle.read().decode("latin-1"))
        with open(status.snapshot_path(self.rig.ws.state), "rb") as handle:
            dump.append(handle.read().decode("ascii"))
        return "\n".join(dump)

    def test_no_marker_appears_anywhere_after_a_normal_run(self):
        net = ReadingNet()
        reporter = self.reporter(net)
        logs = self.cycle(reporter)
        text = self.everything(reporter) + logs + b"".join(call[2] for call in net.readings()).decode() \
            + "".join(call[1] for call in net.readings())
        for marker in MARKERS:
            self.assertNotIn(marker, text, marker)

    def test_no_marker_appears_after_failures_faults_and_refusals(self):
        net = ReadingNet(reading=[OSError(EXCEPTION), (403, {"error": {"code": "FORBIDDEN", "message": EXCEPTION}}),
                                  (422, {"error": {"code": EXCEPTION, "message": EXCEPTION}}), (202, b"garbled " + EXCEPTION.encode())])
        reporter = self.reporter(net)
        logs = self.cycle(reporter)
        with mock.patch.object(readings, "run", side_effect=RuntimeError(EXCEPTION + USER)):
            logs += self.cycle(reporter, 40_000)
        logs += self.cycle(reporter, 200_000)
        logs += self.cycle(reporter, 200_000)
        text = self.everything(reporter) + logs + b"".join(call[2] for call in net.readings()).decode()
        for marker in MARKERS:
            self.assertNotIn(marker, text, marker)
        self.assertEqual(self.rig.state.counter("_readings", "capture_fault"), 1)

    def test_every_body_has_only_the_allow_listed_keys(self):
        net = ReadingNet()
        reporter = self.reporter(net)
        self.cycle(reporter)
        bodies = [json.loads(row[0]) for row in self.rig.state.all("SELECT body FROM reading")]
        bodies += [json.loads(call[2]) for call in net.readings()]
        self.assertEqual(len(bodies), 4)
        for body in bodies:
            self.assertEqual(set(body), ALLOWED_BODY)
            self.assertEqual(set(body["payload"]), ALLOWED_PAYLOAD)
            self.assertEqual(len(body["payload"]["windows"]), 1)
            self.assertEqual(set(body["payload"]["windows"][0]), ALLOWED_WINDOW)
            self.assertEqual((body["kind"], body["payload"]["confidence"], body["payload"]["source_stale"],
                              body["payload"]["covered_seq"]), ("cli_window", "observed", False, None))
            mapper.reading_body_text(body)

    def test_the_counter_names_are_closed_words(self):
        reporter = self.reporter(ReadingNet())
        self.cycle(reporter)
        names = [row[0] for row in self.rig.state.all("SELECT name FROM counter WHERE agent='_readings'")]
        allowed = {"captured", "superseded", "expired", "pruned", "capture_fault", "body_invalid", "refused_streak"}
        allowed |= {"scan:" + name for name in readings.SCAN_COUNTS}
        self.assertTrue(set(names) <= allowed, set(names) - allowed)

    def test_the_status_command_prints_no_marker(self):
        reporter = self.reporter(ReadingNet())
        self.cycle(reporter)
        self.rig.close()
        for argv in (["status", "--config", self.rig.ws.config], ["status", "--config", self.rig.ws.config, "--json"]):
            code, out, err = cli(argv, permissions=lambda p: None)
            self.assertEqual(code, 0, err)
            for marker in MARKERS:
                self.assertNotIn(marker, out + err)

    def test_the_configuration_errors_name_no_marker(self):
        from quota_balancer.reporter import config as cfg
        from tests.test_card7b2_config import _Tmp

        class Case(_Tmp):
            def runTest(self):
                pass
        case = Case()
        case.setUp()
        try:
            for row in (dict(case.row(), agenttalk_account="claude:%s" % USER), dict(case.row(), account=USER),
                        dict(case.row(), bus_root="relative/" + USER), dict(case.row(), provider=USER), dict(case.row(), extra=USER)):
                with self.assertRaises(cfg.ConfigError) as caught:
                    case.parse(row)
                for marker in MARKERS:
                    self.assertNotIn(marker, caught.exception.message)
        finally:
            case.doCleanups()


if __name__ == "__main__":
    unittest.main()
