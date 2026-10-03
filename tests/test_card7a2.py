"""Card 7a-2: the observe-only reporter core (docs/STEP-CARD-7A-2.md;
DESIGN-7A.md Part 4 as changed by Part 12).

Rows: schema_mirror, identity, mapper_totals, completeness_d3,
continuity_d4, finals_d5, crashes, ownership_outbox, errors_d7, requeue,
privacy_7a2. Every journal is a hand-made fixture (tests/journal_fixture.py);
nothing here touches the network, agenttalk, or a real Leasewise server.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

from quota_balancer import http_api, lease
from quota_balancer.reporter import decisions, events, mapper, rounds, state
from quota_balancer.reporter.decisions import Response
from quota_balancer.reporter.mapper import AgentConfig
from quota_balancer.reporter.rounds import run_round
from quota_balancer.reporter.state import State, StateOwned

from tests.journal_fixture import (MODEL, RAW_NULLS, SEAT, T0, USAGE, Journal,
                                   iso, line)

CFG = AgentConfig("alpha", SEAT, MODEL, "bounded_read", "opus", 1, 2, 1000,
                  500, 1_800_000)
NOW = T0 + 1_000_000
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def message(stream, mid, arrive, start, disposed, disposition="completed",
            turn=None, **kw):
    """One message handled in one dispatch and disposed."""
    stream.dispatch(mid, turn or "t-" + mid, start, message_at=arrive, **kw)
    stream.disposed(mid, disposition, disposed, message_at=arrive)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="quota-7a2-")
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "journal")
        os.makedirs(self.root)
        self.state_path = os.path.join(self.tmp.name, "state.db")
        self.state = State(self.state_path)
        self.addCleanup(lambda: self.state.close())
        self.journal = Journal(self.root)
        self.configs = {"alpha": CFG}

    def round(self, now=NOW, **kw):
        return run_round(self.state, self.root, self.configs, now, **kw)

    def reopen(self):
        self.state.close()
        self.state = State(self.state_path)

    def final(self, mid):
        row = self.state.one(
            "SELECT body, complete, reason, state, totals_json FROM final "
            "WHERE agent='alpha' AND message_id=?", (mid,))
        if row is None:
            return None
        return {"body": json.loads(row[0]), "complete": row[1],
                "reason": row[2], "state": row[3], "raw": row[0],
                "totals": json.loads(row[4])}

    def status(self, mid):
        return self.state.one(
            "SELECT complete, reason, reasons, disposition FROM "
            "message_status WHERE agent='alpha' AND message_id=?", (mid,))

    def count(self, table, where="1=1"):
        return self.state.one("SELECT COUNT(*) FROM %s WHERE %s"
                              % (table, where))[0]


def drain(st, transport, now=NOW, limit=100):
    """Send everything the outbox offers through a fake transport."""
    sent = []
    for _ in range(limit):
        item = st.next_item(now)
        if item is None:
            break
        response = transport(item)
        st.apply(item, response, now)
        sent.append((item.kind, item.key, response))
    return sent


def accept(item):
    return Response(201, None, "lease-" + item.message_id)


# --- the schema mirror ---------------------------------------------------

def _event_obj(**over):
    obj = {"v": 1, "kind": "message_disposed", "event_id": str(uuid.uuid4()),
           "stream": "alpha.g1", "at": iso(T0), "agent": "alpha",
           "dropped_total": 0, "seq": 1, "message_id": "m1",
           "disposition": "completed", "message_at": iso(T0 - 1000)}
    obj.update(RAW_NULLS)
    obj.update(over)
    return obj


class SchemaMirror(_Base):
    """schema_mirror: one module mirrors the closed schema (D1, D3, D4)."""

    def parse(self, **over):
        return events.parse_event(line(_event_obj(**over)).rstrip(b"\n"))

    def test_a_valid_event_parses_with_its_time(self):
        self.assertEqual(self.parse()["at_ms"], T0)

    def test_the_disposition_set_is_closed_and_renamed(self):
        for name in ("completed", "dead_lettered", "delivery_failed",
                     "outcome_unknown"):
            self.parse(disposition=name)
        for name in ("consumed_undriven", "cancelled", ""):
            with self.assertRaises(events.JournalError):
                self.parse(disposition=name)

    def test_an_unknown_schema_version_is_refused(self):
        with self.assertRaises(events.UnsupportedVersion):
            self.parse(v=2)

    def test_the_five_raw_gate_fields_are_true_false_or_null(self):
        for name in sorted(events.RAW_GATE_FIELDS):
            for value in (True, False, None):
                self.assertEqual(self.parse(**{name: value})[name], value)
            for bad in (1, 0, "true", [], {}):
                with self.assertRaises(events.JournalError, msg=name):
                    self.parse(**{name: bad})
            obj = _event_obj()
            del obj[name]
            with self.assertRaises(events.JournalError):
                events.parse_event(line(obj).rstrip(b"\n"))

    def test_raw_gate_fields_do_not_change_a_final(self):
        s = self.journal.add_stream("g1", T0)
        s.dispatch("m1", "t1", T0 + 2000, message_at=T0 + 1000)
        s.disposed("m1", "completed", T0 + 3000, message_at=T0 + 1000,
                   consumed=True, landed=False, compliance_success=True,
                   dead_lettered=True, terminal_failure=True)
        self.round()
        self.assertEqual(self.final("m1")["body"]["outcome"], "completed")

    def test_stream_records_carry_no_seq(self):
        header = {"v": 1, "kind": "stream_started",
                  "event_id": str(uuid.uuid4()), "stream": "alpha.g1",
                  "at": iso(T0), "agent": "alpha", "dropped_total": 0,
                  "agent_version": "x", "previous_stream": None,
                  "mode": "loop", "unmanaged": [], "segment": 1,
                  "after_seq": 0}
        events.parse_event(line(header).rstrip(b"\n"))
        header["seq"] = 1
        with self.assertRaises(events.JournalError):
            events.parse_event(line(header).rstrip(b"\n"))
        closed = {"v": 1, "kind": "stream_closed",
                  "event_id": str(uuid.uuid4()), "stream": "alpha.g1",
                  "at": iso(T0), "agent": "alpha", "dropped_total": 3,
                  "last_seq": 9}
        self.assertEqual(
            events.parse_event(line(closed).rstrip(b"\n"))["last_seq"], 9)

    def test_message_at_is_required_but_may_be_null(self):
        self.parse(message_at=None)
        obj = _event_obj()
        del obj["message_at"]
        with self.assertRaises(events.JournalError):
            events.parse_event(line(obj).rstrip(b"\n"))

    def test_extra_keys_and_bad_values_are_damaged(self):
        for over in ({"extra": 1}, {"seq": 0}, {"seq": True},
                     {"dropped_total": -1}, {"stream": "beta.g1"},
                     {"event_id": "nope"}, {"at": "yesterday"}):
            with self.assertRaises(events.JournalError, msg=str(over)):
                self.parse(**over)

    def test_usage_unknown_is_null_never_zero(self):
        base = {"v": 1, "kind": "dispatch_ended",
                "event_id": str(uuid.uuid4()), "stream": "alpha.g1",
                "at": iso(T0), "agent": "alpha", "dropped_total": 0,
                "seq": 1, "message_id": "m1", "turn_id": "t1",
                "launched": True, "outcome": "success", "exit": "normal",
                "duration_ms": 1, "usage": None}
        self.assertIsNone(
            events.parse_event(line(base).rstrip(b"\n"))["usage"])
        base["usage"] = {"input_tokens": None, "output_tokens": 3,
                         "cache_read_tokens": 0, "cache_write_tokens": None}
        events.parse_event(line(base).rstrip(b"\n"))
        base["usage"]["output_tokens"] = -1
        with self.assertRaises(events.JournalError):
            events.parse_event(line(base).rstrip(b"\n"))
        base["usage"] = {"input_tokens": 1}
        with self.assertRaises(events.JournalError):
            events.parse_event(line(base).rstrip(b"\n"))

    def test_launch_flags_must_agree_with_the_outcome(self):
        base = {"v": 1, "kind": "dispatch_ended",
                "event_id": str(uuid.uuid4()), "stream": "alpha.g1",
                "at": iso(T0), "agent": "alpha", "dropped_total": 0,
                "seq": 1, "message_id": "m1", "turn_id": "t1",
                "launched": True, "outcome": "not_launched",
                "exit": "normal", "duration_ms": 1, "usage": None}
        with self.assertRaises(events.JournalError):
            events.parse_event(line(base).rstrip(b"\n"))

    def test_an_unknown_version_in_a_segment_refuses_the_whole_round(self):
        s = self.journal.add_stream("g1", T0)
        message(s, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        s.raw(line(_event_obj(v=2, stream="alpha.g1", seq=9)))
        with self.assertRaises(events.UnsupportedVersion):
            self.round()
        self.assertEqual(self.count("event"), 0)
        self.assertEqual(self.state.round_number(), 0)

    def test_a_damaged_line_is_counted_and_skipped_never_stored(self):
        s = self.journal.add_stream("g1", T0)
        s.raw(b'{"v":1,"secret":"SENTINEL-KEY-0001"}\n')
        s.raw(b"not json at all SENTINEL-KEY-0001\n")
        message(s, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        self.round()
        self.assertEqual(self.state.counter("alpha", "damaged_lines"), 2)
        self.assertIsNotNone(self.final("m1"))
        with open(self.state_path, "rb") as handle:
            self.assertNotIn(b"SENTINEL-KEY-0001", handle.read())


# --- identity and mapping ---------------------------------------------------

class Identity(_Base):
    """identity: a dispatch is its turn id, never the retry counter."""

    def test_two_dispatches_of_one_message_are_two_calls(self):
        s = self.journal.add_stream("g1", T0)
        s.dispatch("m1", "t-a", T0 + 2000, message_at=T0 + 1000)
        s.dispatch("m1", "t-b", T0 + 3000, message_at=T0 + 1000)
        s.disposed("m1", "completed", T0 + 4000, message_at=T0 + 1000)
        self.round()
        final = self.final("m1")
        self.assertEqual(final["body"]["calls"], 2)
        self.assertEqual(final["body"]["input_tokens"], 340)
        self.assertEqual(self.count("report"), 2)
        ids = {r[0] for r in self.state.all("SELECT event_id FROM report")}
        self.assertEqual(len(ids), 2)

    def test_a_dispatch_that_never_launched_is_not_a_call(self):
        s = self.journal.add_stream("g1", T0)
        s.dispatch("m1", "t-a", T0 + 2000, message_at=T0 + 1000,
                   launched=False, outcome="not_launched", usage=None)
        s.dispatch("m1", "t-b", T0 + 3000, message_at=T0 + 1000)
        s.disposed("m1", "completed", T0 + 4000, message_at=T0 + 1000)
        self.round()
        self.assertEqual(self.final("m1")["body"]["calls"], 1)
        self.assertEqual(self.count("report"), 1)

    def test_ids_are_stable_and_scoped_to_the_seat(self):
        a = mapper.turn_uuid(SEAT, "m1")
        self.assertEqual(a, mapper.turn_uuid(SEAT, "m1"))
        self.assertNotEqual(a, mapper.turn_uuid(MODEL, "m1"))
        self.assertNotEqual(a, mapper.turn_uuid(SEAT, "m2"))
        self.assertNotEqual(mapper.request_id(a),
                            mapper.final_event_id(a))
        self.assertNotEqual(mapper.dispatch_event_id(a, "t1"),
                            mapper.dispatch_event_id(a, "t2"))

    def test_the_bodies_are_byte_identical_after_a_restart(self):
        s = self.journal.add_stream("g1", T0)
        message(s, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        self.round()
        before = self.state.all("SELECT body_current FROM acquisition")
        self.reopen()
        self.round(now=NOW + 10)
        self.assertEqual(self.state.all(
            "SELECT body_current FROM acquisition"), before)

    def test_requeue_is_a_new_message_with_its_own_lease(self):
        s = self.journal.add_stream("g1", T0)
        message(s, "m1", T0 + 1000, T0 + 2000, T0 + 3000,
                disposition="dead_lettered")
        message(s, "m2", T0 + 5000, T0 + 6000, T0 + 7000)    # the requeue
        self.round()
        first, second = self.final("m1"), self.final("m2")
        self.assertEqual(first["body"]["outcome"], "failed")
        self.assertEqual(second["body"]["outcome"], "completed")
        self.assertNotEqual(first["body"]["event_id"],
                            second["body"]["event_id"])
        rows = self.state.all("SELECT message_id, turn_uuid, request_id FROM "
                              "acquisition ORDER BY id")
        self.assertEqual(len({r[1] for r in rows}), 2)
        self.assertEqual(len({r[2] for r in rows}), 2)

    def test_an_unconfigured_agent_is_read_and_counted_never_sent(self):
        self.configs = {}
        s = self.journal.add_stream("g1", T0)
        message(s, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        result = self.round()
        self.assertEqual(result.new_events, 3)
        self.assertEqual(self.count("acquisition"), 0)
        self.assertEqual(self.count("report"), 0)
        self.assertEqual(self.count("final"), 0)
        self.assertEqual(self.status("m1")[0], 1)


class MapperBodies(unittest.TestCase):
    """The bodies are exactly what Leasewise's own validators accept."""

    def test_the_acquisition_is_a_valid_shadow_grant_request(self):
        body = mapper.acquisition_body(CFG, "m1", NOW)
        internal = http_api._wire_to_grant(body)
        lease._validate_grant_request(internal)
        self.assertEqual(body["context"]["mode"], "UNKNOWN")
        self.assertIsNone(body["gateway_id"])
        self.assertIsNone(body["context"]["initial_tokens"])

    def test_every_receipt_is_a_valid_settle_body(self):
        lease_id = str(uuid.uuid4())
        bodies = [
            mapper.dispatch_report_body(str(uuid.uuid4()), "success", USAGE),
            mapper.dispatch_report_body(str(uuid.uuid4()), "failed", None),
            mapper.final_body(str(uuid.uuid4()), "completed",
                              mapper.message_totals([USAGE], True)),
            mapper.final_body(str(uuid.uuid4()), "delivery_failed",
                              mapper.message_totals([None], False)),
        ]
        for body in bodies:
            receipt = http_api._wire_to_settle(lease_id, body, NOW)
            lease._validate_settle_receipt(receipt)
            self.assertIsNone(body["actual_micro_eur"])
            self.assertIsNone(body["seq"])

    def test_the_rebuild_keeps_the_id_and_changes_only_the_time(self):
        original = mapper.acquisition_body(CFG, "m1", NOW)
        fresh = mapper.rebuild_body(original, NOW + 500)
        self.assertEqual(fresh["request_id"], original["request_id"])
        self.assertEqual(fresh["context"]["observed_at"], NOW + 500)
        self.assertEqual(original["context"]["observed_at"], NOW)
        fresh["context"]["observed_at"] = NOW
        self.assertEqual(fresh, original)

    def test_a_final_exists_only_for_the_three_real_outcomes(self):
        with self.assertRaises(ValueError):
            mapper.final_body("x", "outcome_unknown", {})


