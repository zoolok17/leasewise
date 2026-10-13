"""Three-accounts step 0: the measuring tool (`tools/three_accounts/measure.py`): collect, from-store and report.

Everything is synthetic: made-up labels and times, a SQLite file the test builds, temporary folders under the scratch area.
"""
import calendar
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "three_accounts"))
import export_capacity as ec  # noqa: E402
import measure  # noqa: E402

DAY0 = calendar.timegm((2026, 10, 12, 0, 0, 0))          # a Monday, 00:00 UTC
HOUR = 3600
DAY = 86400
USER = "tester-one"
SAMPLE = measure.SAMPLE_STEP_S


def record(label="A", window="five_hour", used=10.0, resets=None, observed=None, machine="desk-top", exported=None, seen=None, seats=1, source="claude_stream"):
    observed = DAY0 if observed is None else observed
    return {"machine": machine, "label": label, "window": window, "used_percent": used, "resets_at": observed + HOUR if resets is None else resets,
            "observed_at": observed, "source": source, "seats": seats, "exported_at": observed if exported is None else exported,
            "seen_at": observed if seen is None else seen, "copy_age_s": 0}


class Scratch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AGENTTALK_SCRATCH") or None)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def write_jsonl(self, name, records):
        path = self.tmp / name
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return str(path)


class Collect(Scratch):
    def export(self, exported_at, rows=None, name="copy.json", **counts):
        document = {"export_version": 1, "machine": "desk-top", "exported_at": exported_at, "counts": {**dict.fromkeys(ec.COUNT_KEYS, 0), **counts},
                    "rows": rows if rows is not None else [
                        {"label": "A", "window": "five_hour", "used_percent": 20.0, "resets_at": exported_at + 3000, "observed_at": exported_at - 60, "source": "claude_stream", "seats": 1},
                        {"label": "A", "window": "seven_day", "used_percent": 50.0, "resets_at": exported_at + 300_000, "observed_at": exported_at - 60, "source": "claude_stream", "seats": 1}]}
        path = self.tmp / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    def lines(self):
        path = self.tmp / "history" / "observations.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def test_a_copy_is_kept_as_observation_records_with_its_age(self):
        code, line = measure.collect(self.export(DAY0 + 1000), str(self.tmp / "history"), now=DAY0 + 1300, user_name=USER)
        self.assertEqual(code, 0, line)
        records = self.lines()
        self.assertEqual(len(records), 2)
        self.assertEqual({r["window"] for r in records}, {"five_hour", "seven_day"})
        self.assertTrue(all(measure.valid_observation(r) for r in records))
        self.assertEqual({r["copy_age_s"] for r in records}, {300})
        self.assertEqual({r["machine"] for r in records}, {"desk-top"})

    def test_only_a_newer_copy_is_kept(self):
        history = str(self.tmp / "history")
        measure.collect(self.export(DAY0 + 1000), history, now=DAY0 + 1100, user_name=USER)
        for exported in (DAY0 + 1000, DAY0 + 900):                                   # the same copy again, and an older one
            code, line = measure.collect(self.export(exported), history, now=DAY0 + 1200, user_name=USER)
            self.assertEqual(code, 0, line)
            self.assertIn("appended=0", line)
        self.assertEqual(len(self.lines()), 2)
        measure.collect(self.export(DAY0 + 1300), history, now=DAY0 + 1400, user_name=USER)
        self.assertEqual(len(self.lines()), 4)

    def test_a_bad_copy_appends_nothing(self):
        history = str(self.tmp / "history")
        good = json.loads(Path(self.export(DAY0 + 1000)).read_text(encoding="utf-8"))
        cases = [("not json", None), ("{}", None), ("[]", None)]
        extra = dict(good, extra=1)
        unsafe = json.loads(json.dumps(good))
        unsafe["machine"] = "home-desk"
        label = json.loads(json.dumps(good))
        label["rows"][0]["label"] = "Z"
        for text in (cases[0][0], cases[1][0], cases[2][0], json.dumps(extra), json.dumps(unsafe), json.dumps(label)):
            path = self.tmp / "bad.json"
            path.write_text(text, encoding="utf-8")
            code, line = measure.collect(str(path), history, now=DAY0 + 2000, user_name=USER)
            self.assertEqual(code, 2, text[:30])
            self.assertIn("bad=1", line)
        self.assertEqual(self.lines(), [])
        code, _line = measure.collect(str(self.tmp / "missing.json"), history, now=DAY0, user_name=USER)
        self.assertEqual(code, 2)

    def test_a_copy_with_no_rows_is_kept_as_nothing_and_still_counts_as_seen(self):
        history = str(self.tmp / "history")
        code, line = measure.collect(self.export(DAY0 + 1000, rows=[], old_format=3), history, now=DAY0 + 1100, user_name=USER)
        self.assertEqual(code, 0)
        self.assertIn("old_format=3", line)
        self.assertEqual(self.lines(), [])


