"""Card 7c-2, step 4 fix round 1: `reporter status` while an OLDER reporter still owns the state.

During an upgrade the old reporter keeps its state file and leaves a snapshot of its status next to
it. `status` then shows that snapshot. The old snapshot (status version 1) has no `limit_events`
part, so the text view must not fail on it, and must never say that limit capture is off just
because the old format cannot say anything about it. A snapshot with a version this program does
not know is refused with a fixed sentence. The old snapshot is written by the REAL old code from
the repository's own history (`da91e1f`). Synthetic data only.
"""

import argparse
import contextlib
import io
import json
import os
import subprocess
import tempfile
import types
import unittest

from quota_balancer.reporter import cli, status

NOW = 1_788_224_400_000
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLDER = ("A reporter from before this version is still running. Its status has no limit-event section, "
         "so nothing is said here about limit events.")


def old_module(name):
    """The reporter module `name` exactly as it was at da91e1f, loaded from git."""
    raw = subprocess.check_output(["git", "-C", REPO, "show", "da91e1f:quota_balancer/reporter/%s.py" % name])
    module = types.ModuleType("quota_balancer.reporter.baseline_old_" + name)
    module.__package__ = "quota_balancer.reporter"
    exec(compile(raw, "baseline_%s.py" % name, "exec"), module.__dict__)
    return module


class _Owned(unittest.TestCase):
    """An old reporter owns the state file and has written its snapshot."""

    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH") or None
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-7c2up-")
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "state.db")
        old_state, old_status = old_module("state"), old_module("status")
        self.owner = old_state.State(self.path)
        self.addCleanup(self.owner.close)
        self.old_status = old_status
        old_status.write_snapshot(old_status.snapshot_path(self.path),
                                  old_status.build_status(self.owner, set(), NOW, 86400))
        self.config = types.SimpleNamespace(state_path=self.path, agents={}, silent_after_seconds=86400,
                                            limit_sources=())

    def run_status(self, as_json):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli._status(self.config, argparse.Namespace(json=as_json))
        return code, out.getvalue(), err.getvalue()

    def write_snapshot(self, doc):
        with open(status.snapshot_path(self.path), "w", encoding="ascii") as handle:
            json.dump(doc, handle)


class OlderSnapshot(_Owned):

    def test_the_real_old_snapshot_is_version_1_without_limit_events(self):
        snap = status.read_snapshot(status.snapshot_path(self.path))
        self.assertEqual(snap["status_version"], 1)
        self.assertNotIn("limit_events", snap)

    def test_the_text_view_shows_the_old_status_and_says_it_is_old(self):
        code, out, err = self.run_status(False)
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("reporter status ("), out)
        self.assertIn("snapshot of the running reporter", out)
        self.assertIn(OLDER, out.splitlines())

    def test_the_text_never_says_that_limit_capture_is_off_or_on(self):
        self.config.limit_sources = (types.SimpleNamespace(os_user="u", provider="claude", account="a"),)
        _code, out, _err = self.run_status(False)
        lowered = out.lower()
        for claim in ("limit events (best effort)", "not configured", "capture is off", "no limit events", "disabled"):
            self.assertNotIn(claim, lowered, claim)

    def test_the_json_view_is_the_old_snapshot_as_it_is(self):
        code, out, err = self.run_status(True)
        self.assertEqual((code, err), (0, ""))
        shown = json.loads(out)
        self.assertEqual(shown["status_version"], 1)
        self.assertNotIn("limit_events", shown)                  # nothing is invented for the old format
        self.assertEqual(shown["source"], "snapshot of the running reporter")

    def test_the_current_version_is_still_shown_without_the_sentence(self):
        snap = status.read_snapshot(status.snapshot_path(self.path))
        snap.update(status_version=status.STATUS_VERSION,
                    limit_events={"configured": False, "gap": None})
        self.write_snapshot(snap)
        for as_json in (False, True):
            code, out, _err = self.run_status(as_json)
            self.assertEqual(code, 0)
            self.assertNotIn(OLDER, out)


class UnknownVersion(_Owned):

    def refused(self, doc_changes):
        snap = status.read_snapshot(status.snapshot_path(self.path))
        snap.update(doc_changes)
        self.write_snapshot(snap)
        for as_json in (False, True):
            code, out, err = self.run_status(as_json)
            self.assertEqual((code, out), (2, ""), doc_changes)
            error = json.loads(err)["error"]
            self.assertEqual(error["code"], "STATUS_VERSION")
            self.assertEqual(error["message"], cli.MSG_STATUS_VERSION)

    def test_a_newer_version_is_refused_with_a_fixed_sentence(self):
        self.refused({"status_version": status.STATUS_VERSION + 1})
        self.refused({"status_version": 99})

    def test_a_version_that_is_not_a_plain_whole_number_is_refused(self):
        for bad in (0, -1, "2", 2.0, True, None, [2]):
            self.refused({"status_version": bad})

    def test_a_snapshot_without_a_version_is_refused(self):
        snap = status.read_snapshot(status.snapshot_path(self.path))
        del snap["status_version"]
        self.write_snapshot(snap)
        code, out, err = self.run_status(False)
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(json.loads(err)["error"]["code"], "STATUS_VERSION")

    def test_the_refusal_carries_no_value_from_the_snapshot(self):
        self.refused({"status_version": 99, "agents": {"loud-agent-name-up": {}}})
        _code, _out, err = self.run_status(False)
        self.assertNotIn("loud-agent-name-up", err)
        self.assertNotIn("99", err)


if __name__ == "__main__":
    unittest.main()