class Totals(unittest.TestCase):
    """mapper_totals: DESIGN-7A.md 4.6 and Part 12 F7."""

    def test_claude_cache_mapping(self):
        total = mapper.message_totals([USAGE], True)
        self.assertEqual(total, {"calls": 1, "input_tokens": 170,
                                 "cached_input_tokens": 50,
                                 "output_tokens": 10})

    def test_totals_are_the_sum_over_launched_dispatches(self):
        other = dict(USAGE, input_tokens=1, output_tokens=2,
                     cache_read_tokens=3, cache_write_tokens=4)
        total = mapper.message_totals([USAGE, other], True)
        self.assertEqual(total["input_tokens"], 170 + 8)
        self.assertEqual(total["cached_input_tokens"], 53)
        self.assertEqual(total["output_tokens"], 12)
        self.assertEqual(total["calls"], 2)

    def test_one_unknown_dispatch_makes_each_total_null(self):
        total = mapper.message_totals([USAGE, None], True)
        self.assertEqual(total, {"calls": 2, "input_tokens": None,
                                 "cached_input_tokens": None,
                                 "output_tokens": None})

    def test_one_null_component_nulls_that_total_only(self):
        broken = dict(USAGE, cache_write_tokens=None)
        total = mapper.message_totals([broken], True)
        self.assertIsNone(total["input_tokens"])          # needs all three
        self.assertEqual(total["cached_input_tokens"], 50)
        self.assertEqual(total["output_tokens"], 10)
        broken = dict(USAGE, output_tokens=None)
        total = mapper.message_totals([broken], True)
        self.assertEqual(total["input_tokens"], 170)
        self.assertIsNone(total["output_tokens"])

    def test_an_incomplete_observation_nulls_every_token_field(self):
        total = mapper.message_totals([USAGE, USAGE], False)
        self.assertEqual(total, {"calls": 2, "input_tokens": None,
                                 "cached_input_tokens": None,
                                 "output_tokens": None})

    def test_overflow_is_null_not_wrapped(self):
        big = dict(USAGE, input_tokens=events.INT64_MAX,
                   cache_read_tokens=0, cache_write_tokens=0)
        total = mapper.message_totals([big, big], True)
        self.assertIsNone(total["input_tokens"])
        total = mapper.message_totals([dict(big, input_tokens=5)], True)
        self.assertEqual(total["input_tokens"], 5)
        edge = dict(USAGE, input_tokens=events.INT64_MAX,
                    cache_read_tokens=1, cache_write_tokens=0)
        self.assertIsNone(mapper.message_totals([edge], True)["input_tokens"])

    def test_no_launched_dispatch_means_zero_calls_and_zero_tokens(self):
        self.assertEqual(mapper.message_totals([], True),
                         {"calls": 0, "input_tokens": 0,
                          "cached_input_tokens": 0, "output_tokens": 0})

    def test_a_codex_dispatch_has_no_usage_so_its_tokens_are_unknown(self):
        total = mapper.message_totals([None], True)
        self.assertIsNone(total["input_tokens"])
        self.assertEqual(total["calls"], 1)

    def test_a_per_dispatch_report_may_carry_nulls(self):
        body = mapper.dispatch_report_body("e", "failed", None)
        self.assertEqual((body["input_tokens"], body["cached_input_tokens"],
                          body["output_tokens"]), (None, None, None))
        self.assertEqual(body["outcome"], "failed")
        self.assertEqual(body["calls"], 1)
        self.assertIs(body["final"], False)


