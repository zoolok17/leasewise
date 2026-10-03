"""Card 7c-2, step 3: the strict reader for agenttalk's park markers.

The fixtures are labelled SYNTHETIC markers that follow the documented contract (agenttalk README
"Reading the park marker from another program", at agenttalk a9f97b82), plus the two real reset
times from the Claude samples in C:\\lw\\incoming\\7c-1\\claude-limit-samples.json. Each sample gets
its own clock: the weekly sample's reset is 16.7 days after the five-hour sample's clock, so one clock
cannot fit both (and the 9-day bound is never relaxed). No real marker is read.
"""

import calendar
import json
import os
import tempfile
import time
import unittest

from quota_balancer.reporter import parkreader as pr

# The real samples' reset times (epoch seconds) and the time each one's first refusal was seen.
FIVE_HOUR_RESET = 1788948000                    # 2026-09-09 10:00 UTC (real sample 1)
SEVEN_DAY_RESET = 1790370000                    # 2026-09-25 21:00 UTC (real sample 2)


def utc(text):
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


CLOCK_FIVE = utc("2026-09-09T03:10:00Z") * 1000   # fits sample 1
CLOCK_SEVEN = utc("2026-09-21T23:50:00Z") * 1000  # fits sample 2
NOW = CLOCK_FIVE


def marker(agent="agent-one", now_ms=NOW, **over):
    """A valid SYNTHETIC version-1 marker, as agenttalk's writer lays it out."""
    now_s = now_ms // 1000
    doc = {"schema_version": 1, "agent": agent, "state": "usage_limit_parked", "provider": "claude",
           "window": "five_hour", "reset_epoch": FIVE_HOUR_RESET, "wake_epoch": FIVE_HOUR_RESET + 30,
           "message_id": "20260909-030842-940960-AbCd", "parked_at": "2026-09-09T03:08:42.940960Z",
           "wrapper_generation": "g1abc", "updated_at_epoch": now_s - 5}
    doc.update(over)
    return doc


