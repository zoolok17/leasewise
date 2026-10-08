"""Card 7b, step 7b-4: the upgrade from the previous reporter version (state version 6, status version 4).

The old files are written by the REAL previous code from the repository's own history (`ba17d0e`, the main commit card 7b was
built on), not by a copy in a test. During an upgrade the old reporter keeps its state file and leaves a status snapshot next
to it; the new `status` shows that snapshot, says it comes from an older reporter, and never claims readings are off. A state
file of the previous version is migrated in one step, and the old reporter refuses the new file without changing it.
Synthetic data only.
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import types
import unittest

from quota_balancer.reporter import cli, status
from quota_balancer.reporter.state import State
from tests.card7b2_fixtures import NOW, edited, put
from tests.reporter_env import Rig
from tests.test_card7b2_delivery import ReadingNet, reading_source

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREVIOUS = "ba17d0e"
OLDER = status.OLDER_READINGS


def old_module(name):
    """The reporter module `name` exactly as it was at the previous version, loaded from git."""
    raw = subprocess.check_output(["git", "-C", REPO, "show", "%s:quota_balancer/reporter/%s.py" % (PREVIOUS, name)])
    module = types.ModuleType("quota_balancer.reporter.baseline_prev_" + name)
    module.__package__ = "quota_balancer.reporter"
    exec(compile(raw, "baseline_%s.py" % name, "exec"), module.__dict__)
    return module


class _Old(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH") or None
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-7b2up-")
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "state.db")
        self.old_state, self.old_status = old_module("state"), old_module("status")


class OlderSnapshot(_Old):
    """An old reporter owns the state file and has written its snapshot."""

    def setUp(self):
        super().setUp()
        self.owner = self.old_state.State(self.path)
        self.addCleanup(self.owner.close)
        self.old_status.write_snapshot(self.old_status.snapshot_path(self.path),
                                       self.old_status.build_status(self.owner, set(), NOW, 86400))
        self.config = types.SimpleNamespace(state_path=self.path, agents={}, silent_after_seconds=86400, limit_sources=(),
                                            reading_sources=(), gateway_receipts=None, admission=None)

    def run_status(self, as_json):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli._status(self.config, argparse.Namespace(json=as_json))
        return code, out.getvalue(), err.getvalue()

    def test_the_real_old_snapshot_is_version_4_without_readings(self):
        snap = status.read_snapshot(status.snapshot_path(self.path))
        self.assertEqual(snap["status_version"], 4)
        self.assertNotIn("readings", snap)
        self.assertIn("limit_events", snap)

    def test_the_text_view_says_the_reporter_is_older_and_never_that_readings_are_off(self):
        code, out, err = self.run_status(False)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("snapshot of the running reporter", out)
        self.assertIn(OLDER, out.splitlines())
        for claim in ("readings: off", "readings (best effort)"):
            self.assertNotIn(claim, out)

    def test_the_text_does_not_change_its_mind_when_readings_are_configured_here(self):
        self.config.reading_sources = (types.SimpleNamespace(provider="claude", account="claude-main"),)
        _code, out, _err = self.run_status(False)
        self.assertIn(OLDER, out.splitlines())
        self.assertNotIn("readings: off", out)

    def test_the_json_view_is_the_old_snapshot_as_it_is(self):
        code, out, err = self.run_status(True)
        self.assertEqual((code, err), (0, ""))
        shown = json.loads(out)
        self.assertEqual(shown["status_version"], 4)
        self.assertNotIn("readings", shown)                       # nothing is invented for the old format

    def test_a_snapshot_of_the_current_version_does_not_carry_the_sentence(self):
        snap = status.read_snapshot(status.snapshot_path(self.path))
        snap.update(status_version=status.STATUS_VERSION, readings={"configured": False, "gaps": None})
        with open(status.snapshot_path(self.path), "w", encoding="ascii") as handle:
            json.dump(snap, handle)
        _code, out, _err = self.run_status(False)
        self.assertNotIn(OLDER, out)
        self.assertIn("readings: off", out)

    def test_every_older_snapshot_version_renders_without_failing(self):
        for version in (1, 2, 3, 4):
            snap = status.read_snapshot(status.snapshot_path(self.path))
            for key in ("readings", "money_reader", "gateway_receipts", "limit_events")[:max(0, 5 - version)]:
                snap.pop(key, None)
            snap["status_version"] = version
            with open(status.snapshot_path(self.path), "w", encoding="ascii") as handle:
                json.dump(snap, handle)
            for as_json in (False, True):
                code, _out, err = self.run_status(as_json)
                self.assertEqual((code, err), (0, ""), version)


class OlderStateFile(_Old):
    def make_previous(self):
        """A real file of the previous state version, with rows in the lease and limit tables."""
        old = self.old_state.State(self.path)
        with old.txn():
            old.conn.execute("INSERT INTO acquisition(agent, message_id, turn_uuid, request_id, body_original, body_current)"
                             " VALUES('alpha','m1','t1','r1','{}','{}')")
            old.conn.execute("INSERT INTO limit_event(event_id, account_alias, body, captured_ms) VALUES('e1','a','{}',5)")
            old.conn.execute("INSERT INTO counter(agent, name, value) VALUES('alpha','reports_dropped',3)")
        self.assertEqual(old.one("PRAGMA user_version")[0], 6)
        old.close()

    def fingerprint(self):
        main = open(self.path, "rb").read()
        wal = open(self.path + "-wal", "rb").read() if os.path.exists(self.path + "-wal") else b""
        return hashlib.sha256(main + wal).hexdigest()

    def tables(self, names):
        import sqlite3
        conn = sqlite3.connect(self.path)
        try:
            return {t: conn.execute("SELECT * FROM %s ORDER BY 1" % t).fetchall() for t in names}
        finally:
            conn.close()

    def test_the_previous_version_is_migrated_in_one_step_with_every_old_row_untouched(self):
        self.make_previous()
        before = self.tables(("acquisition", "limit_event", "counter"))
        State(self.path).close()
        import sqlite3
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 7)
        conn.close()
        self.assertEqual(self.tables(("acquisition", "limit_event", "counter")), before)
        self.assertEqual(self.tables(("reading", "reading_window")), {"reading": [], "reading_window": []})

    def test_the_previous_reporter_refuses_the_new_file_and_changes_nothing(self):
        State(self.path).close()
        before = self.fingerprint()
        with self.assertRaises(self.old_state.StateError) as caught:
            self.old_state.State(self.path)
        self.assertEqual(str(caught.exception), "the state file version is not supported")
        self.assertEqual(self.fingerprint(), before)

    def test_a_migrated_file_works_end_to_end_and_status_v5_describes_it(self):
        self.make_previous()
        rig = Rig(server=False)
        self.addCleanup(rig.cleanup)
        rig.clock.ms = NOW
        os.makedirs(os.path.dirname(rig.ws.state), exist_ok=True)
        shutil.copyfile(self.path, rig.ws.state)
        bus = os.path.join(rig.ws.dir, "bus")
        folder = os.path.join(bus, "state")
        os.makedirs(folder)
        put(folder, edited("claude_stream_both", source_agent="seat-a"))
        net = ReadingNet()
        reporter = rig.open(transport=net)
        reporter.reading_sources = (reading_source(bus),)
        reporter.cycle()
        report = reporter.report()
        self.assertEqual(report["status_version"], 5)
        self.assertEqual(report["readings"]["rows"]["sent"], 2)
        self.assertEqual(rig.state.one("PRAGMA user_version")[0], 7)
        self.assertEqual(rig.state.one("SELECT COUNT(*) FROM limit_event")[0], 1)       # the old rows are still there


if __name__ == "__main__":
    unittest.main()
