"""Card 3acc-4: a label per time period in the alias table. Every value is made up; the store and the history folder are built here."""
import calendar
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_three_accounts_explain import ExplainCase, measure  # noqa: E402
from test_three_accounts_now import ALIAS_ONE  # noqa: E402

SWITCH = calendar.timegm((2026, 10, 23, 7, 50, 0))
A_READING = calendar.timegm((2026, 10, 23, 7, 35, 8))
C_READING = calendar.timegm((2026, 10, 23, 7, 52, 0))
LATER = calendar.timegm((2026, 10, 23, 8, 30, 0))
A_WEEK_RESET = calendar.timegm((2026, 10, 24, 1, 0, 0))
C_WEEK_RESET = calendar.timegm((2026, 10, 26, 21, 0, 0))
A_FIVE_RESET = calendar.timegm((2026, 10, 23, 7, 40, 0))
C_FIVE_RESET = calendar.timegm((2026, 10, 23, 12, 50, 0))
SPLIT = {ALIAS_ONE: [{"label": "A", "until": "2026-10-23T07:50:00Z"}, {"label": "C", "from": "2026-10-23T07:50:00Z"}]}
BAD_ALIAS_SENTENCE = "the alias table must map an account alias to exactly A, B or C"
TIME_SENTENCE = "a period time must look like 2026-10-23T07:50:00Z"


class Periods(ExplainCase):
    def table(self, value):
        self.labels.write_text(json.dumps(value), encoding="utf-8")

    def two_readings(self):
        self.store(ALIAS_ONE, A_READING, five=1700, week=9200, reset5=A_FIVE_RESET, reset7=A_WEEK_RESET)
        self.store(ALIAS_ONE, C_READING, five=100, week=0, reset5=C_FIVE_RESET, reset7=C_WEEK_RESET)

    def records(self, table=SPLIT):
        self.table(table)
        self.build_store()
        return measure.read_store(str(self.db), str(self.labels), LATER)

    def problem(self, value):
        self.table(value)
        if self.db.exists():
            self.db.unlink()
        self.build_store()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = measure.main(["now", "--db", str(self.db), "--labels", str(self.labels), "--history", str(self.history), "--now", str(LATER)])
        self.assertEqual(code, 3, value)
        self.assertEqual(out.getvalue().count("\n"), 1)
        self.assertNotIn(str(self.tmp), out.getvalue())
        return out.getvalue().strip()


class Repro(Periods):
    def test_one_alias_gives_a_before_the_switch_and_c_after_it(self):
        self.two_readings()
        recs, skipped = self.records()
        self.assertEqual(skipped, 0)
        self.assertEqual(sorted((r["label"], r["window"], r["observed_at"]) for r in recs),
                         sorted([("A", "five_hour", A_READING), ("A", "seven_day", A_READING), ("C", "five_hour", C_READING), ("C", "seven_day", C_READING)]))

    def test_from_store_writes_the_split_records(self):
        self.two_readings()
        self.table(SPLIT)
        self.build_store()
        out = self.tmp / "store.jsonl"
        code, line = measure.from_store(str(self.db), str(self.labels), str(out), now=LATER)
        self.assertEqual((code, line), (0, "from-store: records=4, skipped=0"))
        self.assertEqual({json.loads(x)["label"] for x in out.read_text(encoding="utf-8").splitlines()}, {"A", "C"})

    def test_now_shows_a_weekly_92_and_c_weekly_0_and_no_possible_switch(self):
        self.two_readings()
        self.table(SPLIT)
        self.put([])
        default, lines = self.run_both(now=LATER)
        self.assertIn("92%", self.rows(default, "A")[1])
        self.assertIn("0%", self.rows(default, "C")[1])
        self.assertNotIn("possible switch", default)
        self.assertFalse(any("possible switch" in line for line in lines), lines)
        self.check_wording(*self.explain_via("cli", now=LATER))

    def test_the_store_is_still_opened_read_only_with_a_period_table(self):
        self.two_readings()
        self.table(SPLIT)
        self.build_store()
        real, uris = measure.sqlite3.connect, []

        def spy(target, *args, **kwargs):
            uris.append(target)
            return real(target, *args, **kwargs)

        with mock.patch.object(measure.sqlite3, "connect", spy):
            measure.read_store(str(self.db), str(self.labels), LATER)
        readers = [u for u in uris if str(u).startswith("file:")]
        self.assertEqual(len(readers), 1)
        self.assertTrue(readers[0].endswith("?mode=ro"))