class FromStore(Scratch):
    def build(self):
        path = self.tmp / "inventory.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE machines(id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE);"
            "CREATE TABLE accounts(id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE);"
            "CREATE TABLE readings(event_id TEXT PRIMARY KEY, machine_id TEXT, account_id TEXT, seat_id TEXT, kind TEXT, observed_at INTEGER, received_at INTEGER, payload TEXT);"
            "INSERT INTO machines VALUES('m1','laptop-b'),('m2','other-box');"
            "INSERT INTO accounts VALUES('a1','main-one'),('a2','main-two'),('a3','not-in-table');")

        def payload(five, week, confidence="observed"):
            return json.dumps({"windows": [{"key": "five_hour", "used_bps": five, "resets_at": (DAY0 + 5 * HOUR) * 1000},
                                           {"key": "seven_day", "used_bps": week, "resets_at": (DAY0 + 5 * DAY) * 1000}],
                               "confidence": confidence, "source_stale": False, "covered_seq": None})
        rows = [("e1", "m1", "a1", None, "cli_window", (DAY0 + 600) * 1000, 0, payload(1250, 4400)),
                ("e2", "m1", "a1", None, "cli_window", (DAY0 + 2400) * 1000, 0, payload(1500, 4400)),
                ("e3", "m1", "a2", None, "cli_window", (DAY0 + 700) * 1000, 0, payload(300, 800)),
                ("e4", "m1", "a1", None, "gateway_snapshot", (DAY0 + 800) * 1000, 0, payload(9999, 9999)),     # another kind
                ("e5", "m2", "a1", None, "cli_window", (DAY0 + 900) * 1000, 0, payload(7000, 7000)),           # another machine
                ("e6", "m1", "a3", None, "cli_window", (DAY0 + 900) * 1000, 0, payload(100, 100)),             # an alias nobody labelled
                ("e7", "m1", "a1", None, "cli_window", (DAY0 + 950) * 1000, 0, "not json")]
        conn.executemany("INSERT INTO readings VALUES(?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
        return path

    def test_laptop_b_readings_become_the_same_records_and_the_store_is_untouched(self):
        db = self.build()
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        labels = self.tmp / "aliases.json"
        labels.write_text(json.dumps({"main-one": "A", "main-two": "B"}), encoding="utf-8")
        out = self.tmp / "store.jsonl"
        code, line = measure.from_store(str(db), str(labels), str(out), now=DAY0 + 5000)
        self.assertEqual(code, 0, line)
        records = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 6)                                                   # three readings, two windows each
        self.assertTrue(all(measure.valid_observation(r) for r in records))
        self.assertEqual({(r["machine"], r["source"]) for r in records}, {("laptop-b", "leasewise-store")})
        first = [r for r in records if r["label"] == "A" and r["window"] == "five_hour" and r["observed_at"] == DAY0 + 600][0]
        self.assertEqual((first["used_percent"], first["resets_at"]), (12.5, DAY0 + 5 * HOUR))     # basis points to percent, milliseconds to seconds
        self.assertEqual(sorted({r["label"] for r in records}), ["A", "B"])
        self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["aliases.json", "inventory.db", "store.jsonl"])     # no journal or temporary file beside the store

    def test_the_store_is_opened_read_only_and_cannot_be_written_through_it(self):
        db = self.build()
        labels = self.tmp / "aliases.json"
        labels.write_text(json.dumps({"main-one": "A"}), encoding="utf-8")
        opened = []
        real = sqlite3.connect

        def spy(target, *args, **kwargs):
            opened.append((target, kwargs.get("uri")))
            return real(target, *args, **kwargs)
        with mock.patch.object(measure.sqlite3, "connect", side_effect=spy):
            measure.from_store(str(db), str(labels), str(self.tmp / "o.jsonl"), now=DAY0)
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0][0].startswith("file:") and opened[0][0].endswith("?mode=ro"), opened[0][0])
        self.assertTrue(opened[0][1])
        read_only = real(opened[0][0], uri=True)
        with self.assertRaises(sqlite3.OperationalError):
            read_only.execute("DELETE FROM readings")
        read_only.close()

    def test_a_missing_store_or_a_bad_alias_table_is_a_plain_error(self):
        labels = self.tmp / "aliases.json"
        labels.write_text(json.dumps({"main-one": "A"}), encoding="utf-8")
        with self.assertRaises(measure.Problem):
            measure.from_store(str(self.tmp / "nothing.db"), str(labels), str(self.tmp / "o.jsonl"))
        self.assertFalse((self.tmp / "nothing.db").exists())                              # opening read-only must not create it
        db = self.build()
        labels.write_text(json.dumps({"main-one": "Z"}), encoding="utf-8")
        with self.assertRaises(measure.Problem):
            measure.from_store(str(db), str(labels), str(self.tmp / "o.jsonl"))


