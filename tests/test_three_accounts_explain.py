"""Card 3acc-3: the `--explain` block under the usage table of tools/three_accounts/measure.py. Every value is made up (labels and percentages only)."""
import calendar
import contextlib
import decimal
import hashlib
import io
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_three_accounts_measure import record  # noqa: E402
from test_three_accounts_now import ALIAS_ONE, ALIAS_TWO, DAY, HOUR, NOW, NowCase, measure  # noqa: E402

D = calendar.timegm((2026, 10, 21, 0, 0, 0))                   # Wednesday 21 October 2026, 00:00 UTC
FRIDAY_21 = calendar.timegm((2026, 10, 23, 21, 0, 0))          # B's own weekly reset, Fri 21:00 UTC
SATURDAY_01 = calendar.timegm((2026, 10, 24, 1, 0, 0))         # the other subscription's weekly reset, Sat 01:00 UTC
FIVE_1530 = D + 15 * HOUR + 30 * 60


def at(hour, minute, second=0):
    return D + hour * HOUR + minute * 60 + second


NOW21 = at(15, 19)                                             # the moment of the misfiled reading's day

FORBIDDEN = ("available", "enough", "room", "can finish", "will last", "lasts", "safe", "best", "should", "rank", "per hour", "rate", "possibly")

STAMP = r"[A-Z][a-z]{2} \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC"
PCT = r"\d+(?:\.\d)?%"
WIN = r"(?:5-hour|weekly)"
T1 = r"If this reading and its label are right, at most %s of the %s allowance is left until %s \(reading of %s\)\." % (PCT, WIN, STAMP, STAMP)
T2 = r" Its readings rose from %s \(%s\) to %s \(%s\)\." % (PCT, STAMP, PCT, STAMP)
T3_REASON = (r"(?:its weekly reset moved from %s to %s and its figure went from %s to %s"
             r"|its weekly figure fell from %s to %s within one reset period \(it resets %s\))" % (STAMP, STAMP, PCT, PCT, PCT, PCT, STAMP))
T3 = (r"[ABC]: a possible switch was flagged on \d{4}-\d{2}-\d{2}: %s\."
      r" [ABC]'s readings since then are not used here until one shows [ABC]'s own reset times again\." % T3_REASON)
T4 = r"[ABC] %s: no (?:newer )?reading has been recorded here\. A turn by a seat logged into [ABC] may supply one; check that a usable reading arrives under [ABC] before relying on it\." % WIN
SENTENCE = re.compile(r"(?:For [ABC]: %s(?:%s)?|%s|%s|Nothing to add to the table\.)" % (T1, T2, T3, T4))


def explanations(output, default_output):
    """The part of an `--explain` output after the default table; the table itself is left unchanged by the flag."""
    assert output.startswith(default_output + "\n\n"), "the table must come first and unchanged"
    return [line for line in output[len(default_output) + 2:].split("\n")]


