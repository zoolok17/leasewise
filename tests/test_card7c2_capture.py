"""Card 7c-2, step 3: from a park marker to one frozen limit event in the reporter's outbox.

One limit fact = (installation, account alias, evidence kind, window, reset). With an unknown reset the
marker's own park time joins the key. The seat, the message and the wrapper generation are not in the
key, so two seats on one account that hit the same limit send ONE event. The body is built field by
field from checked values, frozen at first capture, and never recomputed.

Markers here are SYNTHETIC and follow the documented contract (agenttalk a9f97b82); the reset times
are the two real ones from C:\\lw\\incoming\\7c-1 (each with the clock that fits it).
"""

import json
import os
import tempfile
import unittest
import uuid
from unittest import mock

from quota_balancer.reporter import capture, config as cfg, mapper, parkreader
from quota_balancer.reporter.state import State

from tests.test_card7c2_reader import (CLOCK_FIVE, CLOCK_SEVEN, FIVE_HOUR_RESET, SEVEN_DAY_RESET, marker, utc)

MACHINE = "desktop"
ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"
NOW = CLOCK_FIVE
LOUD = "LOUD-MARKER-agent-message-generation"


def source(bus_root, account="claude-main", account_id=ACCOUNT_ID, provider="claude", os_user="operator-desktop"):
    row = cfg.ResolvedLimitSource()
    row.bus_root, row.folder = bus_root, cfg.folder_key(bus_root)
    row.os_user, row.provider, row.account, row.account_id = os_user, provider, account, account_id
    return row


