"""Card 3acc-1: the read-only 'now' table of tools/three_accounts/measure.py. Every value is made up; the store and the history folder are built here."""
import contextlib
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import os

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "three_accounts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import measure  # noqa: E402
from test_three_accounts_measure import DAY, DAY0, HOUR, record  # noqa: E402

NOW = DAY0 + 3 * DAY + 12 * HOUR                    # a Thursday, 12:00 UTC
ALIAS_ONE = "zz-alias-one"
ALIAS_TWO = "zz-alias-two"
FOOTER = ("The 5-hour figure exists only while a seat on that subscription is working; with no working seat it is old or unknown.",
          "'At least' is a floor: the real figure may be higher.",
          "Use outside agenttalk cannot be separated from agent work with the readings available.",
          "Advice only: nothing is reserved, refused or enforced.")


class NowCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AGENTTALK_SCRATCH") or None)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.store_dir = self.tmp / "store"
        self.store_dir.mkdir()
        self.history = self.tmp / "history"
        self.history.mkdir()
        self.labels = self.tmp / "aliases.json"
        self.labels.write_text(json.dumps({ALIAS_ONE: "A", ALIAS_TWO: "B"}), encoding="utf-8")
        self.db = self.store_dir / "inventory.db"
        self.readings = []

    # --- fixtures
    def store(self, alias, observed, five=None, week=None, reset5=None, reset7=None):
        """One store reading of this machine; `five` and `week` are basis points."""
        windows = []
        if five is not None:
            windows.append({"key": "five_hour", "used_bps": five, "resets_at": (reset5 if reset5 is not None else observed + 3 * HOUR) * 1000})
        if week is not None:
            windows.append({"key": "seven_day", "used_bps": week, "resets_at": (reset7 if reset7 is not None else observed + 3 * DAY) * 1000})
        self.readings.append((alias, observed, json.dumps({"windows": windows})))

    def build_store(self):
        conn = sqlite3.connect(self.db)
        conn.executescript(
            "CREATE TABLE machines(id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE);"
            "CREATE TABLE accounts(id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE);"
            "CREATE TABLE readings(event_id TEXT PRIMARY KEY, machine_id TEXT, account_id TEXT, seat_id TEXT, kind TEXT, observed_at INTEGER, received_at INTEGER, payload TEXT);"
            "INSERT INTO machines VALUES('m1','laptop-b');"
            "INSERT INTO accounts VALUES('a1','%s'),('a2','%s');" % (ALIAS_ONE, ALIAS_TWO))
        for number, (alias, observed, payload) in enumerate(self.readings):
            conn.execute("INSERT INTO readings VALUES(?,?,?,?,?,?,?,?)", ("e%d" % number, "m1", "a1" if alias == ALIAS_ONE else "a2", None, "cli_window", observed * 1000, 0, payload))
        conn.commit()
        conn.close()

    def write_history(self, records, receipt=None, tail=""):
        (self.history / "observations.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records) + tail, encoding="utf-8")
        if receipt is not None:
            (self.history / "receipts.json").write_text(json.dumps({"receipts_version": 1, "last_accepted": {"desktop": receipt}}), encoding="utf-8")

    def table(self, incomplete=(), now=NOW):
        self.build_store()
        return measure.now_command(str(self.db), str(self.labels), str(self.history), incomplete, now=now)

    def rows(self, text, label):
        """The lines under 'Label X', up to the next blank line."""
        lines = text.splitlines()
        start = lines.index("Label %s" % label)
        out = []
        for line in lines[start + 1:]:
            if not line.strip():
                break
            out.append(line)
        return out

    def snapshot(self):
        return {str(p.relative_to(self.tmp)): p.read_bytes() for p in sorted(self.tmp.rglob("*")) if p.is_file()}


def weekday_time(epoch):
    return time.strftime("%a %H:%M UTC", time.gmtime(epoch))


class Shape(NowCase):
    def test_six_rows_always_and_never_an_empty_cell_or_a_zero_for_nothing(self):
        text = self.table()
        for label in "ABC":
            rows = self.rows(text, label)
            self.assertEqual([row.split()[0] for row in rows], ["five_hour", "seven_day"])
            self.assertTrue(all(row.endswith("unknown: no reading recorded") for row in rows), rows)
        self.assertNotIn("0%", text)

    def test_the_footer_is_the_four_fixed_sentences(self):
        text = self.table()
        self.assertEqual(text.splitlines()[-4:], list(FOOTER))

    def test_the_labels_are_always_listed_in_order_even_with_readings_for_one(self):
        self.store(ALIAS_ONE, NOW - 60, five=1000, week=2000)
        text = self.table()
        self.assertEqual([line for line in text.splitlines() if line.startswith("Label ")], ["Label A", "Label B", "Label C"])


class Wordings(NowCase):
    def test_fresh(self):
        self.store(ALIAS_ONE, NOW - 180, five=1200, week=3000)
        rows = self.rows(self.table(), "A")
        self.assertIn("12% (fresh, 3 min old); resets " + weekday_time(NOW - 180 + 3 * HOUR) + "; from laptop-b", rows[0])
        self.assertIn("30% (fresh, 3 min old)", rows[1])

    def test_fresh_at_exactly_600_seconds_and_floor_one_second_later(self):
        self.store(ALIAS_ONE, NOW - 600, five=1200)
        self.assertIn("12% (fresh, 10 min old)", self.rows(self.table(), "A")[0])
        self.readings.clear()
        self.db.unlink()
        self.store(ALIAS_ONE, NOW - 601, five=1200)
        self.assertIn("at least 12% (10 min old)", self.rows(self.table(), "A")[0])

    def test_floor(self):
        self.store(ALIAS_ONE, NOW - (2 * HOUR + 10 * 60), five=4500, reset5=NOW + HOUR)
        self.assertIn("at least 45% (2 h 10 min old); resets " + weekday_time(NOW + HOUR) + "; from laptop-b", self.rows(self.table(), "A")[0])

    def test_a_decimal_percentage_keeps_one_decimal(self):
        self.store(ALIAS_ONE, NOW - 60, five=1250)
        self.assertIn("12.5% (fresh, 1 min old)", self.rows(self.table(), "A")[0])

    def test_reset_passed(self):
        reset = NOW - 3600
        self.store(ALIAS_ONE, NOW - 2 * HOUR, five=9000, reset5=reset)
        row = self.rows(self.table(), "A")[0]
        self.assertTrue(row.endswith("unknown: reset since %s, no new reading" % weekday_time(reset)), row)
        self.assertNotIn("90%", row)

    def test_a_reset_exactly_now_has_passed(self):
        self.store(ALIAS_ONE, NOW - 60, five=500, reset5=NOW)
        self.assertIn("unknown: reset since %s" % weekday_time(NOW), self.rows(self.table(), "A")[0])

    def test_nothing_ever(self):
        self.store(ALIAS_ONE, NOW - 60, five=500)
        rows = self.rows(self.table(), "A")
        self.assertTrue(rows[1].endswith("unknown: no reading recorded"), rows)       # the weekly window has no reading, though the 5-hour one has

    def test_a_reading_observed_after_now_is_not_used(self):
        self.store(ALIAS_ONE, NOW + 600, five=500)
        self.assertTrue(self.rows(self.table(), "A")[0].endswith("unknown: no reading recorded"))


class Sources(NowCase):
    def test_the_newest_observation_wins_across_the_store_and_the_history(self):
        self.store(ALIAS_ONE, NOW - 1000, five=3000)
        self.write_history([record(label="A", window="five_hour", used=33.0, resets=NOW + HOUR, observed=NOW - 100, machine="desktop")], receipt=NOW - 100)
        row = self.rows(self.table(), "A")[0]
        self.assertIn("33% (fresh, 1 min old)", row)
        self.assertTrue(row.endswith("from desktop"), row)

    def test_the_store_wins_when_it_is_newer(self):
        self.store(ALIAS_ONE, NOW - 100, five=3000)
        self.write_history([record(label="A", window="five_hour", used=33.0, resets=NOW + HOUR, observed=NOW - 1000, machine="desktop")], receipt=NOW - 100)
        row = self.rows(self.table(), "A")[0]
        self.assertIn("30% (fresh, 1 min old)", row)
        self.assertTrue(row.endswith("from laptop-b"), row)

    def test_b_comes_from_the_history_alone_and_c_has_nothing(self):
        self.write_history([record(label="B", window="seven_day", used=61.0, resets=NOW + DAY, observed=NOW - 30, machine="desktop")], receipt=NOW - 30)
        text = self.table()
        self.assertIn("61% (fresh, 0 min old)", self.rows(text, "B")[1])
        self.assertTrue(all(row.endswith("unknown: no reading recorded") for row in self.rows(text, "C")))

    def test_a_half_written_last_line_is_skipped_and_counted(self):
        self.write_history([record(label="B", window="five_hour", used=10.0, resets=NOW + HOUR, observed=NOW - 60, machine="desktop")], receipt=NOW - 60,
                           tail='{"machine": "desktop", "label": "B", "wind')
        text = self.table()
        self.assertIn("10% (fresh, 1 min old)", self.rows(text, "B")[0])
        self.assertIn("(1 unreadable history line(s) skipped)", text)

    def test_no_skipped_line_note_when_nothing_was_skipped(self):
        self.assertNotIn("unreadable history line", self.table())


class DesktopCopy(NowCase):
    def test_a_recent_copy(self):
        self.write_history([], receipt=NOW - 5 * 60)
        self.assertIn("desktop copy 5 min old", self.table().splitlines())

    def test_exactly_fifteen_minutes_is_not_late(self):
        self.write_history([], receipt=NOW - 900)
        self.assertIn("desktop copy 15 min old", self.table().splitlines())

    def test_one_second_over_fifteen_minutes_is_late(self):
        self.write_history([], receipt=NOW - 901)
        self.assertIn("desktop copy late (15 min)", self.table().splitlines())

    def test_a_long_late_copy(self):
        self.write_history([], receipt=NOW - 2 * HOUR)
        self.assertIn("desktop copy late (120 min)", self.table().splitlines())

    def test_missing_or_unreadable_receipts_say_so_and_the_table_still_prints(self):
        unreadable = "desktop history unreadable: B can only be old or unknown"
        self.write_history([])
        self.assertIn(unreadable, self.table().splitlines())                                  # no receipts file
        for text in ("not json", "[]", json.dumps({"receipts_version": 1, "last_accepted": {"other-box": 5}}),
                     json.dumps({"receipts_version": 2, "last_accepted": {"desktop": 5}})):
            self.db.unlink()
            self.readings.clear()
            (self.history / "receipts.json").write_text(text, encoding="utf-8")
            result = self.table()
            self.assertIn(unreadable, result.splitlines(), text)
            self.assertEqual(len(self.rows(result, "A")), 2)

    def test_a_missing_history_folder_is_the_same_as_no_history(self):
        self.build_store()
        text = measure.now_command(str(self.db), str(self.labels), str(self.tmp / "no-such-history"), (), now=NOW)
        self.assertIn("desktop history unreadable: B can only be old or unknown", text)
        self.assertFalse((self.tmp / "no-such-history").exists())


class Switches(NowCase):
    def weekly(self, days_ago):
        """Two weekly figures of A whose reset time jumps while the earlier one is still ahead; the second was observed `days_ago` days before NOW."""
        later = NOW - int(days_ago * DAY)
        self.write_history([record(label="A", window="seven_day", used=40.0, resets=later + 2 * DAY, observed=later - HOUR, machine="desktop"),
                            record(label="A", window="seven_day", used=41.0, resets=later + 9 * DAY, observed=later - 900, machine="desktop")], receipt=NOW - 60)

    def test_a_flag_six_days_old_marks_the_label_and_hides_nothing(self):
        self.weekly(6)
        rows = self.rows(self.table(), "A")
        marks = [row for row in rows if "possible switch on" in row]
        self.assertEqual(len(marks), 1)
        self.assertIn("possible switch on %s: which subscription this is, is uncertain (the lead confirms it in the accounts note)"
                      % time.strftime("%Y-%m-%d", time.gmtime(NOW - 6 * DAY)), marks[0])
        self.assertIn("at least 41%", rows[1])                                                 # the figure is still shown, under its own label

    def test_a_flag_eight_days_old_does_not(self):
        self.weekly(8)
        text = self.table()
        self.assertNotIn("possible switch", text)

    def test_a_normal_weekly_reset_marks_nothing(self):
        earlier = NOW - 2 * DAY
        self.write_history([record(label="A", window="seven_day", used=90.0, resets=earlier, observed=earlier - HOUR, machine="desktop"),
                            record(label="A", window="seven_day", used=3.0, resets=earlier + 7 * DAY, observed=earlier + 600, machine="desktop")], receipt=NOW - 60)
        self.assertNotIn("possible switch", self.table())

    def test_the_mark_sits_under_the_label_it_belongs_to(self):
        self.weekly(1)
        text = self.table()
        self.assertEqual(sum("possible switch" in row for row in self.rows(text, "A")), 1)
        self.assertFalse(any("possible switch" in row for row in self.rows(text, "B") + self.rows(text, "C")))


class Coverage(NowCase):
    LINE = "  coverage incomplete: a machine that does not report also uses this subscription"

    def test_the_line_appears_only_for_the_labels_asked_for(self):
        text = self.table(incomplete=("A",))
        self.assertIn(self.LINE, self.rows(text, "A"))
        self.assertNotIn(self.LINE, self.rows(text, "B") + self.rows(text, "C"))

    def test_two_labels(self):
        text = self.table(incomplete=("A", "C"))
        self.assertEqual(sum(self.LINE in self.rows(text, label) for label in "ABC"), 2)

    def test_an_unknown_label_is_refused_with_a_plain_sentence(self):
        self.build_store()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = measure.main(["now", "--db", str(self.db), "--labels", str(self.labels), "--history", str(self.history), "--incomplete-coverage", "Z"])
        self.assertEqual(code, 3)
        self.assertEqual(stdout.getvalue().strip(), "not done: --incomplete-coverage must be A, B or C")


class ReadOnlyAndPrivate(NowCase):
    def test_nothing_is_written_to_the_history_folder_or_the_store_folder(self):
        self.store(ALIAS_ONE, NOW - 60, five=500, week=900)
        self.write_history([record(label="B", window="five_hour", used=10.0, resets=NOW + HOUR, observed=NOW - 60, machine="desktop")], receipt=NOW - 60,
                           tail="half a line")
        self.build_store()
        before = self.snapshot()
        measure.now_command(str(self.db), str(self.labels), str(self.history), ("A",), now=NOW)
        self.assertEqual(self.snapshot(), before)                                              # same file names, same bytes
        self.assertFalse((self.history / ".collect.lock").exists())

    def test_the_output_holds_no_alias_path_or_user_name(self):
        self.store(ALIAS_ONE, NOW - 60, five=500, week=900)
        self.store(ALIAS_TWO, NOW - 60, five=500)
        self.build_store()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = measure.main(["now", "--db", str(self.db), "--labels", str(self.labels), "--history", str(self.history), "--now", str(NOW)])
        self.assertEqual(code, 0)
        shown = stdout.getvalue() + stderr.getvalue()
        for private in (ALIAS_ONE, ALIAS_TWO, str(self.tmp), self.tmp.name, "@", "home-", "\\"):
            self.assertNotIn(private, shown)
        self.assertEqual(stderr.getvalue(), "")

    def test_the_default_clock_is_used_without_now(self):
        self.build_store()
        with mock.patch.object(measure.time, "time", return_value=float(NOW)):
            text = measure.now_command(str(self.db), str(self.labels), str(self.history))
        self.assertIn("Usage now, Thu 2026-10-15 12:00 UTC", text)

    def run_now(self, db, labels):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = measure.main(["now", "--db", str(db), "--labels", str(labels), "--history", str(self.history), "--now", str(NOW)])
        self.assertNotIn(str(self.tmp), stdout.getvalue() + stderr.getvalue())
        self.assertNotIn("Traceback", stdout.getvalue() + stderr.getvalue())
        return code, stdout.getvalue().strip()

    def test_a_missing_store_is_one_plain_sentence_with_no_path(self):
        self.assertEqual(self.run_now(self.tmp / "missing.db", self.labels), (3, "not done: the store could not be opened read-only"))

    def test_a_missing_alias_table_is_one_plain_sentence_with_no_path(self):
        self.build_store()
        self.assertEqual(self.run_now(self.db, self.tmp / "missing.json"), (3, "not done: the alias table cannot be read as a JSON object"))

    def test_a_bad_now_value_is_the_generic_argument_sentence(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = measure.main(["now", "--db", "x", "--labels", "y", "--history", "z", "--now", "canary-value"])
        self.assertEqual(code, 3)
        self.assertNotIn("canary", stdout.getvalue() + stderr.getvalue())


class FromStoreIsUnchanged(unittest.TestCase):
    """The reading of the store was moved into `read_store`; the file `from-store` writes must stay byte-for-byte what it was before."""

    def test_the_output_file_and_the_counts_are_exactly_what_they_were(self):
        from test_three_accounts_measure import FromStore
        case = FromStore()
        case.setUp()
        self.addCleanup(case._tmp.cleanup)
        db = case.build()
        labels = case.tmp / "aliases.json"
        labels.write_text(json.dumps({"main-one": "A", "main-two": "B"}), encoding="utf-8")
        out = case.tmp / "store.jsonl"
        code, line = measure.from_store(str(db), str(labels), str(out), now=DAY0 + 5000)
        self.assertEqual((code, line), (0, "from-store: records=6, skipped=2"))
        self.assertEqual(hashlib.sha256(out.read_bytes()).hexdigest(), "29c637dcddc9f46d81f42b3630af28f55b5b3ffae158cb0b377077524f037ae4")


if __name__ == "__main__":
    unittest.main()
