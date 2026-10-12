"""Three-accounts step 0: the desktop export (`tools/three_accounts/export_capacity.py`).

The fixtures are complete schema-2 documents built from exactly the reader's key list plus the two tolerated keys, so they stay in step
with `quota_balancer.reporter.capacityreader`. Every account text and user name is made up; nothing here touches a real state folder.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "three_accounts"))
import export_capacity as ec  # noqa: E402
from quota_balancer.reporter import capacityreader  # noqa: E402

NOW = 1_791_800_000                      # a fixed "export time" (epoch seconds)
USER = "tester-one"                      # the made-up local user name used for the privacy check
ACCOUNT_A = "claude:tester-one:home-aaaaaaaa"
ACCOUNT_B = "claude:tester-one:home-bbbbbbbb"
ACCOUNT_X = "claude:tester-one:home-cccccccc"      # never in a label table


def stamp(epoch):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def doc(account=ACCOUNT_A, **over):
    """One complete capacity document: every listed key, the two tolerated keys, nothing else."""
    base = {
        "schema_version": 2, "source_agent": "unit-1", "source": "claude_stream", "observed_at": stamp(NOW - 120), "confidence": "observed",
        "reason": None, "scope": "account", "account": account,
        "primary_used_percent": 12.0, "primary_resets_at": NOW + 3600, "primary_window_minutes": 300, "primary_window_basis": "assumed",
        "primary_status": "allowed", "primary_observed_at": stamp(NOW - 120),
        "secondary_used_percent": 40.0, "secondary_resets_at": NOW + 400_000, "secondary_window_minutes": 10080, "secondary_window_basis": "assumed",
        "secondary_status": "allowed", "secondary_observed_at": stamp(NOW - 120),
        "last_status": "allowed", "last_status_window": "five_hour", "last_status_at": stamp(NOW - 120), "rate_limit_reached_type": None,
        "context_used_percent": None, "context_window_size": None, "context_tokens": None,
        "plan_type": None, "limit_id": None}
    base.update(over)
    return base


class Folder(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AGENTTALK_SCRATCH") or None)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.labels = self.tmp / "labels.json"
        self.labels.write_text(json.dumps({ACCOUNT_A: "A", ACCOUNT_B: "B"}), encoding="utf-8")
        self.out = self.tmp / "export.json"
        self.stems = 0

    def put(self, document, name=None, raw=None):
        self.stems += 1
        name = name or "unit-%d" % self.stems
        path = self.state / (name + ".capacity.json")
        path.write_bytes(raw if raw is not None else json.dumps(document).encode("utf-8"))
        return path

    def run_export(self, machine="desk-top", user=USER):
        return ec.run(str(self.state), str(self.labels), machine, str(self.out), now=NOW, user_name=user)

    def exported(self):
        return json.loads(self.out.read_text(encoding="utf-8"))


class FixturesStayInStepWithTheReader(unittest.TestCase):
    def test_the_key_lists_are_the_readers(self):
        self.assertEqual(ec.LISTED_KEYS, capacityreader.LISTED_KEYS)
        self.assertEqual(ec.TOLERATED_KEYS, capacityreader.TOLERATED_KEYS)
        self.assertEqual(set(doc()), capacityreader.LISTED_KEYS | capacityreader.TOLERATED_KEYS)

    def test_a_fixture_is_a_file_the_readers_own_strict_check_accepts(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("AGENTTALK_SCRATCH") or None) as folder:
            path = Path(folder) / "unit-1.capacity.json"
            path.write_text(json.dumps(doc()), encoding="utf-8")
            outcome, _capacity = capacityreader.read_capacity(str(path), "unit-1", NOW * 1000)
        self.assertEqual(outcome, capacityreader.OK)


class Choosing(Folder):
    def test_a_mapped_file_is_exported_with_its_figures(self):
        self.put(doc())
        code, line = self.run_export()
        self.assertEqual(code, 0, line)
        data = self.exported()
        self.assertEqual(data["export_version"], 1)
        self.assertEqual((data["machine"], data["exported_at"]), ("desk-top", NOW))
        self.assertEqual(data["rows"], [
            {"label": "A", "window": "five_hour", "used_percent": 12.0, "resets_at": NOW + 3600, "observed_at": NOW - 120, "source": "claude_stream", "seats": 1},
            {"label": "A", "window": "seven_day", "used_percent": 40.0, "resets_at": NOW + 400_000, "observed_at": NOW - 120, "source": "claude_stream", "seats": 1}])
        self.assertEqual(data["counts"]["files"], 1)
        self.assertEqual(data["counts"]["used"], 1)
        self.assertEqual(sorted(data), ["counts", "export_version", "exported_at", "machine", "rows"])

    def test_the_newest_observation_wins_across_a_labels_seat_files(self):
        self.put(doc(primary_used_percent=10.0, primary_observed_at=stamp(NOW - 900)))
        self.put(doc(primary_used_percent=30.0, primary_observed_at=stamp(NOW - 60), source="claude_statusline"))
        self.put(doc(ACCOUNT_B, primary_used_percent=5.0))
        self.run_export()
        rows = {(r["label"], r["window"]): r for r in self.exported()["rows"]}
        self.assertEqual(rows[("A", "five_hour")]["used_percent"], 30.0)
        self.assertEqual(rows[("A", "five_hour")]["source"], "claude_statusline")
        self.assertEqual(rows[("A", "five_hour")]["seats"], 2)         # the number of seat files that carried the label
        self.assertEqual(rows[("B", "five_hour")]["seats"], 1)

    def test_each_window_uses_its_own_observed_time_and_falls_back_to_the_file_time(self):
        self.put(doc(primary_observed_at=stamp(NOW - 30), secondary_used_percent=41.0, secondary_observed_at=stamp(NOW - 5000)))
        self.put(doc(primary_observed_at=stamp(NOW - 4000), secondary_used_percent=44.0, secondary_observed_at=stamp(NOW - 100)))
        self.put(doc(ACCOUNT_B, primary_observed_at=None, observed_at=stamp(NOW - 77)))
        self.run_export()
        rows = {(r["label"], r["window"]): r for r in self.exported()["rows"]}
        self.assertEqual(rows[("A", "five_hour")]["observed_at"], NOW - 30)      # from the first file
        self.assertEqual(rows[("A", "seven_day")]["observed_at"], NOW - 100)     # from the second file: a different file wins each window
        self.assertEqual(rows[("A", "seven_day")]["used_percent"], 44.0)
        self.assertEqual(rows[("B", "five_hour")]["observed_at"], NOW - 77)      # the window had no time of its own: the file's

    def test_a_window_without_a_figure_gives_no_row(self):
        self.put(doc(secondary_used_percent=None, secondary_resets_at=None, secondary_observed_at=None))
        self.run_export()
        self.assertEqual([(r["label"], r["window"]) for r in self.exported()["rows"]], [("A", "five_hour")])


class Skipping(Folder):
    def test_the_old_desktop_files_give_zero_rows_and_a_count_not_a_failure(self):
        old = {"five_hour_used": 3.0, "updated": "2026-06-01T10:00:00Z", "source": "claude_statusline"}     # a leftover without schema 2 or an account
        self.put(old)
        self.put({"schema_version": 1, "account": ACCOUNT_A, "source": "claude_stream"})
        code, _line = self.run_export()
        self.assertEqual(code, 0)
        data = self.exported()
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["counts"]["old_format"], 2)

    def test_everything_else_is_counted_by_kind_and_keeps_no_text(self):
        self.put(doc(confidence="unknown", primary_used_percent=None, primary_resets_at=None, primary_observed_at=None,
                     secondary_used_percent=None, secondary_resets_at=None, secondary_observed_at=None))          # unknown confidence
        self.put(doc(ACCOUNT_X))                                                                                   # not in the label table
        self.put(doc("codex:tester-one:home-dddddddd", source="codex_rollout"))                                    # another provider
        self.put(doc(source="unknown"))                                                                            # a source that is not the subscription's own
        self.put(None, raw=b'{"half": ')                                                                           # half a file
        self.put(None, raw=b"not json at all")
        broken = doc()
        del broken["scope"]
        self.put(broken)                                                                                           # a listed key missing
        self.put(doc(primary_used_percent=float("nan")), raw=json.dumps(doc()).replace("12.0", "NaN").encode())    # not a finite number
        self.put(doc(context_tokens={"nested": 1}))                                                                # a nested value
        self.put(doc(primary_used_percent=-3.0))                                                                   # a number out of range
        self.put(doc(observed_at="yesterday"))                                                                     # a time that is not a time
        self.put(doc(extra_key="x"))                                                                               # a key nobody listed
        self.put(doc())                                                                                            # and one good file
        code, line = self.run_export()
        self.assertEqual(code, 0, line)
        counts = self.exported()["counts"]
        self.assertEqual((counts["files"], counts["used"], counts["unknown_confidence"], counts["unmapped"], counts["other_source"]), (13, 1, 1, 1, 2))
        self.assertEqual((counts["unreadable"], counts["refused"], counts["old_format"]), (2, 6, 0))
        self.assertEqual(len(self.exported()["rows"]), 2)
        self.assertNotIn(ACCOUNT_X, self.out.read_text(encoding="utf-8"))

    def test_the_input_bounds_are_the_readers(self):
        for number in range(70):
            self.put(doc(), name="unit-%03d" % number)
        self.run_export()
        counts = self.exported()["counts"]
        self.assertEqual((counts["files"], counts["over_limit"], counts["used"]), (ec.MAX_FILES, 6, ec.MAX_FILES))
        for path in self.state.iterdir():
            path.unlink()
        big = json.dumps(doc(reason="x" * 20_000)).encode("utf-8")
        self.put(None, raw=big)                                          # over 16 KiB
        self.put(None, raw=json.dumps(doc()).encode("utf-8"))            # under it
        self.run_export()
        counts = self.exported()["counts"]
        self.assertEqual((counts["refused"], counts["used"]), (1, 1))
        self.assertEqual(ec.MAX_BYTES, capacityreader.MAX_BYTES)
        self.assertEqual((ec.MAX_FILES, ec.MAX_ENTRIES), (capacityreader.MAX_FILES, capacityreader.MAX_ENTRIES))

    def test_only_capacity_files_with_plain_names_are_looked_at(self):
        (self.state / "notes.txt").write_text("x", encoding="utf-8")
        (self.state / "bad name.capacity.json").write_text(json.dumps(doc()), encoding="utf-8")
        (self.state / "folder.capacity.json").mkdir()
        self.run_export()
        self.assertEqual(self.exported()["counts"]["files"], 0)

    def test_it_never_changes_the_state_folder(self):
        self.put(doc())
        before = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in self.state.iterdir())
        self.run_export()
        after = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in self.state.iterdir())
        self.assertEqual(before, after)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["export.json", "labels.json", "state"])     # no temporary file is left


class Refusing(Folder):
    def good_document(self):
        return {"export_version": 1, "machine": "desk-top", "exported_at": NOW, "counts": dict.fromkeys(ec.COUNT_KEYS, 0),
                "rows": [{"label": "A", "window": "five_hour", "used_percent": 1.0, "resets_at": NOW + 5, "observed_at": NOW - 5, "source": "claude_stream", "seats": 1}]}

    def refused(self, document):
        with self.assertRaises(ec.Refused):
            ec.check_export(document, USER)

    def test_a_good_document_passes(self):
        ec.check_export(self.good_document(), USER)

    def test_each_unsafe_value_is_refused(self):
        for unsafe in ("name@example.invalid", "C:\\somewhere", "a/b", "x:\\y", "home-1234abcd", "my-" + USER + "-box", "MY-" + USER.upper()):
            document = self.good_document()
            document["machine"] = unsafe
            self.refused(document)
            document = self.good_document()
            document["rows"][0]["source"] = unsafe
            self.refused(document)
            document = self.good_document()
            document["counts"][unsafe] = 0
            self.refused(document)

    def test_a_label_or_word_outside_its_set_is_refused(self):
        for key, value in (("label", "D"), ("label", "a"), ("label", "AA"), ("window", "monthly"), ("source", "codex_rollout")):
            document = self.good_document()
            document["rows"][0][key] = value
            self.refused(document)

    def test_a_row_with_an_extra_or_missing_field_or_a_bad_number_is_refused(self):
        for change in ({"account": ACCOUNT_A}, {"used_percent": True}, {"used_percent": float("inf")}, {"resets_at": 0}, {"seats": 0}, {"observed_at": "2026"}):
            document = self.good_document()
            document["rows"][0].update(change)
            self.refused(document)
        document = self.good_document()
        del document["rows"][0]["seats"]
        self.refused(document)
        document = self.good_document()
        document["extra"] = 1
        self.refused(document)

    def test_a_refusal_writes_nothing_prints_counts_only_and_exits_non_zero(self):
        self.put(doc())
        self.out.write_text("previous export", encoding="utf-8")
        code, line = ec.run(str(self.state), str(self.labels), "home-desk", str(self.out), now=NOW, user_name=USER)       # a machine name that looks like a folder hash
        self.assertEqual(code, 2)
        self.assertEqual(self.out.read_text(encoding="utf-8"), "previous export")                                        # untouched
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["export.json", "labels.json", "state"])
        self.assertTrue(line.startswith("refused"))
        for text in (ACCOUNT_A, "tester", str(self.state)):
            self.assertNotIn(text, line)
        code, line = ec.run(str(self.state), str(self.labels), USER, str(self.out), now=NOW, user_name=USER)             # a machine name equal to the user name
        self.assertEqual(code, 2)
        self.assertNotIn(USER, line)

    def test_a_bad_machine_name_or_label_table_is_a_plain_error_before_anything_is_read(self):
        for machine in ("", "UPPER", "a b", "x" * 33, "a/b"):
            with self.assertRaises(ec.BadArguments):
                ec.run(str(self.state), str(self.labels), machine, str(self.out), now=NOW)
        for text in ("not json", "[]", "{}", json.dumps({ACCOUNT_A: "D"}), json.dumps({ACCOUNT_A: "a"}), json.dumps({"": "A"}), '{"k": "A", "k": "B"}'):
            self.labels.write_text(text, encoding="utf-8")
            with self.assertRaises(ec.BadArguments):
                ec.run(str(self.state), str(self.labels), "desk-top", str(self.out), now=NOW)
        self.assertFalse(self.out.exists())


class Writing(Folder):
    def test_the_write_is_atomic_and_leaves_no_partial_file_after_a_failure(self):
        self.put(doc())
        self.out.write_text("previous export", encoding="utf-8")
        with mock.patch.object(ec.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.run_export()
        self.assertEqual(self.out.read_text(encoding="utf-8"), "previous export")           # the old file is whole
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["export.json", "labels.json", "state"])     # and no temporary file remains

    def test_the_new_file_appears_whole_in_one_step(self):
        self.put(doc())
        seen = []
        real = os.replace

        def spy(source, target):
            seen.append((Path(source).read_text(encoding="utf-8"), Path(target).exists()))   # at the moment of the rename the temporary file is complete
            return real(source, target)
        with mock.patch.object(ec.os, "replace", side_effect=spy):
            self.run_export()
        self.assertEqual(len(seen), 1)
        self.assertEqual(json.loads(seen[0][0]), self.exported())
        self.assertFalse(seen[0][1])                                                         # nothing existed under the final name before it

    def test_the_command_line_prints_counts_only(self):
        self.put(doc())
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), mock.patch.object(ec.getpass, "getuser", return_value=USER):
            code = ec.main(["--state-dir", str(self.state), "--labels", str(self.labels), "--machine", "desk-top", "--out", str(self.out)])
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn("written: 2 row(s)", text)
        for forbidden in (ACCOUNT_A, USER, str(self.tmp), "home-"):
            self.assertNotIn(forbidden, text)

    def test_the_command_line_reports_a_bad_table_with_exit_3(self):
        self.labels.write_text("nonsense", encoding="utf-8")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = ec.main(["--state-dir", str(self.state), "--labels", str(self.labels), "--machine", "desk-top", "--out", str(self.out)])
        self.assertEqual(code, 3)
        self.assertNotIn("nonsense", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