class ExplainCase(NowCase):
    def explain_via(self, how="direct", incomplete=(), now=None):
        """-> (default output, explain output), by calling `now_command` or by running the command line; both read the same fixtures."""
        if self.db.exists():
            self.db.unlink()
        self.build_store()
        when = NOW if now is None else now
        args = (str(self.db), str(self.labels), str(self.history))
        default = measure.now_command(*args, incomplete, now=when)
        if how == "direct":
            return default, measure.now_command(*args, incomplete, now=when, explain=True)
        argv = ["now", "--db", args[0], "--labels", args[1], "--history", args[2], "--now", str(when), "--explain"]
        for label in incomplete:
            argv += ["--incomplete-coverage", label]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = measure.main(argv)
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        return default, stdout.getvalue().rstrip("\n")

    def check_wording(self, default, explained):
        """THE shared check on every explanation output (every test of this file that prints one calls it): the table comes first and unchanged, every line of the
        block is one of the five sentences, and no forbidden word appears anywhere in the output apart from the four fixed footer sentences of the table, which
        were approved before this card and must stay byte-for-byte (one of them says "available"). -> the lines of the block."""
        lines = explanations(explained, default)
        self.assertTrue(lines and all(lines), "the block has sentences, one per line, no blank line")
        for line in lines:
            self.assertRegex(line, "^" + SENTENCE.pattern + "$", line)
        checked = explained
        for fixed in measure.NOW_FOOTER:
            checked = checked.replace(fixed, "")
        for word in FORBIDDEN:
            self.assertIsNone(re.search(r"\b%s\b" % re.escape(word), checked, re.IGNORECASE), word)
        return lines

    def run_both(self, how="direct", incomplete=(), **kwargs):
        """-> (default output, lines of the block), after the shared check."""
        default, explained = self.explain_via(how, incomplete, **kwargs)
        return default, self.check_wording(default, explained)

    def b_series(self):
        """B's own weekly readings of 21 October, all resetting Friday 21:00 UTC."""
        return [record(label="B", window="seven_day", used=87.0, resets=FRIDAY_21, observed=at(6, 8, 2), machine="desktop"),
                record(label="B", window="seven_day", used=91.0, resets=FRIDAY_21, observed=at(12, 31, 23), machine="desktop"),
                record(label="B", window="seven_day", used=92.0, resets=FRIDAY_21, observed=at(14, 17, 55), machine="desktop")]

    def misfiled(self):
        """The desktop reading of the other subscription, filed under B: weekly 60% resetting Sat 01:00, 5-hour 6% resetting 15:30."""
        return [record(label="B", window="seven_day", used=60.0, resets=SATURDAY_01, observed=at(14, 48, 58), machine="desktop"),
                record(label="B", window="five_hour", used=6.0, resets=FIVE_1530, observed=at(14, 48, 58), machine="desktop")]

    def a_readings(self):
        self.store(ALIAS_ONE, at(10, 35), five=800, week=4100, reset5=FIVE_1530, reset7=SATURDAY_01)

    def put(self, records, receipt=None):
        self.write_history(records, receipt=at(15, 15) if receipt is None else receipt)

    def rich(self):
        """The fixture of the byte-for-byte test: both sources, a flagged label, a coverage line, a copy line."""
        self.a_readings()
        self.store(ALIAS_TWO, at(3, 0), five=300)
        self.put(self.b_series() + self.misfiled())


# The default output of `now` for ExplainCase.rich at NOW21 with coverage on A, recorded from the code of main bd3332b3 before this change.
BASELINE_SHA = "58d97867b11257c91f4cdd334050a4e33b911d9c3bd0e944a8a6a5c4344c397b"


class DefaultOutputIsUnchanged(ExplainCase):
    def test_the_default_output_is_byte_identical_to_the_code_before_this_change(self):
        self.rich()
        self.build_store()
        text = measure.now_command(str(self.db), str(self.labels), str(self.history), ("A",), now=NOW21)
        self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(), BASELINE_SHA, text)

    def test_without_the_flag_no_explanation_is_printed(self):
        self.rich()
        self.build_store()
        text = measure.now_command(str(self.db), str(self.labels), str(self.history), now=NOW21)
        self.assertNotIn("If this reading", text)
        self.assertNotIn("Nothing to add", text)
        self.assertTrue(text.endswith(measure.NOW_FOOTER[-1]))


