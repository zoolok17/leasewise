"""Card 7b, step 7b-1: the window mapping and the choice of one figure (DESIGN-7B2.md sections 3 and 4).

Pure functions over an injected millisecond clock; the files are the labelled SYNTHETIC agenttalk 0.99.0
fixtures with one thing changed. Nothing here reads a folder.
"""

import itertools
import unittest
from unittest import mock

from quota_balancer.reporter import capacityreader as cr
from quota_balancer.reporter.readings import COUNTERS, select, used_bps
from tests.card7b2_fixtures import (CLAUDE_ACCOUNT, CODEX_ACCOUNT, FIVE_RESET_S, MAPPING, NOW, T0, WEEK_RESET_S, edited,
                                    fixture, utc_ms)

CLAUDE_ALIAS = "alias-claude"


def cap(doc):
    outcome, capacity = cr._check(doc, doc["source_agent"], NOW + 60_000)
    assert outcome == cr.OK, doc
    return capacity


def seat(stem, name="claude_stream_both", **over):
    return cap(edited(name, source_agent=stem, **over))


def at(seconds_from_now):
    """An agenttalk time this many seconds from the fixtures' moment."""
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW // 1000 + seconds_from_now))


def nonzero(chosen):
    return {k: v for k, v in chosen.counters.items() if v}


class Currency(unittest.TestCase):
    """3.1 step 2: a figure is current when -25 s <= now - observed <= 600 s."""

    def one(self, age_seconds):
        doc = edited("claude_stream_both", primary_observed_at=at(-age_seconds), secondary_observed_at=at(-age_seconds))
        return select([cap(doc)], MAPPING, NOW)

    def test_the_edges(self):
        for age, current in ((0, True), (1, True), (599, True), (600, True), (-25, True),
                             (601, False), (700, False), (86_400, False), (-26, False), (-29, False)):
            chosen = self.one(age)
            self.assertEqual(len(chosen.picks) == 2, current, age)

    def test_older_than_600_seconds_is_not_current_and_ahead_of_25_seconds_is_future(self):
        old = self.one(601)
        self.assertEqual((nonzero(old), old.picks), ({"not_current": 2}, {}))
        ahead = self.one(-26)
        self.assertEqual((nonzero(ahead), ahead.picks), ({"future": 2}, {}))

    def test_the_clock_is_the_argument_and_the_files_times_are_the_sources_own(self):
        capacity = cap(fixture("claude_stream_both"))
        self.assertEqual(len(select([capacity], MAPPING, NOW + 600_000).picks), 2)
        self.assertEqual(select([capacity], MAPPING, NOW + 601_000).picks, {})
        self.assertEqual(len(select([capacity], MAPPING, NOW - 25_000).picks), 2)
        self.assertEqual(select([capacity], MAPPING, NOW - 26_000).picks, {})

    def test_a_clock_that_moved_back_or_forward_refuses_rather_than_invents(self):
        capacity = cap(fixture("claude_stream_both"))
        self.assertEqual(nonzero(select([capacity], MAPPING, NOW - 3_600_000)), {"future": 2})
        self.assertEqual(nonzero(select([capacity], MAPPING, NOW + 3_600_000)), {"not_current": 2})

    def test_a_time_more_than_30_seconds_ahead_is_a_time_problem_not_a_figure(self):
        doc = edited("claude_stream_both", primary_observed_at=at(31))
        outcome, capacity = cr._check(doc, "seat-a", NOW)
        chosen = select([capacity], MAPPING, NOW)
        self.assertEqual(nonzero(chosen), {"time_unusable": 1})
        self.assertEqual(list(chosen.picks), [(CLAUDE_ALIAS, "seven_day")])

    def test_nothing_reads_a_real_clock(self):
        capacity = cap(fixture("claude_stream_both"))
        with mock.patch("time.time", side_effect=AssertionError), mock.patch("time.monotonic", side_effect=AssertionError):
            self.assertEqual(len(select([capacity], MAPPING, NOW).picks), 2)


