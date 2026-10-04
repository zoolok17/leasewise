"""Card R1-3b: reading document version 4, gateway totals and epochs written at ingest, the event-id rule, and the durable collision
record (DESIGN-R1-3.md 1.3, 3 and 9).

* **Version 4** adds five keys to a gateway snapshot (`generation`, `receipts_seq`, `quiet`, `held_over_reservation`,
  `reader_incarnation`) and REQUIRES both policy hashes; every version-3 rule stays; versions 2 and 3 are unchanged and never reach the
  new tables. A bad report is refused by the validator with the existing closed word, never by a store or SQL failure.
* **Ingest** copies each accepted version-4 report into `gateway_totals` and records its (reader incarnation, epoch) with its generation
  in `gateway_epochs`; the same triple with another generation is an `epoch_generation_mismatch` transition and the first mapping stays.
* **The event id** of a version-4 report is the one derived from its incarnation, generation and number.
* **The collision record:** the same event id with different content is refused as before (`IDEMPOTENCY_CONFLICT`, nothing of the
  refused report stored) but leaves one durable `sequence_conflict`, written inside the store's own write transaction, on both collision
  paths, only for the gateway's real owner, only with provenance, never for a retired reader, and a repeat is a no-op. Nothing in this
  step resolves or overwrites a transition. Nothing reads the new rows until card R1-3c.

Synthetic reports only.
"""

import contextlib
import copy
import os
import sqlite3
import unittest
import uuid
from unittest import mock

from quota_balancer import errors, http_api, store
from quota_balancer.errors import QuotaError
from quota_balancer.validate_readings import derive_event_id, validate_readings as validate

from tests.test_card2b1 import (_Base, _doc_v3, _eid, _gateway_snapshot, _month_bounds_of,
                                _now_ms, _v3_gateway_snapshot, _write_json)

INC = "d" * 32
INC2 = "e" * 32
GEN = "c" * 32
GEN2 = "b" * 32
PRICE = "a" * 64
CAP = "9" * 64


def payload(**over):
    """A version-4 gateway snapshot payload (internal shape: `gateway` is the alias)."""
    start, end = _month_bounds_of(_now_ms() - 1000)
    base = {
        "gateway": "g1", "ledger_epoch": 1, "seq": 1, "period_start": start, "period_end": end,
        "policy_hashes": {"price": PRICE, "child_cap": CAP}, "sized_ready": False, "reconciliation_required": False,
        "settled_micro_eur": 5000, "reserved_micro_eur": 200, "uncertain_micro_eur": 30, "unattributed_micro_eur": 7,
        "generation": GEN, "receipts_seq": 4, "quiet": True, "held_over_reservation": 0, "reader_incarnation": INC,
    }
    base.update(over)
    return base


def report(machine="desktop", account="meter", event_id=None, **over):
    """A version-4 reading with the event id derived from its own identity (unless one is given)."""
    body = payload(**over)
    return {
        "event_id": event_id or derive_event_id(body["reader_incarnation"], body["generation"], body["seq"]),
        "machine": machine, "account": account, "kind": "gateway_snapshot", "observed_at": _now_ms() - 1000, "payload": body}


def doc4(*readings):
    return {"version": 4, "readings": list(readings)}