class UpperBound(ExplainCase):
    def test_case_one_b_before_the_switch(self):
        self.a_readings()
        self.put([r for r in self.b_series() if r["observed_at"] <= at(12, 31, 23)], receipt=at(13, 30))
        _default, lines = self.run_both(now=at(13, 34))
        self.assertIn("For B: If this reading and its label are right, at most 9% of the weekly allowance is left until Fri 2026-10-23 21:00 UTC "
                      "(reading of Wed 2026-10-21 12:31 UTC). Its readings rose from 87% (Wed 2026-10-21 06:08 UTC) to 91% (Wed 2026-10-21 12:31 UTC).", lines)
        self.assertIn("For A: If this reading and its label are right, at most 59% of the weekly allowance is left until Sat 2026-10-24 01:00 UTC "
                      "(reading of Wed 2026-10-21 10:35 UTC).", lines)

    def test_the_bound_is_never_below_one_hundred_minus_the_stored_value_and_never_negative_for_every_hundredth_to_1000(self):
        t1 = re.compile("^" + T1 + "$")
        for hundredths in range(0, 100001):
            used = hundredths / 100
            text = measure._left_text(used)
            shown = decimal.Decimal(text.rstrip("%"))
            exact = decimal.Decimal(100) - decimal.Decimal(str(used))
            self.assertGreaterEqual(shown, exact, used)
            self.assertGreaterEqual(shown, 0, used)
            self.assertNotIn("-", text, used)
            self.assertLess(shown - max(exact, decimal.Decimal(0)), decimal.Decimal("0.1"), used)                    # and no looser than one decimal
            self.assertRegex(measure._sentence_t1(record(window="seven_day", used=used, resets=SATURDAY_01, observed=at(10, 0)), "seven_day"), t1)

    def test_an_over_use_figure_gives_a_bound_of_zero_in_the_real_output(self):
        for stored_bps in (10004, 10000, 99999):
            self.readings.clear()
            self.store(ALIAS_ONE, at(15, 15), week=stored_bps, reset7=SATURDAY_01)
            self.put([])
            _default, lines = self.run_both(now=NOW21)
            self.assertTrue(any("at most 0% of the weekly allowance" in line for line in lines), (stored_bps, lines))

    def test_the_bound_comes_from_the_stored_value_not_the_cut_figure_of_the_table(self):
        self.store(ALIAS_ONE, at(15, 15), week=int(8764), reset7=SATURDAY_01)                 # stored 87.64%: the table shows 87.6%, the bound is 12.4%
        self.put([])
        default, lines = self.run_both(now=NOW21)
        self.assertIn("87.6% (fresh", default)
        self.assertTrue(any("at most 12.4% of the weekly" in line for line in lines), lines)

    def test_no_bound_for_a_figure_whose_reset_has_passed_or_that_does_not_exist(self):
        self.store(ALIAS_ONE, at(9, 0), five=500, reset5=at(12, 0))
        self.put([])
        _default, lines = self.run_both(now=NOW21)
        self.assertFalse(any(line.startswith("For A") and "5-hour" in line for line in lines), lines)
        self.assertTrue(any(line.startswith("A 5-hour: no newer reading") for line in lines), lines)
        self.assertTrue(any(line.startswith("C 5-hour: no reading has been recorded here") for line in lines), lines)


class ObservedChange(ExplainCase):
    def test_times_are_the_observations_own_times_to_the_minute(self):
        self.put(self.b_series(), receipt=at(14, 30))
        _default, lines = self.run_both(now=at(14, 40))
        line = next(line for line in lines if line.startswith("For B"))
        self.assertIn("rose from 87% (Wed 2026-10-21 06:08 UTC) to 92% (Wed 2026-10-21 14:17 UTC).", line)
        self.assertNotIn("06:07", line)

    def test_a_rise_under_one_point_or_a_single_reading_adds_no_change_sentence(self):
        self.put([record(label="B", window="seven_day", used=50.0, resets=FRIDAY_21, observed=at(10, 0), machine="desktop"),
                  record(label="B", window="seven_day", used=50.9, resets=FRIDAY_21, observed=at(14, 0), machine="desktop")])
        _default, lines = self.run_both(now=at(14, 30))
        self.assertNotIn("rose", next(line for line in lines if line.startswith("For B")))
        self.readings.clear()
        self.put([record(label="B", window="seven_day", used=50.0, resets=FRIDAY_21, observed=at(10, 0), machine="desktop")])
        _default, lines = self.run_both(now=at(14, 30))
        self.assertNotIn("rose", next(line for line in lines if line.startswith("For B")))

    def test_only_the_last_twelve_hours_and_the_same_reset_period_count(self):
        self.put([record(label="B", window="seven_day", used=10.0, resets=FRIDAY_21, observed=at(1, 0), machine="desktop"),
                  record(label="B", window="seven_day", used=40.0, resets=FRIDAY_21, observed=at(5, 0), machine="desktop"),
                  record(label="B", window="seven_day", used=45.0, resets=FRIDAY_21, observed=at(14, 0), machine="desktop")])
        _default, lines = self.run_both(now=at(14, 30))
        line = next(line for line in lines if line.startswith("For B"))
        self.assertIn("rose from 40% (Wed 2026-10-21 05:00 UTC) to 45%", line)                     # 01:00 is more than 12 hours before 14:30
        self.assertNotIn("10%", line)

    def test_no_rate_and_no_projection(self):
        self.put(self.b_series(), receipt=at(14, 30))
        _default, lines = self.run_both(now=at(14, 40))
        for line in lines:
            self.assertNotRegex(line, r"(?i)\bper\b|\bwill\b|\bby (?:Fri|Sat|Sun|Mon|Tue|Wed|Thu)\b")