class Freshest(unittest.TestCase):
    """3.1 step 3: the newest current candidate wins; ties go to the larger percent, the smaller reset, the smaller stem."""

    def pick(self, files, window="five_hour"):
        return select(files, MAPPING, NOW).picks[(CLAUDE_ALIAS, window)]

    def test_the_newest_wins_whatever_the_order(self):
        older = seat("seat-a", primary_observed_at=at(-120), primary_used_percent=90.0)
        newer = seat("seat-b", primary_observed_at=at(-30), primary_used_percent=10.0)
        for files in itertools.permutations([older, newer]):
            winner = self.pick(list(files))
            self.assertEqual((winner.used_bps, winner.observed_ms), (1000, NOW - 30_000))

    def test_an_old_candidate_does_not_hide_behind_a_newer_one_that_is_not_current(self):
        stale = seat("seat-a", primary_observed_at=at(-700), primary_used_percent=99.0)
        fresh = seat("seat-b", primary_observed_at=at(-100), primary_used_percent=5.0)
        chosen = select([stale, fresh], MAPPING, NOW)
        self.assertEqual(chosen.picks[(CLAUDE_ALIAS, "five_hour")].used_bps, 500)
        self.assertEqual(chosen.counters["not_current"], 1)
        future = seat("seat-c", primary_observed_at=at(28), primary_used_percent=70.0)
        chosen = select([future, fresh], MAPPING, NOW)
        self.assertEqual(chosen.picks[(CLAUDE_ALIAS, "five_hour")].used_bps, 500)
        self.assertEqual(chosen.counters["future"], 1)

    def test_a_tie_on_the_second_goes_to_the_larger_percent(self):
        low = seat("seat-a", primary_used_percent=10.0)
        high = seat("seat-b", primary_used_percent=40.0)
        for files in itertools.permutations([low, high]):
            self.assertEqual(self.pick(list(files)).used_bps, 4000)

    def test_then_to_the_smaller_reset(self):
        late = seat("seat-a", primary_used_percent=10.0, primary_resets_at=FIVE_RESET_S + 3600)
        early = seat("seat-b", primary_used_percent=10.0, primary_resets_at=FIVE_RESET_S)
        for files in itertools.permutations([late, early]):
            self.assertEqual(self.pick(list(files)).resets_at_ms, FIVE_RESET_S * 1000)

    def test_identical_candidates_give_one_pick_in_any_order_and_the_stem_is_not_kept(self):
        first = seat("seat-a")
        second = seat("seat-b")
        for files in itertools.permutations([first, second]):
            chosen = select(list(files), MAPPING, NOW)
            self.assertEqual(sorted(chosen.picks), [(CLAUDE_ALIAS, "five_hour"), (CLAUDE_ALIAS, "seven_day")])
            self.assertNotIn("seat-", repr(chosen.picks) + repr(chosen.counters))

    def test_a_window_is_chosen_on_its_own_time_not_the_files(self):
        file_new = seat("seat-a", observed_at=at(-10), primary_observed_at=at(-500), primary_used_percent=20.0)
        file_old = seat("seat-b", observed_at=at(-300), primary_observed_at=at(-200), primary_used_percent=30.0)
        self.assertEqual(self.pick([file_new, file_old]).used_bps, 3000)

    def test_a_null_window_time_uses_the_files_time(self):
        file_new = seat("seat-a", observed_at=at(-10), primary_observed_at=None, primary_used_percent=20.0)
        file_old = seat("seat-b", observed_at=at(-300), primary_observed_at=None, primary_used_percent=30.0)
        self.assertEqual(self.pick([file_new, file_old]).used_bps, 2000)


