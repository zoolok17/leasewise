"""Card 7b, step 7b-1: the strict reader for agenttalk's capacity files (DESIGN-7B2.md section 1).

The fixtures are labelled SYNTHETIC files made once by agenttalk 0.99.0's own writer (see
tests/fixtures/capacity/manifest.synthetic.json); the bad files are those fixtures with one thing broken.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from quota_balancer.reporter import capacityreader as cr
from quota_balancer.reporter import readings, strictfile
from tests.card7b2_fixtures import (CLAUDE_ACCOUNT, FIXTURES, MAPPING, NOW, edited, fixture, fixture_names, put)


class _Folder(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH") or None
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-7b2rd-")
        self.addCleanup(self._tmp.cleanup)
        self.bus = os.path.join(self._tmp.name, "bus")
        self.folder = os.path.join(self.bus, "state")
        os.makedirs(self.folder)

    def read(self, doc=None, raw=None, stem=None):
        path = put(self.folder, doc or {"source_agent": stem}, stem=stem, raw=raw)
        return cr.read_capacity(path, stem or doc["source_agent"], NOW)

    def read_as_seat_a(self, doc):
        """The file is named seat-a whatever its source_agent says (a hand copy shows up as a refusal)."""
        return self.read(doc, stem="seat-a")


class FixtureShape(unittest.TestCase):
    def test_every_fixture_has_exactly_the_keys_of_table_1_2(self):
        names = fixture_names()
        self.assertEqual(len(names), 11)
        expected = cr.LISTED_KEYS | cr.TOLERATED_KEYS
        self.assertEqual(len(cr.LISTED_KEYS), 27)
        self.assertEqual(len(expected), 29)
        for name in names:
            self.assertEqual(set(fixture(name)), expected, name)

    def test_the_manifest_names_the_agenttalk_version_and_the_call(self):
        with open(os.path.join(FIXTURES, "manifest.synthetic.json"), encoding="ascii") as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["agenttalk_version"], "0.99.0")
        self.assertIn("SYNTHETIC", manifest["label"])
        self.assertEqual(sorted(n[:-len(".synthetic.json")] for n in manifest["files"]), fixture_names())
        for entry in manifest["files"].values():
            self.assertTrue(entry["call"] and entry["note"])

    def test_no_fixture_holds_a_real_account(self):
        for name in fixture_names():
            account = fixture(name)["account"]
            self.assertTrue(account is None or "<os-user>" in account, name)


class Outcomes(_Folder):
    def test_every_fixture_is_ok(self):
        for name in fixture_names():
            doc = fixture(name)
            outcome, capacity = self.read(doc)
            self.assertEqual(outcome, cr.OK, name)
            self.assertIsInstance(capacity, cr.Capacity)

    def test_a_missing_file_is_read_later(self):
        path = os.path.join(self.folder, "seat-a.capacity.json")
        self.assertEqual(cr.read_capacity(path, "seat-a", NOW), (cr.READ_LATER, None))

    def test_half_a_file_is_read_later(self):
        text = json.dumps(fixture("claude_stream_both"), indent=2)
        for cut in (0, 1, len(text) // 2, len(text) - 2):
            self.assertEqual(self.read(raw=text[:cut].encode(), stem="seat-a"), (cr.READ_LATER, None), cut)

    def test_half_a_character_is_read_later(self):
        raw = json.dumps(dict(fixture("claude_stream_both"), reason="é")).encode("utf-8")
        self.assertEqual(self.read(raw=raw[:-4] + b"\xc3", stem="seat-a"), (cr.READ_LATER, None))

    def test_bytes_that_are_not_utf8_are_read_later(self):
        self.assertEqual(self.read(raw=b"\xff\xfe{}", stem="seat-a"), (cr.READ_LATER, None))

    def test_a_file_that_cannot_be_opened_is_read_later(self):
        with mock.patch("builtins.open", side_effect=PermissionError("sharing violation, secret-text")):
            result = cr.read_capacity(os.path.join(self.folder, "seat-a.capacity.json"), "seat-a", NOW)
        self.assertEqual(result, (cr.READ_LATER, None))

    def test_a_valid_file_is_ok_and_a_broken_one_is_refused(self):
        self.assertEqual(self.read(fixture("claude_stream_both"))[0], cr.OK)
        self.assertEqual(self.read(edited("claude_stream_both", scope="seat"))[0], cr.REFUSED)


class StrictRules(_Folder):
    """Every rule of 1.3, each with a file that breaks only that rule."""

    def refused(self, doc=None, raw=None, stem="seat-a"):
        outcome, capacity = self.read(doc, raw=raw, stem=stem)
        self.assertEqual((outcome, capacity), (cr.REFUSED, None))

    def test_a_file_larger_than_16384_bytes_is_refused(self):
        doc = fixture("claude_stream_both")
        doc["plan_type"] = "x" * 16_384
        self.refused(doc)
        doc["plan_type"] = "x" * 15_000
        self.assertEqual(self.read(doc)[0], cr.OK)

    def test_the_size_limit_is_exact(self):
        text = json.dumps(fixture("claude_stream_both"))
        for size, outcome in ((16_384, cr.OK), (16_385, cr.REFUSED)):
            raw = (text + " " * (size - len(text))).encode()
            self.assertEqual(len(raw), size)
            self.assertEqual(self.read(raw=raw, stem="seat-a")[0], outcome, size)

    def test_something_that_is_not_one_object_is_refused(self):
        for raw in (b"[]", b"3", b'"seat-a"', b"null"):
            self.refused(raw=raw)

    def test_a_duplicate_key_is_refused(self):
        text = json.dumps(fixture("claude_stream_both"))
        self.refused(raw=text.replace("{", '{"scope": "account", ', 1).encode())

    def test_nan_and_infinity_are_refused(self):
        for word in ("NaN", "Infinity", "-Infinity"):
            doc = json.dumps(fixture("claude_stream_both")).replace('"primary_used_percent": 2.0', '"primary_used_percent": ' + word)
            self.assertIn(word, doc)
            self.refused(raw=doc.encode())

    def test_a_number_that_overflows_to_infinity_is_refused(self):
        text = json.dumps(fixture("claude_stream_both"))
        for was in ('"primary_used_percent": 2.0', '"context_used_percent": null', '"plan_type": null',
                    '"secondary_resets_at": 1791763200'):
            self.assertIn(was, text)
            self.refused(raw=text.replace(was, was.split(":")[0] + ": 1e999").encode())
            self.refused(raw=text.replace(was, was.split(":")[0] + ": -1e999").encode())

    def test_a_list_or_an_object_anywhere_is_refused(self):
        for key, value in (("reason", []), ("account", {"a": 1}), ("plan_type", [1]), ("limit_id", {}),
                           ("unlisted", [1]), ("unlisted_too", {"a": {}}), ("context_tokens", [3])):
            self.refused(edited("claude_stream_both", **{key: value}))

    def test_a_deeply_nested_file_is_refused_not_a_crash(self):
        self.refused(raw=b'{"x": ' + b"[" * 9000 + b"]" * 9000 + b"}")

    def test_a_boolean_is_never_a_number(self):
        for key in ("primary_used_percent", "secondary_used_percent", "primary_resets_at", "secondary_resets_at",
                    "primary_window_minutes", "secondary_window_minutes", "context_used_percent",
                    "context_window_size", "context_tokens", "schema_version"):
            for value in (True, False):
                self.refused(edited("claude_stream_both", **{key: value}))

    def test_used_percent_bounds(self):
        for key in ("primary_used_percent", "secondary_used_percent"):
            for bad in (-0.1, 1000.1, "5", "NaN"):
                self.refused(edited("claude_stream_both", **{key: bad}))
            for good in (0, 0.0, 100.0, 103.0, 1000, 1000.0):
                self.assertEqual(self.read(edited("claude_stream_both", **{key: good}))[0], cr.OK, good)

    def test_resets_at_bounds(self):
        for key in ("primary_resets_at", "secondary_resets_at"):
            for bad in (0, -1, 4_102_444_801, 1.5, 1791475200.0, "1791475200"):
                self.refused(edited("claude_stream_both", **{key: bad}))
            for good in (1, 4_102_444_800):
                self.assertEqual(self.read(edited("claude_stream_both", **{key: good}))[0], cr.OK, good)

    def test_window_minutes_bounds(self):
        for key in ("primary_window_minutes", "secondary_window_minutes"):
            for bad in (0, -5, 100_001, 300.0, "300"):
                self.refused(edited("claude_stream_both", **{key: bad}))
            for good in (1, 100_000):
                self.assertEqual(self.read(edited("claude_stream_both", **{key: good}))[0], cr.OK, good)

    def test_closed_words(self):
        cases = {"source": "gemini", "confidence": "high", "primary_window_basis": "guess",
                 "secondary_window_basis": "Measured", "primary_status": "blocked", "secondary_status": "ALLOWED",
                 "last_status": "ok"}
        for key, bad in cases.items():
            self.refused(edited("claude_stream_both", **{key: bad}))
        for key in ("primary_status", "secondary_status", "last_status"):
            for good in ("allowed", "allowed_warning", "rejected", None):
                self.assertEqual(self.read(edited("claude_stream_both", **{key: good}))[0], cr.OK, (key, good))
        for good in ("measured", "assumed", None):
            self.assertEqual(self.read(edited("claude_stream_both", primary_window_basis=good))[0], cr.OK)

    def test_schema_version_must_be_the_integer_2(self):
        for bad in (1, 3, 0, 2.0, "2", None, [2]):
            self.refused(edited("claude_stream_both", schema_version=bad))

    def test_scope_must_be_account(self):
        for bad in ("seat", "Account", None, 1):
            self.refused(edited("claude_stream_both", scope=bad))

    def test_the_account_text_is_at_most_160_characters(self):
        self.refused(edited("claude_stream_both", account="a" * 161))
        self.refused(edited("claude_stream_both", account=""))
        self.refused(edited("claude_stream_both", account=7))
        self.assertEqual(self.read(edited("claude_stream_both", account="a" * 160))[0], cr.OK)

    def test_short_texts_are_at_most_64_characters(self):
        for key in ("reason", "last_status_window", "rate_limit_reached_type"):
            self.refused(edited("claude_stream_both", **{key: "r" * 65}))
            self.assertEqual(self.read(edited("claude_stream_both", **{key: "r" * 64}))[0], cr.OK)
            self.refused(edited("claude_stream_both", **{key: 5}))

    def test_source_agent_is_the_file_stem_and_a_legal_name(self):
        self.refused(edited("claude_stream_both", source_agent="seat-b"), stem="seat-a")   # a hand copy
        doc = edited("claude_stream_both", source_agent="bad name")
        self.refused(doc, stem="bad name")
        doc = edited("claude_stream_both", source_agent="a" * 129)
        self.refused(doc, stem="a" * 129)
        for bad in (None, 5, ""):
            self.refused(edited("claude_stream_both", source_agent=bad))
        doc = edited("claude_stream_both", source_agent="a" * 128)
        self.assertEqual(self.read(doc)[0], cr.OK)

    def test_times_must_be_text_and_a_malformed_status_time_is_refused(self):
        self.refused(edited("claude_stream_both", observed_at=5))
        self.refused(edited("claude_stream_both", observed_at=None))
        self.refused(edited("claude_stream_both", primary_observed_at=5))
        self.refused(edited("claude_stream_both", last_status_at="yesterday"))
        self.refused(edited("claude_stream_both", last_status_at=7))

    def test_a_window_time_that_does_not_parse_makes_the_window_unusable_not_the_file(self):
        for bad in ("yesterday", "2026-10-08 12:00:00", "2026-10-08T12:00:00.5Z", "2026-13-40T99:00:00Z",
                    "2026-10-08T12:00:00+00:00"):
            outcome, capacity = self.read(edited("claude_stream_both", primary_observed_at=bad))
            self.assertEqual(outcome, cr.OK, bad)
            self.assertIsNone(capacity.windows[0].observed_ms, bad)
            self.assertEqual(capacity.windows[1].observed_ms, NOW, bad)

    def test_a_time_more_than_30_seconds_ahead_is_unusable(self):
        _, capacity = self.read(edited("claude_stream_both", primary_observed_at="2026-10-08T12:00:31Z"))
        self.assertIsNone(capacity.windows[0].observed_ms)
        _, capacity = self.read(edited("claude_stream_both", primary_observed_at="2026-10-08T12:00:30Z"))
        self.assertEqual(capacity.windows[0].observed_ms, NOW + 30_000)

    def test_a_null_window_time_falls_back_to_observed_at(self):
        _, capacity = self.read(edited("claude_stream_both", primary_observed_at=None,
                                       observed_at="2026-10-08T11:59:00Z"))
        self.assertEqual(capacity.windows[0].observed_ms, NOW - 60_000)
        self.assertEqual(capacity.windows[1].observed_ms, NOW)

    def test_a_context_value_must_be_a_flat_finite_number_or_null(self):
        for key in ("context_used_percent", "context_window_size", "context_tokens"):
            for bad in ("12", [1], {"a": 1}):
                self.refused(edited("claude_stream_both", **{key: bad}))
            for good in (None, 0, 12.5, 200_000):
                self.assertEqual(self.read(edited("claude_stream_both", **{key: good}))[0], cr.OK)

    def test_a_missing_listed_key_refuses_the_file_whatever_it_holds(self):
        for key in sorted(cr.LISTED_KEYS):
            for name in ("claude_stream_both", "unknown_unbound"):
                doc = fixture(name)
                del doc[key]
                self.refused(doc)

    def test_null_is_normal_where_the_writer_can_write_it(self):
        nullable = cr.LISTED_KEYS - {"schema_version", "source_agent", "source", "observed_at", "confidence", "scope"}
        for key in sorted(nullable):
            self.assertEqual(self.read(edited("claude_stream_both", **{key: None}))[0], cr.OK, key)
        for key in ("schema_version", "source_agent", "source", "observed_at", "confidence", "scope"):
            self.refused(edited("claude_stream_both", **{key: None}))

    def test_the_two_unlisted_keys_may_be_absent_or_flat(self):
        for key in sorted(cr.TOLERATED_KEYS):
            doc = fixture("claude_stream_both")
            del doc[key]
            self.assertEqual(self.read(doc)[0], cr.OK, key)
            for value in ("pro", 3, 2.5, True, None):
                self.assertEqual(self.read(edited("claude_stream_both", **{key: value}))[0], cr.OK, (key, value))

    def test_another_flat_unlisted_key_is_tolerated_and_ignored(self):
        outcome, capacity = self.read(edited("claude_stream_both", future_field="x", other=7))
        self.assertEqual(outcome, cr.OK)
        self.assertNotIn("future_field", repr(vars_of(capacity)))

    def test_a_bug_in_the_check_is_a_refusal_not_a_crash(self):
        path = put(self.folder, fixture("claude_stream_both"))
        with mock.patch.object(cr, "_check", side_effect=RuntimeError("secret-text")):
            self.assertEqual(cr.read_capacity(path, "seat-a", NOW), (cr.REFUSED, None))


def vars_of(capacity):
    return {slot: getattr(capacity, slot) for slot in capacity.__slots__}


class WhatIsKept(_Folder):
    def test_the_objects_hide_their_text(self):
        _, capacity = self.read(fixture("claude_stream_both"))
        for text in (repr(capacity), str(capacity), repr(capacity.windows[0]), str(capacity.windows)):
            self.assertNotIn(CLAUDE_ACCOUNT, text)
            self.assertNotIn("claude", text)
        self.assertEqual(capacity.account, CLAUDE_ACCOUNT)           # it is there to be compared

    def test_a_capacity_holds_only_the_named_fields_and_the_windows_only_numbers(self):
        _, capacity = self.read(fixture("claude_stream_both"))
        self.assertFalse(hasattr(capacity, "__dict__"))
        self.assertEqual(capacity.__slots__, ("stem", "source", "confidence", "account", "windows"))
        for window in capacity.windows:
            self.assertFalse(hasattr(window, "__dict__"))
            for value in vars_of(window).values():
                self.assertTrue(value is None or type(value) in (int, float) or value in ("primary", "secondary", "measured", "assumed"))

    def test_nothing_from_a_broken_file_or_an_exception_survives(self):
        marker = "secret-text-9d41"
        _, capacity = self.read(edited("claude_stream_both", reason=marker, plan_type=marker, last_status_window=marker))
        self.assertNotIn(marker, repr(vars_of(capacity)))
        self.assertNotIn(marker, repr([vars_of(w) for w in capacity.windows]))
        self.assertEqual(self.read(edited("claude_stream_both", scope=marker)), (cr.REFUSED, None))
        put(self.folder, edited("claude_stream_both", scope=marker, source_agent="seat-z"))
        put(self.folder, fixture("claude_stream_both"), raw=b"{" + marker.encode())
        found = cr.scan(self.bus, NOW)
        self.assertEqual((found.refused, found.read_later), (1, 1))
        self.assertNotIn(marker, repr([vars_of(c) for c in found.files]) + repr([getattr(found, s) for s in found.__slots__]))
        for slot in found.__slots__:
            self.assertIn(type(getattr(found, slot)), (list, int, bool))

    def test_the_account_text_never_reaches_a_counter_or_a_pick(self):
        put(self.folder, fixture("claude_stream_both"))
        found = cr.scan(self.bus, NOW)
        chosen = readings.select(found.files, {CLAUDE_ACCOUNT: ("alias-claude", "claude")}, NOW)
        self.assertNotIn(CLAUDE_ACCOUNT, repr(chosen.counters) + repr(chosen.picks))
        for value in chosen.counters.values():
            self.assertIs(type(value), int)


class Scanning(_Folder):
    def test_a_folder_that_does_not_exist_is_nobody_wrote_anything(self):
        found = cr.scan(os.path.join(self._tmp.name, "nowhere"), NOW)
        self.assertEqual((found.files, found.read_later, found.refused, found.too_many_entries, found.folder_unreadable),
                         ([], 0, 0, False, False))

    def test_a_folder_that_cannot_be_listed_is_flagged(self):
        with mock.patch("os.scandir", side_effect=PermissionError("secret-text")):
            found = cr.scan(self.bus, NOW)
        self.assertTrue(found.folder_unreadable)
        self.assertEqual(found.files, [])

    def test_only_capacity_files_with_a_legal_stem_are_read(self):
        put(self.folder, fixture("claude_stream_both"))
        for name in ("seat-a.json", "seat-a.capacity.json.tmp", "other.park.json", ".capacity.json", "bad name.capacity.json",
                     "note.txt"):
            with open(os.path.join(self.folder, name), "w") as handle:
                handle.write("{}")
        os.makedirs(os.path.join(self.folder, "dir.capacity.json"))
        found = cr.scan(self.bus, NOW)
        self.assertEqual([c.stem for c in found.files], ["seat-a"])
        self.assertEqual((found.read_later, found.refused), (0, 0))

    def test_files_are_counted_by_outcome_and_judged_each_time(self):
        put(self.folder, fixture("claude_stream_both"))
        put(self.folder, fixture("claude_stream_weekly_only"))
        put(self.folder, edited("claude_stream_both", scope="x", source_agent="seat-r"))
        put(self.folder, fixture("claude_stream_both"), stem="seat-t", raw=b'{"a"')
        found = cr.scan(self.bus, NOW)
        self.assertEqual(sorted(c.stem for c in found.files), ["seat-a", "seat-b"])
        self.assertEqual((found.refused, found.read_later), (1, 1))
        put(self.folder, edited("claude_stream_both", source_agent="seat-t"), stem="seat-t")
        again = cr.scan(self.bus, NOW)
        self.assertEqual(sorted(c.stem for c in again.files), ["seat-a", "seat-b", "seat-t"])
        self.assertEqual((again.refused, again.read_later), (1, 0))

    def test_a_stem_that_differs_from_source_agent_is_refused(self):
        put(self.folder, fixture("claude_stream_both"), stem="seat-copy")
        found = cr.scan(self.bus, NOW)
        self.assertEqual((found.files, found.refused), ([], 1))

    def test_at_most_64_files_are_read_and_the_rest_set_a_flag(self):
        for number in range(70):
            put(self.folder, edited("claude_stream_both", source_agent="seat-%03d" % number))
        found = cr.scan(self.bus, NOW)
        self.assertEqual(len(found.files), 64)
        self.assertEqual([c.stem for c in found.files], ["seat-%03d" % n for n in range(64)])
        self.assertTrue(found.too_many_entries)

    def test_exactly_64_files_is_not_too_many(self):
        for number in range(64):
            put(self.folder, edited("claude_stream_both", source_agent="seat-%03d" % number))
        found = cr.scan(self.bus, NOW)
        self.assertEqual((len(found.files), found.too_many_entries), (64, False))

    def test_at_most_4096_entries_are_visited(self):
        for number in range(4100):
            with open(os.path.join(self.folder, "n%04d.txt" % number), "w"):
                pass
        put(self.folder, fixture("claude_stream_both"), stem="zzz-late", raw=None)
        found = cr.scan(self.bus, NOW)
        self.assertTrue(found.too_many_entries)
        self.assertLessEqual(len(found.files) + found.refused + found.read_later, 1)

    def test_a_link_is_skipped(self):
        real = put(self.folder, fixture("claude_stream_both"), stem="real-file")
        link = os.path.join(self.folder, "seat-link.capacity.json")
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            self.skipTest("this machine does not allow symbolic links")
        found = cr.scan(self.bus, NOW)
        self.assertEqual(found.refused + len(found.files), 1)         # only real-file.capacity.json; not the link
        self.assertTrue(all(c.stem != "seat-link" for c in found.files))

    def test_the_walk_is_the_shared_one(self):
        self.assertIs(cr.OK, strictfile.OK)
        self.assertIs(cr.REFUSED, strictfile.REFUSED)
        with mock.patch.object(strictfile, "walk", wraps=strictfile.walk) as walk:
            cr.scan(self.bus, NOW)
        args = walk.call_args.args
        self.assertEqual((args[1], args[3], args[4]), (".capacity.json", 4096, 64))


class ImpossibleTimes(_Folder):
    """A time that is well shaped but not a real moment is refused; an old or a future real moment is not (validity is
    not freshness). `last_status_at` is the one time the reader checks and drops; the others are parsed, and a time that
    does not parse only makes its window unusable."""

    IMPOSSIBLE = ("2026-99-99T99:99:99Z", "2026-02-30T00:00:00Z", "2026-13-01T00:00:00Z", "2026-10-08T24:00:00Z",
                  "2026-10-08T12:60:00Z", "2026-10-08T12:00:60Z", "2026-00-10T00:00:00Z", "2026-10-00T00:00:00Z",
                  "0000-01-01T00:00:00Z", "2025-02-29T00:00:00Z")

    def test_an_impossible_last_status_time_refuses_a_populated_file(self):
        for text in self.IMPOSSIBLE:
            with self.subTest(text=text):
                self.assertEqual(self.read_as_seat_a(edited("claude_stream_both", last_status_at=text)), (cr.REFUSED, None))

    def test_an_impossible_last_status_time_refuses_an_unknown_snapshot_too(self):
        for name in ("unknown_unbound", "unknown_bound"):
            for text in self.IMPOSSIBLE:
                with self.subTest(name=name, text=text):
                    self.assertEqual(self.read_as_seat_a(edited(name, last_status_at=text)), (cr.REFUSED, None))

    def test_an_old_or_a_future_valid_last_status_time_is_accepted(self):
        for text in ("2020-01-01T00:00:00Z", "1970-01-02T00:00:00Z", "2099-12-31T23:59:59Z", "2024-02-29T12:00:00Z"):
            for name in ("claude_stream_both", "unknown_bound"):
                with self.subTest(name=name, text=text):
                    self.assertEqual(self.read_as_seat_a(edited(name, last_status_at=text))[0], cr.OK)

    def test_the_other_times_never_refuse_the_file_they_make_the_window_unusable(self):
        for text in self.IMPOSSIBLE:
            with self.subTest(text=text):
                outcome, capacity = self.read_as_seat_a(edited("claude_stream_both", primary_observed_at=text))
                self.assertEqual(outcome, cr.OK)
                self.assertIsNone(capacity.windows[0].observed_ms)
                outcome, capacity = self.read_as_seat_a(edited("claude_stream_both", observed_at=text, primary_observed_at=None,
                                                               secondary_observed_at=None))
                self.assertEqual(outcome, cr.OK)
                self.assertEqual([w.observed_ms for w in capacity.windows], [None, None])


class DecisionOrder(_Folder):
    """DESIGN-7B2.md 1.5: the first rule that fits ends the decision, after the shape checks."""

    def decide(self, doc, mapping=MAPPING, now_ms=NOW):
        outcome, capacity = self.read(doc)
        self.assertEqual(outcome, cr.OK)
        return readings.select([capacity], mapping, now_ms)

    def only(self, selection, name):
        self.assertEqual({k: v for k, v in selection.counters.items() if v}, {name: 1})
        self.assertEqual(selection.picks, {})

    def test_an_unknown_snapshot_is_no_reading_whether_or_not_it_has_an_account(self):
        self.only(self.decide(fixture("unknown_unbound")), "no_reading")
        self.only(self.decide(fixture("unknown_bound")), "no_reading")
        self.only(self.decide(fixture("unknown_bound"), mapping={}), "no_reading")      # never unmapped

    def test_an_unknown_snapshot_with_a_figure_and_a_mapped_account_is_still_no_reading(self):
        for key, value in (("confidence", "unknown"), ("source", "unknown")):
            doc = edited("claude_stream_both", **{key: value})
            self.only(self.decide(doc), "no_reading")

    def test_the_status_line_source_is_not_accepted_before_anything_else(self):
        self.only(self.decide(fixture("claude_statusline")), "source_not_accepted")
        self.only(self.decide(edited("claude_statusline", account=None)), "source_not_accepted")
        self.only(self.decide(fixture("claude_statusline"), mapping={}), "source_not_accepted")
        self.only(self.decide(edited("claude_statusline", primary_used_percent=None, secondary_used_percent=None)),
                  "source_not_accepted")

    def test_unknown_comes_before_the_status_line_rule(self):
        self.only(self.decide(edited("claude_statusline", confidence="unknown")), "no_reading")

    def test_no_figure_comes_before_no_account(self):
        for name in ("claude_stream_status_only", "claude_stream_stale"):
            self.only(self.decide(edited(name, account=None)), "no_figure")
        self.only(self.decide(fixture("claude_stream_status_only"), mapping={}), "no_figure")      # not unmapped

    def test_a_figure_without_an_account_is_no_account(self):
        self.only(self.decide(edited("claude_stream_both", account=None)), "no_account")

    def test_no_account_comes_before_unmapped(self):
        self.only(self.decide(edited("claude_stream_both", account=None), mapping={}), "no_account")

    def test_an_account_in_no_row_is_unmapped(self):
        self.only(self.decide(fixture("claude_stream_both"), mapping={}), "unmapped")
        self.only(self.decide(fixture("claude_stream_both"), mapping={"other": ("a", "claude")}), "unmapped")

    def test_an_account_in_a_row_of_another_provider_is_refused(self):
        self.only(self.decide(fixture("claude_stream_both"), mapping={CLAUDE_ACCOUNT: ("a", "codex")}), "refused")

    def test_a_mapped_file_goes_on_to_selection(self):
        chosen = self.decide(fixture("claude_stream_both"))
        self.assertEqual(sorted(chosen.picks), [("alias-claude", "five_hour"), ("alias-claude", "seven_day")])
        self.assertFalse(any(chosen.counters.values()))

    def test_malformed_plus_unknown_is_refused_not_no_reading(self):
        breaks = (("a nested list", {"plan_type": [1]}), ("another version", {"schema_version": 1}),
                  ("a wrong scope", {"scope": "seat"}), ("a bad word", {"primary_status": "blocked"}),
                  ("a boolean percent", {"primary_used_percent": True}), ("a long account", {"account": "a" * 161}),
                  ("a hand copy", {"source_agent": "seat-zzz"}), ("a bad source word", {"source": "unknownn"}))
        for name in ("unknown_unbound", "unknown_bound"):
            for label, change in breaks:
                outcome, capacity = self.read_as_seat_a(edited(name, **change))
                self.assertEqual((outcome, capacity), (cr.REFUSED, None), (name, label))
        doc = fixture("unknown_bound")
        del doc["reason"]
        self.assertEqual(self.read(doc), (cr.REFUSED, None))
        self.assertEqual(self.read_as_seat_a(fixture("unknown_bound"))[0], cr.OK)       # the control: nothing broken

    def test_malformed_plus_status_line_or_unmapped_is_refused_too(self):
        self.assertEqual(self.read(edited("claude_statusline", scope="seat")), (cr.REFUSED, None))
        self.assertEqual(self.read(edited("claude_stream_both", account="x" * 200)), (cr.REFUSED, None))

    def test_the_empty_shapes_never_count_as_refused_or_read_later(self):
        for number, name in enumerate(("unknown_unbound", "unknown_bound", "claude_stream_status_only", "claude_stream_stale")):
            put(self.folder, edited(name, source_agent="seat-%d" % number))
        found = cr.scan(self.bus, NOW)
        self.assertEqual((len(found.files), found.refused, found.read_later), (4, 0, 0))

    def test_verdict_gives_only_the_first_two_steps(self):
        outcome, capacity = self.read(fixture("unknown_bound"))
        self.assertEqual(cr.verdict(capacity), "no_reading")
        _, capacity = self.read(fixture("claude_statusline"))
        self.assertEqual(cr.verdict(capacity), "source_not_accepted")
        _, capacity = self.read(fixture("claude_stream_both"))
        self.assertIsNone(cr.verdict(capacity))


if __name__ == "__main__":
    unittest.main()