class Report(Scratch):
    def week(self, records, day_from="2026-10-12", day_to="2026-10-18"):
        return measure.build_report(measure.distinct(records), day_from, day_to)

    def half_hourly(self, label="A", window="five_hour", start=DAY0, end=DAY0 + 7 * DAY, offset=27 * 60, resets_step=3 * HOUR, fixed_reset=None):
        """Observations at hh:27 and hh:57 for the whole period, each with its reset time a few hours ahead (or one fixed reset time)."""
        out = []
        t = start + offset
        while t < end:
            out.append(record(label, window, used=(t - start) / DAY, observed=t, resets=fixed_reset or t + resets_step))
            t += SAMPLE
        return out

    def test_every_boundary_with_a_figure_three_minutes_old_is_fresh(self):
        report = self.week(self.half_hourly(), "2026-10-12", "2026-10-12")
        info = report["labels"]["A"]["windows"]["five_hour"]
        self.assertEqual((info["samples"], info["fresh"], info["floor"], info["unknown"]), (48, 47, 0, 1))     # 00:00 comes before the first figure

    def test_an_older_figure_is_a_floor_until_its_reset_and_then_unknown(self):
        records = [record(window="five_hour", used=33.0, observed=DAY0 + 8 * HOUR, resets=DAY0 + 10 * HOUR)]
        info = self.week(records, "2026-10-12", "2026-10-12")["labels"]["A"]["windows"]["five_hour"]
        # 08:00 is fresh (the figure is as old as the boundary), 08:30 to 09:30 are floors, 10:00 is exactly the reset: no longer a floor, so unknown
        self.assertEqual(info["fresh"], 1)
        self.assertEqual(info["floor"], 3)
        boundary = DAY0 + 10 * HOUR
        self.assertEqual(measure.classify(records, boundary - 1800)[0], "floor")
        self.assertEqual(measure.classify(records, boundary)[0], "unknown")
        self.assertEqual(measure.classify(records, boundary + 1800)[0], "unknown")
        self.assertEqual(measure.classify(records, DAY0 + 8 * HOUR - 1800)[0], "unknown")       # a figure from the future is not used
        kind, rec = measure.classify(records, DAY0 + 9 * HOUR)
        self.assertEqual((kind, rec["used_percent"]), ("floor", 33.0))
        self.assertEqual(self.week(records, "2026-10-12", "2026-10-12")["labels"]["A"]["windows"]["five_hour"]["last_floor_by_day"]["2026-10-12"], 33.0)

    def test_a_figure_exactly_at_the_fresh_limit_counts_as_fresh_and_one_second_more_does_not(self):
        records = [record(observed=DAY0 + 1800 - 600, resets=DAY0 + 20 * HOUR)]
        self.assertEqual(measure.classify(records, DAY0 + 1800)[0], "fresh")
        records = [record(observed=DAY0 + 1800 - 601, resets=DAY0 + 20 * HOUR)]
        self.assertEqual(measure.classify(records, DAY0 + 1800)[0], "floor")

    def test_a_label_that_is_mostly_unknown_gets_the_stop_signal_and_a_covered_one_does_not(self):
        covered = self.half_hourly("A", "five_hour") + self.half_hourly("A", "seven_day", fixed_reset=DAY0 + 7 * DAY)
        thin = [record("B", "five_hour", observed=DAY0 + HOUR, resets=DAY0 + 2 * HOUR), record("B", "seven_day", observed=DAY0 + HOUR, resets=DAY0 + 2 * HOUR)]
        report = self.week(covered + thin)
        self.assertFalse(report["labels"]["A"]["stop_signal"])
        self.assertTrue(report["labels"]["B"]["stop_signal"])
        self.assertGreater(report["labels"]["B"]["unknown_share"], 0.5)
        self.assertEqual(report["labels"]["A"]["windows"]["five_hour"]["observations"], len(self.half_hourly()))

    def test_a_label_with_no_observation_in_the_period_is_not_in_use(self):
        report = self.week([record("C", observed=DAY0 - 3 * DAY, resets=DAY0 - 2 * DAY)])
        self.assertEqual(report["labels"], {})

    def test_the_same_observation_from_two_files_is_counted_once(self):
        one = record(observed=DAY0 + 100)
        info = self.week([one, dict(one), dict(one, seen_at=DAY0 + 999)], "2026-10-12", "2026-10-12")["labels"]["A"]["windows"]["five_hour"]
        self.assertEqual(info["observations"], 1)


