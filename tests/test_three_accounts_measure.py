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
import sys
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


if __name__ == "__main__":
    unittest.main()