class LabelInDoubt(ExplainCase):
    def test_case_two_the_misfiled_reading_gets_the_doubt_sentence_without_a_hint_and_no_bound(self):
        self.a_readings()
        self.put(self.b_series() + self.misfiled(), receipt=at(15, 15))
        _default, lines = self.run_both(now=NOW21)
        t3 = next(line for line in lines if line.startswith("B: a possible switch"))
        self.assertIn("flagged on 2026-10-21", t3)
        self.assertIn("moved from Fri 2026-10-23 21:00 UTC to Sat 2026-10-24 01:00 UTC and its figure went from 92% to 60%", t3)
        self.assertNotIn("may be", t3)                                                              # the hint is cut: the sentence names no other label
        self.assertEqual(set(re.findall(r"\b[ABC]\b", t3)), {"B"})
        self.assertFalse(any(line.startswith("For B") for line in lines), "no bound and no change sentence for the label in doubt")
        self.assertFalse(any(line.startswith("B ") for line in lines), "and no 'cannot tell' sentence for rows that do have readings")
        self.assertTrue(any(line.startswith("For A") for line in lines))

    def test_the_doubt_outlives_the_seven_day_note(self):
        self.a_readings()
        self.put(self.b_series() + self.misfiled(), receipt=at(15, 15) + 9 * DAY)
        default, lines = self.run_both(now=NOW21 + 9 * DAY)
        self.assertNotIn("possible switch on", default)                                        # the table's own note has ended
        self.assertTrue(any(line.startswith("B: a possible switch") for line in lines))
        self.assertFalse(any(line.startswith("For B") for line in lines))

    def test_a_later_b_reading_on_the_other_subscriptions_reset_times_does_not_end_it(self):
        self.a_readings()
        later = record(label="B", window="seven_day", used=63.0, resets=SATURDAY_01, observed=at(16, 30), machine="desktop")
        self.put(self.b_series() + self.misfiled() + [later])
        _default, lines = self.run_both(now=at(16, 40))
        self.assertTrue(any(line.startswith("B: a possible switch") for line in lines))
        self.assertFalse(any(line.startswith("For B") for line in lines))

    def test_b_is_eligible_again_only_on_its_own_friday_series(self):
        self.a_readings()
        own = record(label="B", window="seven_day", used=93.0, resets=FRIDAY_21, observed=at(17, 0), machine="desktop")
        self.put(self.b_series() + self.misfiled() + [own])
        _default, lines = self.run_both(now=at(17, 10))
        self.assertFalse(any(line.startswith("B: a possible switch") for line in lines))
        self.assertTrue(any(line.startswith("For B: If this reading") and "at most 7% of the weekly" in line for line in lines), lines)

    def test_the_returning_reading_after_a_week_fits_the_series_by_whole_weeks(self):
        next_week = record(label="B", window="seven_day", used=3.0, resets=FRIDAY_21 + 7 * DAY, observed=FRIDAY_21 + 3600, machine="desktop")
        self.put(self.b_series() + self.misfiled() + [next_week], receipt=FRIDAY_21 + 3500)
        _default, lines = self.run_both(now=FRIDAY_21 + 3700)
        self.assertFalse(any(line.startswith("B: a possible switch") for line in lines))
        self.assertTrue(any(line.startswith("For B") and "at most 97%" in line for line in lines), lines)

    def test_the_misfiled_five_hour_reading_gets_no_bound_either(self):
        self.put(self.b_series() + self.misfiled())
        _default, lines = self.run_both(now=NOW21)
        self.assertFalse(any("5-hour allowance" in line and line.startswith("For B") for line in lines))