class Aggregation(unittest.TestCase):
    def test_two_windows_may_come_from_two_files(self):
        five = seat("seat-a", name="claude_stream_both", primary_observed_at=at(-60), secondary_observed_at=at(-500),
                    secondary_used_percent=10.0)
        week = seat("seat-b", name="claude_stream_weekly_only", secondary_observed_at=at(-30), secondary_used_percent=22.0,
                    primary_used_percent=None, primary_resets_at=None)
        chosen = select([five, week], MAPPING, NOW)
        self.assertEqual(chosen.picks[(CLAUDE_ALIAS, "five_hour")].observed_ms, NOW - 60_000)
        self.assertEqual(chosen.picks[(CLAUDE_ALIAS, "seven_day")].observed_ms, NOW - 30_000)
        self.assertEqual(chosen.picks[(CLAUDE_ALIAS, "seven_day")].used_bps, 2200)
        self.assertEqual(len(chosen.picks), 2)

    def test_two_accounts_of_one_alias_share_one_selection(self):
        mapping = {CLAUDE_ACCOUNT: ("shared", "claude"), "claude:<os-user>:home-9999aaaa": ("shared", "claude")}
        one = seat("seat-a", primary_observed_at=at(-200), primary_used_percent=10.0)
        two = seat("seat-b", account="claude:<os-user>:home-9999aaaa", primary_observed_at=at(-20), primary_used_percent=33.0)
        chosen = select([one, two], mapping, NOW)
        self.assertEqual(chosen.picks[("shared", "five_hour")].used_bps, 3300)
        self.assertEqual(sorted(chosen.picks), [("shared", "five_hour"), ("shared", "seven_day")])

    def test_two_aliases_stay_apart(self):
        mapping = {CLAUDE_ACCOUNT: ("one", "claude"), "claude:<os-user>:home-9999aaaa": ("two", "claude")}
        chosen = select([seat("seat-a"), seat("seat-b", account="claude:<os-user>:home-9999aaaa", primary_used_percent=50.0)],
                        mapping, NOW)
        self.assertEqual(chosen.picks[("one", "five_hour")].used_bps, 200)
        self.assertEqual(chosen.picks[("two", "five_hour")].used_bps, 5000)

    def test_a_claude_and_a_codex_account_do_not_mix(self):
        chosen = select([seat("seat-a"), cap(fixture("codex_measured"))], MAPPING, NOW)
        self.assertEqual(chosen.picks[("alias-codex", "five_hour")].used_bps, 1200)
        self.assertEqual(chosen.picks[("alias-codex", "seven_day")].used_bps, 4100)
        self.assertEqual(chosen.picks[(CLAUDE_ALIAS, "five_hour")].used_bps, 200)

    def test_seats_with_no_current_figure_add_nothing(self):
        files = [cap(fixture("unknown_bound")), cap(fixture("claude_stream_stale")), cap(fixture("claude_stream_status_only")),
                 seat("seat-old", primary_observed_at=at(-900), secondary_observed_at=at(-900))]
        chosen = select(files, MAPPING, NOW)
        self.assertEqual(chosen.picks, {})
        self.assertEqual(nonzero(chosen), {"no_reading": 1, "no_figure": 2, "not_current": 2})

    def test_nothing_is_invented_when_nothing_is_current(self):
        self.assertEqual(select([], MAPPING, NOW).picks, {})
        self.assertEqual(nonzero(select([], MAPPING, NOW)), {})

    def test_every_counter_is_always_present_as_a_whole_number(self):
        chosen = select([seat("seat-a")], MAPPING, NOW)
        self.assertEqual(tuple(chosen.counters), COUNTERS)
        self.assertTrue(all(type(v) is int for v in chosen.counters.values()))

    def test_the_result_does_not_depend_on_the_order_of_the_files(self):
        files = [seat("seat-a"), seat("seat-b", primary_used_percent=7.0), seat("seat-c", secondary_used_percent=60.0),
                 cap(fixture("codex_measured")), cap(fixture("unknown_bound"))]
        shape = None
        for order in itertools.permutations(files):
            chosen = select(list(order), MAPPING, NOW)
            current = ({k: (p.used_bps, p.resets_at_ms, p.observed_ms) for k, p in chosen.picks.items()}, chosen.counters)
            shape = shape or current
            self.assertEqual(current, shape)