class Version4(unittest.TestCase):
    """The validator, with no store: a bad report is refused with the closed word before anything else can fail."""

    def refused(self, doc, code=errors.CODE_INVALID_CONFIG):
        with self.assertRaises(QuotaError) as caught:
            validate(doc, _now_ms())
        self.assertEqual(caught.exception.code, code)

    def test_a_report_with_every_key_is_accepted_and_normalized(self):
        (reading,) = validate(doc4(report()), _now_ms())
        for key, value in (("generation", GEN), ("reader_incarnation", INC), ("receipts_seq", 4), ("quiet", True),
                           ("held_over_reservation", 0), ("settled_micro_eur", 5000)):
            self.assertEqual(reading[key], value, key)
        self.assertEqual(reading["policy_hashes"], {"price": PRICE, "child_cap": CAP})

    def test_a_null_receipts_seq_is_accepted_it_means_no_coverage_claim(self):
        (reading,) = validate(doc4(report(receipts_seq=None)), _now_ms())
        self.assertIsNone(reading["receipts_seq"])

    def test_each_of_the_five_keys_missing_is_refused(self):
        for key in ("generation", "receipts_seq", "quiet", "held_over_reservation", "reader_incarnation"):
            with self.subTest(key=key):
                item = report()
                del item["payload"][key]
                self.refused(doc4(item))

    def test_each_key_with_a_wrong_value_is_refused(self):
        bad = {
            "generation": ("c" * 31, "C" * 32, "c" * 33, "", None, 5, "g" * 32),
            "reader_incarnation": ("d" * 31, "D" * 32, "d" * 33, "", None, 5, "z" * 32),
            "receipts_seq": (-1, "1", True, 1.5, [], {}),
            "quiet": (1, 0, "true", None, "yes"),
            "held_over_reservation": (-1, 1.5, True, "0", None),
        }
        for key, values in bad.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    item = report()
                    item["payload"][key] = value
                    item["event_id"] = _eid("any")                      # the validator must refuse the shape, not only the id
                    self.refused(doc4(item))

    def test_an_unknown_key_in_a_version_4_payload_is_refused(self):
        item = report()
        item["payload"]["extra"] = 1
        self.refused(doc4(item))

    def test_version_4_requires_both_policy_hashes_and_never_makes_one_up(self):
        for hashes in (None, {"price": PRICE}, {"child_cap": CAP}, {"price": PRICE, "child_cap": None},
                       {"price": "A" * 64, "child_cap": CAP}, {"price": PRICE, "child_cap": CAP, "extra": PRICE}):
            with self.subTest(hashes=hashes):
                self.refused(doc4(report(policy_hashes=hashes)))

    def test_version_3_still_accepts_null_policy_hashes_and_versions_2_and_3_are_unchanged(self):
        now = _now_ms()
        v3 = _v3_gateway_snapshot(_eid("v3"), "desktop", "meter", "g1", 1, 1, now - 1000, 1, 2, 3, 0, policy_hashes=None)
        (reading,) = validate(_doc_v3([v3]), now)
        self.assertIsNone(reading["policy_hashes"])
        self.assertNotIn("reader_incarnation", reading)
        v2 = _gateway_snapshot(_eid("v2"), "desktop", "meter", "g1", 1, 1, now - 1000)
        (reading,) = validate({"version": 2, "readings": [v2]}, now)
        self.assertNotIn("generation", reading)

    def test_the_five_keys_are_not_allowed_in_a_version_3_document(self):
        item = _v3_gateway_snapshot(_eid("v3x"), "desktop", "meter", "g1", 1, 1, _now_ms() - 1000, 1, 2, 3, 0)
        item["payload"].update(generation=GEN, receipts_seq=1, quiet=True, held_over_reservation=0, reader_incarnation=INC)
        self.refused(_doc_v3([item]))

    def test_a_version_4_document_needs_the_version_4_payload(self):
        v3 = _v3_gateway_snapshot(_eid("v3y"), "desktop", "meter", "g1", 1, 1, _now_ms() - 1000, 1, 2, 3, 0)
        self.refused(doc4(v3))

    def test_version_5_is_still_an_unsupported_version(self):
        self.refused({"version": 5, "readings": [report()]}, errors.CODE_SCHEMA_UNSUPPORTED)


class EventId(unittest.TestCase):

    def test_the_event_id_is_a_uuid_derived_from_the_incarnation_the_generation_and_the_number(self):
        one = derive_event_id(INC, GEN, 1)
        self.assertEqual(str(uuid.UUID(one)), one)
        self.assertEqual(one, derive_event_id(INC, GEN, 1))                      # deterministic: a resend is a replay
        for other in (derive_event_id(INC2, GEN, 1), derive_event_id(INC, GEN2, 1), derive_event_id(INC, GEN, 2)):
            self.assertNotEqual(one, other)                                      # each part counts, so a new incarnation never collides

    def test_a_version_4_report_with_any_other_event_id_is_refused(self):
        for event_id in (_eid("random"), str(uuid.uuid4()), derive_event_id(INC2, GEN, 1), derive_event_id(INC, GEN, 2)):
            with self.subTest(event_id=event_id):
                with self.assertRaises(QuotaError) as caught:
                    validate(doc4(report(event_id=event_id)), _now_ms())
                self.assertEqual(caught.exception.code, errors.CODE_INVALID_CONFIG)

    def test_a_non_uuid_event_id_is_refused_as_before(self):
        with self.assertRaises(QuotaError):
            validate(doc4(report(event_id="not-a-uuid")), _now_ms())

    def test_versions_2_and_3_keep_any_event_id(self):
        now = _now_ms()
        validate(_doc_v3([_v3_gateway_snapshot(_eid("any3"), "desktop", "meter", "g1", 1, 1, now - 1000, 1, 2, 3, 0)]), now)