class DoubtRuleItself(unittest.TestCase):
    """`doubt_walk` and `fits_series` on their own."""

    def weekly(self, *readings):
        return [record(label="B", window="seven_day", used=used, resets=resets, observed=observed, machine="desktop") for used, resets, observed in readings]

    def test_a_series_with_no_flag_is_all_eligible(self):
        eligible, episode = measure.doubt_walk("B", self.weekly((10, FRIDAY_21, at(1, 0)), (20, FRIDAY_21, at(5, 0))))
        self.assertEqual((eligible, episode), ([True, True], None))

    def test_the_flagged_reading_and_every_later_misfit_are_in_doubt_whatever_their_age(self):
        series = self.weekly((90, FRIDAY_21, at(1, 0)), (60, SATURDAY_01, at(2, 0)), (61, SATURDAY_01, at(2, 0) + 30 * DAY))
        eligible, episode = measure.doubt_walk("B", series)
        self.assertEqual(eligible, [True, False, False])
        self.assertEqual(episode["anchor"], FRIDAY_21)

    def test_a_reading_on_the_old_series_ends_it_and_its_own_flag_does_not_restart_it(self):
        series = self.weekly((90, FRIDAY_21, at(1, 0)), (60, SATURDAY_01, at(2, 0)), (91, FRIDAY_21, at(3, 0)), (92, FRIDAY_21, at(4, 0)))
        eligible, episode = measure.doubt_walk("B", series)
        self.assertEqual((eligible, episode), ([True, False, True, True], None))

    def test_a_whole_number_of_weeks_fits_and_a_part_of_a_week_does_not(self):
        self.assertTrue(measure.fits_series(FRIDAY_21 + 14 * DAY, FRIDAY_21))
        self.assertTrue(measure.fits_series(FRIDAY_21 + 299, FRIDAY_21))
        self.assertFalse(measure.fits_series(FRIDAY_21 + 301, FRIDAY_21))
        self.assertFalse(measure.fits_series(SATURDAY_01, FRIDAY_21))
        self.assertFalse(measure.fits_series(FRIDAY_21 - 7 * DAY, FRIDAY_21), "weeks before the anchor are not the label's series going forward")

    def test_a_new_misfit_after_recovery_starts_a_new_doubt_on_the_recovered_series(self):
        series = self.weekly((90, FRIDAY_21, at(1, 0)), (60, SATURDAY_01, at(2, 0)), (91, FRIDAY_21, at(3, 0)), (50, SATURDAY_01, at(4, 0)))
        eligible, episode = measure.doubt_walk("B", series)
        self.assertEqual(eligible, [True, False, True, False])
        self.assertEqual(episode["anchor"], FRIDAY_21)

    def test_a_percentage_fall_on_the_same_reset_time_is_in_doubt_until_a_reading_fits(self):
        series = self.weekly((80, FRIDAY_21, at(1, 0)), (30, FRIDAY_21, at(2, 0)), (31, FRIDAY_21, at(3, 0)))
        eligible, episode = measure.doubt_walk("B", series)
        self.assertEqual(eligible, [True, False, True])                                     # the next reading is on the label's own reset time
        self.assertIsNone(episode)

    def test_a_five_hour_reading_follows_the_latest_weekly_reading_at_its_time(self):
        series = self.weekly((90, FRIDAY_21, at(1, 0)), (60, SATURDAY_01, at(14, 0)), (91, FRIDAY_21, at(16, 0)))
        eligible, _episode = measure.doubt_walk("B", series)
        early = record(label="B", window="five_hour", observed=at(0, 30))
        during = record(label="B", window="five_hour", observed=at(15, 0))
        after = record(label="B", window="five_hour", observed=at(17, 0))
        verdicts = [measure.reading_eligible("five_hour", r, series, eligible) for r in (early, during, after)]
        self.assertEqual(verdicts, [True, False, True])