class Windows(unittest.TestCase):
    """4.1: which window is which, and which figure is usable."""

    def test_claude_windows_come_from_the_providers_names_and_assumed_is_accepted(self):
        chosen = select([cap(fixture("claude_stream_both"))], MAPPING, NOW)
        self.assertEqual(cap(fixture("claude_stream_both")).windows[0].basis, "assumed")
        self.assertEqual(chosen.picks[(CLAUDE_ALIAS, "five_hour")].used_bps, 200)
        self.assertEqual(chosen.picks[(CLAUDE_ALIAS, "seven_day")].used_bps, 1400)
        self.assertFalse(any(chosen.counters.values()))

    def test_claude_basis_does_not_matter(self):
        for basis in ("assumed", "measured", None):
            chosen = select([seat("seat-a", primary_window_basis=basis, secondary_window_basis=basis)], MAPPING, NOW)
            self.assertEqual(len(chosen.picks), 2, basis)

    def test_a_claude_window_with_the_wrong_length_is_unusable_and_the_other_still_counts(self):
        chosen = select([seat("seat-a", primary_window_minutes=60)], MAPPING, NOW)
        self.assertEqual(list(chosen.picks), [(CLAUDE_ALIAS, "seven_day")])
        self.assertEqual(nonzero(chosen), {"window_incomplete": 1})
        chosen = select([seat("seat-a", secondary_window_minutes=300, primary_window_minutes=None)], MAPPING, NOW)
        self.assertEqual(chosen.picks, {})
        self.assertEqual(nonzero(chosen), {"window_incomplete": 2})

    def test_a_claude_window_is_never_keyed_by_its_length(self):
        chosen = select([seat("seat-a", primary_window_minutes=10080, secondary_window_minutes=300)], MAPPING, NOW)
        self.assertEqual(chosen.picks, {})

    def test_a_codex_window_is_keyed_by_its_measured_length_alone(self):
        swapped = cap(edited("codex_measured", primary_window_minutes=10080, primary_used_percent=12.0,
                             secondary_window_minutes=300, secondary_used_percent=41.0))
        chosen = select([swapped], MAPPING, NOW)
        self.assertEqual(chosen.picks[("alias-codex", "five_hour")].used_bps, 4100)
        self.assertEqual(chosen.picks[("alias-codex", "seven_day")].used_bps, 1200)

    def test_a_codex_window_with_an_assumed_length_is_dropped(self):
        chosen = select([cap(fixture("codex_assumed"))], MAPPING, NOW)
        self.assertEqual((chosen.picks, nonzero(chosen)), ({}, {"assumed_dropped": 2}))

    def test_a_mixed_codex_file_keeps_only_the_measured_window(self):
        mixed = cap(edited("codex_mixed", primary_used_percent=12.0, primary_resets_at=FIVE_RESET_S, primary_observed_at=None))
        chosen = select([mixed], MAPPING, NOW)
        self.assertEqual(list(chosen.picks), [("alias-codex", "seven_day")])
        self.assertEqual(nonzero(chosen), {"assumed_dropped": 1})

    def test_the_real_mixed_fixture_has_one_measured_window_and_no_other_figure(self):
        chosen = select([cap(fixture("codex_mixed"))], MAPPING, NOW)
        self.assertEqual(list(chosen.picks), [("alias-codex", "seven_day")])
        self.assertFalse(any(chosen.counters.values()))

    def test_a_codex_length_that_is_neither_five_hours_nor_a_week_is_dropped(self):
        for minutes in (60, 301, 1440, 43_200, None):
            capacity = cap(edited("codex_measured", primary_window_minutes=minutes))
            chosen = select([capacity], MAPPING, NOW)
            self.assertEqual(list(chosen.picks), [("alias-codex", "seven_day")], minutes)
            self.assertEqual(nonzero(chosen), {"window_incomplete": 1}, minutes)

    def test_two_codex_windows_for_one_key_drop_both(self):
        capacity = cap(edited("codex_measured", secondary_window_minutes=300))
        chosen = select([capacity], MAPPING, NOW)
        self.assertEqual((chosen.picks, nonzero(chosen)), ({}, {"ambiguous_window": 1}))
        capacity = cap(edited("codex_measured", primary_window_minutes=10080))
        self.assertEqual(nonzero(select([capacity], MAPPING, NOW)), {"ambiguous_window": 1})

    def test_the_status_never_decides_usability(self):
        for status in ("allowed", "allowed_warning", "rejected", None):
            chosen = select([seat("seat-a", primary_status=status, secondary_status=status, last_status=status)], MAPPING, NOW)
            self.assertEqual(len(chosen.picks), 2, status)

    def test_a_rejected_window_is_reported_as_the_figure_read(self):
        chosen = select([cap(fixture("claude_stream_rejected"))], MAPPING, NOW)
        self.assertEqual(list(chosen.picks), [(CLAUDE_ALIAS, "seven_day")])
        self.assertEqual(chosen.picks[(CLAUDE_ALIAS, "seven_day")].used_bps, 10_000)
        self.assertFalse(hasattr(chosen.picks[(CLAUDE_ALIAS, "seven_day")], "status"))

    def test_a_window_needs_a_value_and_a_reset(self):
        chosen = select([seat("seat-a", primary_resets_at=None)], MAPPING, NOW)
        self.assertEqual((list(chosen.picks), nonzero(chosen)), ([(CLAUDE_ALIAS, "seven_day")], {"window_incomplete": 1}))
        chosen = select([seat("seat-a", secondary_used_percent=None)], MAPPING, NOW)
        self.assertEqual((list(chosen.picks), nonzero(chosen)), ([(CLAUDE_ALIAS, "five_hour")], {"window_incomplete": 1}))

    def test_a_window_with_nothing_in_it_is_not_a_problem(self):
        chosen = select([seat("seat-b", name="claude_stream_weekly_only", observed_at=T0, secondary_observed_at=T0)],
                        MAPPING, NOW)
        self.assertEqual(list(chosen.picks), [(CLAUDE_ALIAS, "seven_day")])
        self.assertFalse(any(chosen.counters.values()))

    def test_a_file_with_no_figure_at_all_is_no_figure(self):
        chosen = select([cap(fixture("claude_stream_status_only"))], MAPPING, NOW)
        self.assertEqual(nonzero(chosen), {"no_figure": 1})

    def test_the_reset_must_follow_the_observation_and_be_within_nine_days(self):
        seen = utc_ms("2026-10-08T12:00:00Z") // 1000
        for reset, good in ((seen + 1, True), (seen, False), (seen - 1, False), (seen + 9 * 86_400, True),
                            (seen + 9 * 86_400 + 1, False)):
            chosen = select([seat("seat-a", primary_resets_at=reset)], MAPPING, NOW)
            self.assertEqual((CLAUDE_ALIAS, "five_hour") in chosen.picks, good, reset)
            if not good:
                self.assertEqual(nonzero(chosen), {"window_incomplete": 1}, reset)

    def test_a_reset_is_judged_against_the_windows_own_time(self):
        own = at(-300)
        reset_before_own = utc_ms(own) // 1000
        chosen = select([seat("seat-a", primary_observed_at=own, primary_resets_at=reset_before_own)], MAPPING, NOW)
        self.assertNotIn((CLAUDE_ALIAS, "five_hour"), chosen.picks)

    def test_a_window_with_no_usable_time_is_time_unusable(self):
        chosen = select([seat("seat-a", primary_observed_at="yesterday")], MAPPING, NOW)
        self.assertEqual((list(chosen.picks), nonzero(chosen)), ([(CLAUDE_ALIAS, "seven_day")], {"time_unusable": 1}))
        chosen = select([seat("seat-a", observed_at="yesterday", primary_observed_at=None, secondary_observed_at=None)],
                        MAPPING, NOW)
        self.assertEqual((chosen.picks, nonzero(chosen)), ({}, {"time_unusable": 2}))
        chosen = select([seat("seat-a", observed_at="yesterday", primary_observed_at=at(-5), secondary_observed_at=None)], MAPPING, NOW)
        self.assertEqual(list(chosen.picks), [(CLAUDE_ALIAS, "five_hour")])

    def test_a_stale_file_whose_figures_were_emptied_gives_nothing(self):
        chosen = select([cap(fixture("claude_stream_stale"))], MAPPING, NOW + 1_200_000)
        self.assertEqual((chosen.picks, nonzero(chosen)), ({}, {"no_figure": 1}))