class Switches(unittest.TestCase):
    R1 = DAY0 + 3 * DAY                     # the weekly reset time an account has now

    def flags(self, records):
        return measure.build_report(measure.distinct(records), "2026-10-12", "2026-10-18")["flags"]

    def weekly(self, observed, used, resets, label="A"):
        return record(label, "seven_day", used=used, observed=observed, resets=resets)

    def test_a_reset_time_that_moves_before_the_earlier_one_passed_is_flagged_once(self):
        flags = self.flags([self.weekly(DAY0 + HOUR, 30.0, self.R1), self.weekly(DAY0 + 2 * HOUR, 31.0, self.R1),
                            self.weekly(DAY0 + 3 * HOUR, 5.0, self.R1 + 2 * DAY)])
        self.assertEqual(len(flags), 1)
        self.assertEqual((flags[0]["kind"], flags[0]["label"], flags[0]["at"]), ("possible switch", "A", DAY0 + 3 * HOUR))

    def test_a_normal_reset_passing_is_not_flagged_even_though_the_percentage_falls(self):
        flags = self.flags([self.weekly(self.R1 - HOUR, 90.0, self.R1), self.weekly(self.R1 + HOUR, 2.0, self.R1 + 7 * DAY)])
        self.assertEqual(flags, [])

    def test_a_small_drift_of_the_reset_time_is_not_flagged(self):
        flags = self.flags([self.weekly(DAY0 + HOUR, 30.0, self.R1), self.weekly(DAY0 + 2 * HOUR, 31.0, self.R1 + measure.SWITCH_RESET_TOLERANCE_S - 1),
                            self.weekly(DAY0 + 3 * HOUR, 32.0, self.R1 + 120)])      # each step is under the tolerance
        self.assertEqual(flags, [])

    def test_a_percentage_that_falls_inside_one_reset_period_is_flagged_but_rounding_is_not(self):
        flagged = self.flags([self.weekly(DAY0 + HOUR, 44.0, self.R1), self.weekly(DAY0 + 2 * HOUR, 20.0, self.R1)])
        self.assertEqual([f["reason"] for f in flagged], ["used percentage fell within one reset period"])
        quiet = self.flags([self.weekly(DAY0 + HOUR, 44.0, self.R1), self.weekly(DAY0 + 2 * HOUR, 44.0 - measure.PERCENT_FALL_TOLERANCE, self.R1)])
        self.assertEqual(quiet, [])

    def test_only_the_weekly_window_is_watched_and_each_label_on_its_own(self):
        five = [record("A", "five_hour", used=50.0, observed=DAY0 + HOUR, resets=DAY0 + 2 * HOUR),
                record("A", "five_hour", used=1.0, observed=DAY0 + 90 * 60, resets=DAY0 + 9 * HOUR)]
        self.assertEqual(self.flags(five), [])
        other = [self.weekly(DAY0 + HOUR, 30.0, self.R1, "A"), self.weekly(DAY0 + 2 * HOUR, 10.0, self.R1 + 3 * DAY, "B")]
        self.assertEqual(self.flags(other), [])


class CommandLine(Scratch):
    def test_report_prints_text_with_the_stop_signal_and_json_on_request(self):
        path = self.write_jsonl("obs.jsonl", [record("B", "five_hour", observed=DAY0 + HOUR, resets=DAY0 + 2 * HOUR)])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = measure.main(["report", "--observations", path, "--from", "2026-10-12", "--to", "2026-10-12"])
        self.assertEqual(code, 0)
        self.assertIn("STOP SIGNAL", stdout.getvalue())
        self.assertIn("Label B", stdout.getvalue())
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            measure.main(["report", "--observations", path, "--from", "2026-10-12", "--to", "2026-10-12", "--json"])
        data = json.loads(stdout.getvalue())
        self.assertTrue(data["labels"]["B"]["stop_signal"])
        self.assertEqual(data["thresholds"]["switch_reset_tolerance_s"], 300)

    def test_report_reads_several_files_and_skips_bad_lines(self):
        one = self.write_jsonl("one.jsonl", [record(observed=DAY0 + 100)])
        two = self.tmp / "two.jsonl"
        two.write_text("garbage\n" + json.dumps(record(observed=DAY0 + 200)) + "\n" + json.dumps({"label": "A"}) + "\n", encoding="utf-8")
        text = measure.report_command([one, str(two)], "2026-10-12", "2026-10-12", True)
        data = json.loads(text)
        self.assertEqual(data["unreadable_lines"], 2)
        self.assertEqual(data["labels"]["A"]["windows"]["five_hour"]["observations"], 2)

    def test_a_bad_date_is_a_plain_error(self):
        path = self.write_jsonl("obs.jsonl", [])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(measure.main(["report", "--observations", path, "--from", "12/10/2026", "--to", "2026-10-18"]), 3)
            self.assertEqual(measure.main(["report", "--observations", path, "--from", "2026-10-18", "--to", "2026-10-12"]), 3)


