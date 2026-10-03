"""Card L6a-1 (revised), step 1: the reporter's receipt custody chain in its state file (version 3).

Two durable stages (DESIGN-6A 8.3). Stage 1, CAPTURE: the evidence exactly as received, and the cursor, in one
transaction, even when no binding is known. Stage 2, RELEASE: when a binding for the capture's FROZEN gateway is
known, the immutable delivery body, created in one transaction with the state change. Quarantine and delivery state
live in the one mutable table. The file moves from version 1 or 2 to version 3 in one transaction; a reporter that
knows only version 2 refuses a version-3 file. Everything here is synthetic.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from unittest import mock

from quota_balancer.reporter import admission_store, decisions, mapper, receipts
from quota_balancer.reporter import state as state_mod
from quota_balancer.reporter.state import State, StateError

from tests.l6a1_env import (GATEWAY_ALIAS, GATEWAY_ID, GENERATION, GENERATION_B, OTHER_GATEWAY_ID, encode, lease,
                            page, receipt)

NOW = 1_788_224_400_000


def parsed(rows, after=0, generation=GENERATION):
    return receipts.parse_page(encode(page(rows, after=after, generation=generation)), after)


def binding(lease_id, gateway_id=GATEWAY_ID):
    one = admission_store.Binding()
    one.ref_sha256, one.lease_id, one.gateway_id = mapper.lease_ref_hash(lease_id), lease_id, gateway_id
    one.agent, one.message_id, one.expires_ms = "alpha", "m-" + uuid.uuid4().hex[:6], NOW + 1_800_000
    return one


class _Tmp(unittest.TestCase):
    current = None

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(dir=os.environ.get("AGENTTALK_SCRATCH") or None,
                                                prefix="reporter-l6a1-")
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "r.db")

    def open(self):
        state = State(self.path)
        self.addCleanup(state.close)
        self.current = state
        return state

    def raw(self, sql, params=()):
        if self.current is not None:
            return self.current.all(sql, params)
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def source(self, state, gateway_id=GATEWAY_ID):
        return state.ensure_source(GATEWAY_ALIAS, gateway_id, NOW)

    def capture(self, state, rows, after=0, generation=GENERATION, gateway_id=GATEWAY_ID):
        return state.capture_page(self.source(state, gateway_id), gateway_id, parsed(rows, after, generation), NOW)

    def states(self):
        return self.raw("SELECT c.seq, s.state, s.reason FROM receipt_capture c JOIN receipt_state s"
                        " ON s.capture_id=c.id ORDER BY c.id")


def _version_2(test):
    """The code of card 7c-2: version 2, no receipt tables."""
    original = (state_mod.SCHEMA_VERSION, state_mod._DDL)
    state_mod.SCHEMA_VERSION, state_mod._DDL = 2, state_mod._DDL_V1 + state_mod._LIMIT_DDL
    test.addCleanup(lambda: setattr(state_mod, "SCHEMA_VERSION", original[0]))
    test.addCleanup(lambda: setattr(state_mod, "_DDL", original[1]))


class Schema(_Tmp):

    def test_a_fresh_file_has_the_custody_tables_and_triggers(self):
        state = self.open()
        self.assertEqual(state.one("PRAGMA user_version")[0], 3)
        names = {r[0] for r in state.all("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"receipt_source", "receipt_cursor", "receipt_binding", "receipt_capture",
                         "receipt_delivery", "receipt_state"} <= names)
        self.assertNotIn("gateway_receipt", names)                    # the parked layout never shipped
        self.assertNotIn("lease_ref", names)
        self.assertEqual([r[1] for r in state.all("PRAGMA table_info(receipt_capture)")],
                         ["id", "source_id", "gateway_id", "generation", "seq", "ref_sha256", "outcome", "calls",
                          "input_tokens", "output_tokens", "actual_micro_eur", "closed_at", "captured_ms"])
        triggers = {r[0] for r in state.all("SELECT name FROM sqlite_master WHERE type='trigger'")}
        for table in ("receipt_source", "receipt_binding", "receipt_capture", "receipt_delivery"):
            self.assertTrue({table + "_immutable", table + "_keep"} <= triggers, table)
        self.assertIn("receipt_state_keep", triggers)

    def _make_old(self, version):
        original = (state_mod.SCHEMA_VERSION, state_mod._DDL)
        state_mod.SCHEMA_VERSION = version
        state_mod._DDL = state_mod._DDL_V1 + (state_mod._LIMIT_DDL if version == 2 else ())
        try:
            old = State(self.path)
            with old.txn():
                old.conn.execute(
                    "INSERT INTO acquisition(agent, message_id, turn_uuid, request_id, body_original, body_current,"
                    " state, lease_id) VALUES('alpha','m1','t1','r1','{}','{}','acquired',?)", (str(uuid.uuid4()),))
                old.conn.execute("INSERT INTO counter(agent, name, value) VALUES('alpha','reports_dropped',3)")
                if version == 2:
                    old.conn.execute(
                        "INSERT INTO limit_event(event_id, account_alias, body, captured_ms) VALUES(?,?,?,?)",
                        (str(uuid.uuid4()), "claude-main", "{}", NOW))
            old.close()
        finally:
            state_mod.SCHEMA_VERSION, state_mod._DDL = original

    def snapshot(self):
        return [self.raw("SELECT * FROM %s ORDER BY 1" % t) for t in ("acquisition", "counter", "note", "meta")]

    def test_versions_1_and_2_migrate_to_3_with_every_old_row_untouched(self):
        for version in (1, 2):
            with self.subTest(version=version):
                self.path = os.path.join(self._dir.name, "old%d.db" % version)
                self.current = None
                self._make_old(version)
                before = self.snapshot()
                self.assertEqual(self.raw("PRAGMA user_version"), [(version,)])
                state = self.open()
                self.assertEqual(state.one("PRAGMA user_version")[0], 3)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.raw("SELECT * FROM receipt_capture"), [])
                self.assertEqual(len(self.raw("SELECT name FROM sqlite_master WHERE name IN ('limit_event',"
                                              "'receipt_capture','receipt_state')")), 3)
                state.close()
                self.current = None

    def test_a_second_open_is_a_no_op(self):
        self._make_old(2)
        State(self.path).close()
        schema = self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name")
        State(self.path).close()
        self.assertEqual(self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name"), schema)
        self.assertEqual(self.raw("PRAGMA user_version"), [(3,)])

    def test_a_migration_that_fails_leaves_the_file_at_its_version_whole(self):
        for version in (1, 2):
            with self.subTest(version=version):
                self.path = os.path.join(self._dir.name, "fail%d.db" % version)
                self.current = None
                self._make_old(version)
                before = self.snapshot()
                schema = self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name")
                original = state_mod._RECEIPT_DDL
                state_mod._RECEIPT_DDL = original + ("THIS IS NOT SQL",)
                try:
                    with self.assertRaises(StateError):
                        State(self.path)
                finally:
                    state_mod._RECEIPT_DDL = original
                self.assertEqual(self.raw("PRAGMA user_version"), [(version,)])
                self.assertEqual(self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name"), schema)
                self.assertEqual(self.snapshot(), before)
                self.open().close()                                   # and the good code still migrates it
                self.current = None
                self.assertEqual(self.raw("PRAGMA user_version"), [(3,)])

    def _fingerprint(self):
        with open(self.path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        return digest, self.raw("PRAGMA user_version"), self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name")

    def test_a_reporter_that_knows_only_version_2_refuses_a_version_3_file_and_changes_nothing(self):
        State(self.path).close()
        before = self._fingerprint()
        _version_2(self)
        with self.assertRaises(StateError) as caught:
            State(self.path)
        self.assertEqual(str(caught.exception), "the state file version is not supported")
        self.assertEqual(self._fingerprint(), before)

    def test_a_newer_unknown_version_is_refused_and_changes_nothing(self):
        State(self.path).close()
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA user_version=4")
        conn.commit()
        conn.close()
        before = self._fingerprint()
        with self.assertRaises(StateError):
            State(self.path)
        self.assertEqual(self._fingerprint(), before)


class Sources(_Tmp):

    def test_a_source_is_its_name_and_its_gateway_for_good(self):
        state = self.open()
        one = state.ensure_source(GATEWAY_ALIAS, GATEWAY_ID, NOW)
        self.assertEqual(state.ensure_source(GATEWAY_ALIAS, GATEWAY_ID, NOW + 5), one)
        other = state.ensure_source(GATEWAY_ALIAS, OTHER_GATEWAY_ID, NOW + 9)
        self.assertNotEqual(one, other)                                # the same name mapped elsewhere: a new source
        self.assertEqual(self.raw("SELECT id, alias, gateway_id, created_ms FROM receipt_source ORDER BY id"),
                         [(one, GATEWAY_ALIAS, GATEWAY_ID, NOW), (other, GATEWAY_ALIAS, OTHER_GATEWAY_ID, NOW + 9)])

    def test_a_source_cannot_be_rebound_or_deleted(self):
        state = self.open()
        self.source(state)
        for sql in ("UPDATE receipt_source SET gateway_id='x'", "DELETE FROM receipt_source"):
            with self.assertRaises(sqlite3.DatabaseError):
                with state.txn():
                    state.conn.execute(sql)

    def test_a_new_generation_starts_at_zero_and_the_old_cursor_is_kept(self):
        state = self.open()
        one = self.source(state)
        self.capture(state, [receipt(lease(), 1), receipt(lease(), 2)])
        self.assertEqual(state.current_cursor(one), (GENERATION, 2))
        self.assertEqual(state.switch_generation(one, GENERATION_B), 0)
        self.assertEqual(state.current_cursor(one), (GENERATION_B, 0))
        self.assertEqual(state.cursor_for(one, GENERATION), 2)           # kept
        self.assertEqual(state.counter(state_mod.RECEIPT_AGENT, "generation_change"), 1)

    def test_a_generation_seen_again_takes_its_kept_cursor_back_a_b_a(self):
        state = self.open()
        one = self.source(state)
        self.capture(state, [receipt(lease(), 1), receipt(lease(), 2), receipt(lease(), 3)], generation=GENERATION)
        state.switch_generation(one, GENERATION_B)
        self.capture(state, [receipt(lease(), 1)], generation=GENERATION_B)
        self.assertEqual(state.current_cursor(one), (GENERATION_B, 1))
        self.assertEqual(state.switch_generation(one, GENERATION), 3)    # A again: its cursor, not 0
        self.assertEqual(state.current_cursor(one), (GENERATION, 3))
        # and the next page of A starts after 3, and B's cursor is still its own
        self.assertEqual(self.capture(state, [receipt(lease(), 4)], after=3, generation=GENERATION), 1)
        self.assertEqual((state.cursor_for(one, GENERATION), state.cursor_for(one, GENERATION_B)), (4, 1))

    def test_the_same_number_in_two_generations_is_two_captures(self):
        state = self.open()
        self.capture(state, [receipt(lease(), 1)], generation=GENERATION)
        self.source(state)
        state.switch_generation(1, GENERATION_B)
        self.capture(state, [receipt(lease(), 1)], generation=GENERATION_B)
        self.assertEqual(self.raw("SELECT generation, seq FROM receipt_capture ORDER BY id"),
                         [(GENERATION, 1), (GENERATION_B, 1)])


class Capture(_Tmp):

    def test_a_receipt_is_captured_exactly_as_received_even_with_no_binding(self):
        state = self.open()
        one = lease()
        row = receipt(one, 1, input_tokens=None, output_tokens=7, outcome="provider_limit", calls=3,
                      actual_micro_eur=99)
        self.assertEqual(self.capture(state, [row]), 1)
        (stored,) = self.raw("SELECT source_id, gateway_id, generation, seq, ref_sha256, outcome, calls, input_tokens,"
                             " output_tokens, actual_micro_eur, closed_at, captured_ms FROM receipt_capture")
        self.assertEqual(stored, (1, GATEWAY_ID, GENERATION, 1, mapper.lease_ref_hash(one), "provider_limit", 3,
                                  None, 7, 99, "2026-10-03T10:00:00.000000Z", NOW))
        self.assertEqual(self.states(), [(1, "captured", None)])
        self.assertEqual(self.raw("SELECT COUNT(*) FROM receipt_delivery"), [(0,)])      # no body without a binding

    def test_the_same_page_twice_stores_one_set_of_captures(self):
        state = self.open()
        rows = [receipt(lease(), 1), receipt(lease(), 2)]
        self.assertEqual(self.capture(state, rows), 2)
        before = self.raw("SELECT * FROM receipt_capture")
        source = self.source(state)
        # the same page again: it was asked for at 0 but the cursor is 2, so it is not taken at all
        with self.assertRaises(StateError):
            state.capture_page(source, GATEWAY_ID, parsed(rows), NOW + 5)
        self.assertEqual(self.raw("SELECT * FROM receipt_capture"), before)
        self.assertEqual(state.counter(state_mod.RECEIPT_AGENT, "stored"), 2)

    def test_the_cursor_moves_with_the_captures_and_never_without_them(self):
        state = self.open()
        one = self.source(state)
        self.assertEqual(state.current_cursor(one), (None, 0))
        self.capture(state, [receipt(lease(), 1), receipt(lease(), 2)])
        self.assertEqual(state.current_cursor(one), (GENERATION, 2))
        state.capture_page(one, GATEWAY_ID, parsed([], after=2), NOW)             # an empty page moves nothing
        self.assertEqual(state.current_cursor(one), (GENERATION, 2))

    def test_a_page_that_does_not_start_at_the_cursor_stores_nothing(self):
        state = self.open()
        one = self.source(state)
        self.capture(state, [receipt(lease(), 1)])
        with self.assertRaises(StateError) as caught:
            state.capture_page(one, GATEWAY_ID, parsed([receipt(lease(), 6)], after=5), NOW)
        self.assertEqual(str(caught.exception), "a receipt page does not start at the stored cursor")
        self.assertEqual(self.raw("SELECT COUNT(*) FROM receipt_capture"), [(1,)])
        self.assertEqual(state.cursor_for(one, GENERATION), 1)

    def test_a_failure_in_the_middle_stores_nothing_and_moves_nothing(self):
        state = self.open()
        one = self.source(state)
        calls = []
        real = state.conn

        class Breaking:
            """The connection, except that the third capture row fails."""

            def __getattr__(self, name):
                return getattr(real, name)

            def execute(self, sql, params=()):
                if sql.lstrip().startswith("INSERT OR IGNORE INTO receipt_capture"):
                    calls.append(1)
                    if len(calls) == 3:
                        raise sqlite3.OperationalError("simulated fault")
                return real.execute(sql, params)

        state.conn = Breaking()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                state.capture_page(one, GATEWAY_ID, parsed([receipt(lease(), n) for n in (1, 2, 3)]), NOW)
        finally:
            state.conn = real
        self.assertEqual(self.raw("SELECT COUNT(*) FROM receipt_capture"), [(0,)])
        self.assertEqual(self.raw("SELECT COUNT(*) FROM receipt_state"), [(0,)])
        self.assertEqual(self.raw("SELECT COUNT(*) FROM receipt_cursor"), [(0,)])
        self.assertEqual(state.counter(state_mod.RECEIPT_AGENT, "stored"), 0)
        self.assertEqual(self.capture(state, [receipt(lease(), n) for n in (1, 2, 3)]), 3)     # the same read works later

    def test_a_capture_cannot_be_changed_or_deleted(self):
        state = self.open()
        self.capture(state, [receipt(lease(), 1)])
        for column, value in (("gateway_id", OTHER_GATEWAY_ID), ("seq", 9), ("input_tokens", 0), ("outcome", "failed"),
                              ("ref_sha256", "0" * 64), ("closed_at", "x"), ("generation", "x")):
            with self.assertRaises(sqlite3.DatabaseError, msg=column):
                with state.txn():
                    state.conn.execute("UPDATE receipt_capture SET %s=?" % column, (value,))
        with self.assertRaises(sqlite3.DatabaseError):
            with state.txn():
                state.conn.execute("DELETE FROM receipt_capture")
        with self.assertRaises(sqlite3.DatabaseError):
            with state.txn():
                state.conn.execute("DELETE FROM receipt_state")

    def test_a_read_that_stored_nothing_is_counted_by_its_closed_word_only(self):
        state = self.open()
        state.note_receipt_read(receipts.READ_LATER, "timeout", NOW)
        state.note_receipt_read(receipts.READ_LATER, "timeout", NOW + 1)
        state.note_receipt_read(receipts.REFUSED, "bad_row", NOW + 2)
        agent = state_mod.RECEIPT_AGENT
        self.assertEqual((state.counter(agent, "read_later:timeout"), state.counter(agent, "refused:bad_row")), (2, 1))
        self.assertEqual(state.note("receipt_last_read"), ("refused", NOW + 2))
        self.assertEqual(self.raw("SELECT COUNT(*) FROM receipt_capture"), [(0,)])


class Release(_Tmp):

    def test_an_unknown_reference_is_quarantined_with_no_delivery_and_released_when_its_binding_appears(self):
        state = self.open()
        one = lease()
        self.capture(state, [receipt(one, 1)])
        self.assertEqual(state.release_waiting(NOW), 0)
        self.assertEqual(self.states(), [(1, "quarantined", "unknown_reference")])
        self.assertEqual(self.raw("SELECT COUNT(*) FROM receipt_delivery"), [(0,)])
        self.assertIsNone(state.next_receipt_item(NOW))
        self.assertEqual(state.release_waiting(NOW + 1), 0)                      # looked at again, still unknown
        state.copy_bindings([binding(one)], NOW + 2)
        self.assertEqual(state.release_waiting(NOW + 3), 1)
        self.assertEqual(self.states(), [(1, "pending", None)])
        (delivery,) = self.raw("SELECT capture_id, event_id, lease_id, gateway_id, created_ms FROM receipt_delivery")
        self.assertEqual((delivery[0], delivery[2], delivery[3], delivery[4]), (1, one, GATEWAY_ID, NOW + 3))

    def test_a_binding_for_another_gateway_is_quarantined_and_never_gets_a_delivery(self):
        state = self.open()
        one = lease()
        self.capture(state, [receipt(one, 1)])
        state.copy_bindings([binding(one, OTHER_GATEWAY_ID)], NOW)
        self.assertEqual(state.release_waiting(NOW), 0)
        self.assertEqual(self.states(), [(1, "quarantined", "wrong_gateway")])
        self.assertEqual(self.raw("SELECT COUNT(*) FROM receipt_delivery"), [(0,)])
        self.assertIsNone(state.next_receipt_item(NOW + 10 ** 9))

    def test_a_second_receipt_for_one_reference_and_generation_is_quarantined(self):
        state = self.open()
        one = lease()
        state.copy_bindings([binding(one)], NOW)
        self.capture(state, [receipt(one, 1), receipt(one, 2)])                  # the gateway promised one only
        self.assertEqual(state.release_waiting(NOW), 1)
        self.assertEqual(self.states(), [(1, "pending", None), (2, "quarantined", "duplicate_reference")])
        self.assertEqual(self.raw("SELECT COUNT(*) FROM receipt_delivery"), [(1,)])

    def test_the_delivery_body_is_the_frozen_canonical_settle_text(self):
        state = self.open()
        one = lease()
        state.copy_bindings([binding(one)], NOW)
        self.capture(state, [receipt(one, 1, outcome="failed", calls=3, input_tokens=None, output_tokens=4,
                                     actual_micro_eur=99)])
        state.release_waiting(NOW)
        (text, event_id) = self.raw("SELECT body, event_id FROM receipt_delivery")[0]
        body = json.loads(text)
        self.assertEqual(body, {
            "event_id": mapper.receipt_event_id(mapper.lease_ref_hash(one), GENERATION), "outcome": "failed",
            "calls": 3, "input_tokens": None, "cached_input_tokens": 0, "output_tokens": 4, "actual_micro_eur": 99,
            "gateway_id": GATEWAY_ID, "seq": 1, "final": True})
        self.assertEqual(set(body), mapper.RECEIPT_BODY_KEYS)
        self.assertEqual(text, json.dumps(body, sort_keys=True, separators=(",", ":")))       # the wire bytes
        self.assertEqual(event_id, body["event_id"])
        uuid.UUID(event_id)

    def test_the_event_id_comes_from_the_reference_and_the_generation_only(self):
        ref = mapper.lease_ref_hash(lease())
        a = mapper.receipt_event_id(ref, GENERATION)
        self.assertEqual(a, mapper.receipt_event_id(ref, GENERATION))
        self.assertNotEqual(a, mapper.receipt_event_id(ref, GENERATION_B))
        self.assertNotEqual(a, mapper.receipt_event_id(mapper.lease_ref_hash(lease()), GENERATION))

    def test_a_fault_between_the_delivery_insert_and_the_state_update_rolls_both_back(self):
        state = self.open()
        one = lease()
        state.copy_bindings([binding(one)], NOW)
        self.capture(state, [receipt(one, 1)])
        with mock.patch.object(State, "_mark_pending", side_effect=sqlite3.OperationalError("simulated")):
            with self.assertRaises(sqlite3.OperationalError):
                state.release_waiting(NOW)
        self.assertEqual(self.raw("SELECT COUNT(*) FROM receipt_delivery"), [(0,)])        # the insert did not survive
        self.assertEqual(self.states(), [(1, "captured", None)])                          # and the state did not move
        self.assertIsNone(state.next_receipt_item(NOW))

    def test_release_after_the_fault_builds_the_identical_body_event_id_and_bytes(self):
        reference = []
        for faulty in (False, True):
            path = os.path.join(self._dir.name, "r%s.db" % faulty)
            state = State(path)
            self.addCleanup(state.close)
            self.current = state
            one = "11111111-2222-4333-8444-555555555555"
            state.copy_bindings([binding(one)], NOW)
            state.capture_page(state.ensure_source(GATEWAY_ALIAS, GATEWAY_ID, NOW), GATEWAY_ID,
                               parsed([receipt(one, 1, input_tokens=None)]), NOW)
            if faulty:
                with mock.patch.object(State, "_mark_pending", side_effect=sqlite3.OperationalError("x")):
                    with self.assertRaises(sqlite3.OperationalError):
                        state.release_waiting(NOW)
            state.release_waiting(NOW + 50)
            reference.append(self.raw("SELECT event_id, lease_id, gateway_id, body FROM receipt_delivery"))
        self.assertEqual(reference[0], reference[1])

    def test_a_delivery_cannot_be_changed_or_deleted_but_its_state_can_move(self):
        state = self.open()
        one = lease()
        state.copy_bindings([binding(one)], NOW)
        self.capture(state, [receipt(one, 1)])
        state.release_waiting(NOW)
        for column, value in (("body", "{}"), ("event_id", "x"), ("lease_id", lease()), ("gateway_id", "x"),
                              ("capture_id", 9)):
            with self.assertRaises(sqlite3.DatabaseError, msg=column):
                with state.txn():
                    state.conn.execute("UPDATE receipt_delivery SET %s=?" % column, (value,))
        with self.assertRaises(sqlite3.DatabaseError):
            with state.txn():
                state.conn.execute("DELETE FROM receipt_delivery")
        with state.txn():
            state.conn.execute("UPDATE receipt_state SET state='sent', tries=2, not_before_ms=5, last_error_code='X'")
        self.assertEqual(self.states(), [(1, "sent", None)])

    def test_a_binding_is_copied_once_and_never_replaced(self):
        state = self.open()
        one = lease()
        first = binding(one)
        self.assertEqual(state.copy_bindings([first], NOW), 1)
        self.assertEqual(state.copy_bindings([first], NOW + 1), 0)                 # the same row again: nothing
        changed = binding(one, OTHER_GATEWAY_ID)
        self.assertEqual(state.copy_bindings([changed], NOW + 2), 0)               # a changed one never replaces it
        self.assertEqual(self.raw("SELECT lease_id, gateway_id FROM receipt_binding"), [(one, GATEWAY_ID)])
        self.assertEqual(state.counter(state_mod.RECEIPT_AGENT, "binding_changed"), 1)
        for sql in ("UPDATE receipt_binding SET gateway_id='x'", "DELETE FROM receipt_binding"):
            with self.assertRaises(sqlite3.DatabaseError):
                with state.txn():
                    state.conn.execute(sql)


class SourceOwnership(_Tmp):
    """Requirement 6: a capture keeps the gateway its source belonged to at capture time; release compares the
    binding with that frozen gateway, never with today's configuration."""

    def test_a_changed_source_mapping_never_releases_an_old_capture_under_the_new_mapping(self):
        state = self.open()
        one = lease()
        self.capture(state, [receipt(one, 1)], gateway_id=GATEWAY_ID)              # unknown reference, source -> G1
        self.assertEqual(state.release_waiting(NOW), 0)
        state.close()
        # a restart with the same source name mapped to ANOTHER gateway: a new source identity
        state = State(self.path)
        self.addCleanup(state.close)
        self.current = state
        new_source = state.ensure_source(GATEWAY_ALIAS, OTHER_GATEWAY_ID, NOW + 10)
        self.assertEqual(new_source, 2)
        # the binding arrives, expecting the NEW gateway
        state.copy_bindings([binding(one, OTHER_GATEWAY_ID)], NOW + 20)
        self.assertEqual(state.release_waiting(NOW + 30), 0)
        self.assertEqual(self.states(), [(1, "quarantined", "wrong_gateway")])      # never released under the new mapping
        self.assertEqual(self.raw("SELECT COUNT(*) FROM receipt_delivery"), [(0,)])
        self.assertEqual(self.raw("SELECT gateway_id FROM receipt_capture"), [(GATEWAY_ID,)])   # the frozen gateway

    def test_a_binding_for_the_frozen_gateway_still_releases_after_the_mapping_changed(self):
        state = self.open()
        one = lease()
        self.capture(state, [receipt(one, 1)], gateway_id=GATEWAY_ID)
        state.ensure_source(GATEWAY_ALIAS, OTHER_GATEWAY_ID, NOW + 10)
        state.copy_bindings([binding(one, GATEWAY_ID)], NOW + 20)
        self.assertEqual(state.release_waiting(NOW + 30), 1)
        self.assertEqual(self.raw("SELECT gateway_id FROM receipt_delivery"), [(GATEWAY_ID,)])

    def test_the_new_source_starts_its_own_cursors_and_captures_alongside_the_old(self):
        state = self.open()
        old = self.source(state)
        self.capture(state, [receipt(lease(), 1)])
        new = state.ensure_source(GATEWAY_ALIAS, OTHER_GATEWAY_ID, NOW + 10)
        self.assertEqual(state.current_cursor(new), (None, 0))
        state.capture_page(new, OTHER_GATEWAY_ID, parsed([receipt(lease(), 1)]), NOW + 11)
        self.assertEqual(self.raw("SELECT source_id, gateway_id, seq FROM receipt_capture ORDER BY id"),
                         [(old, GATEWAY_ID, 1), (new, OTHER_GATEWAY_ID, 1)])