class _Harness(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH") or None
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-7c2cap-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.bus = os.path.join(self.dir, "bus")
        self.folder = os.path.join(self.bus, "state", "usage-limit-park")
        os.makedirs(self.folder)
        self.state = State(os.path.join(self.dir, "r.db"))
        self.addCleanup(self.state.close)
        self.sources = (source(self.bus),)

    def put(self, agent="agent-one", **over):
        doc = marker(agent=agent, now_ms=over.pop("now_ms", NOW), **over)
        with open(os.path.join(self.folder, agent + ".json"), "w", encoding="utf-8") as handle:
            json.dump(doc, handle)
        return doc

    def run_capture(self, now_ms=NOW, sources=None, machine=MACHINE):
        return capture.run(self.state, self.sources if sources is None else sources, machine, now_ms)

    def rows(self):
        return self.state.all("SELECT event_id, account_alias, body, state, captured_ms FROM limit_event ORDER BY id")

    def counts(self):
        return {r[0][len("scan:"):]: r[1] for r in self.state.all(
            "SELECT name, value FROM counter WHERE agent=? AND name LIKE 'scan:%'", (capture.LIMIT_AGENT,))}

    def bodies(self):
        return [json.loads(r[2]) for r in self.rows()]


class Body(_Harness):

    def test_the_body_is_built_field_by_field_from_the_checked_values(self):
        self.put()
        self.assertEqual(self.run_capture(), 1)
        (event_id, alias, text, state, captured), = self.rows()
        body = json.loads(text)
        key = "|".join((MACHINE, "claude-main", "claude_exhausted", "five_hour", str(FIVE_HOUR_RESET)))
        self.assertEqual(body, {
            "event_id": str(uuid.uuid5(mapper.NAMESPACE, "limit|" + key)),
            "turn_id": str(uuid.uuid5(mapper.NAMESPACE, "limit-turn|" + key)),
            "account_id": ACCOUNT_ID, "evidence_kind": "claude_exhausted",
            "observed_at": utc("2026-09-09T03:08:42Z") * 1000 + 940,        # parked_at, in ms, counted down
            "resets_at": FIVE_HOUR_RESET * 1000,                            # RAW: Leasewise adds the margin
            "exhausted_windows": ["five_hour"]})
        self.assertNotIn("model_id", body)
        self.assertEqual((event_id, alias, state, captured), (body["event_id"], "claude-main", "pending", NOW))
        self.assertEqual(text, mapper.limit_body_text(body))               # the stored bytes are the canonical bytes
        self.assertNotEqual(body["event_id"], body["turn_id"])

    def test_nothing_private_is_in_the_body_or_the_row(self):
        self.put(message_id=LOUD, wrapper_generation=LOUD)
        self.run_capture()
        text = json.dumps(self.rows())
        for private in (LOUD, "agent-one", "g1abc", "wake"):
            self.assertNotIn(private, text)
        self.assertEqual(set(self.bodies()[0]), set(mapper.LIMIT_BODY_KEYS))

    def test_the_weekly_sample_with_its_own_clock(self):
        self.put(now_ms=CLOCK_SEVEN, window="seven_day", reset_epoch=SEVEN_DAY_RESET,
                 parked_at="2026-09-21T23:45:43.399974Z")
        self.assertEqual(self.run_capture(now_ms=CLOCK_SEVEN), 1)
        body = self.bodies()[0]
        self.assertEqual((body["exhausted_windows"], body["resets_at"]), (["seven_day"], SEVEN_DAY_RESET * 1000))
        self.assertEqual(body["observed_at"], utc("2026-09-21T23:45:43Z") * 1000 + 399)

    def test_an_unknown_reset_is_null_and_the_park_time_joins_the_key(self):
        self.put(reset_epoch=None, wake_epoch=None)
        self.run_capture()
        body = self.bodies()[0]
        self.assertIsNone(body["resets_at"])
        key = "|".join((MACHINE, "claude-main", "claude_exhausted", "five_hour", "unknown",
                        str(body["observed_at"])))
        self.assertEqual(body["event_id"], str(uuid.uuid5(mapper.NAMESPACE, "limit|" + key)))

    def test_the_ids_are_stable_and_the_machine_is_part_of_the_key(self):
        self.put()
        self.run_capture()
        first = self.bodies()[0]["event_id"]
        self.assertEqual(mapper.limit_event_id(mapper.limit_key(MACHINE, "claude-main", "five_hour",
                                                                FIVE_HOUR_RESET, 1)), first)
        self.assertNotEqual(mapper.limit_event_id(mapper.limit_key("laptop", "claude-main", "five_hour",
                                                                   FIVE_HOUR_RESET, 1)), first)


class Identity(_Harness):

    def test_two_seats_on_one_account_with_the_same_limit_make_one_event(self):
        self.put("agent-a", message_id="m-a")
        self.put("agent-b", message_id="m-b", parked_at="2026-09-09T03:08:50Z")
        self.assertEqual(self.run_capture(), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_two_messages_on_one_seat_with_the_same_reset_make_one_event(self):
        self.put(message_id="m-1")
        self.run_capture()
        self.put(message_id="m-2", parked_at="2026-09-09T03:09:30Z")
        self.assertEqual(self.run_capture(), 0)
        self.assertEqual(len(self.rows()), 1)

    def test_a_re_park_with_a_later_reset_is_a_new_event(self):
        self.put()
        self.run_capture()
        self.put(reset_epoch=FIVE_HOUR_RESET + 3600, wake_epoch=FIVE_HOUR_RESET + 3630)
        self.assertEqual(self.run_capture(), 1)
        first, second = self.bodies()
        self.assertNotEqual(first["event_id"], second["event_id"])
        self.assertNotEqual(first["turn_id"], second["turn_id"])
        self.assertEqual(second["resets_at"], (FIVE_HOUR_RESET + 3600) * 1000)

    def test_a_fresh_to_stale_reread_adds_nothing_and_touches_nothing(self):
        self.put()
        self.run_capture()
        before = self.rows()
        later = NOW + 301_000                                              # the file was not refreshed: stale
        self.assertEqual(self.run_capture(now_ms=later), 0)
        self.assertEqual(self.counts()["stale"], 1)
        self.assertEqual(self.rows(), before)
        self.put(updated_at_epoch=later // 1000 - 2)                       # fresh again, the same fact
        self.assertEqual(self.run_capture(now_ms=later), 0)
        self.assertEqual(self.rows(), before)

    def test_a_stale_marker_alone_makes_no_event(self):
        self.put(updated_at_epoch=NOW // 1000 - 301)
        self.assertEqual(self.run_capture(), 0)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.counts()["stale"], 1)

    def test_the_stale_boundary_is_exactly_300_seconds(self):
        self.put(updated_at_epoch=NOW // 1000 - 300)
        self.assertEqual(self.run_capture(), 1)
        self.state.conn.execute("DELETE FROM counter")
        other = os.path.join(self.dir, "bus2", "state", "usage-limit-park")
        os.makedirs(other)
        with open(os.path.join(other, "agent-x.json"), "w", encoding="utf-8") as handle:
            json.dump(marker(agent="agent-x", updated_at_epoch=NOW // 1000 - 301), handle)
        self.assertEqual(capture.run(self.state, (source(os.path.join(self.dir, "bus2")),), MACHINE, NOW), 0)

    def test_a_remapped_account_is_a_different_key(self):
        self.put()
        self.run_capture()
        remapped = (source(self.bus, account="claude-other", account_id=OTHER_ID),)
        self.assertEqual(self.run_capture(sources=remapped), 1)
        old, new = self.bodies()
        self.assertNotEqual(old["event_id"], new["event_id"])
        self.assertEqual((old["account_id"], new["account_id"]), (ACCOUNT_ID, OTHER_ID))
        self.assertEqual([r[1] for r in self.rows()], ["claude-main", "claude-other"])

    def test_two_parks_with_unknown_resets_are_two_events(self):
        self.put(reset_epoch=None, wake_epoch=None, parked_at="2026-09-09T03:08:42Z")
        self.run_capture()
        self.put(reset_epoch=None, wake_epoch=None, parked_at="2026-09-09T03:09:10Z")
        self.assertEqual(self.run_capture(), 1)
        self.assertEqual(len(self.rows()), 2)

    def test_two_seats_parked_at_the_same_moment_with_unknown_resets_are_one_event(self):
        self.put("agent-a", reset_epoch=None, wake_epoch=None)
        self.put("agent-b", reset_epoch=None, wake_epoch=None)
        self.assertEqual(self.run_capture(), 1)

    def test_a_re_park_after_another_failure_with_an_unknown_reset_is_a_new_event(self):
        # agenttalk: a try that fails for another reason ends the park; a later limit refusal of the same
        # message starts a new park with a new parked_at (README: parked_at is when the CURRENT park began).
        self.put(reset_epoch=None, wake_epoch=None, parked_at="2026-09-09T03:08:42.5Z")
        self.run_capture()
        self.put(reset_epoch=None, wake_epoch=None, parked_at="2026-09-09T03:09:20.25Z")
        self.assertEqual(self.run_capture(), 1)
        first, second = self.bodies()
        self.assertEqual(second["observed_at"] - first["observed_at"], 37_750)

    def test_a_re_park_after_another_failure_with_a_known_reset_is_the_same_fact(self):
        self.put(parked_at="2026-09-09T03:08:42Z")
        self.run_capture()
        self.put(parked_at="2026-09-09T03:09:20Z")
        self.assertEqual(self.run_capture(), 0)

    def test_the_body_is_frozen_at_first_capture(self):
        self.put()
        self.run_capture()
        before = self.rows()
        self.put(wake_epoch=FIVE_HOUR_RESET + 99, updated_at_epoch=NOW // 1000 - 1, message_id="m-new",
                 parked_at="2026-09-09T03:09:59Z")
        self.run_capture()
        self.assertEqual(self.rows(), before)                              # the same bytes, the same capture time

    def test_two_folders_on_one_account_with_the_same_fact_make_one_event(self):
        other = os.path.join(self.dir, "bus2")
        os.makedirs(os.path.join(other, "state", "usage-limit-park"))
        with open(os.path.join(other, "state", "usage-limit-park", "agent-z.json"), "w", encoding="utf-8") as h:
            json.dump(marker(agent="agent-z", parked_at="2026-09-09T03:09:00Z"), h)
        self.put()
        self.assertEqual(self.run_capture(sources=(source(self.bus), source(other))), 1)


class Skips(_Harness):

    def test_a_reset_that_has_already_passed_gives_no_event(self):
        self.put(reset_epoch=NOW // 1000 - 1, wake_epoch=None)
        self.assertEqual(self.run_capture(), 0)
        self.assertEqual((self.rows(), self.counts()["reset_passed"]), ([], 1))

    def test_a_reset_at_this_very_second_has_passed_and_one_second_later_has_not(self):
        self.put(reset_epoch=NOW // 1000)
        self.assertEqual(self.run_capture(), 0)
        self.put(reset_epoch=NOW // 1000 + 1)
        self.assertEqual(self.run_capture(), 1)

    def test_a_window_that_is_null_gives_no_event(self):
        self.put(window=None)
        self.assertEqual(self.run_capture(), 0)
        self.assertEqual(self.counts()["window_unknown"], 1)

    def test_a_park_time_that_is_null_gives_no_event(self):
        self.put(parked_at=None)
        self.assertEqual(self.run_capture(), 0)
        self.assertEqual(self.counts()["parked_at_unknown"], 1)

    def test_a_provider_with_no_source_in_that_folder_is_unmapped(self):
        self.put()
        only_codex = (source(self.bus, provider="codex", account="codex-main"),)
        self.assertEqual(self.run_capture(sources=only_codex), 0)
        self.assertEqual((self.rows(), self.counts()["unmapped"]), ([], 1))
        both = (source(self.bus, provider="codex", account="codex-main"), source(self.bus))
        self.assertEqual(self.run_capture(sources=both), 1)                # the claude source takes it

    def test_the_reader_outcomes_are_counted_and_nothing_is_queued(self):
        self.put("agent-a")
        with open(os.path.join(self.folder, "agent-b.json"), "wb") as handle:
            handle.write(b'{"agent": "agent-b", ')                          # torn
        with open(os.path.join(self.folder, "agent-c.json"), "w", encoding="utf-8") as handle:
            json.dump(marker(agent="agent-c", schema_version=2), handle)    # refused
        self.assertEqual(self.run_capture(), 1)
        counts = self.counts()
        self.assertEqual((counts["read_later"], counts["refused"]), (1, 1))

    def test_a_sharing_violation_is_read_later_and_the_queue_is_untouched(self):
        self.put()
        self.run_capture()
        before = self.rows()
        real = open

        def deny(path, *args, **kwargs):
            if str(path).endswith("agent-one.json"):
                raise PermissionError(LOUD)
            return real(path, *args, **kwargs)
        with mock.patch("builtins.open", deny):
            self.assertEqual(self.run_capture(), 0)
        self.assertEqual(self.counts()["read_later"], 1)
        self.assertEqual(self.rows(), before)
        self.assertNotIn(LOUD, json.dumps(self.state.all("SELECT * FROM counter")))

    def test_every_outcome_is_judged_again_on_the_next_scan(self):
        with open(os.path.join(self.folder, "agent-one.json"), "wb") as handle:
            handle.write(b'{"schema_ver')                                   # torn right now
        self.assertEqual(self.run_capture(), 0)
        self.put()                                                          # complete on the next scan
        self.assertEqual(self.run_capture(), 1)
        self.assertEqual(self.counts()["read_later"], 0)                    # counts describe the LAST scan

    def test_a_folder_that_cannot_be_listed_is_counted(self):
        with mock.patch("os.scandir", side_effect=PermissionError(LOUD)):
            self.assertEqual(self.run_capture(), 0)
        self.assertEqual(self.counts()["folder_unreadable"], 1)

    def test_a_folder_with_more_than_the_cap_is_flagged_not_counted(self):
        for i in range(parkreader.MAX_ENTRIES + 3):
            self.put("agent-%04d" % i, parked_at="2026-09-09T03:08:%02dZ" % (i % 60), reset_epoch=None,
                     wake_epoch=None)
        self.run_capture()
        self.assertEqual(self.counts()["too_many_entries"], 1)             # a flag per folder, never the folder size
        self.assertNotIn("over_cap", self.counts())

    def test_nothing_is_added_for_no_sources(self):
        self.put()
        self.assertEqual(self.run_capture(sources=()), 0)
        self.assertEqual(self.rows(), [])


class Atomic(_Harness):

    def test_the_new_events_and_the_scan_counts_are_written_in_one_transaction(self):
        self.put()
        self.state.conn.execute("CREATE TEMP TRIGGER boom BEFORE INSERT ON limit_event BEGIN "
                                "SELECT RAISE(ABORT, 'x'); END")
        with self.assertRaises(Exception):
            self.run_capture()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.counts(), {})                                 # no half-written counts either
        self.state.conn.execute("DROP TRIGGER boom")
        self.assertEqual(self.run_capture(), 1)


if __name__ == "__main__":
    unittest.main()