class _Folder(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH") or None
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-7c2rd-")
        self.addCleanup(self._tmp.cleanup)
        self.bus = os.path.join(self._tmp.name, "bus")
        self.folder = os.path.join(self.bus, "state", "usage-limit-park")
        os.makedirs(self.folder)

    def put(self, doc, name=None, raw=None):
        name = name or doc["agent"]
        path = os.path.join(self.folder, name + ".json")
        with open(path, "wb") as handle:
            handle.write(raw if raw is not None else json.dumps(doc, indent=2).encode("utf-8"))
        return path

    def scan(self, now_ms=NOW):
        return pr.scan(self.bus, now_ms)

    def one(self, doc=None, raw=None, now_ms=NOW, name="agent-one"):
        self.put(doc or {}, name=name, raw=raw)
        return self.scan(now_ms)

    def outcome(self, result):
        return len(result.markers), result.read_later, result.refused


class Valid(_Folder):

    def test_a_valid_marker_gives_exactly_what_an_event_needs(self):
        result = self.one(marker())
        self.assertEqual(self.outcome(result), (1, 0, 0))
        m = result.markers[0]
        self.assertEqual((m.provider, m.window, m.reset_epoch), ("claude", "five_hour", FIVE_HOUR_RESET))
        self.assertEqual(m.updated_at_epoch, NOW // 1000 - 5)
        self.assertEqual(m.parked_at_ms, utc("2026-09-09T03:08:42Z") * 1000 + 940)   # fraction counted down
        self.assertEqual(sorted(m.__slots__), ["parked_at_ms", "provider", "reset_epoch", "updated_at_epoch",
                                               "window"])                            # nothing else is kept

    def test_the_weekly_sample_with_its_own_clock(self):
        doc = marker(now_ms=CLOCK_SEVEN, window="seven_day", reset_epoch=SEVEN_DAY_RESET,
                     wake_epoch=SEVEN_DAY_RESET + 30, parked_at="2026-09-21T23:45:43.399974Z")
        result = self.one(doc, now_ms=CLOCK_SEVEN)
        self.assertEqual(self.outcome(result), (1, 0, 0))
        self.assertEqual(result.markers[0].window, "seven_day")

    def test_one_clock_cannot_fit_both_samples_and_the_bound_is_not_relaxed(self):
        doc = marker(window="seven_day", reset_epoch=SEVEN_DAY_RESET)                # at the five-hour clock
        self.assertEqual(self.outcome(self.one(doc, now_ms=CLOCK_FIVE)), (0, 0, 1))   # 16.7 days ahead: refused
        self.assertEqual(self.outcome(self.one(doc, now_ms=CLOCK_SEVEN)), (1, 0, 0))

    def test_unknown_reset_window_and_parked_at_are_valid_markers(self):
        for over in ({"reset_epoch": None, "wake_epoch": None}, {"window": None}, {"parked_at": None},
                     {"wrapper_generation": None}):
            with self.subTest(over=over):
                self.assertEqual(self.outcome(self.one(marker(**over))), (1, 0, 0))

    def test_parked_at_without_a_fraction_or_with_one_to_six_digits(self):
        for text, extra in (("2026-09-09T03:08:42Z", 0), ("2026-09-09T03:08:42.5Z", 500),
                            ("2026-09-09T03:08:42.123456Z", 123), ("2026-09-09T03:08:42.999999Z", 999)):
            with self.subTest(text=text):
                m = self.one(marker(parked_at=text)).markers[0]
                self.assertEqual(m.parked_at_ms, utc("2026-09-09T03:08:42Z") * 1000 + extra)   # never rounded up

    def test_the_marker_time_helper_alone(self):
        self.assertIsNone(pr.parked_at_ms("2026-02-30T03:08:42Z", NOW))
        self.assertIsNone(pr.parked_at_ms("2026-09-09 03:08:42Z", NOW))
        self.assertIsNone(pr.parked_at_ms("2026-09-09T03:08:42+00:00", NOW))
        self.assertIsNone(pr.parked_at_ms("2026-09-09T03:08:42.1234567Z", NOW))
        self.assertIsNone(pr.parked_at_ms("1969-12-31T23:59:59Z", NOW))
        self.assertIsNone(pr.parked_at_ms("2026-09-09T04:08:42Z", NOW))               # an hour ahead: refused
        self.assertIsNotNone(pr.parked_at_ms("2026-09-09T03:10:29Z", NOW))            # up to 30 s ahead is fine


class ReadLater(_Folder):
    """A torn read, a missing or unopenable file, a missing documented key: read again on the next scan."""

    def test_a_partial_file_is_read_later_never_a_park(self):
        text = json.dumps(marker(), indent=2).encode("utf-8")
        for cut in (0, 1, 10, len(text) // 2, len(text) - 2):
            with self.subTest(cut=cut):
                self.assertEqual(self.outcome(self.one(raw=text[:cut])), (0, 1, 0))

    def test_half_a_multibyte_character_is_read_later(self):
        self.assertEqual(self.outcome(self.one(raw=b'{"agent": "\xe2\x82')), (0, 1, 0))

    def test_an_empty_or_whitespace_file_is_read_later(self):
        for raw in (b"", b"   \n"):
            with self.subTest(raw=raw):
                self.assertEqual(self.outcome(self.one(raw=raw)), (0, 1, 0))

    def test_a_missing_documented_key_is_read_later_not_refused(self):
        for key in sorted(pr.KEYS):
            with self.subTest(missing=key):
                doc = marker()
                del doc[key]
                self.assertEqual(self.outcome(self.one(doc)), (0, 1, 0))

    def test_a_sharing_violation_on_open_is_read_later(self):
        self.put(marker())
        real = open

        def deny(path, *args, **kwargs):
            if str(path).endswith("agent-one.json"):
                raise PermissionError("denied")
            return real(path, *args, **kwargs)
        from unittest import mock
        with mock.patch("builtins.open", deny):
            self.assertEqual(self.outcome(self.scan()), (0, 1, 0))

    def test_a_file_that_vanishes_between_listing_and_reading_is_read_later(self):
        path = self.put(marker())
        real = pr.read_marker

        def vanish(p, stem, now_ms):
            os.remove(path)
            return real(p, stem, now_ms)
        from unittest import mock
        with mock.patch.object(pr, "read_marker", vanish):
            self.assertEqual(self.outcome(self.scan()), (0, 1, 0))

    def test_a_folder_that_does_not_exist_means_nobody_is_parked(self):
        os.rmdir(self.folder)
        result = self.scan()
        self.assertEqual(self.outcome(result), (0, 0, 0))
        self.assertFalse(result.folder_unreadable)

    def test_a_folder_that_cannot_be_listed_is_flagged(self):
        from unittest import mock
        with mock.patch("os.scandir", side_effect=PermissionError("no")):
            result = self.scan()
        self.assertTrue(result.folder_unreadable)
        self.assertEqual(self.outcome(result), (0, 0, 0))


class Refused(_Folder):
    """It parsed, but breaks a rule: no event."""

    def refused(self, doc=None, raw=None, now_ms=NOW):
        self.assertEqual(self.outcome(self.one(doc, raw=raw, now_ms=now_ms)), (0, 0, 1))

    def test_other_versions_and_look_alikes(self):
        for version in (2, 0, -1, True, False, 1.0, "1", None, [1], {"v": 1}):
            with self.subTest(version=version):
                self.refused(marker(schema_version=version))

    def test_unknown_or_missing_provider(self):
        for provider in ("codex", "openai", "Claude", "", None, 1, ["claude"]):
            with self.subTest(provider=provider):
                self.refused(marker(provider=provider))

    def test_an_extra_key_is_refused_even_beside_a_missing_one(self):
        self.refused(marker(model="opus"))
        doc = marker(model="opus")
        del doc["window"]
        self.refused(doc)

    def test_wrong_types_and_ranges(self):
        cases = {"state": ["usage_limit_idle", None, 1], "agent": ["other-agent", None, 7],
                 "window": ["monthly", "Five_Hour", 5, True], "message_id": ["", None, 5, "x" * 65, "bad id", "a/b"],
                 "wrapper_generation": [5, "x" * 65, "bad id"], "parked_at": ["yesterday", 5, True, "2026-09-09"],
                 "reset_epoch": [True, 0, -1, 1.5, "5", 2 ** 63, 4_102_444_801, FIVE_HOUR_RESET + 10 ** 7],
                 "wake_epoch": [True, 0, -5, 2.0, "x", 4_102_444_801],
                 "updated_at_epoch": [True, 0, -1, 1.5, "5", NOW // 1000 + 61, 4_102_444_801, None]}
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.refused(marker(**{key: value}))

    def test_a_nested_value_is_refused(self):
        self.refused(marker(window={"a": 1}))
        self.refused(marker(message_id=["x"]))

    def test_not_one_json_object(self):
        for raw in (b"[]", b"null", b"5", b'"text"', b"true", b"[" * 5000 + b"]" * 5000):
            with self.subTest(raw=raw[:12]):
                self.assertEqual(self.outcome(self.one(raw=raw)), (0, 0, 1))

    def test_duplicate_keys_nan_and_infinity(self):
        good = json.dumps(marker()).encode("utf-8")
        self.refused(raw=good[:-1] + b', "window": "seven_day"}')
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant):
                self.refused(raw=good.replace(b'"reset_epoch": 1788948000', b'"reset_epoch": ' + constant)
                             .replace(b'"reset_epoch":1788948000', b'"reset_epoch":' + constant))

    def test_a_boolean_is_never_a_number(self):
        self.refused(marker(updated_at_epoch=True))
        self.refused(marker(reset_epoch=True))

    def test_an_oversized_file_is_refused_and_one_at_the_limit_is_read(self):
        self.refused(raw=b" " * (pr.MAX_BYTES + 1))
        doc = json.dumps(marker(), indent=None).encode("utf-8")
        padded = doc + b" " * (pr.MAX_BYTES - len(doc))
        self.assertEqual(len(padded), pr.MAX_BYTES)
        self.assertEqual(self.outcome(self.one(raw=padded)), (1, 0, 0))

    def test_a_marker_from_the_future_is_refused(self):
        self.refused(marker(updated_at_epoch=NOW // 1000 + 3600))
        self.refused(marker(parked_at="2026-09-09T05:00:00Z"))


class Listing(_Folder):

    def test_only_json_names_whose_stem_is_an_agent_name_are_read(self):
        for name in ("a.json.tmp", "b.txt", "c", ".json", "d e.json", "-start.json", "x" * 129 + ".json",
                     "agent-one.json.bak"):
            with open(os.path.join(self.folder, name), "wb") as handle:
                handle.write(json.dumps(marker(agent=name)).encode())
        self.assertEqual(self.outcome(self.scan()), (0, 0, 0))

    def test_the_agent_name_must_equal_the_file_stem(self):
        self.put(marker(agent="agent-two"), name="agent-one")
        self.assertEqual(self.outcome(self.scan()), (0, 0, 1))

    def test_a_subfolder_with_a_json_name_is_skipped(self):
        os.mkdir(os.path.join(self.folder, "dir-one.json"))
        self.assertEqual(self.outcome(self.scan()), (0, 0, 0))

    def test_a_symbolic_link_is_skipped(self):
        target = self.put(marker(agent="real-one"), name="real-one")
        link = os.path.join(self.folder, "agent-one.json")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symbolic links are not available here")
        result = self.scan()
        self.assertEqual(self.outcome(result), (1, 0, 0))                      # only the real file

    def test_more_than_the_cap_are_not_read_and_the_scan_says_so(self):
        for i in range(pr.MAX_ENTRIES + 5):
            self.put(marker(agent="agent-%04d" % i), name="agent-%04d" % i)
        result = self.scan()
        self.assertEqual(len(result.markers), pr.MAX_ENTRIES)
        self.assertTrue(result.too_many_entries)                  # a flag, never a count of the whole folder

    def test_exactly_the_cap_is_not_too_many(self):
        for i in range(pr.MAX_ENTRIES):
            self.put(marker(agent="agent-%04d" % i), name="agent-%04d" % i)
        result = self.scan()
        self.assertEqual(len(result.markers), pr.MAX_ENTRIES)
        self.assertFalse(result.too_many_entries)

    def test_several_files_give_independent_outcomes(self):
        self.put(marker(agent="agent-a"))
        self.put(marker(agent="agent-b", provider="codex"))
        self.put({}, name="agent-c", raw=b"{")
        self.assertEqual(self.outcome(self.scan()), (1, 1, 1))

    def test_a_scan_has_no_side_effect_on_the_folder(self):
        self.put(marker(agent="agent-a"))
        self.put({}, name="agent-c", raw=b"{")
        before = {n: os.stat(os.path.join(self.folder, n)).st_mtime_ns for n in os.listdir(self.folder)}
        self.scan()
        self.scan()
        self.assertEqual({n: os.stat(os.path.join(self.folder, n)).st_mtime_ns for n in os.listdir(self.folder)},
                         before)
        self.assertEqual(sorted(os.listdir(self.folder)), ["agent-a.json", "agent-c.json"])


class FakeEntries:
    """A fake `os.scandir` result that counts how far the reader advanced it."""

    def __init__(self, names, total=None):
        self.names = names
        self.advances = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        from types import SimpleNamespace
        for name in self.names:
            self.advances += 1
            yield SimpleNamespace(name=name, is_symlink=lambda: False,
                                  stat=lambda **kw: SimpleNamespace(st_file_attributes=0),
                                  is_file=lambda **kw: True)


class Bounded(unittest.TestCase):
    """The folder walk is bounded BEFORE anything is collected, so a huge folder cannot delay lease
    sending or use memory without bound. (One entry beyond the cap is looked at, to know there is more.)"""

    def scan(self, names):
        from unittest import mock
        fake = FakeEntries(names)
        with mock.patch.object(pr.os, "scandir", return_value=fake), \
                mock.patch.object(pr, "read_marker", return_value=(pr.READ_LATER, None)) as read:
            result = pr.scan("synthetic-unused-path", NOW)
        return result, fake, read

    def test_a_thousand_entries_are_not_all_visited_or_held(self):
        result, fake, read = self.scan(["agent-%04d.json" % i for i in range(1000)])
        self.assertLessEqual(fake.advances, pr.MAX_ENTRIES + 1)
        self.assertEqual(read.call_count, pr.MAX_ENTRIES)
        self.assertTrue(result.too_many_entries)

    def test_ignored_names_count_toward_the_bound_too(self):
        result, fake, read = self.scan(["notes-%04d.txt" % i for i in range(1000)])
        self.assertLessEqual(fake.advances, pr.MAX_ENTRIES + 1)
        self.assertEqual(read.call_count, 0)
        self.assertTrue(result.too_many_entries)

    def test_a_mixed_folder_keeps_only_what_it_visited(self):
        names = ["junk-%04d.txt" % i for i in range(200)] + ["agent-%04d.json" % i for i in range(300)]
        result, fake, read = self.scan(names)
        self.assertLessEqual(fake.advances, pr.MAX_ENTRIES + 1)
        self.assertEqual(read.call_count, pr.MAX_ENTRIES - 200)   # only the 56 json names inside the first 256
        self.assertTrue(result.too_many_entries)

    def test_a_normal_folder_is_unchanged(self):
        result, fake, read = self.scan(["agent-a.json", "agent-b.json", "other.txt"])
        self.assertEqual((fake.advances, read.call_count, result.too_many_entries), (3, 2, False))
        self.assertEqual(result.read_later, 2)

    def test_the_names_that_were_kept_are_still_read_in_sorted_order(self):
        from unittest import mock
        order = []
        fake = FakeEntries(["agent-c.json", "agent-a.json", "agent-b.json"])
        with mock.patch.object(pr.os, "scandir", return_value=fake), \
                mock.patch.object(pr, "read_marker", side_effect=lambda p, stem, n: (order.append(stem),
                                                                                    (pr.READ_LATER, None))[1]):
            pr.scan("synthetic-unused-path", NOW)
        self.assertEqual(order, ["agent-a", "agent-b", "agent-c"])


class WakeBound(_Folder):
    """`wake_epoch` is the wrapper's try-again time: the reset plus 30 seconds. It is checked in the same
    range as the reset, with that offset allowed explicitly. It is never sent."""

    def test_the_edges(self):
        now_s = NOW // 1000
        top = now_s + pr.MAX_RESET_AHEAD_SECONDS + pr.WAKE_AFTER_RESET_SECONDS
        self.assertEqual(pr.WAKE_AFTER_RESET_SECONDS, 30)
        for wake, expected in ((top, (1, 0, 0)), (top + 1, (0, 0, 1)), (1, (1, 0, 0)), (0, (0, 0, 1)),
                               (None, (1, 0, 0)), (pr.MAX_EPOCH, (0, 0, 1)), (4_102_444_800, (0, 0, 1)),
                               (now_s - 5, (1, 0, 0))):
            with self.subTest(wake=wake):
                self.assertEqual(self.outcome(self.one(marker(wake_epoch=wake))), expected)

    def test_a_reset_at_its_own_edge_still_has_room_for_its_wake(self):
        now_s = NOW // 1000
        edge = now_s + pr.MAX_RESET_AHEAD_SECONDS
        self.assertEqual(self.outcome(self.one(marker(reset_epoch=edge, wake_epoch=edge + 30))), (1, 0, 0))
        self.assertEqual(self.outcome(self.one(marker(reset_epoch=edge + 1, wake_epoch=edge + 31))), (0, 0, 1))

    def test_the_wake_is_still_never_kept(self):
        self.assertNotIn("wake", " ".join(pr.Marker.__slots__))


class Privacy(_Folder):

    def test_nothing_from_a_file_or_an_exception_comes_back(self):
        loud = "LOUD-MARKER-agent-and-message"
        self.put(marker(agent="agent-one", message_id=loud, wrapper_generation=loud))
        self.put(marker(agent="agent-two", message_id=loud, extra_key=loud), name="agent-two")
        from unittest import mock
        with mock.patch("builtins.open", side_effect=OSError(loud)):
            denied = self.scan()
        found = self.scan()
        for result in (denied, found):
            text = repr([result.markers, result.read_later, result.refused, result.too_many_entries])
            self.assertNotIn(loud, text)
            self.assertNotIn("agent-one", text)
        self.assertEqual(self.outcome(found), (1, 0, 1))
        self.assertNotIn(loud, repr(found.markers[0]))
        self.assertFalse(hasattr(found.markers[0], "__dict__"))


if __name__ == "__main__":
    unittest.main()