class Offer(_Tmp):

    def released(self, state, count=2):
        leases = [lease() for _ in range(count)]
        state.copy_bindings([binding(l) for l in leases], NOW)
        self.capture(state, [receipt(l, n + 1) for n, l in enumerate(leases)])
        state.release_waiting(NOW)
        return leases

    def test_the_oldest_due_pending_delivery_is_offered_with_its_own_path_key_and_kind(self):
        state = self.open()
        a, _b = self.released(state)
        item = state.next_receipt_item(NOW)
        self.assertEqual((item.kind, item.path, item.lease_id), (decisions.RECEIPT, "/v1/leases/%s/settle" % a, a))
        self.assertEqual(item.key, item.body["event_id"])
        self.assertEqual(item.body["seq"], 1)
        self.assertEqual((item.agent, item.message_id), (state_mod.RECEIPT_AGENT, ""))
        self.assertEqual(item.ref, ("receipt_state", 1))

    def test_the_item_body_is_the_stored_delivery_body_unchanged(self):
        state = self.open()
        self.released(state, 1)
        item = state.next_receipt_item(NOW)
        stored = self.raw("SELECT body FROM receipt_delivery")[0][0]
        self.assertEqual(json.dumps(item.body, sort_keys=True, separators=(",", ":")), stored)

    def test_answers_move_only_the_state_row_and_a_refused_row_never_stops_the_next(self):
        state = self.open()
        self.released(state, 3)
        first = state.next_receipt_item(NOW)
        state.apply(first, decisions.Response(None), NOW, 0.5)                    # no answer: back off, same bytes
        self.assertEqual(state.next_receipt_item(NOW).body["seq"], 2)             # the first waits out its back-off
        second = state.next_receipt_item(NOW)
        state.apply(second, decisions.Response(410, "LEASE_CLOSED"), NOW)         # NOT a "done" word
        self.assertEqual(self.states(), [(1, "pending", None), (2, "failed_permanent", None), (3, "pending", None)])
        self.assertEqual(self.raw("SELECT last_error_code FROM receipt_state WHERE capture_id=2"), [("LEASE_CLOSED",)])
        self.assertEqual(state.next_receipt_item(NOW).body["seq"], 3)             # the next row still goes
        state.apply(state.next_receipt_item(NOW), decisions.Response(200), NOW)
        self.assertEqual(state.next_receipt_item(NOW + 10 ** 7).body["seq"], 1)
        # a permanent failure never erases custody: the capture, the delivery and the binding are all still there
        self.assertEqual(self.raw("SELECT COUNT(*) FROM receipt_capture"), [(3,)])
        self.assertEqual(self.raw("SELECT COUNT(*) FROM receipt_delivery"), [(3,)])
        self.assertEqual(self.raw("SELECT COUNT(*) FROM receipt_binding"), [(3,)])

    def test_a_quarantined_capture_is_never_offered(self):
        state = self.open()
        self.capture(state, [receipt(lease(), 1)])
        state.release_waiting(NOW)
        self.assertIsNone(state.next_receipt_item(NOW + 10 ** 9))


if __name__ == "__main__":
    unittest.main()