class TrustedSeries(unittest.TestCase):
    """The label keeps its own weekly series for good, not only while a doubt is open."""

    def weekly(self, *readings):
        return [record(label="B", window="seven_day", used=used, resets=resets, observed=observed, machine="desktop") for used, resets, observed in readings]

    def test_friday_saturday_friday_then_saturday_again_after_the_friday_reset_is_ineligible(self):
        after_reset = FRIDAY_21 + 3600
        series = self.weekly((90, FRIDAY_21, at(1, 0)), (60, SATURDAY_01, at(2, 0)), (91, FRIDAY_21, at(3, 0)), (5, SATURDAY_01, after_reset))
        eligible, episode = measure.doubt_walk("B", series)
        self.assertEqual(eligible, [True, False, True, False])
        self.assertIsNotNone(episode)
        self.assertEqual(episode["anchor"], FRIDAY_21)

    def test_that_end_to_end_prints_the_doubt_sentence_and_no_bound(self):
        case = ExplainCase("run_both")
        case.setUp()
        self.addCleanup(case._tmp.cleanup)
        after_reset = FRIDAY_21 + 3600
        case.put([record(label="B", window="seven_day", used=u, resets=r, observed=o, machine="desktop") for u, r, o in
                  ((90, FRIDAY_21, at(1, 0)), (60, SATURDAY_01, at(2, 0)), (91, FRIDAY_21, at(3, 0)), (5, SATURDAY_01, after_reset))], receipt=after_reset - 60)
        _default, lines = case.run_both(now=after_reset + 600)
        self.assertTrue(any(line.startswith("B: a possible switch") for line in lines), lines)
        self.assertFalse(any(line.startswith("For B") for line in lines), lines)

    def test_a_reset_that_moves_without_any_flag_still_opens_a_doubt(self):
        series = self.weekly((90, FRIDAY_21, at(1, 0)), (91, FRIDAY_21 + 1800, at(2, 0)))              # the earlier reset had not passed, but 30 minutes apart is a flag anyway
        eligible, _episode = measure.doubt_walk("B", series)
        self.assertEqual(eligible, [True, False])
        late = self.weekly((90, FRIDAY_21, at(1, 0)), (3, SATURDAY_01, FRIDAY_21 + 600))                # after the earlier reset: the detector says nothing, the series does
        self.assertEqual(measure.flag_pairs("B", late), [])
        self.assertEqual(measure.doubt_walk("B", late)[0], [True, False])

    def test_readings_with_the_same_time_give_the_same_result_in_any_order(self):
        fit = (91, FRIDAY_21, at(3, 0))
        misfit = (60, SATURDAY_01, at(3, 0))
        for series in (self.weekly((90, FRIDAY_21, at(1, 0)), fit, misfit), self.weekly((90, FRIDAY_21, at(1, 0)), misfit, fit)):
            eligible, episode = measure.doubt_walk("B", series)
            self.assertEqual(eligible[0], True)
            self.assertEqual(eligible[1:], [False, False], "a tied misfit puts the whole instant in doubt")
            self.assertIsNotNone(episode)

    def test_a_tied_misfit_does_not_end_an_open_doubt_in_either_order(self):
        for order in (0, 1):
            tied = [(91, FRIDAY_21, at(4, 0)), (60, SATURDAY_01, at(4, 0))]
            tied = tied if order == 0 else tied[::-1]
            series = self.weekly((90, FRIDAY_21, at(1, 0)), (60, SATURDAY_01, at(2, 0)), *tied, (92, FRIDAY_21, at(5, 0)))
            eligible, episode = measure.doubt_walk("B", series)
            self.assertEqual(eligible, [True, False, False, False, True], order)
            self.assertIsNone(episode)

    def test_a_reading_list_in_another_order_gives_the_same_verdicts_per_reading(self):
        series = self.weekly((90, FRIDAY_21, at(1, 0)), (60, SATURDAY_01, at(2, 0)), (91, FRIDAY_21, at(3, 0)))
        forward, _ = measure.doubt_walk("B", series)
        backward, _ = measure.doubt_walk("B", series[::-1])
        self.assertEqual(forward, backward[::-1])

    def test_a_five_hour_reading_at_a_tied_instant_is_in_doubt_if_any_weekly_reading_there_is(self):
        series = self.weekly((90, FRIDAY_21, at(1, 0)), (91, FRIDAY_21, at(3, 0)), (60, SATURDAY_01, at(3, 0)))
        eligible, _ = measure.doubt_walk("B", series)
        five = record(label="B", window="five_hour", observed=at(3, 0))
        self.assertFalse(measure.reading_eligible("five_hour", five, series, eligible))


class NoIdentityHint(ExplainCase):
    """The hint "it may be <M>'s reading" is cut (decision of the lead after the second read): the doubt sentence never names another label."""

    def t3_for(self, five_resets=FIVE_1530, extra=()):
        self.a_readings()
        self.put(self.b_series() + [record(label="B", window="seven_day", used=60.0, resets=SATURDAY_01, observed=at(14, 48, 58), machine="desktop"),
                                    record(label="B", window="five_hour", used=6.0, resets=five_resets, observed=at(14, 48, 58), machine="desktop"), *extra])
        _default, lines = self.run_both(now=NOW21)
        t3 = next(line for line in lines if line.startswith("B: a possible switch"))
        self.assertNotIn("may be", t3)
        self.assertEqual(set(re.findall(r"\b[ABC]\b", t3)), {"B"}, "no other label is named")
        return t3

    def test_exactly_equal_reset_times_in_both_windows_still_print_no_hint(self):
        self.t3_for()

    def test_a_five_minute_difference_prints_no_hint(self):
        self.t3_for(five_resets=FIVE_1530 + 300)

    def test_a_tie_at_the_newest_instant_prints_no_hint(self):
        self.t3_for(extra=[record(label="B", window="seven_day", used=61.0, resets=SATURDAY_01 + 3 * HOUR, observed=at(14, 48, 58), machine="desktop-two")])


