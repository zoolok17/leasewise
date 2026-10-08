"""Card 7b, step 7b-2: one figure is one fact; the frozen body; latest only (DESIGN-7B2.md 6.1-6.5 and 8).

The capture cases run the real capture step (`readings.run`) over a fake bus folder of writer-made synthetic
files (agenttalk 0.99.0 shapes, placeholders for every account and user) and a real state file, on an injected
clock. Nothing is sent in this step: these tests prove what is stored, kept and counted.
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid

from quota_balancer.reporter import config as cfg
from quota_balancer.reporter import mapper, readings
from quota_balancer.reporter.state import READING_AGENT, State
from tests.card7b2_fixtures import (CLAUDE_ACCOUNT, CODEX_ACCOUNT, FIVE_RESET_S, NOW, WEEK_RESET_S, edited, fixture,
                                    put)

ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"
ACCOUNT_ID_2 = "22222222-2222-4222-8222-222222222222"
MACHINE = "desktop"
ALPHA = "claude-main"
EVENT_FIVE = "53520fa4-8118-5fe2-b976-3846725799a8"
EVENT_WEEK = "167228b1-1ac5-58af-b3b0-6f61a63dd6e6"
LOUD = "SECRET-USER-9f3a"


def at(seconds):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW // 1000 + seconds))


class Mapper(unittest.TestCase):
    """6.1 the key and the id; 6.2 and 8 the body."""

    def good(self, **over):
        body = mapper.reading_body("k", ACCOUNT_ID, "five_hour", 200, FIVE_RESET_S * 1000, NOW)
        body = copy.deepcopy(body)
        for path, value in over.items():
            target = body
            *head, last = path.split("__")
            for part in head:
                target = target[0] if part == "0" else target[part]
            if last == "0":
                target[0] = value
            else:
                target[last] = value
        return body

    def test_the_key_leaves_out_the_seat_the_file_the_account_text_and_the_figure(self):
        key = mapper.reading_key(MACHINE, ALPHA, "five_hour", NOW)
        self.assertEqual(key, "desktop|claude-main|cli_window|five_hour|%d" % (NOW // 1000))

    def test_the_id_is_stable_and_has_its_own_prefix(self):
        key = mapper.reading_key(MACHINE, ALPHA, "five_hour", NOW)
        self.assertEqual(mapper.reading_event_id(key), EVENT_FIVE)
        self.assertEqual(mapper.reading_event_id(mapper.reading_key(MACHINE, ALPHA, "seven_day", NOW)), EVENT_WEEK)
        self.assertEqual(mapper.reading_event_id(key), str(uuid.uuid5(mapper.NAMESPACE, "reading|" + key)))
        for other in (mapper.limit_event_id(key), mapper.limit_turn_id(key), mapper.final_event_id(key),
                      mapper.request_id(key)):
            self.assertNotEqual(mapper.reading_event_id(key), other)

    def test_ids_are_stable_across_processes(self):
        code = ("from quota_balancer.reporter import mapper;"
                "print(mapper.reading_event_id(mapper.reading_key('desktop','claude-main','five_hour',%d)))" % NOW)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), EVENT_FIVE)

    def test_every_part_of_the_key_changes_the_id(self):
        base = mapper.reading_event_id(mapper.reading_key(MACHINE, ALPHA, "five_hour", NOW))
        for args in (("laptop", ALPHA, "five_hour", NOW), (MACHINE, "other", "five_hour", NOW),
                     (MACHINE, ALPHA, "seven_day", NOW), (MACHINE, ALPHA, "five_hour", NOW + 1000)):
            self.assertNotEqual(mapper.reading_event_id(mapper.reading_key(*args)), base)

    def test_the_key_counts_whole_seconds(self):
        self.assertEqual(mapper.reading_key(MACHINE, ALPHA, "five_hour", NOW + 999), mapper.reading_key(MACHINE, ALPHA, "five_hour", NOW))

    def test_the_body_has_exactly_the_allow_listed_shape(self):
        body = mapper.reading_body(mapper.reading_key(MACHINE, ALPHA, "seven_day", NOW), ACCOUNT_ID, "seven_day", 1400,
                                   WEEK_RESET_S * 1000, NOW)
        self.assertEqual(body, {
            "event_id": EVENT_WEEK, "account_id": ACCOUNT_ID, "observed_at": NOW, "kind": "cli_window",
            "payload": {"windows": [{"key": "seven_day", "used_bps": 1400, "resets_at": WEEK_RESET_S * 1000}],
                        "confidence": "observed", "source_stale": False, "covered_seq": None}})
        self.assertEqual(set(body), mapper.READING_BODY_KEYS)

    def test_the_canonical_text_is_sorted_and_compact(self):
        body = mapper.reading_body(mapper.reading_key(MACHINE, ALPHA, "seven_day", NOW), ACCOUNT_ID, "seven_day", 1400,
                                   WEEK_RESET_S * 1000, NOW)
        text = mapper.reading_body_text(body)
        self.assertEqual(text, json.dumps(body, sort_keys=True, separators=(",", ":")))
        self.assertEqual(json.loads(text), body)
        self.assertNotIn(" ", text)

    def test_the_values_are_checked_explicitly(self):
        mapper.reading_body_text(self.good())
        bad = {
            "a boolean observed_at": {"observed_at": True}, "a float observed_at": {"observed_at": 1.5},
            "a zero observed_at": {"observed_at": 0}, "a huge observed_at": {"observed_at": 2 ** 63},
            "a list observed_at": {"observed_at": [1]}, "an object observed_at": {"observed_at": {"a": 1}},
            "an uppercase event_id": {"event_id": EVENT_FIVE.upper()}, "a short event_id": {"event_id": "abc"},
            "a numeric event_id": {"event_id": 5}, "a list event_id": {"event_id": [EVENT_FIVE]},
            "an uppercase account_id": {"account_id": "ABCDEFAB-ABCD-4ABC-8ABC-ABCDEFABCDEF"}, "a null account_id": {"account_id": None},
            "another kind": {"kind": "window"}, "a list kind": {"kind": ["cli_window"]},
            "another confidence": {"payload__confidence": "unknown"}, "a list confidence": {"payload__confidence": ["observed"]},
            "a stale source": {"payload__source_stale": True}, "a zero source_stale": {"payload__source_stale": 0},
            "a text source_stale": {"payload__source_stale": "false"}, "a covered_seq": {"payload__covered_seq": 0},
            "a bps over": {"payload__windows__0__used_bps": 10_001}, "a bps under": {"payload__windows__0__used_bps": -1},
            "a boolean bps": {"payload__windows__0__used_bps": True}, "a float bps": {"payload__windows__0__used_bps": 10.0},
            "a null bps": {"payload__windows__0__used_bps": None}, "a list bps": {"payload__windows__0__used_bps": [1]},
            "a zero reset": {"payload__windows__0__resets_at": 0}, "a boolean reset": {"payload__windows__0__resets_at": True},
            "a huge reset": {"payload__windows__0__resets_at": 2 ** 63}, "a null reset": {"payload__windows__0__resets_at": None},
            "another window": {"payload__windows__0__key": "monthly"}, "a list window": {"payload__windows__0__key": ["five_hour"]},
            "an object window": {"payload__windows__0__key": {"a": 1}},
        }
        for label, change in bad.items():
            with self.subTest(label):
                with self.assertRaises(ValueError) as caught:
                    mapper.reading_body_text(self.good(**change))
                self.assertNotIn(str(change), str(caught.exception))
                self.assertNotIn(ACCOUNT_ID, str(caught.exception))

    def test_the_shape_is_closed(self):
        for label, change in (("a missing key", lambda b: b.pop("kind")), ("an extra key", lambda b: b.update(seat_id="x")),
                              ("an extra payload key", lambda b: b["payload"].update(x=1)),
                              ("a missing payload key", lambda b: b["payload"].pop("covered_seq")),
                              ("no window", lambda b: b["payload"].update(windows=[])),
                              ("two windows", lambda b: b["payload"]["windows"].append(copy.deepcopy(b["payload"]["windows"][0]))),
                              ("an extra window key", lambda b: b["payload"]["windows"][0].update(status="rejected")),
                              ("a missing window key", lambda b: b["payload"]["windows"][0].pop("used_bps")),
                              ("windows not a list", lambda b: b["payload"].update(windows={"key": "five_hour"})),
                              ("a payload list", lambda b: b.update(payload=[])), ("a body list", None)):
            body = self.good()
            with self.subTest(label):
                if change is None:
                    body = []
                else:
                    change(body)
                with self.assertRaises(ValueError):
                    mapper.reading_body_text(body)

    def test_no_private_word_is_allowed_into_a_body(self):
        text = mapper.reading_body_text(self.good())
        for word in (CLAUDE_ACCOUNT, "claude", "seat-", "home-", "rejected", "allowed", "assumed", "measured", "<os-user>"):
            self.assertNotIn(word, text)


class Rig(unittest.TestCase):
    """A fake bus folder, a real state file and the real capture step on an injected clock."""

    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH") or None
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-7b2id-")
        self.addCleanup(self._tmp.cleanup)
        self.bus = os.path.join(self._tmp.name, "bus")
        self.folder = os.path.join(self.bus, "state")
        os.makedirs(self.folder)
        self.state_path = os.path.join(self._tmp.name, "state.db")
        self.state = State(self.state_path)
        self.addCleanup(lambda: self.state.close())
        self.alias = {CLAUDE_ACCOUNT: ALPHA, CODEX_ACCOUNT: "codex-main"}
        self.ids = {ALPHA: ACCOUNT_ID, "codex-main": ACCOUNT_ID_2, "claude-other": ACCOUNT_ID_2}

    def sources(self, mapping=None, bus_root=None):
        rows = []
        for text, alias in (mapping or self.alias).items():
            row = cfg.ResolvedReadingSource()
            row.bus_root = bus_root or self.bus
            row.folder = cfg.folder_key(row.bus_root)
            row.provider = text.split(":")[0]
            row.agenttalk_account, row.account, row.account_id = text, alias, self.ids[alias]
            rows.append(row)
        return tuple(rows)

    def scan(self, now_ms=NOW, mapping=None):
        return readings.run(self.state, self.sources(mapping), MACHINE, now_ms)

    def write(self, name, stem, **over):
        return put(self.folder, edited(name, source_agent=stem, **over))

    def rows(self, where=""):
        return self.state.all("SELECT event_id, account_alias, window_key, state, observed_ms, resets_ms, body FROM reading %s ORDER BY id" % where)

    def pending(self):
        return {(r[1], r[2]): r for r in self.rows("WHERE state='pending'")}

    def count(self, name):
        return self.state.counter(READING_AGENT, name)

    def scan_count(self, name):
        return self.state.counter(READING_AGENT, "scan:" + name)

    def reopen(self):
        self.state.close()
        self.state = State(self.state_path)


class Capture(Rig):
    def test_one_file_makes_one_row_per_window_with_the_frozen_body(self):
        self.write("claude_stream_both", "seat-a")
        self.assertEqual(self.scan(), 2)
        rows = {(r[1], r[2]): r for r in self.rows()}
        self.assertEqual(sorted(rows), [(ALPHA, "five_hour"), (ALPHA, "seven_day")])
        five = rows[(ALPHA, "five_hour")]
        self.assertEqual((five[0], five[3], five[4], five[5]), (EVENT_FIVE, "pending", NOW, FIVE_RESET_S * 1000))
        body = json.loads(five[6])
        self.assertEqual(body["account_id"], ACCOUNT_ID)
        self.assertEqual(body["payload"]["windows"], [{"key": "five_hour", "used_bps": 200, "resets_at": FIVE_RESET_S * 1000}])
        self.assertEqual(five[6], mapper.reading_body_text(body))
        week = json.loads(rows[(ALPHA, "seven_day")][6])
        self.assertEqual(week["payload"]["windows"][0]["used_bps"], 1400)
        self.assertEqual(self.count("captured"), 2)

    def test_same_figures_again_is_one_row_also_after_a_restart(self):
        self.write("claude_stream_both", "seat-a")
        self.assertEqual(self.scan(), 2)
        self.assertEqual(self.scan(NOW + 30_000), 0)
        self.reopen()
        self.assertEqual(self.scan(NOW + 60_000), 0)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.count("captured"), 2)
        self.assertEqual(self.scan_count("same"), 2)

    def test_two_seats_same_second_make_one_row_with_the_larger_figure_in_either_order(self):
        for first, second in (("seat-a", "seat-b"), ("seat-b", "seat-a")):
            with self.subTest(first=first):
                self.tearDown_state()
                self.write("claude_stream_both", first, primary_used_percent=10.0)
                self.write("claude_stream_both", second, primary_used_percent=40.0)
                self.scan()
                body = json.loads(self.pending()[(ALPHA, "five_hour")][6])
                self.assertEqual(body["payload"]["windows"][0]["used_bps"], 4000)
                self.assertEqual(len(self.rows("WHERE window_key='five_hour'")), 1)

    def tearDown_state(self):
        self.state.close()
        for name in os.listdir(self.folder):
            os.remove(os.path.join(self.folder, name))
        os.remove(self.state_path)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(self.state_path + suffix):
                os.remove(self.state_path + suffix)
        self.state = State(self.state_path)

    def test_two_seats_different_times_the_newer_wins_and_the_older_is_not_stored(self):
        self.write("claude_stream_both", "seat-a", primary_observed_at=at(-120), primary_used_percent=90.0)
        self.write("claude_stream_both", "seat-b", primary_observed_at=at(-30), primary_used_percent=10.0)
        self.scan()
        self.assertEqual(len([r for r in self.rows() if r[2] == "five_hour"]), 1)
        self.assertEqual(self.pending()[(ALPHA, "five_hour")][4], NOW - 30_000)

    def test_two_windows_from_two_files_make_two_rows_each_with_its_own_time(self):
        self.write("claude_stream_both", "seat-a", primary_observed_at=at(-60), secondary_observed_at=at(-500),
                   secondary_used_percent=10.0)
        self.write("claude_stream_both", "seat-b", primary_observed_at=at(-400), secondary_observed_at=at(-30),
                   secondary_used_percent=22.0, primary_used_percent=77.0)
        self.scan()
        pending = self.pending()
        self.assertEqual(pending[(ALPHA, "five_hour")][4], NOW - 60_000)
        self.assertEqual(pending[(ALPHA, "seven_day")][4], NOW - 30_000)
        self.assertEqual(len(self.rows()), 2)

    def test_a_later_scan_with_a_different_figure_for_a_stored_second_is_a_conflict_and_the_first_stays(self):
        self.write("claude_stream_both", "seat-a", primary_used_percent=10.0)
        self.scan()
        before = self.rows()
        self.write("claude_stream_both", "seat-a", primary_used_percent=30.0)
        self.assertEqual(self.scan(NOW + 30_000), 0)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.scan_count("same_second_conflict"), 1)

    def test_the_conflict_check_comes_before_the_older_check(self):
        self.write("claude_stream_both", "seat-a", primary_used_percent=10.0)
        self.scan()
        self.write("claude_stream_both", "seat-a", primary_used_percent=30.0)       # the same second: also "not newer"
        self.scan(NOW + 30_000)
        self.assertEqual((self.scan_count("same_second_conflict"), self.scan_count("older")), (1, 0))

    def test_an_older_observation_is_not_stored(self):
        self.write("claude_stream_both", "seat-a")
        self.scan()
        self.write("claude_stream_both", "seat-a", primary_observed_at=at(-100), primary_used_percent=77.0)
        self.assertEqual(self.scan(NOW + 30_000), 0)
        self.assertEqual(self.scan_count("older"), 1)
        self.assertEqual(len(self.rows()), 2)

    def test_the_memory_survives_a_restart(self):
        self.write("claude_stream_both", "seat-a")
        self.scan()
        self.reopen()
        self.write("claude_stream_both", "seat-a", primary_observed_at=at(-100), primary_used_percent=77.0)
        self.assertEqual(self.scan(NOW + 30_000), 0)
        self.assertEqual(self.scan_count("older"), 1)
        row = self.state.one("SELECT last_observed_ms, last_reset_ms, last_used_bps, gate_ms, last_sent_reset_ms FROM reading_window"
                             " WHERE account_alias=? AND window_key='five_hour'", (ALPHA,))
        self.assertEqual(row, (NOW, FIVE_RESET_S * 1000, 200, 0, None))

    def test_a_newer_figure_supersedes_a_pending_row_tried_or_not(self):
        for tried in (False, True):
            with self.subTest(tried=tried):
                self.tearDown_state()
                self.write("claude_stream_both", "seat-a", primary_observed_at=at(-100), primary_used_percent=10.0)
                self.scan()
                if tried:
                    self.state.conn.execute("UPDATE reading SET tries=3, not_before_ms=?, last_error_code='UNREACHABLE' "
                                            "WHERE window_key='five_hour'", (NOW + 600_000,))
                self.write("claude_stream_both", "seat-a", primary_observed_at=at(-10), primary_used_percent=20.0)
                self.scan(NOW + 30_000)
                states = [(r[3], r[4]) for r in self.rows("WHERE window_key='five_hour'")]
                self.assertEqual(states, [("superseded", NOW - 100_000), ("pending", NOW - 10_000)])
                new = self.pending()[(ALPHA, "five_hour")]
                self.assertEqual(self.state.one("SELECT tries, not_before_ms FROM reading WHERE event_id=?", (new[0],)), (0, 0))
                self.assertEqual(self.count("superseded"), 1)

    def test_the_superseded_peak_is_lost_on_purpose(self):
        self.write("claude_stream_both", "seat-a", primary_observed_at=at(-100), primary_used_percent=97.0)
        self.scan()
        self.write("claude_stream_both", "seat-a", primary_observed_at=at(-10), primary_used_percent=20.0)
        self.scan(NOW + 30_000)
        pending = json.loads(self.pending()[(ALPHA, "five_hour")][6])
        self.assertEqual(pending["payload"]["windows"][0]["used_bps"], 2000)
        gone = [json.loads(r[6])["payload"]["windows"][0]["used_bps"] for r in self.rows("WHERE state='superseded'")]
        self.assertEqual(gone, [9700])
        self.assertEqual(self.count("superseded"), 1)

    def test_only_the_latest_is_pending_whatever_the_order_of_offers(self):
        for offsets in ((-300, -200, -100), (-100, -200, -300), (-200, -100, -300)):
            with self.subTest(offsets=offsets):
                self.tearDown_state()
                for step, offset in enumerate(offsets):
                    self.write("claude_stream_both", "seat-a", primary_observed_at=at(offset), primary_used_percent=10.0 + step)
                    self.scan(NOW + 30_000 * step)
                    pending = [r for r in self.rows() if r[2] == "five_hour" and r[3] == "pending"]
                    self.assertEqual(len(pending), 1)
                self.assertEqual(self.pending()[(ALPHA, "five_hour")][4], NOW + max(offsets) * 1000)

    def test_a_pending_row_is_not_changed_by_a_changed_file_or_clock(self):
        self.write("claude_stream_both", "seat-a")
        self.scan()
        before = self.rows()
        os.remove(os.path.join(self.folder, "seat-a.capacity.json"))
        self.scan(NOW + 1_000_000)
        self.assertEqual(self.rows(), before)
        put(self.folder, fixture("unknown_bound"))
        self.scan(NOW + 2_000_000)
        self.assertEqual(self.rows(), before)

    def test_the_stored_bytes_are_the_bytes_that_a_later_send_would_use(self):
        self.write("claude_stream_both", "seat-a")
        self.scan()
        for row in self.rows():
            self.assertEqual(row[6], mapper.reading_body_text(json.loads(row[6])))
            body = json.loads(row[6])
            self.assertEqual(row[0], body["event_id"])

    def test_a_remapped_account_gets_new_ids_new_rows_and_a_fresh_memory(self):
        self.write("claude_stream_both", "seat-a")
        self.scan()
        old = self.rows()
        self.scan(NOW + 30_000, mapping={CLAUDE_ACCOUNT: "claude-other"})
        rows = self.rows()
        self.assertEqual(rows[:2], old)
        self.assertEqual(len(rows), 4)
        new = rows[2:]
        self.assertEqual({r[1] for r in new}, {"claude-other"})
        self.assertTrue({r[0] for r in new}.isdisjoint({r[0] for r in old}))
        self.assertEqual({json.loads(r[6])["account_id"] for r in new}, {ACCOUNT_ID_2})
        self.assertEqual(self.state.one("SELECT COUNT(*) FROM reading_window")[0], 4)

    def test_two_accounts_on_one_alias_share_one_selection(self):
        other = "claude:<os-user>:home-9999aaaa"
        mapping = {CLAUDE_ACCOUNT: ALPHA, other: ALPHA}
        self.write("claude_stream_both", "seat-a", primary_observed_at=at(-200), primary_used_percent=10.0)
        self.write("claude_stream_both", "seat-b", account=other, primary_observed_at=at(-20), primary_used_percent=33.0)
        self.scan(mapping=mapping)
        self.assertEqual(len(self.pending()), 2)
        five = json.loads(self.pending()[(ALPHA, "five_hour")][6])
        self.assertEqual(five["payload"]["windows"][0]["used_bps"], 3300)

    def test_a_codex_account_is_stored_under_its_own_alias(self):
        self.write("codex_measured", "seat-c")
        self.assertEqual(self.scan(), 2)
        self.assertEqual(sorted(self.pending()), [("codex-main", "five_hour"), ("codex-main", "seven_day")])

    def test_no_current_figure_writes_nothing(self):
        for number, name in enumerate(("unknown_unbound", "unknown_bound", "claude_stream_stale", "claude_stream_status_only",
                                       "claude_statusline", "codex_assumed")):
            put(self.folder, edited(name, source_agent="seat-%d" % number))
        self.assertEqual(self.scan(), 0)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.state.one("SELECT COUNT(*) FROM reading_window")[0], 0)
        for name in ("refused", "unmapped"):
            self.assertEqual(self.scan_count(name), 0)
        self.assertGreater(self.scan_count("no_reading") + self.scan_count("no_figure") + self.scan_count("source_not_accepted"), 0)

    def test_the_empty_and_unbound_files_are_never_refused(self):
        for number, name in enumerate(("unknown_unbound", "unknown_bound", "claude_stream_stale", "claude_stream_status_only")):
            put(self.folder, edited(name, source_agent="seat-%d" % number))
        self.scan()
        self.assertEqual(self.scan_count("refused"), 0)
        self.assertEqual(self.scan_count("file_refused"), 0)
        self.assertEqual(self.scan_count("no_reading"), 2)
        self.assertEqual(self.scan_count("no_figure"), 2)

    def test_an_unmapped_account_counts_and_writes_nothing(self):
        self.write("claude_stream_both", "seat-a", account="claude:<os-user>:home-deadbeef")
        self.assertEqual(self.scan(), 0)
        self.assertEqual((self.scan_count("unmapped"), self.rows()), (1, []))

    def test_a_stale_then_rewritten_file_with_the_same_observation_adds_no_row(self):
        self.write("claude_stream_both", "seat-a")
        self.scan()
        self.assertEqual(self.scan(NOW + 700_000), 0)
        self.assertEqual(self.scan_count("not_current"), 2)
        self.write("claude_stream_both", "seat-a")
        self.assertEqual(self.scan(NOW + 700_000), 0)
        self.assertEqual(len(self.rows()), 2)

    def test_a_refused_file_is_counted_and_the_other_files_still_count(self):
        self.write("claude_stream_both", "seat-a")
        put(self.folder, edited("claude_stream_both", source_agent="seat-b", scope="seat"))
        put(self.folder, fixture("claude_stream_both"), stem="seat-c", raw=b'{"half"')
        self.assertEqual(self.scan(), 2)
        self.assertEqual((self.scan_count("file_refused"), self.scan_count("file_read_later")), (1, 1))

    def test_two_folders_are_each_scanned_once(self):
        second = os.path.join(self._tmp.name, "bus2")
        os.makedirs(os.path.join(second, "state"))
        put(os.path.join(second, "state"), edited("claude_stream_weekly_only", source_agent="seat-z", secondary_observed_at=at(-5),
                                                   observed_at=at(-5), secondary_used_percent=44.0))
        self.write("claude_stream_both", "seat-a", secondary_used_percent=10.0, secondary_observed_at=at(-100))
        rows = list(self.sources())
        extra = self.sources(bus_root=second)
        self.assertEqual(readings.run(self.state, tuple(rows) + extra, MACHINE, NOW), 2)
        week = json.loads(self.pending()[(ALPHA, "seven_day")][6])
        self.assertEqual(week["payload"]["windows"][0]["used_bps"], 4400)

    def test_the_folder_flags_reach_the_counts_without_names_or_sizes(self):
        for number in range(70):
            self.write("claude_stream_both", "seat-%03d" % number)
        self.assertEqual(self.scan(), 2)
        self.assertEqual((self.scan_count("too_many_entries"), self.scan_count("folder_unreadable")), (1, 0))
        from unittest import mock
        with mock.patch("os.scandir", side_effect=PermissionError(LOUD)):
            self.scan(NOW + 30_000)
        self.assertEqual((self.scan_count("too_many_entries"), self.scan_count("folder_unreadable")), (0, 1))

    def test_a_vanished_folder_stores_nothing_and_is_not_a_fault(self):
        self.assertEqual(readings.run(self.state, self.sources(bus_root=os.path.join(self._tmp.name, "nowhere")), MACHINE, NOW), 0)
        self.assertEqual(self.count("capture_fault"), 0)

    def test_the_scan_leaves_no_private_text_in_the_state(self):
        marker_account = "claude:%s:home-0123abcd" % LOUD
        mapping = {marker_account: ALPHA}
        self.write("claude_stream_both", "seat-a", account=marker_account, plan_type=LOUD, reason=LOUD, last_status_window=LOUD)
        self.scan(mapping=mapping)
        self.state.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dump = "\n".join(str(row) for table in ("reading", "reading_window", "counter", "note")
                         for row in self.state.all("SELECT * FROM " + table))
        for word in (LOUD, "seat-a", "home-0123abcd", self.bus, "<os-user>"):
            self.assertNotIn(word, dump)
        with open(self.state_path, "rb") as handle:
            self.assertNotIn(LOUD.encode(), handle.read())


if __name__ == "__main__":
    unittest.main()