class QuietFailures(Scratch):
    """Fix round 1: one generic sentence, never a path, an exception text or a traceback; the output name is never written through an existing file."""

    CANARY = "canary-private-folder-7f3a"

    def call(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = measure.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_quiet(self, out, err):
        for text in (out, err):
            self.assertNotIn(self.CANARY, text)
            self.assertNotIn("Traceback", text)
            self.assertNotIn(str(self.tmp), text)

    def test_collect_with_a_history_parent_that_is_a_file_prints_no_path(self):
        copy = Collect.export(self, DAY0 + 1000)
        blocker = self.tmp / self.CANARY
        blocker.write_text("a file where a folder is needed", encoding="utf-8")
        code, out, err = self.call(["collect", "--in", copy, "--history", str(blocker / "history")])
        self.assertEqual(code, 3)
        self.assertIn("a file operation failed", out)
        self.assert_quiet(out, err)
        self.assertEqual(blocker.read_text(encoding="utf-8"), "a file where a folder is needed")

    def test_from_store_with_a_missing_output_folder_prints_no_path_and_writes_nothing(self):
        db = FromStore.build(self)
        labels = self.tmp / "aliases.json"
        labels.write_text(json.dumps({"main-one": "A"}), encoding="utf-8")
        code, out, err = self.call(["from-store", "--db", str(db), "--labels", str(labels), "--out", str(self.tmp / self.CANARY / "store.jsonl")])
        self.assertEqual(code, 3)
        self.assert_quiet(out, err)
        self.assertFalse((self.tmp / self.CANARY).exists())

    def test_an_unexpected_failure_is_a_generic_sentence_too(self):
        path = self.write_jsonl("obs.jsonl", [])
        with mock.patch.object(measure, "report_command", side_effect=RuntimeError("detail with " + self.CANARY)):
            code, out, err = self.call(["report", "--observations", path, "--from", "2026-10-12", "--to", "2026-10-12"])
        self.assertEqual(code, 3)
        self.assert_quiet(out, err)

    def test_from_store_never_writes_through_a_name_that_already_exists(self):
        db = FromStore.build(self)
        labels = self.tmp / "aliases.json"
        labels.write_text(json.dumps({"main-one": "A"}), encoding="utf-8")
        out = self.tmp / "store.jsonl"
        precious = self.tmp / "precious.txt"
        precious.write_bytes(b"do not touch me")
        guess = self.tmp / (".%s.%d.tmp" % (out.name, os.getpid()))
        os.link(precious, guess)
        before = {p.name: p.read_bytes() for p in self.tmp.iterdir() if p.is_file()}
        code, _line = measure.from_store(str(db), str(labels), str(out), now=DAY0)
        self.assertEqual(code, 0)
        for name, content in before.items():
            self.assertEqual((self.tmp / name).read_bytes(), content, name)
        self.assertTrue(out.exists())
        os.unlink(guess)


class WalStore(Scratch):
    """The live store is in WAL mode. Reading it read-only may leave SQLite's own `-wal` and `-shm` beside it, and nothing else."""

    def build(self, rows):
        path = self.tmp / "inventory.db"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            "CREATE TABLE machines(id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE);"
            "CREATE TABLE accounts(id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE);"
            "CREATE TABLE readings(event_id TEXT PRIMARY KEY, machine_id TEXT, account_id TEXT, seat_id TEXT, kind TEXT, observed_at INTEGER, received_at INTEGER, payload TEXT);"
            "INSERT INTO machines VALUES('m1','laptop-b');INSERT INTO accounts VALUES('a1','main-one');")
        self.add(conn, rows)
        return path, conn

    @staticmethod
    def add(conn, rows):
        for number, (observed, bps) in enumerate(rows):
            payload = json.dumps({"windows": [{"key": "five_hour", "used_bps": bps, "resets_at": (observed + 3 * HOUR) * 1000}], "confidence": "observed",
                                  "source_stale": False, "covered_seq": None})
            conn.execute("INSERT INTO readings VALUES(?, 'm1', 'a1', NULL, 'cli_window', ?, 0, ?)", ("w%d-%d" % (observed, number), observed * 1000, payload))
        conn.commit()

    def labels(self):
        path = self.tmp / "aliases.json"
        path.write_text(json.dumps({"main-one": "A"}), encoding="utf-8")
        return str(path)

    def listing(self):
        return {p.name: p.stat().st_size for p in self.tmp.iterdir()}

    def test_a_wal_store_with_no_side_files_gets_at_most_the_two_side_files_and_stays_byte_identical(self):
        path, conn = self.build([(DAY0 + 600, 1000)])
        conn.close()                                                               # a clean close checkpoints: no -wal or -shm remains
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["inventory.db"])
        before_bytes = hashlib.sha256(path.read_bytes()).hexdigest()
        before = set(self.listing())
        code, line = measure.from_store(str(path), self.labels(), str(self.tmp / "out.jsonl"), now=DAY0)
        self.assertEqual(code, 0, line)
        after = set(self.listing())
        self.assertLessEqual(after - before, {"inventory.db-wal", "inventory.db-shm", "out.jsonl", "aliases.json"})
        self.assertEqual(before - after, set())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before_bytes)
        self.assertEqual(len((self.tmp / "out.jsonl").read_text(encoding="utf-8").splitlines()), 1)

    def test_a_committed_row_that_is_still_in_the_wal_is_read_and_nothing_new_appears(self):
        path, conn = self.build([(DAY0 + 600, 1000)])
        try:
            conn.execute("PRAGMA wal_autocheckpoint=0")
            self.add(conn, [(DAY0 + 1200, 2500)])                                # committed, but left in the WAL by the open writer
            self.assertTrue((self.tmp / "inventory.db-wal").exists())
            db_before = hashlib.sha256(path.read_bytes()).hexdigest()
            names_before = set(self.listing())
            labels = self.labels()
            names_before.add("aliases.json")
            code, line = measure.from_store(str(path), labels, str(self.tmp / "out.jsonl"), now=DAY0)
            self.assertEqual(code, 0, line)
            records = [json.loads(x) for x in (self.tmp / "out.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(sorted(r["used_percent"] for r in records), [10.0, 25.0])      # the WAL row is read
            self.assertEqual(set(self.listing()) - names_before, {"out.jsonl"})              # the side files already existed; nothing else is new
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), db_before)
        finally:
            conn.close()