class Speed(ExplainCase):
    """A week of readings every five minutes in both windows, for all three labels, must explain quickly (it once took about 50 seconds)."""

    WEEK_START = D
    STEP = 300

    def week(self, readings_per_window):
        rows = []
        for label in "ABC":
            for number in range(readings_per_window):
                observed = self.WEEK_START + number * self.STEP
                rows.append(record(label=label, window="seven_day", used=min(99.0, number / 25), resets=self.WEEK_START + 8 * DAY, observed=observed, machine="desktop"))
                rows.append(record(label=label, window="five_hour", used=min(99.0, (number % 60) / 2), resets=observed + 3 * HOUR, observed=observed, machine="desktop"))
        return rows

    def test_a_full_week_of_five_minute_readings_in_both_windows_explains_in_under_three_seconds(self):
        # Bound: after the fix a week (2,016 readings per window and label) takes a fraction of a second on this machine; the old code took about 50 seconds.
        # 3 seconds leaves ample room for a slow shared machine and still fails loudly on any return of the slowdown.
        count = 2016
        self.put(self.week(count), receipt=self.WEEK_START + count * self.STEP)
        self.build_store()
        args = (str(self.db), str(self.labels), str(self.history))
        now = self.WEEK_START + count * self.STEP
        default = measure.now_command(*args, now=now)                                                   # built before the timer: only the explanation is timed
        started = time.monotonic()
        text = measure.now_command(*args, now=now, explain=True)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3.0, "explaining a week of readings took %.1f s" % elapsed)
        self.assertIn("For A: If this reading and its label are right", text)
        self.check_wording(default, text)

    def test_the_index_gives_the_same_answers_as_the_plain_rule_for_every_reading(self):
        weekly = [record(label="B", window="seven_day", used=1.0, resets=FRIDAY_21 + (SATURDAY_01 - FRIDAY_21) * (n % 2), observed=at(n // 2, 0), machine="m%d" % n) for n in range(12)]
        eligible, _episode = measure.doubt_walk("B", weekly)
        index = measure.eligibility_index(weekly, eligible)
        for hour in range(-1, 8):
            for minute in (0, 30):
                five = record(label="B", window="five_hour", observed=at(0, 0) + hour * HOUR + minute * 60)
                instants = [w["observed_at"] for w in weekly if w["observed_at"] <= five["observed_at"]]
                expected = True if not instants else all(ok for w, ok in zip(weekly, eligible, strict=True) if w["observed_at"] == max(instants))
                self.assertEqual(measure.index_eligible("five_hour", five, index), expected, (hour, minute))
                self.assertEqual(measure.reading_eligible("five_hour", five, weekly, eligible), expected)


class CannotTell(ExplainCase):
    def test_c_and_every_row_without_a_usable_figure_get_the_cannot_tell_sentence(self):
        self.put([])
        _default, lines = self.run_both(now=NOW21)
        self.assertIn("C 5-hour: no reading has been recorded here. A turn by a seat logged into C may supply one; check that a usable reading arrives under C before relying on it.", lines)
        self.assertIn("C weekly: no reading has been recorded here. A turn by a seat logged into C may supply one; check that a usable reading arrives under C before relying on it.", lines)

    def test_a_label_seen_before_says_no_newer_reading(self):
        self.store(ALIAS_ONE, at(2, 0), five=500, week=900, reset5=at(7, 0), reset7=SATURDAY_01)
        self.put([])
        _default, lines = self.run_both(now=NOW21)
        self.assertTrue(any(line.startswith("A 5-hour: no newer reading has been recorded here.") for line in lines), lines)

    def test_it_never_suggests_using_a_label_or_starting_work(self):
        self.put([])
        _default, lines = self.run_both(now=NOW21)
        for line in lines:
            self.assertNotRegex(line, r"(?i)\b(use|start|switch to|log in|run)\b(?! outside)")
            self.assertNotIn("choose", line.lower())

    def test_six_rows_with_a_figure_each_get_a_bound_and_no_cannot_tell_sentence(self):
        for alias in (ALIAS_ONE, ALIAS_TWO):
            self.store(alias, at(15, 10), five=100, week=200, reset5=at(20, 0), reset7=SATURDAY_01)
        self.put([record(label="C", window="five_hour", used=1.0, resets=at(20, 0), observed=at(15, 10), machine="desktop"),
                  record(label="C", window="seven_day", used=2.0, resets=SATURDAY_01, observed=at(15, 10), machine="desktop")])
        _default, lines = self.run_both(now=NOW21)
        self.assertEqual(len([line for line in lines if line.startswith("For ")]), 6)
        self.assertNotIn("Nothing to add to the table.", lines)

    def test_nothing_to_add_text_and_the_empty_case(self):
        self.assertEqual(measure.NOTHING_TO_ADD, "Nothing to add to the table.")
        self.assertEqual(len(measure.explain_block({}, NOW21)), 6)                                      # no readings at all: six rows that cannot be told, never an empty block


class Promise(ExplainCase):
    def test_nothing_is_written_to_the_history_or_the_store_folder(self):
        self.rich()
        self.build_store()
        before = self.snapshot()
        default, explained = self.explain_via("direct", ("A",), NOW21)
        self.check_wording(default, explained)
        self.assertEqual(self.snapshot(), before)

    def test_the_store_is_opened_read_only(self):
        self.rich()
        self.build_store()
        real = measure.sqlite3.connect
        uris = []

        def spy(target, *args, **kwargs):
            uris.append(target)
            return real(target, *args, **kwargs)

        with mock.patch.object(measure.sqlite3, "connect", spy):
            default, explained = self.explain_via("direct", (), NOW21)
        self.check_wording(default, explained)
        readers = [uri for uri in uris if str(uri).startswith("file:")]                                 # the fixture's own builder opens a plain path, not a URI
        self.assertEqual(len(readers), 2)                                                               # the default table and the explained one, one connection each
        self.assertTrue(all(uri.endswith("?mode=ro") for uri in readers))

    def test_the_flag_is_wired_through_the_command_line_and_prints_no_identifier(self):
        self.rich()
        default, shown = self.explain_via("cli", ("A",), NOW21)
        self.check_wording(default, shown)
        self.assertIn("a possible switch was flagged on", shown)
        for private in (ALIAS_ONE, ALIAS_TWO, str(self.tmp), self.tmp.name, "@", "home-", "\\"):
            self.assertNotIn(private, shown)


class WordingChecksReachEveryOutput(ExplainCase):
    """The shared check must really fail when a sentence goes wrong, on every path that prints an explanation."""

    PATHS = (("Promise", "test_nothing_is_written_to_the_history_or_the_store_folder"), ("Promise", "test_the_store_is_opened_read_only"),
             ("Promise", "test_the_flag_is_wired_through_the_command_line_and_prints_no_identifier"),
             ("LabelInDoubt", "test_case_two_the_misfiled_reading_gets_the_doubt_sentence_without_a_hint_and_no_bound"),
             ("UpperBound", "test_case_one_b_before_the_switch"), ("CannotTell", "test_a_label_seen_before_says_no_newer_reading"),
             ("Speed", "test_a_full_week_of_five_minute_readings_in_both_windows_explains_in_under_three_seconds"))

    SENTENCE_OF = {"Promise": "_sentence_t3", "LabelInDoubt": "_sentence_t3", "UpperBound": "_sentence_t1", "Speed": "_sentence_t1", "CannotTell": "_sentence_t4"}

    def broken(self, name, text):
        """Patch one sentence builder so that it adds `text` (before its final full stop for the doubt sentence)."""
        original = getattr(measure, name)

        def wrapper(*args):
            made = original(*args)
            return made[:-1] + text + "." if name == "_sentence_t3" else made + text

        return mock.patch.object(measure, name, wrapper)

    def test_a_forbidden_word_fails_every_path_that_prints_a_doubt_or_a_bound_sentence(self):
        for class_name, test_name in self.PATHS:
            if class_name == "CannotTell":
                continue                                          # its T4 sentence is checked by the shape test below
            case = globals()[class_name](test_name)
            case.setUp()
            self.addCleanup(case._tmp.cleanup)
            with self.broken(self.SENTENCE_OF[class_name], " available"), self.assertRaises(AssertionError) as caught:
                getattr(case, test_name)()
            self.assertIn("available", str(caught.exception), (class_name, test_name))

    def test_a_sentence_outside_the_five_shapes_fails_every_path_that_prints_one(self):
        for class_name, test_name in self.PATHS:
            case = globals()[class_name](test_name)
            case.setUp()
            self.addCleanup(case._tmp.cleanup)
            with self.broken(self.SENTENCE_OF[class_name], " Extra words"), self.assertRaises(AssertionError):
                getattr(case, test_name)()


if __name__ == "__main__":
    unittest.main()