class _Store(_Base):

    def rows(self, text, params=()):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(text, params).fetchall()

    def run_sql(self, text, params=()):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(text, params)
            conn.commit()

    def gateway_id(self, alias="g1"):
        return self.rows("SELECT id FROM gateways WHERE alias=?", (alias,))[0][0]

    def ingest(self, *readings, name="reading.json"):
        path = os.path.join(self.tmp, name)
        _write_json(path, doc4(*readings))
        store.reading_import(self.db, path)

    def transitions(self):
        return self.rows("SELECT kind, event_id, conflict_incarnation, conflict_epoch, conflict_seq, resolved_by_adoption "
                         "FROM gateway_transitions ORDER BY id")

    def everything(self):
        """Every reading-related table, so 'nothing was stored' is checked rather than assumed."""
        return {table: self.rows("SELECT * FROM " + table + " ORDER BY 1, 2")                           # fixed names
                for table in ("readings", "gateway_totals", "gateway_epochs", "http_request_identity")}


class Ingest(_Store):

    def test_an_accepted_version_4_report_lands_in_totals_and_epochs_joined_to_its_reading(self):
        item = report(seq=3, quiet=False, held_over_reservation=2, receipts_seq=None, reconciliation_required=True)
        self.ingest(item)
        event = item["event_id"]
        self.assertEqual(self.rows("SELECT kind, machine_id IS NOT NULL FROM readings WHERE event_id=?", (event,)),
                         [("gateway_snapshot", 1)])
        (row,) = self.rows(
            "SELECT gateway_id, reader_incarnation, ledger_epoch, generation, seq, settled_micro_eur, reserved_micro_eur, "
            "uncertain_micro_eur, unattributed_micro_eur, receipts_seq, quiet, held_over_reservation, price_hash, child_cap_hash, "
            "gateway_flag, observed_at FROM gateway_totals WHERE event_id=?", (event,))
        self.assertEqual(row, (self.gateway_id(), INC, 1, GEN, 3, 5000, 200, 30, 7, None, 0, 2, PRICE, CAP, 1,
                               item["observed_at"]))
        self.assertEqual(self.rows("SELECT gateway_id, reader_incarnation, ledger_epoch, generation, first_event_id "
                                   "FROM gateway_epochs"), [(self.gateway_id(), INC, 1, GEN, event)])
        self.assertEqual(self.transitions(), [])
        # the legacy gateway columns are still reduced exactly as before
        self.assertEqual(self.rows("SELECT ledger_epoch, last_seq FROM gateways WHERE alias='g1'"), [(1, 3)])

    def test_a_second_report_of_the_same_epoch_adds_a_row_and_no_new_epoch(self):
        self.ingest(report(seq=1))
        self.ingest(report(seq=2, settled_micro_eur=6000), name="second.json")
        self.assertEqual(self.rows("SELECT seq FROM gateway_totals ORDER BY seq"), [(1,), (2,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM gateway_epochs"), [(1,)])

    def test_a_replay_of_the_same_report_changes_nothing(self):
        item = report()
        self.ingest(item)
        before = self.everything()
        self.ingest(item, name="again.json")
        self.assertEqual(self.everything(), before)

    def test_one_reader_may_return_to_a_generation_under_a_new_epoch_and_two_readers_may_read_one_ledger(self):
        self.ingest(report(seq=1, ledger_epoch=1, generation=GEN))
        self.ingest(report(seq=2, ledger_epoch=2, generation=GEN2), name="b.json")
        self.ingest(report(seq=3, ledger_epoch=3, generation=GEN), name="c.json")               # back to the first generation
        self.ingest(report(seq=1, ledger_epoch=1, generation=GEN, reader_incarnation=INC2), name="d.json")   # another reader
        self.assertEqual(self.rows("SELECT COUNT(*) FROM gateway_epochs"), [(4,)])
        self.assertEqual(self.transitions(), [])

    def test_an_epoch_triple_seen_with_a_second_generation_is_an_identity_conflict_and_the_first_mapping_stays(self):
        first = report(seq=1, ledger_epoch=1, generation=GEN)
        second = report(seq=2, ledger_epoch=1, generation=GEN2)                 # the same (gateway, incarnation, epoch)
        self.ingest(first)
        self.ingest(second, name="second.json")
        self.assertEqual(self.rows("SELECT generation FROM gateway_epochs"), [(GEN,)])         # the first mapping stays
        self.assertEqual(self.rows("SELECT COUNT(*) FROM gateway_totals"), [(2,)])             # both reports are kept
        self.assertEqual(self.transitions(), [("epoch_generation_mismatch", second["event_id"], INC, 1, 2, None)])
        self.ingest(second, name="third.json")                                  # a replay adds no second row
        self.assertEqual(len(self.transitions()), 1)

    def test_versions_2_and_3_are_stored_as_before_and_never_reach_the_new_tables(self):
        now = _now_ms()
        path = os.path.join(self.tmp, "v3.json")
        _write_json(path, _doc_v3([_v3_gateway_snapshot(_eid("v3i"), "desktop", "meter", "g1", 1, 1, now - 1000, 9, 2, 3, 0)]))
        store.reading_import(self.db, path)
        path = os.path.join(self.tmp, "v2.json")
        _write_json(path, {"version": 2, "readings": [_gateway_snapshot(_eid("v2i"), "desktop", "meter", "g1", 1, 2, now - 900)]})
        store.reading_import(self.db, path)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM readings"), [(2,)])
        for table in ("gateway_totals", "gateway_epochs", "gateway_transitions"):
            self.assertEqual(self.rows("SELECT COUNT(*) FROM " + table), [(0,)], table)               # fixed names

    def test_a_report_from_a_machine_that_is_not_the_gateways_owner_is_refused_and_stores_nothing(self):
        before = self.everything()
        with self.assertRaises(QuotaError) as caught:
            self.ingest(report(machine="worker"))
        self.assertEqual(caught.exception.code, errors.CODE_INVALID_CONFIG)
        self.assertEqual(self.everything(), before)

    def test_a_later_report_never_resolves_or_overwrites_a_transition(self):
        self.ingest(report(seq=1, ledger_epoch=1, generation=GEN))
        self.ingest(report(seq=2, ledger_epoch=1, generation=GEN2), name="b.json")
        recorded = self.transitions()
        self.ingest(report(seq=3, ledger_epoch=2, generation=GEN2), name="c.json")
        self.ingest(report(seq=4, ledger_epoch=2, generation=GEN2), name="d.json")
        self.assertEqual(self.transitions(), recorded)


class Collisions(_Store):
    """The durable collision record, on both paths. The colliding report has the same derived event id and different content."""

    def setUp(self):
        super().setUp()
        self.first = report(seq=1, settled_micro_eur=5000)
        self.rival = copy.deepcopy(self.first)
        self.rival["payload"]["settled_micro_eur"] = 9999                         # same identity, different content
        self.assertEqual(self.first["event_id"], self.rival["event_id"])
        self.owner = self.rows("SELECT id FROM machines WHERE alias='desktop'")[0][0]
        self.worker = self.rows("SELECT id FROM machines WHERE alias='worker'")[0][0]
        self.account = self.rows("SELECT id FROM accounts WHERE alias='meter'")[0][0]

    def wire(self, item):
        """The HTTP body of a reading (`gateway_id` is the store UUID; the machine is the authenticated caller)."""
        body = dict(item["payload"])
        del body["gateway"]
        body["gateway_id"] = self.gateway_id(item["payload"]["gateway"])
        return {"event_id": item["event_id"], "account_id": self.account, "observed_at": item["observed_at"],
                "kind": "gateway_snapshot", "payload": body}

    def http(self, item, machine_id=None, alias="desktop"):
        return http_api.handle_readings(self.db, (machine_id or self.owner, alias), self.wire(item), _now_ms(), item["event_id"])

    def conflict(self, call, *args, **kwargs):
        with self.assertRaises(QuotaError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, errors.CODE_IDEMPOTENCY_CONFLICT)

    def expected_record(self):
        return [("sequence_conflict", self.first["event_id"], INC, 1, 1, None)]

    def test_the_reading_idempotency_path_leaves_one_durable_conflict_and_no_rejected_data(self):
        self.ingest(self.first)
        before = self.everything()
        self.conflict(self.ingest, self.rival, name="rival.json")
        self.assertEqual(self.transitions(), self.expected_record())
        self.assertEqual(self.everything(), before)                              # nothing of the refused report was stored
        (detected,) = self.rows("SELECT detected_at FROM gateway_transitions")[0]
        self.assertGreater(detected, 0)

    def test_the_http_identity_path_leaves_one_durable_conflict_and_no_rejected_data(self):
        self.http(self.first)
        before = self.everything()
        self.conflict(self.http, self.rival)                                      # the same key, a different wire body
        self.assertEqual(self.transitions(), self.expected_record())
        self.assertEqual(self.everything(), before)                              # and the key is still bound to the first body

    def test_a_sender_that_is_not_the_owner_writes_nothing_on_either_path(self):
        self.ingest(self.first)
        stranger = copy.deepcopy(self.rival)
        stranger["machine"] = "worker"                                            # the authenticated sender is not the owner
        self.conflict(self.ingest, stranger, name="stranger.json")
        self.assertEqual(self.transitions(), [])
        self.http(self.first)
        self.conflict(self.http, self.rival, self.worker, "worker")               # over HTTP, as the other machine
        self.assertEqual(self.transitions(), [])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM readings"), [(1,)])

    def test_a_sender_that_names_the_right_gateway_but_the_wrong_account_writes_nothing(self):
        self.ingest(self.first)
        wrong = copy.deepcopy(self.rival)
        wrong["account"] = None                                                   # an ambiguous source is not the owner either
        self.conflict(self.ingest, wrong, name="wrong.json")
        self.assertEqual(self.transitions(), [])

    def test_a_collision_with_no_provenance_writes_nothing(self):
        """The stored reading under that event id is not a version-4 report of this gateway, incarnation and epoch."""
        now = _now_ms()
        stored = _v3_gateway_snapshot(self.first["event_id"], "desktop", "meter", "g1", 1, 1, now - 1000, 1, 2, 3, 0)
        path = os.path.join(self.tmp, "v3.json")
        _write_json(path, _doc_v3([stored]))
        store.reading_import(self.db, path)
        self.conflict(self.ingest, self.first, name="clash.json")
        self.assertEqual(self.transitions(), [])
        stored_elsewhere = report(seq=7, ledger_epoch=1)                          # a stored report of another epoch
        self.ingest(stored_elsewhere, name="seven.json")
        other = copy.deepcopy(stored_elsewhere)
        other["payload"]["ledger_epoch"] = 2                                      # same id, another epoch, other content
        self.conflict(self.ingest, other, name="other-epoch.json")
        self.assertEqual(self.transitions(), [])

    def test_a_retired_incarnation_is_only_counted_never_a_transition(self):
        self.ingest(self.first)
        gateway = self.gateway_id()
        self.run_sql("INSERT INTO gateway_readers(gateway_id, reader_incarnation, first_event_id, first_seen_at, state, "
                     "decided_by_adoption, retired_at) VALUES(?,?,?,?,?,?,?)",
                     (gateway, INC, self.first["event_id"], 1, "retired", 1, 5))        # a seeded row (R1-3c writes them)
        self.conflict(self.ingest, self.rival, name="r1.json")
        self.assertEqual(self.transitions(), [])
        self.assertEqual(self.rows("SELECT retired_reader_reports FROM gateway_chain_state WHERE gateway_id=?", (gateway,)), [(1,)])
        self.conflict(self.ingest, self.rival, name="r2.json")
        self.assertEqual(self.rows("SELECT retired_reader_reports FROM gateway_chain_state WHERE gateway_id=?", (gateway,)), [(2,)])
        self.assertEqual(self.transitions(), [])
        self.assertEqual(self.rows("SELECT current_adoption_id FROM gateway_chain_state WHERE gateway_id=?", (gateway,)), [(None,)])

    def test_a_pending_or_current_incarnation_still_gets_the_record(self):
        self.ingest(self.first)
        for state, decided in (("pending", None), ("current", 1)):
            with self.subTest(state=state):
                self.run_sql("DELETE FROM gateway_readers")
                self.run_sql("INSERT INTO gateway_readers(gateway_id, reader_incarnation, first_event_id, first_seen_at, state, "
                             "decided_by_adoption, retired_at) VALUES(?,?,?,?,?,?,NULL)",
                             (self.gateway_id(), INC, self.first["event_id"], 1, state, decided))
                self.run_sql("DELETE FROM gateway_transitions")
                self.conflict(self.ingest, self.rival, name="p-%s.json" % state)
                self.assertEqual(self.transitions(), self.expected_record())

    def test_a_repeat_of_the_same_collision_is_a_no_op(self):
        self.ingest(self.first)
        for attempt in range(3):
            self.conflict(self.ingest, self.rival, name="again-%d.json" % attempt)
        self.http(self.first)
        self.conflict(self.http, self.rival)
        self.conflict(self.http, self.rival)
        self.assertEqual(self.transitions(), self.expected_record())             # one row, however many times it is refused

    def test_the_same_content_under_the_same_id_is_a_replay_not_a_collision(self):
        self.ingest(self.first)
        self.ingest(self.first, name="same.json")
        self.assertEqual(self.transitions(), [])

    def test_a_stop_between_the_record_and_the_commit_leaves_nothing_and_a_clean_run_leaves_everything(self):
        self.ingest(self.first)
        before = self.everything()

        def stop(conn):
            raise RuntimeError("stopped before the commit")
        with mock.patch.object(store, "_after_collision_record", stop):
            with self.assertRaises(RuntimeError):
                self.ingest(self.rival, name="stopped.json")
        self.assertEqual(self.transitions(), [])                                  # nothing: the record rolled back with it all
        self.assertEqual(self.everything(), before)
        self.conflict(self.ingest, self.rival, name="clean.json")                      # a clean run then leaves the record
        self.assertEqual(self.transitions(), self.expected_record())
        self.assertEqual(self.everything(), before)

    def test_the_conflict_is_still_returned_with_the_existing_closed_word_and_message(self):
        self.ingest(self.first)
        with self.assertRaises(QuotaError) as caught:
            self.ingest(self.rival, name="word.json")
        self.assertEqual((caught.exception.code, caught.exception.message),
                         (errors.CODE_IDEMPOTENCY_CONFLICT, errors.MSG_IDEMPOTENCY_CONFLICT))

    def test_other_conflicts_leave_no_record(self):
        """A collision of a version-3 reading (not a version-4 report) is refused as before and writes no transition."""
        now = _now_ms()
        first = _v3_gateway_snapshot(_eid("v3c"), "desktop", "meter", "g1", 1, 1, now - 1000, 1, 2, 3, 0)
        rival = copy.deepcopy(first)
        rival["payload"]["settled_micro_eur"] = 2
        path = os.path.join(self.tmp, "a.json")
        _write_json(path, _doc_v3([first]))
        store.reading_import(self.db, path)
        _write_json(path, _doc_v3([rival]))
        with self.assertRaises(QuotaError) as caught:
            store.reading_import(self.db, path)
        self.assertEqual(caught.exception.code, errors.CODE_IDEMPOTENCY_CONFLICT)
        self.assertEqual(self.transitions(), [])

    def test_a_batch_that_collides_part_way_stores_none_of_its_other_readings(self):
        self.ingest(self.first)
        fresh = report(seq=5)
        before = self.everything()
        self.conflict(self.ingest, fresh, self.rival, name="batch.json")
        self.assertEqual(self.everything(), before)                              # the new report in the same batch is not kept
        self.assertEqual(self.transitions(), self.expected_record())


if __name__ == "__main__":
    unittest.main()