class ExpiryAndFalls(unittest.TestCase):
    def test_a_figure_observed_a_minute_before_its_reset_is_unknown_at_the_reset(self):
        rec = record(observed=DAY0 + 29 * 60, resets=DAY0 + 30 * 60)           # observed 00:29, resets 00:30
        self.assertEqual(measure.classify([rec], DAY0 + 29 * 60 + 30)[0], "fresh")
        self.assertEqual(measure.classify([rec], DAY0 + 30 * 60)[0], "unknown")         # at the boundary the period has ended: not fresh
        self.assertEqual(measure.classify([rec], DAY0 + 31 * 60)[0], "unknown")
        info = measure.build_report([rec], "2026-10-12", "2026-10-12")["labels"]["A"]["windows"]["five_hour"]
        self.assertEqual((info["fresh"], info["floor"]), (0, 0))                       # the 00:30 sample is not fresh

    def test_a_figure_observed_at_or_after_its_own_reset_is_never_fresh_or_floor_and_is_counted(self):
        late = record(observed=DAY0 + 3600, resets=DAY0 + 3600)                       # observed exactly at its reset
        later = record(observed=DAY0 + 4000, resets=DAY0 + 3000, machine="other-box")  # observed after its reset
        for boundary in (DAY0 + 3600, DAY0 + 5400, DAY0 + 7200):
            self.assertEqual(measure.classify([late], boundary)[0], "unknown")
        info = measure.build_report([late, later], "2026-10-12", "2026-10-12")["labels"]["A"]["windows"]["five_hour"]
        self.assertEqual((info["fresh"], info["floor"]), (0, 0))
        self.assertEqual(info["already_reset"], 2)

    def test_a_lower_figure_observed_after_the_earlier_ones_reset_is_not_a_fall_within_one_period(self):
        r1 = DAY0 + 3 * DAY
        earlier = record(window="seven_day", used=80.0, observed=r1 - HOUR, resets=r1)
        after_reset = record(window="seven_day", used=3.0, observed=r1 + 60, resets=r1)       # the same reset time, observed after it had passed
        self.assertEqual(measure.switch_flags("A", measure.distinct([earlier, after_reset])), [])
        within = record(window="seven_day", used=3.0, observed=r1 - 60, resets=r1)            # the same, observed while the period was live
        flags = measure.switch_flags("A", measure.distinct([earlier, within]))
        self.assertEqual([f["reason"] for f in flags], ["used percentage fell within one reset period"])

    def test_the_old_boundary_at_a_reset_case_still_holds(self):
        rec = record(observed=DAY0 + 8 * HOUR, resets=DAY0 + 10 * HOUR)
        self.assertEqual(measure.classify([rec], DAY0 + 10 * HOUR - 1800)[0], "floor")
        self.assertEqual(measure.classify([rec], DAY0 + 10 * HOUR)[0], "unknown")