# --- completeness (D3, D3a) -------------------------------------------------

class CompletenessD3(_Base):
    def test_a_clean_single_stream_is_complete(self):
        s = self.journal.add_stream("g1", T0)
        message(s, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        self.round()
        self.assertEqual(self.status("m1")[:2], (1, "ok"))
        self.assertEqual(self.final("m1")["complete"], 1)

    def test_an_unclean_predecessor_that_lost_a_whole_dispatch(self):
        # g1 died without closing; the message arrived while it ran, and
        # whatever g1 did with it was lost. g2 finished it.
        g1 = self.journal.add_stream("g1", T0)
        g1.dispatch("other", "t-o", T0 + 1000, message_at=T0 + 500)
        g2 = self.journal.add_stream("g2", T0 + 100_000, previous="g1")
        message(g2, "m1", T0 + 10_000, T0 + 110_000, T0 + 120_000)
        self.round()
        status = self.status("m1")
        self.assertEqual(status[0], 0)
        self.assertIn("unclean_predecessor", status[2].split(","))
        final = self.final("m1")
        self.assertEqual(final["complete"], 0)
        self.assertIsNone(final["body"]["input_tokens"])
        self.assertEqual(final["body"]["calls"], 1)

    def unclean_pair(self, arrive):
        g1 = self.journal.add_stream("g1", T0)
        g1.dispatch("other", "t-o", T0 + 1000, message_at=T0 + 500)
        g2 = self.journal.add_stream("g2", T0 + 100_000, previous="g1")
        message(g2, "m1", arrive, T0 + 2_000_000, T0 + 2_100_000)
        self.round()

    def test_a_message_that_arrived_long_after_the_successor_stays_complete(
            self):
        self.unclean_pair(T0 + 100_000 + 700_000)
        self.assertEqual(self.status("m1")[:2], (1, "ok"))
        self.assertEqual(self.final("m1")["body"]["input_tokens"], 170)

    def test_the_clock_skew_margin_has_two_sides(self):
        edge = T0 + 100_000 + rounds.CLOCK_SKEW_ALLOWANCE_SECONDS * 1000
        self.assertEqual(rounds.CLOCK_SKEW_ALLOWANCE_SECONDS, 600)
        self.unclean_pair(edge - 1)               # just inside the margin
        self.assertIn("unclean_predecessor", self.status("m1")[2].split(","))

    def test_a_message_exactly_at_the_margin_is_outside_it(self):
        edge = T0 + 100_000 + rounds.CLOCK_SKEW_ALLOWANCE_SECONDS * 1000
        self.unclean_pair(edge)
        self.assertEqual(self.status("m1")[:2], (1, "ok"))

    def test_a_message_just_after_the_successor_start_is_still_suspect(self):
        self.unclean_pair(T0 + 100_000 + 5_000)   # the old test's case
        self.assertIn("unclean_predecessor", self.status("m1")[2].split(","))

    def test_a_cleanly_closed_predecessor_costs_nothing(self):
        g1 = self.journal.add_stream("g1", T0)
        message(g1, "old", T0 + 500, T0 + 1000, T0 + 2000)
        g1.close(T0 + 50_000)
        g2 = self.journal.add_stream("g2", T0 + 100_000, previous="g1")
        message(g2, "m1", T0 + 60_000, T0 + 110_000, T0 + 120_000)
        self.round()
        self.assertEqual(self.status("m1")[:2], (1, "ok"))

    def test_the_disposition_stream_needs_no_closure(self):
        s = self.journal.add_stream("g1", T0)
        message(s, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        self.round()
        self.assertEqual(self.count("stream", "closed=1"), 0)
        self.assertEqual(self.status("m1")[:2], (1, "ok"))

    def test_a_missing_message_time_is_incomplete(self):
        s = self.journal.add_stream("g1", T0)
        message(s, "m1", None, T0 + 2000, T0 + 3000)
        self.round()
        self.assertEqual(self.status("m1")[1], "unknown_arrival")
        final = self.final("m1")
        self.assertEqual(final["complete"], 0)
        self.assertIsNone(final["body"]["output_tokens"])

    def test_an_unreadable_message_time_is_unknown_too(self):
        s = self.journal.add_stream("g1", T0)
        s.dispatch("m1", "t1", T0 + 2000, message_at=T0 + 1000)
        s.raw(line({**_event_obj(stream="alpha.g1", seq=s.seq + 1,
                                 at=iso(T0 + 3000), disposition="completed"),
                    "message_at": "last tuesday"}))
        self.round()
        self.assertEqual(self.status("m1")[1], "unknown_arrival")

    def test_a_message_time_later_than_its_disposition_is_inconsistent(self):
        s = self.journal.add_stream("g1", T0)
        message(s, "m1", T0 + 9000, T0 + 9500, T0 + 3000)
        self.round()
        self.assertIn("clock_inconsistent", self.status("m1")[2].split(","))
        self.assertEqual(self.final("m1")["complete"], 0)

    def test_a_message_time_after_its_only_event_is_inconsistent(self):
        s = self.journal.add_stream("g1", T0)
        s.disposed("m1", "outcome_unknown", T0 + 3000, message_at=T0 + 9000)
        self.round()
        self.assertEqual(self.status("m1")[1], "clock_inconsistent")

    def test_two_different_message_times_are_inconsistent(self):
        s = self.journal.add_stream("g1", T0)
        s.dispatch("m1", "t1", T0 + 2000, message_at=T0 + 1000)
        s.disposed("m1", "completed", T0 + 3000, message_at=T0 + 1500)
        self.round()
        self.assertIn("clock_inconsistent", self.status("m1")[2].split(","))

    def test_a_stream_that_starts_before_its_predecessor_is_inconsistent(self):
        g1 = self.journal.add_stream("g1", T0 + 50_000)
        g1.close(T0 + 60_000)
        g2 = self.journal.add_stream("g2", T0 + 10_000, previous="g1")
        message(g2, "m1", T0 + 20_000, T0 + 30_000, T0 + 40_000)
        self.round()
        self.assertIn("clock_inconsistent", self.status("m1")[2].split(","))

    def test_a_clock_set_back_inside_a_stream_is_inconsistent(self):
        s = self.journal.add_stream("g1", T0)
        s.dispatch("a", "t-a", T0 + 50_000, end_at=T0 + 50_100,
                   message_at=T0 + 49_000)
        s.dispatch("m1", "t-m", T0 + 20_000, message_at=T0 + 19_000)
        s.disposed("m1", "completed", T0 + 21_000, message_at=T0 + 19_000)
        self.round()
        self.assertIn("clock_inconsistent", self.status("m1")[2].split(","))

    def test_a_missing_predecessor_makes_an_old_message_incomplete(self):
        s = self.journal.add_stream("g2", T0 + 100_000, previous="g1")
        message(s, "old", T0 + 50_000, T0 + 110_000, T0 + 120_000)
        message(s, "new", T0 + 105_000, T0 + 130_000, T0 + 140_000)
        self.round()
        self.assertIn("missing_stream", self.status("old")[2].split(","))
        self.assertEqual(self.status("new")[:2], (1, "ok"))

    def test_a_registered_stream_whose_files_are_all_gone_is_missing(self):
        self.journal.add_stream("g1", T0, header=False)
        g2 = self.journal.add_stream("g2", T0 + 100_000, previous="g1")
        message(g2, "m1", T0 + 20_000, T0 + 110_000, T0 + 120_000)
        self.round()
        self.assertIn("missing_stream", self.status("m1")[2].split(","))

    def test_a_dispatch_without_an_end_is_unmatched(self):
        s = self.journal.add_stream("g1", T0)
        s.dispatch("m1", "t1", T0 + 2000, message_at=T0 + 1000, ended=False)
        s.disposed("m1", "completed", T0 + 3000, message_at=T0 + 1000)
        self.round()
        self.assertEqual(self.status("m1")[1], "unmatched_dispatch")
        self.assertEqual(self.final("m1")["complete"], 0)


# --- continuity over seq (D4) --------------------------------------------------

class ContinuityD4(_Base):
    def test_seq_continues_across_segments_through_after_seq(self):
        s = self.journal.add_stream("g1", T0)
        s.dispatch("m1", "t1", T0 + 2000, message_at=T0 + 1000)
        s.rotate(T0 + 2500)
        s.disposed("m1", "completed", T0 + 3000, message_at=T0 + 1000)
        self.round()
        self.assertEqual(self.status("m1")[:2], (1, "ok"))
        self.assertEqual(self.count("segment"), 2)

    def test_a_deleted_segment_is_a_gap_for_a_message_that_overlaps_it(self):
        s = self.journal.add_stream("g1", T0)
        s.dispatch("a", "t-a", T0 + 1000, message_at=T0 + 500)
        s.rotate(T0 + 2000)
        s.dispatch("b", "t-b", T0 + 2100, message_at=T0 + 2050)
        s.rotate(T0 + 3000)
        message(s, "m1", T0 + 1500, T0 + 3100, T0 + 3200)
        message(s, "late", T0 + 3300, T0 + 3400, T0 + 3500)
        os.remove(s.path(2))
        self.round()
        self.assertIn("seq_gap", self.status("m1")[2].split(","))
        self.assertEqual(self.status("late")[:2], (1, "ok"))

    def test_a_missing_tail_is_exposed_by_the_closing_record(self):
        s = self.journal.add_stream("g1", T0)
        message(s, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        s.dispatch("m2", "t-2", T0 + 4000, message_at=T0 + 3500)
        s.lose()                                  # the end of m2, never written
        s.close(T0 + 9000)
        self.round()
        self.assertEqual(self.status("m1")[:2], (1, "ok"))
        self.assertIn("seq_gap", self.status("m2")[2].split(","))

    def test_a_lost_event_in_the_middle_of_a_stream(self):
        s = self.journal.add_stream("g1", T0)
        s.dispatch("m1", "t1", T0 + 2000, message_at=T0 + 1000)
        s.lose()
        s.disposed("m1", "completed", T0 + 3000, message_at=T0 + 1000)
        self.round()
        self.assertEqual(self.status("m1")[1], "seq_gap")
        self.assertIsNone(self.final("m1")["body"]["input_tokens"])

    def test_a_drop_is_visible_by_seq_and_by_the_counter(self):
        s = self.journal.add_stream("g1", T0)
        s.dispatch("m1", "t1", T0 + 2000, message_at=T0 + 1000)
        s.drop()
        s.disposed("m1", "completed", T0 + 3000, message_at=T0 + 1000)
        self.round()
        self.assertIn("seq_gap", self.status("m1")[2].split(","))

    def test_a_rising_drop_counter_alone_is_a_gap(self):
        s = self.journal.add_stream("g1", T0)
        s.dispatch("m1", "t1", T0 + 2000, message_at=T0 + 1000)
        s.dropped = 2
        s.disposed("m1", "completed", T0 + 3000, message_at=T0 + 1000)
        self.round()
        self.assertEqual(self.status("m1")[1], "dropped")

    def test_a_live_tear_followed_by_a_later_segment(self):
        s = self.journal.add_stream("g1", T0)
        s.dispatch("m1", "t1", T0 + 2000, message_at=T0 + 1000)
        s.tear()
        s.lose()
        s.rotate(T0 + 2500)
        s.disposed("m1", "completed", T0 + 3000, message_at=T0 + 1000)
        self.round()
        self.assertEqual(self.count("segment", "torn=1"), 1)
        self.assertEqual(self.status("m1")[1], "torn_tail")

    def test_a_crash_tail_is_confirmed_once_a_later_stream_starts(self):
        g1 = self.journal.add_stream("g1", T0)
        g1.dispatch("m1", "t1", T0 + 2000, message_at=T0 + 1000)
        g1.tear()
        self.round()
        self.assertEqual(self.count("segment", "torn=1"), 0)   # may be live
        g2 = self.journal.add_stream("g2", T0 + 100_000, previous="g1")
        g2.dispatch("m1", "t2", T0 + 110_000, message_at=T0 + 1000)
        g2.disposed("m1", "completed", T0 + 120_000, message_at=T0 + 1000)
        self.round()
        self.assertEqual(self.count("segment", "torn=1"), 1)
        self.assertIn("torn_tail", self.status("m1")[2].split(","))

    def test_a_half_written_line_is_read_once_it_is_complete(self):
        s = self.journal.add_stream("g1", T0)
        whole = line(_event_obj(stream="alpha.g1", seq=1, at=iso(T0 + 500),
                                message_id="m1", message_at=iso(T0 + 100)))
        s.raw(whole[:30])
        self.round()
        self.assertEqual(self.count("event"), 0)
        s.raw(whole[30:])
        self.round()
        self.assertEqual(self.count("event"), 1)
        self.assertEqual(self.count("segment", "torn=1"), 0)

    def test_a_lost_cursor_re_reads_without_double_counting(self):
        s = self.journal.add_stream("g1", T0)
        message(s, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        self.round()
        raw = self.final("m1")["raw"]
        self.state.conn.execute("DELETE FROM cursor")
        result = self.round()
        self.assertEqual(result.new_events, 0)
        self.assertEqual(self.count("late"), 0)
        self.assertEqual(self.count("report"), 1)
        self.assertEqual(self.final("m1")["raw"], raw)

    def test_a_second_round_reads_only_what_is_new(self):
        s = self.journal.add_stream("g1", T0)
        s.dispatch("m1", "t1", T0 + 2000, message_at=T0 + 1000)
        first = self.round()
        again = self.round()
        s.disposed("m1", "completed", T0 + 3000, message_at=T0 + 1000)
        third = self.round()
        self.assertEqual((first.new_events, again.new_events,
                          third.new_events), (2, 0, 1))
        self.assertIsNotNone(self.final("m1"))

    def test_reverse_stream_discovery_is_not_a_problem(self):
        # The older stream sorts AFTER the newer one by name.
        old = self.journal.add_stream("zz", T0)
        old.dispatch("m1", "t-old", T0 + 30_000, message_at=T0 + 20_000)
        old.close(T0 + 40_000)
        new = self.journal.add_stream("aa", T0 + 50_000, previous="zz")
        new.dispatch("m1", "t-new", T0 + 60_000, message_at=T0 + 20_000)
        new.disposed("m1", "completed", T0 + 70_000, message_at=T0 + 20_000)
        snapshot = events.take_snapshot(self.root)
        self.assertEqual([r.stream for r in snapshot["alpha"].segments],
                         ["alpha.aa", "alpha.zz"])
        self.round()
        final = self.final("m1")
        self.assertEqual(final["complete"], 1)
        self.assertEqual(final["body"]["calls"], 2)

    def test_a_segment_that_vanishes_during_the_round_is_tolerated(self):
        s = self.journal.add_stream("g1", T0)
        message(s, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        snapshot = events.take_snapshot(self.root)
        ref = snapshot["alpha"].segments[0]
        os.remove(ref.path)
        self.assertTrue(events.read_segment(ref, 0).missing)


# --- finals (D5) --------------------------------------------------------------

class FinalsD5(_Base):
    """finals_d5: every cell of the truth table."""

    def make(self, mid, disposition, complete):
        base = T0 + 10_000 * self.index
        self.index += 1
        message(self.stream, mid, base + 1000 if complete else None,
                base + 2000, base + 3000, disposition=disposition)

    def setUp(self):
        super().setUp()
        self.stream = self.journal.add_stream("g1", T0)
        self.index = 0

    def test_the_three_outcomes_complete_and_incomplete(self):
        expected = {"completed": "completed", "dead_lettered": "failed",
                    "delivery_failed": "failed"}
        for disposition, outcome in expected.items():
            for complete in (True, False):
                mid = "%s-%s" % (disposition, complete)
                self.make(mid, disposition, complete)
        self.round()
        for disposition, outcome in expected.items():
            full = self.final("%s-True" % disposition)
            self.assertEqual(full["body"]["outcome"], outcome)
            self.assertEqual(full["complete"], 1)
            self.assertEqual(full["body"]["input_tokens"], 170)
            partial = self.final("%s-False" % disposition)
            self.assertEqual(partial["body"]["outcome"], outcome)
            self.assertEqual(partial["complete"], 0)
            self.assertEqual(partial["body"]["calls"], 1)
            for name in ("input_tokens", "cached_input_tokens",
                         "output_tokens"):
                self.assertIsNone(partial["body"][name])
            self.assertIsNone(partial["body"]["actual_micro_eur"])

    def test_outcome_unknown_sends_no_final_in_either_column(self):
        self.make("u-complete", "outcome_unknown", True)
        self.make("u-partial", "outcome_unknown", False)
        self.round()
        self.assertEqual(self.count("final"), 0)
        self.assertEqual(self.status("u-complete")[3], "outcome_unknown")
        self.assertEqual(self.status("u-partial")[3], "outcome_unknown")

    def test_no_disposition_seen_means_no_final(self):
        self.stream.dispatch("open", "t-o", T0 + 2000, message_at=T0 + 1000)
        self.round()
        self.assertEqual(self.count("final"), 0)
        self.assertEqual(self.count("acquisition"), 1)

    def test_no_observed_dispatch_means_no_lease_and_no_final(self):
        self.stream.disposed("ghost", "completed", T0 + 3000,
                             message_at=T0 + 1000)
        self.round()
        self.assertEqual(self.count("final"), 0)
        self.assertEqual(self.count("acquisition"), 0)

    def test_a_final_waits_for_the_round_that_holds_the_disposition(self):
        s = self.stream
        s.dispatch("m1", "t1", T0 + 2000, message_at=T0 + 1000)
        self.round()
        self.assertIsNone(self.final("m1"))
        s.disposed("m1", "completed", T0 + 3000, message_at=T0 + 1000)
        result = self.round()
        self.assertEqual(result.finals, [("alpha", "m1")])

    def test_the_final_is_stored_before_it_is_sent_and_resent_unchanged(self):
        message(self.stream, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        self.round()
        raw = self.final("m1")["raw"]
        self.assertEqual(self.final("m1")["state"], "pending")
        drain(self.state, lambda item: accept(item)
              if item.kind != decisions.FINAL else Response(None))
        self.reopen()                       # a crash between store and send
        item = None
        for _ in range(5):
            item = self.state.next_item(NOW + 10 ** 9)
            if item and item.kind == decisions.FINAL:
                break
            self.state.apply(item, accept(item), NOW)
        self.assertEqual(item.kind, decisions.FINAL)
        self.assertEqual(json.dumps(item.body, sort_keys=True), raw)
        self.assertEqual(item.key, self.final("m1")["body"]["event_id"])

    def test_a_replayed_duplicate_disposition_makes_no_second_final(self):
        s = self.stream
        message(s, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        s.disposed("m1", "completed", T0 + 3100, message_at=T0 + 1000)
        self.round()
        self.assertEqual(self.count("final"), 1)
        raw = self.final("m1")["raw"]
        s.disposed("m1", "dead_lettered", T0 + 4000, message_at=T0 + 1000)
        self.round()
        self.assertEqual(self.count("final"), 1)
        self.assertEqual(self.final("m1")["raw"], raw)
        self.assertEqual(self.count("late", "count=1"), 1)

    def test_a_late_observation_is_counted_and_never_sent(self):
        s = self.stream
        message(s, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        self.round()
        raw = self.final("m1")["raw"]
        reports = self.count("report")
        s.dispatch("m1", "t-late", T0 + 5000, message_at=T0 + 1000)
        self.round()
        self.assertEqual(self.final("m1")["raw"], raw)
        self.assertEqual(self.count("report"), reports)
        self.assertEqual(self.state.one(
            "SELECT count FROM late WHERE message_id='m1'")[0], 2)

    def test_the_final_row_is_immutable(self):
        message(self.stream, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        self.round()
        for sql in ("UPDATE final SET body='{}'",
                    "UPDATE final SET totals_json='{}'",
                    "UPDATE final SET event_id='x'",
                    "UPDATE final SET complete=0", "DELETE FROM final"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.state.conn.execute(sql)
        self.state.conn.execute("UPDATE final SET tries=tries+1")

    def test_finals_wait_behind_the_acquisition_and_reports(self):
        message(self.stream, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        self.round()
        kinds = [k for k, _key, _r in drain(self.state, accept)]
        self.assertEqual(kinds, ["acquire", "report", "final"])
        self.assertEqual(self.final("m1")["state"], "sent")


# --- crashes at the boundaries ---------------------------------------------------

class _Crash(Exception):
    pass


class Crashes(_Base):
    def build(self):
        s = self.journal.add_stream("g1", T0)
        message(s, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        return s

    def crash_at(self, stage):
        def fault(name):
            if name == stage:
                raise _Crash()
        with self.assertRaises(_Crash):
            self.round(fault=fault)

    def converge(self):
        self.reopen()
        self.round()
        self.assertEqual(self.count("final"), 1)
        self.assertEqual(self.count("report"), 1)
        self.assertEqual(self.count("acquisition"), 1)
        self.assertEqual(self.count("event"), 3)
        self.assertEqual(self.final("m1")["body"]["calls"], 1)
        self.assertEqual(self.count("late"), 0)

    def test_a_crash_after_the_data_and_before_the_cursor(self):
        self.build()
        self.crash_at("data")
        self.assertEqual(self.count("cursor"), 0)
        self.assertEqual(self.count("event"), 3)
        self.converge()

    def test_a_crash_after_the_cursor_and_before_the_finals(self):
        self.build()
        self.crash_at("cursor")
        self.assertEqual(self.count("final"), 0)
        self.converge()

    def test_a_crash_after_the_finals_are_stored(self):
        self.build()
        self.crash_at("final")
        self.assertEqual(self.count("final"), 1)
        raw = self.final("m1")["raw"]
        self.reopen()
        self.round()
        self.assertEqual(self.final("m1")["raw"], raw)
        self.assertEqual(self.count("final"), 1)

    def test_a_crash_inside_the_data_step_stores_nothing(self):
        self.build()
        original = rounds._store_event
        calls = []

        def boom(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise _Crash()
            return original(*args, **kwargs)

        with mock.patch.object(rounds, "_store_event", boom):
            with self.assertRaises(_Crash):
                self.round()
        self.assertEqual(self.count("event"), 0)
        self.assertEqual(self.state.round_number(), 0)
        self.converge()

    def test_a_crash_inside_the_evaluation_keeps_the_stored_data(self):
        self.build()
        with mock.patch.object(rounds, "_evaluate", side_effect=_Crash):
            with self.assertRaises(_Crash):
                self.round()
        self.assertEqual(self.count("final"), 0)
        self.assertEqual(self.count("cursor"), 1)
        self.reopen()
        self.round()
        self.assertEqual(self.count("final"), 1)

    def test_a_crash_between_storing_and_sending_a_report(self):
        self.build()
        self.round()
        item = self.state.next_item(NOW)
        self.state.apply(item, accept(item), NOW)
        self.reopen()
        nxt = self.state.next_item(NOW)
        self.assertEqual(nxt.kind, decisions.REPORT)
        again = self.state.next_item(NOW)
        self.assertEqual(json.dumps(nxt.body, sort_keys=True),
                         json.dumps(again.body, sort_keys=True))


# --- ownership and the outbox ------------------------------------------------------

class OwnershipOutbox(_Base):
    def test_a_second_reporter_fails_at_once(self):
        with self.assertRaises(StateOwned) as caught:
            State(self.state_path)
        self.assertEqual(str(caught.exception),
                         "state is owned by another reporter")

    def test_a_second_process_fails_at_once_too(self):
        code = ("import sys; from quota_balancer.reporter.state import "
                "State, StateOwned\n"
                "try:\n State(sys.argv[1])\nexcept StateOwned as e:\n"
                " print(e); sys.exit(7)\n")
        result = subprocess.run([sys.executable, "-c", code, self.state_path],
                                cwd=REPO_ROOT, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("owned by another reporter", result.stdout)

    def test_the_state_can_be_taken_again_after_a_clean_close(self):
        self.state.close()
        self.state = State(self.state_path)

    def test_a_newer_state_version_is_refused(self):
        self.state.close()
        conn = sqlite3.connect(self.state_path)
        conn.execute("PRAGMA user_version=99")
        conn.close()
        with self.assertRaises(state.StateError):
            State(self.state_path)
        self.state = State.__new__(State)        # nothing to close
        self.state.close = lambda: None

    def test_one_request_at_a_time_in_order(self):
        s = self.journal.add_stream("g1", T0)
        message(s, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        message(s, "m2", T0 + 4000, T0 + 5000, T0 + 6000)
        self.round()
        order = [(k, item) for k, item, _r in drain(self.state, accept)]
        kinds = [k for k, _ in order]
        self.assertEqual(kinds, ["acquire", "report", "final"] * 2)

    def test_an_unacknowledged_acquisition_blocks_its_own_message_only(self):
        s = self.journal.add_stream("g1", T0)
        message(s, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        message(s, "m2", T0 + 4000, T0 + 5000, T0 + 6000)
        self.round()
        item = self.state.next_item(NOW)
        self.assertEqual((item.kind, item.message_id), ("acquire", "m1"))
        self.state.apply(item, Response(None), NOW)           # network error
        later = self.state.next_item(NOW)
        self.assertEqual((later.kind, later.message_id), ("acquire", "m2"))

    def test_a_retry_waits_and_then_sends_the_same_bytes(self):
        s = self.journal.add_stream("g1", T0)
        message(s, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        self.round()
        item = self.state.next_item(NOW)
        raw = json.dumps(item.body, sort_keys=True)
        decision = self.state.apply(item, Response(503), NOW, jitter=0.0)
        self.assertEqual(decision.action, decisions.RETRY)
        self.assertIsNone(self.state.next_item(NOW + 1000))
        again = self.state.next_item(NOW + 10 ** 7)
        self.assertEqual(json.dumps(again.body, sort_keys=True), raw)

    def test_the_report_cap_counts_the_extra_ones(self):
        s = self.journal.add_stream("g1", T0)
        for index in range(rounds.MAX_REPORTS_PER_MESSAGE + 3):
            s.dispatch("m1", "t-%d" % index, T0 + 2000 + index,
                       message_at=T0 + 1000)
        self.round()
        self.assertEqual(self.count("report"), rounds.MAX_REPORTS_PER_MESSAGE)
        self.assertEqual(self.state.counter("alpha", "reports_over_cap"), 3)

    def test_a_configuration_fault_stops_that_agent_only(self):
        self.configs["beta"] = AgentConfig("beta", SEAT, MODEL, "bounded_read",
                                           "opus", 1, 2, 1000, 500, 1000)
        other = Journal(self.root, "beta")
        a = self.journal.add_stream("g1", T0)
        b = other.add_stream("g1", T0)
        message(a, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        message(b, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        self.round()
        item = self.state.next_item(NOW)
        self.assertEqual(item.agent, "alpha")
        self.state.apply(item, Response(403, "POLICY_DENIED"), NOW)
        sent = drain(self.state, accept)
        self.assertTrue(sent)
        self.assertEqual({self.state.one(
            "SELECT agent FROM acquisition WHERE state='acquired'")[0]},
            {"beta"})
        self.assertEqual(self.state.one(
            "SELECT failed_code FROM agent_state WHERE agent='alpha'")[0],
            "POLICY_DENIED")


# --- the error table (4.5) and the rebuild budget (D7) -----------------------------

class ErrorTable(unittest.TestCase):
    def test_every_row(self):
        table = [
            (decisions.ACQUIRE, Response(None), 0, decisions.RETRY),
            (decisions.ACQUIRE, Response(503), 0, decisions.RETRY),
            (decisions.REPORT, Response(429, "ACCOUNT_PARKED"), 0,
             decisions.RETRY),
            (decisions.FINAL, Response(500), 0, decisions.RETRY),
            (decisions.ACQUIRE, Response(201, None, "L"), 0, decisions.DONE),
            (decisions.REPORT, Response(200), 0, decisions.DONE),
            (decisions.ACQUIRE, Response(400, "ESTIMATE_STALE"), 0,
             decisions.REBUILD),
            (decisions.ACQUIRE, Response(400, "ESTIMATE_STALE"), 2,
             decisions.REBUILD),
            (decisions.ACQUIRE, Response(400, "ESTIMATE_STALE"), 3,
             decisions.FAIL),
            (decisions.REPORT, Response(400, "ESTIMATE_STALE"), 0,
             decisions.FAIL),
            (decisions.FINAL, Response(400, "ESTIMATE_STALE"), 0,
             decisions.FAIL),
            (decisions.ACQUIRE, Response(400, "INVALID_REQUEST"), 0,
             decisions.FAIL),
            (decisions.ACQUIRE, Response(400, None), 0, decisions.FAIL),
            (decisions.ACQUIRE, Response(403, "POLICY_DENIED"), 0,
             decisions.FAIL_AGENT),
            (decisions.ACQUIRE, Response(404, "NOT_FOUND"), 0,
             decisions.FAIL_AGENT),
            (decisions.ACQUIRE, Response(403, "FORBIDDEN"), 0,
             decisions.FAIL_AGENT),
            (decisions.ACQUIRE, Response(400, "CONTEXT_UNKNOWN"), 0,
             decisions.FAIL_AGENT),
            (decisions.REPORT, Response(410, "LEASE_EXPIRED"), 0,
             decisions.DROP),
            (decisions.REPORT, Response(409, "RECEIPT_CONFLICT"), 0,
             decisions.DROP),
            (decisions.FINAL, Response(409, "RECEIPT_CONFLICT"), 0,
             decisions.FAIL),
            (decisions.FINAL, Response(410, "LEASE_EXPIRED"), 0,
             decisions.FAIL),
            (decisions.ACQUIRE, Response(409, "IDEMPOTENCY_CONFLICT"), 0,
             decisions.FAIL),
            (decisions.REPORT, Response(409, "IDEMPOTENCY_CONFLICT"), 0,
             decisions.FAIL),
        ]
        for kind, response, rebuilds, action in table:
            self.assertEqual(decisions.decide(kind, response, rebuilds).action,
                             action, (kind, response.status, response.code))

    def test_the_backoff_is_bounded_and_grows(self):
        values = [decisions.backoff_ms(n, 0.99) for n in range(1, 12)]
        self.assertEqual(values, sorted(values))
        self.assertGreaterEqual(decisions.backoff_ms(1, 0.0), 2500)
        self.assertLessEqual(max(values), 900_000)
        self.assertGreaterEqual(decisions.backoff_ms(20, 0.0), 450_000)


class RebuildD7(_Base):
    def setUp(self):
        super().setUp()
        s = self.journal.add_stream("g1", T0)
        message(s, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        self.round()

    def acquisition(self):
        return self.state.one(
            "SELECT body_original, body_current, rebuilds, state, "
            "last_error_code FROM acquisition")

    def stale(self, now):
        item = self.state.next_item(now)
        self.assertEqual(item.kind, decisions.ACQUIRE)
        return self.state.apply(item, Response(400, "ESTIMATE_STALE"), now)

    def test_three_rebuilds_then_the_fourth_answer_fails(self):
        original = self.acquisition()[0]
        for number in (1, 2, 3):
            now = NOW + number * 500_000
            decision = self.stale(now)
            self.assertEqual(decision.action, decisions.REBUILD)
            row = self.acquisition()
            self.assertEqual(row[0], original)            # kept for audit
            self.assertEqual(row[2], number)
            self.assertEqual(json.loads(row[1])["context"]["observed_at"], now)
            self.assertEqual(json.loads(row[1])["request_id"],
                             json.loads(original)["request_id"])
        decision = self.stale(NOW + 5_000_000)
        self.assertEqual(decision.action, decisions.FAIL)
        row = self.acquisition()
        self.assertEqual((row[3], row[2]), ("failed_permanent", 3))
        self.assertEqual(row[0], original)
        self.assertIsNone(self.state.next_item(NOW + 10 ** 9))

    def test_the_budget_survives_a_restart(self):
        for number in (1, 2, 3):
            self.stale(NOW + number)
            self.reopen()
            self.assertEqual(self.acquisition()[2], number)
        self.assertEqual(self.stale(NOW + 9).action, decisions.FAIL)

    def test_a_generic_invalid_request_never_rebuilds(self):
        row = self.acquisition()
        item = self.state.next_item(NOW)
        decision = self.state.apply(item, Response(400, "INVALID_REQUEST"),
                                    NOW)
        self.assertEqual(decision.action, decisions.FAIL)
        after = self.acquisition()
        self.assertEqual((after[0], after[1], after[2]), row[:3])
        self.assertEqual((after[3], after[4]),
                         ("failed_permanent", "INVALID_REQUEST"))

    def test_the_original_body_cannot_be_overwritten(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.state.conn.execute("UPDATE acquisition SET body_original='x'")

    def test_a_rebuilt_request_is_granted_and_then_the_flow_continues(self):
        self.stale(NOW + 500_000)
        sent = drain(self.state, accept, now=NOW + 500_001)
        self.assertEqual([k for k, _key, _r in sent],
                         ["acquire", "report", "final"])


# --- privacy ------------------------------------------------------------------------

class PrivacyCore(_Base):
    def test_the_state_holds_only_journal_facts_and_derived_bodies(self):
        s = self.journal.add_stream("g1", T0)
        s.raw(line(_event_obj(stream="alpha.g1", seq=77, at=iso(T0 + 1),
                              prompt="SENTINEL-PROMPT-7")))
        message(s, "m1", T0 + 1000, T0 + 2000, T0 + 3000)
        self.round()
        self.state.close()
        with open(self.state_path, "rb") as handle:
            data = handle.read()
        wal = self.state_path + "-wal"
        if os.path.exists(wal):
            with open(wal, "rb") as handle:
                data += handle.read()
        self.assertNotIn(b"SENTINEL-PROMPT-7", data)
        self.state = State(self.state_path)

    def test_the_errors_are_static(self):
        for exc in (events.JournalError("x"), state.StateOwned("y")):
            self.assertNotIn(self.tmp.name, str(exc))
        try:
            events.parse_event(b'{"v":1,"kind":"SENTINEL-KIND-9"}')
        except events.JournalError as exc:
            self.assertEqual(str(exc), "the journal record is invalid")


if __name__ == "__main__":
    unittest.main()