class Numbers(unittest.TestCase):
    """4.2: what a figure becomes."""

    def test_used_bps_rounds_half_up_and_stops_at_the_ends(self):
        table = ((0, 0), (0.0, 0), (0.004, 0), (0.005, 1), (0.01, 1), (2.0, 200), (14.0, 1400), (12.34, 1234),
                 (99.99, 9999), (100, 10_000), (100.0, 10_000), (103.0, 10_000), (1000, 10_000))
        for percent, bps in table:
            self.assertEqual(used_bps(percent), bps, percent)

    def test_used_bps_is_a_whole_number(self):
        for percent in (0.0, 14.0, 99.999, 1000.0):
            self.assertIs(type(used_bps(percent)), int)

    def test_a_pick_carries_only_numbers_in_the_payloads_units(self):
        chosen = select([cap(fixture("claude_stream_both"))], MAPPING, NOW)
        five = chosen.picks[(CLAUDE_ALIAS, "five_hour")]
        week = chosen.picks[(CLAUDE_ALIAS, "seven_day")]
        self.assertEqual((five.used_bps, five.resets_at_ms, five.observed_ms), (200, FIVE_RESET_S * 1000, NOW))
        self.assertEqual((week.used_bps, week.resets_at_ms, week.observed_ms), (1400, WEEK_RESET_S * 1000, NOW))
        for pick in (five, week):
            self.assertEqual(pick.__slots__, ("alias", "window", "used_percent", "used_bps", "resets_at_ms", "observed_ms"))
            self.assertTrue(0 <= pick.used_bps <= 10_000)

    def test_the_reading_time_is_the_windows_own_time_in_milliseconds(self):
        chosen = select([seat("seat-a", primary_observed_at=at(-90))], MAPPING, NOW)
        self.assertEqual(chosen.picks[(CLAUDE_ALIAS, "five_hour")].observed_ms, NOW - 90_000)
        self.assertEqual(chosen.picks[(CLAUDE_ALIAS, "seven_day")].observed_ms, NOW)

    def test_a_value_past_the_limit_is_held_at_ten_thousand(self):
        for percent in (100.5, 250.0, 1000.0):
            chosen = select([seat("seat-a", primary_used_percent=percent)], MAPPING, NOW)
            self.assertEqual(chosen.picks[(CLAUDE_ALIAS, "five_hour")].used_bps, 10_000)

    def test_the_other_fields_are_not_carried(self):
        chosen = select([cap(fixture("claude_stream_rejected"))], MAPPING, NOW)
        text = repr(chosen.picks) + repr(list(chosen.counters.values()))
        for word in ("rejected", "allowed", "assumed", "measured", "seat-", "claude_stream", CLAUDE_ACCOUNT):
            self.assertNotIn(word, text)


class Providers(unittest.TestCase):
    def test_a_codex_figure_under_a_claude_row_is_refused_and_the_reverse(self):
        wrong = {CODEX_ACCOUNT: ("x", "claude"), CLAUDE_ACCOUNT: ("y", "codex")}
        chosen = select([cap(fixture("codex_measured")), cap(fixture("claude_stream_both"))], wrong, NOW)
        self.assertEqual((chosen.picks, nonzero(chosen)), ({}, {"refused": 2}))

    def test_the_status_line_is_not_accepted_even_with_a_mapped_account(self):
        chosen = select([cap(fixture("claude_statusline"))], MAPPING, NOW)
        self.assertEqual((chosen.picks, nonzero(chosen)), ({}, {"source_not_accepted": 1}))


if __name__ == "__main__":
    unittest.main()