class Receipts(Scratch):
    def export(self, exported_at, rows=True):
        return Collect.export(self, exported_at, rows=None if rows else [], name="copy-%d.json" % exported_at)

    def lines(self):
        path = self.tmp / "history" / "observations.jsonl"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def test_an_accepted_empty_copy_moves_the_last_accepted_time_forward(self):
        history = str(self.tmp / "history")
        T = DAY0
        code, line = measure.collect(self.export(T + 2000, rows=False), history, now=T + 2100, user_name=USER)
        self.assertEqual(code, 0, line)
        self.assertEqual(self.lines(), [])                                          # no usage observation is invented
        receipts = json.loads((self.tmp / "history" / "receipts.json").read_text(encoding="utf-8"))
        self.assertEqual(receipts["last_accepted"], {"desk-top": T + 2000})
        code, line = measure.collect(self.export(T + 1000), history, now=T + 2200, user_name=USER)
        self.assertEqual(code, 0)
        self.assertIn("appended=0", line)
        self.assertEqual(self.lines(), [])                                          # the older, non-empty copy is refused
        code, _line = measure.collect(self.export(T + 3000), history, now=T + 3100, user_name=USER)
        self.assertEqual(len(self.lines()), 2)
        receipts = json.loads((self.tmp / "history" / "receipts.json").read_text(encoding="utf-8"))
        self.assertEqual(receipts["last_accepted"], {"desk-top": T + 3000})

    def test_a_damaged_receipts_file_is_refused_with_a_generic_sentence_and_nothing_is_appended(self):
        history = self.tmp / "history"
        history.mkdir()
        for text in ("not json", "[]", "{}", json.dumps({"receipts_version": 2, "last_accepted": {}}), json.dumps({"receipts_version": 1, "last_accepted": {"A B": 5}}),
                     json.dumps({"receipts_version": 1, "last_accepted": {"desk-top": "late"}}), '{"receipts_version": 1, "receipts_version": 1, "last_accepted": {}}'):
            (history / "receipts.json").write_text(text, encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = measure.main(["collect", "--in", self.export(DAY0 + 1000), "--history", str(history)])
            self.assertEqual(code, 3, text[:30])
            self.assertIn("receipts file", stdout.getvalue())
            self.assertNotIn("Traceback", stdout.getvalue() + stderr.getvalue())
            self.assertNotIn(str(self.tmp), stdout.getvalue() + stderr.getvalue())
            self.assertEqual(self.lines(), [])

    def test_the_receipt_and_the_rows_never_disagree_after_a_rerun(self):
        history = str(self.tmp / "history")
        measure.collect(self.export(DAY0 + 1000), history, now=DAY0 + 1100, user_name=USER)
        (self.tmp / "history" / "receipts.json").unlink()                           # the receipt is lost: the rows still hold the last time
        code, line = measure.collect(self.export(DAY0 + 1000), history, now=DAY0 + 1200, user_name=USER)
        self.assertEqual(code, 0)
        self.assertIn("appended=0", line)
        self.assertEqual(len(self.lines()), 2)


class ReportWording(Scratch):
    def test_per_window_unknown_counts_sit_next_to_the_pooled_decision_and_no_stop_is_not_called_proof(self):
        records = []
        t = DAY0 + 27 * 60
        while t < DAY0 + DAY:
            records.append(record("A", "five_hour", observed=t, resets=t + 3 * HOUR))        # a covered five-hour window
            t += 1800
        records.append(record("A", "seven_day", observed=DAY0 + HOUR, resets=DAY0 + 6 * HOUR))      # a weekly window that is mostly unknown
        text = measure.render(measure.build_report(measure.distinct(records), "2026-10-12", "2026-10-12"))
        self.assertIn("both windows pooled", text)
        self.assertIn("(no stop signal)", text)
        self.assertIn("five_hour", text)
        self.assertIn("seven_day", text)
        self.assertRegex(text, r"seven_day\s+observations=1 fresh=1 floor=9 unknown=38 \(79% unknown\)")
        self.assertIn("'no stop signal' does not show that each window is reliable", text)


def plain(text):
    """Wording only: no Markdown marks, line breaks (also one after a hyphen, as the help wrapper may make) or case."""
    return " ".join(text.replace("`", "").replace("*", "").split()).replace("- ", "-").casefold()


class StorePromiseWording(unittest.TestCase):
    def test_the_step_record_and_the_help_text_state_the_read_only_promise_in_the_same_words(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit):
            measure.main(["from-store", "--help"])
        record_text = (ROOT / "docs" / "STEP-CARD-3ACC-0.md").read_text(encoding="utf-8")
        self.assertIn(plain(measure.STORE_PROMISE), plain(stdout.getvalue()))
        self.assertIn(plain(measure.STORE_PROMISE), plain(record_text))


class SingleCollector(Scratch):
    """Fix round 2 (D-351): exactly one collector writes a history folder; a second one is refused at once and never waits."""

    REFUSAL = "another collect is running on this history folder; nothing done"

    def setUp(self):
        super().setUp()
        self.history = self.tmp / "history"

    def export(self, exported_at, rows=None):
        return Collect.export(self, exported_at, rows=rows, name="copy-%d.json" % exported_at)

    def receipt(self):
        path = self.history / "receipts.json"
        if not path.exists():
            return 0
        return json.loads(path.read_text(encoding="utf-8"))["last_accepted"].get("desk-top", 0)

    def rows(self):
        path = self.history / "observations.jsonl"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def test_two_overlapping_collectors_never_move_the_accepted_time_backwards(self):
        T = DAY0
        reached, release, outcome, seen = threading.Event(), threading.Event(), {}, []
        original = measure.write_receipts

        def held_write(path, receipts):
            if threading.current_thread().name == "older-call":
                reached.set()
                release.wait(20)
            original(path, receipts)

        def older():
            outcome["older"] = measure.collect(self.export(T + 2000, rows=[]), str(self.history), now=T + 2100, user_name=USER)

        with mock.patch.object(measure, "write_receipts", held_write):
            worker = threading.Thread(target=older, name="older-call")
            worker.start()
            try:
                self.assertTrue(reached.wait(20))
                code, line = measure.collect(self.export(T + 3000, rows=[]), str(self.history), now=T + 3100, user_name=USER)
                seen.append(self.receipt())
            finally:
                release.set()
                worker.join(30)
        self.assertFalse(worker.is_alive())
        self.assertEqual(code, 2, line)
        self.assertIn(self.REFUSAL, line)
        self.assertEqual(outcome["older"][0], 0)
        seen.append(self.receipt())
        # a later, non-empty copy: accepted only if it is newer than everything accepted before
        measure.collect(self.export(T + 2500), str(self.history), now=T + 3200, user_name=USER)
        seen.append(self.receipt())
        self.assertEqual(seen, sorted(seen), "the accepted time went backwards: %r" % seen)
        if self.receipt() == T + 3000:
            self.assertEqual(self.rows(), [])
        else:
            self.assertEqual(self.receipt(), T + 2500)

    def test_a_held_lock_makes_collect_refuse_with_no_change_and_no_traceback(self):
        self.history.mkdir()
        (self.history / "receipts.json").write_text(json.dumps({"receipts_version": 1, "last_accepted": {"desk-top": DAY0 + 10}}), encoding="utf-8")
        before = (self.history / "receipts.json").read_bytes()
        with measure.single_collector(self.history) as held:
            self.assertTrue(held)
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = measure.main(["collect", "--in", self.export(DAY0 + 1000), "--history", str(self.history)])
        self.assertEqual(code, 2)
        self.assertIn(self.REFUSAL, stdout.getvalue())
        self.assertNotIn("Traceback", stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(str(self.tmp), stdout.getvalue() + stderr.getvalue())
        self.assertEqual(self.rows(), [])
        self.assertEqual((self.history / "receipts.json").read_bytes(), before)

    def test_the_lock_is_let_go_after_each_collect_and_the_file_is_never_deleted(self):
        code, line = measure.collect(self.export(DAY0 + 1000), str(self.history), now=DAY0 + 1100, user_name=USER)
        self.assertEqual(code, 0, line)
        self.assertTrue((self.history / ".collect.lock").exists())
        code, _line = measure.collect(self.export(DAY0 + 2000), str(self.history), now=DAY0 + 2100, user_name=USER)
        self.assertEqual(code, 0)
        self.assertEqual(self.receipt(), DAY0 + 2000)
        self.assertTrue((self.history / ".collect.lock").exists())

    def child(self):
        """A process that takes the lock, says so, waits for its input to close and then ends without letting go, as a crash would."""
        tools = str(Path(measure.__file__).resolve().parent)
        code = ("import os, sys, pathlib\nsys.path.insert(0, sys.argv[1])\nimport measure\n"
                "cm = measure.single_collector(pathlib.Path(sys.argv[2]))\n"
                "print('held' if cm.__enter__() else 'refused', flush=True)\n"
                "sys.stdin.read()\nos._exit(0)\n")
        self.history.mkdir(exist_ok=True)
        proc = subprocess.Popen([sys.executable, "-I", "-c", code, tools, str(self.history)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertEqual(proc.stdout.readline().strip(), "held")
        return proc

    def test_another_process_holding_the_lock_refuses_collect_and_a_finished_process_does_not(self):
        proc = self.child()
        code, line = measure.collect(self.export(DAY0 + 1000), str(self.history), now=DAY0 + 1100, user_name=USER)
        self.assertEqual(code, 2, line)
        self.assertEqual(self.rows(), [])
        proc.stdin.close()                                          # the child ends without unlocking: a crash
        self.assertEqual(proc.wait(30), 0)
        proc.stdout.close()
        code, line = measure.collect(self.export(DAY0 + 1000), str(self.history), now=DAY0 + 1100, user_name=USER)
        self.assertEqual(code, 0, line)
        self.assertEqual(len(self.rows()), 2)

    def test_a_receipt_on_disk_that_is_newer_is_never_replaced_by_an_older_one(self):
        self.history.mkdir()
        original = measure.read_receipts
        calls = []

        def racing(path):
            result = original(path)
            calls.append(1)
            if len(calls) == 1:                                     # after the first read, something else moves the receipt on
                measure.write_receipts(path, {"desk-top": DAY0 + 5000})
            return result

        with mock.patch.object(measure, "read_receipts", racing):
            code, line = measure.collect(self.export(DAY0 + 1000, rows=[]), str(self.history), now=DAY0 + 1100, user_name=USER)
        self.assertEqual(code, 0, line)
        self.assertEqual(self.receipt(), DAY0 + 5000)


class ArgumentErrors(Scratch):
    """Fix round 2 (D-351): an argument error prints one generic sentence with no value from the command line, and exits 3."""

    CANARY = str(Path("C:/Users/canary-private-person/secret-folder-91c2"))
    SENTENCE = "not done: the command line is not valid; see --help"

    def call(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = measure.main(argv)
            except SystemExit as why:
                code = why.code
        return code, stdout.getvalue(), stderr.getvalue()

    def check(self, argv):
        code, out, err = self.call(argv)
        self.assertEqual(code, 3, argv)
        self.assertIn(self.SENTENCE, out + err)
        for text in (out, err):
            self.assertNotIn("canary", text.casefold())
            self.assertNotIn("secret-folder", text)
            self.assertNotIn("Traceback", text)

    def test_an_unknown_option_followed_by_a_private_path(self):
        self.check(["collect", "--in", "x", "--history", "y", "--bogus", self.CANARY])
        self.check(["--bogus", self.CANARY, "report"])

    def test_a_missing_required_option(self):
        self.check(["collect", "--history", self.CANARY])
        self.check(["from-store", "--db", self.CANARY, "--labels", self.CANARY])
        self.check(["report", "--observations", self.CANARY, "--from", "2026-10-12"])

    def test_a_missing_or_unknown_subcommand(self):
        self.check([])
        self.check([self.CANARY])
        self.check(["collect-canary-private", self.CANARY])

    def test_a_stray_value_after_a_complete_command(self):
        self.check(["report", "--observations", self.CANARY, "--from", "2026-10-12", "--to", "2026-10-13", "extra-" + self.CANARY])

    def test_a_bad_date_that_gets_past_the_parser_prints_no_value_either(self):
        code, out, err = self.call(["report", "--observations", self.CANARY, "--from", "canary-not-a-date", "--to", "canary-2"])
        self.assertEqual(code, 3)
        self.assertNotIn("canary", (out + err).casefold())

    def test_help_still_prints_the_usage_and_exits_zero(self):
        for argv in (["--help"], ["from-store", "--help"], ["collect", "--help"], ["report", "--help"]):
            code, out, _err = self.call(argv)
            self.assertEqual(code, 0, argv)
            self.assertIn("usage:", out)


if __name__ == "__main__":
    unittest.main()