class OldForm(Periods):
    def test_a_plain_label_and_one_all_time_period_give_identical_output(self):
        self.two_readings()
        outputs = []
        for value in ("A", [{"label": "A"}]):
            self.table({ALIAS_ONE: value})
            if self.db.exists():
                self.db.unlink()
            self.build_store()
            out = self.tmp / "o.jsonl"
            measure.from_store(str(self.db), str(self.labels), str(out), now=LATER)
            outputs.append((out.read_bytes(), measure.now_command(str(self.db), str(self.labels), str(self.history), now=LATER)))
        self.assertEqual(outputs[0], outputs[1])


class Boundaries(Periods):
    def at(self, *times, table=SPLIT):
        for t in times:
            self.store(ALIAS_ONE, t, week=100, reset7=A_WEEK_RESET)
        return self.records(table)

    def test_a_reading_at_from_belongs_to_that_period_and_at_until_does_not(self):
        recs, _ = self.at(SWITCH - 1, SWITCH, SWITCH + 1)
        self.assertEqual({r["observed_at"]: r["label"] for r in recs}, {SWITCH - 1: "A", SWITCH: "C", SWITCH + 1: "C"})

    def test_readings_in_a_gap_or_before_the_first_from_are_skipped_and_counted(self):
        table = {ALIAS_ONE: [{"label": "A", "from": "2026-10-23T07:00:00Z", "until": "2026-10-23T07:30:00Z"},
                             {"label": "B", "from": "2026-10-23T08:00:00Z"}]}
        base = calendar.timegm((2026, 10, 23, 7, 0, 0))
        recs, skipped = self.at(base - 1, base, base + 1800 - 1, base + 1800, base + 3600, table=table)
        self.assertEqual([(r["label"], r["observed_at"]) for r in recs], [("A", base), ("A", base + 1799), ("B", base + 3600)])
        self.assertEqual(skipped, 0 + 2)


class SkippedCount(Periods):
    """A row whose observation time is not an integer is counted once per recognised window, as before periods existed; the old form and a period table agree."""

    def build_bad(self, alias_value, rows):
        self.table({ALIAS_ONE: alias_value})
        self.build_store()
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM readings")
        for number, (observed, windows) in enumerate(rows):
            payload = json.dumps({"windows": [{"key": k, "used_bps": 100, "resets_at": 1_000_000} for k in windows]})
            conn.execute("INSERT INTO readings VALUES(?,?,?,?,?,?,?,?)", ("x%d" % number, "m1", "a1", None, "cli_window", observed, 0, payload))
        conn.commit()
        conn.close()
        return measure.read_store(str(self.db), str(self.labels), LATER)

    def test_two_rows_with_a_bad_time_and_two_windows_each_count_four_in_both_forms(self):
        both = ["five_hour", "seven_day"]
        for value in ("A", [{"label": "A"}], SPLIT[ALIAS_ONE]):
            if self.db.exists():
                self.db.unlink()
            recs, skipped = self.build_bad(value, [("bad", both), ("bad", both)])
            self.assertEqual((len(recs), skipped), (0, 4), value)

    def test_rows_with_no_window_or_one_window_and_a_bad_time(self):
        for value in ("A", SPLIT[ALIAS_ONE]):
            if self.db.exists():
                self.db.unlink()
            recs, skipped = self.build_bad(value, [("bad", []), ("bad", ["five_hour"]), ("bad", ["five_hour", "seven_day"])])
            self.assertEqual((len(recs), skipped), (0, 3), value)

    def test_a_row_in_no_period_still_counts_once(self):
        both = ["five_hour", "seven_day"]
        recs, skipped = self.build_bad([{"label": "A", "from": "2026-10-23T09:00:00Z"}], [(1_000_000, both)])
        self.assertEqual((len(recs), skipped), (0, 1))


class Invalid(Periods):
    def test_each_invalid_table_exits_3_with_its_own_plain_sentence(self):
        good = {"label": "A", "until": "2026-10-23T07:50:00Z"}
        cases = {
            "neither": ("A and B", BAD_ALIAS_SENTENCE),
            "number": (5, BAD_ALIAS_SENTENCE),
            "empty": ([], "an alias with a list of periods needs at least one period"),
            "not-object": (["A"], "each period must be an object with a label and optional from and until"),
            "key": ([{"label": "A", "since": "2026-10-23T07:50:00Z"}], "a period may only have the keys label, from and until"),
            "label": ([{"label": "Z"}], "a period label must be exactly A, B or C"),
            "no-label": ([{"until": "2026-10-23T07:50:00Z"}], "a period label must be exactly A, B or C"),
            "no-z": ([{"label": "A", "until": "2026-10-23T07:50:00"}], TIME_SENTENCE),
            "fraction": ([{"label": "A", "until": "2026-10-23T07:50:00.5Z"}], TIME_SENTENCE),
            "date-only": ([{"label": "A", "until": "2026-10-23"}], TIME_SENTENCE),
            "impossible": ([{"label": "A", "until": "2026-02-30T07:50:00Z"}], TIME_SENTENCE),
            "from-after-until": ([{"label": "A", "from": "2026-10-23T08:00:00Z", "until": "2026-10-23T07:50:00Z"}], "a period must start before it ends"),
            "from-equals-until": ([{"label": "A", "from": "2026-10-23T07:50:00Z", "until": "2026-10-23T07:50:00Z"}], "a period must start before it ends"),
            "overlap": ([good, {"label": "C", "from": "2026-10-23T07:40:00Z"}], "periods of one alias must not overlap"),
            "no-from-after-first": ([good, {"label": "C"}], "every period after the first needs a from"),
            "order": ([{"label": "C", "from": "2026-10-23T09:00:00Z", "until": "2026-10-23T10:00:00Z"},
                       {"label": "A", "from": "2026-10-23T07:00:00Z", "until": "2026-10-23T08:00:00Z"}], "periods of one alias must be in time order"),
            "missing-until": ([{"label": "A", "from": "2026-10-23T07:00:00Z"}, {"label": "C", "from": "2026-10-23T08:00:00Z"}],
                              "every period except the last needs an until"),
        }
        for name, (value, sentence) in cases.items():
            self.assertEqual(self.problem({ALIAS_ONE: value}), "not done: " + sentence, name)

    def test_overlap_and_order_are_told_apart_by_the_start_only(self):
        order, overlap = "periods of one alias must be in time order", "periods of one alias must not overlap"
        cases = [
            ([{"label": "A", "until": "2026-10-23T10:00:00Z"}, {"label": "C", "from": "2026-10-23T08:00:00Z", "until": "2026-10-23T09:00:00Z"}], overlap),     # inside an open start
            ([{"label": "A", "from": "2026-10-23T09:00:00Z", "until": "2026-10-23T10:00:00Z"},
              {"label": "C", "from": "2026-10-23T07:00:00Z", "until": "2026-10-23T08:00:00Z"}], order),
            ([{"label": "A", "from": "2026-10-23T07:00:00Z", "until": "2026-10-23T10:00:00Z"}, {"label": "C", "from": "2026-10-23T09:00:00Z"}], overlap),
        ]
        for number, (value, sentence) in enumerate(cases):
            self.assertEqual(self.problem({ALIAS_ONE: value}), "not done: " + sentence, number)

    def test_a_table_that_is_not_an_object_keeps_todays_sentence(self):
        self.assertEqual(self.problem(["A"]), "not done: " + BAD_ALIAS_SENTENCE)


if __name__ == "__main__":
    unittest.main()
