"""Card L6a-1, step 3: the reporter's durable outbox for gateway receipts (state version 3).

A receipt is stored once, with its settle body frozen, BEFORE anything is sent; retries resend the
stored bytes. The last `seq` is stored in the SAME transaction as the new rows, so it moves only with
rows that are really stored. The file moves from version 1 or 2 to version 3 in one transaction, and a
reporter that knows only version 2 refuses a version-3 file and never opens or changes it. Everything
here is synthetic: generated ids and made-up figures.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from unittest import mock

from quota_balancer.reporter import decisions, mapper, receipts
from quota_balancer.reporter import state as state_mod
from quota_balancer.reporter.state import State, StateError

from tests.l6a1_env import GATEWAY_ALIAS, GATEWAY_ID, GENERATION, lease, receipt

NOW = 1_788_224_400_000


def parsed(rows, after=0):
    """Receipts as the adapter returns them (so a test cannot store a shape the adapter would refuse)."""
    return receipts.parse(json.dumps(rows).encode(), after)


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

    def rows(self):
        return self.raw("SELECT id, event_id, gateway_alias, generation, seq, ref_sha256, lease_id, body, state,"
                        " tries, not_before_ms, last_error_code, stored_ms FROM gateway_receipt ORDER BY id")

    def store(self, state, leases, rows, now=NOW):
        """Make the leases known (the way an acquired lease is), then store the receipts."""
        with state.txn():
            for lease_id in leases:
                state.remember_lease(lease_id)
        return state.record_receipts(GATEWAY_ALIAS, GATEWAY_ID, parsed(rows), now)


def _version_2(test):
    """The code of card 7c-2: version 2, no receipt tables."""
    original = (state_mod.SCHEMA_VERSION, state_mod._DDL)
    state_mod.SCHEMA_VERSION, state_mod._DDL = 2, state_mod._DDL_V1 + state_mod._LIMIT_DDL
    test.addCleanup(lambda: setattr(state_mod, "SCHEMA_VERSION", original[0]))
    test.addCleanup(lambda: setattr(state_mod, "_DDL", original[1]))
    return original


class Schema(_Tmp):

    def test_a_fresh_file_has_the_receipt_tables_and_triggers(self):
        state = self.open()
        self.assertEqual(state.one("PRAGMA user_version")[0], 3)
        columns = [r[1] for r in state.all("PRAGMA table_info(gateway_receipt)")]
        self.assertEqual(columns, ["id", "event_id", "gateway_alias", "generation", "seq", "ref_sha256",
                                   "lease_id", "body", "state", "tries", "not_before_ms", "last_error_code",
                                   "stored_ms"])
        self.assertEqual([r[1] for r in state.all("PRAGMA table_info(lease_ref)")], ["ref_sha256", "lease_id"])
        triggers = {r[0] for r in state.all("SELECT name FROM sqlite_master WHERE type='trigger'")}
        self.assertTrue({"gateway_receipt_immutable", "gateway_receipt_keep"} <= triggers)

    def _make_old(self, version):
        """A real older file, with lease rows (and, for version 2, a limit event) in it."""
        original = (state_mod.SCHEMA_VERSION, state_mod._DDL)
        state_mod.SCHEMA_VERSION = version
        state_mod._DDL = state_mod._DDL_V1 + (state_mod._LIMIT_DDL if version == 2 else ())
        try:
            old = State(self.path)
            with old.txn():
                old.conn.execute(
                    "INSERT INTO acquisition(agent, message_id, turn_uuid, request_id, body_original, body_current,"
                    " state, lease_id) VALUES('alpha','m1','t1','r1','{}','{}','acquired',?)", (self.leased,))
                old.conn.execute(
                    "INSERT INTO acquisition(agent, message_id, turn_uuid, request_id, body_original, body_current)"
                    " VALUES('alpha','m2','t2','r2','{}','{}')")
                old.conn.execute("INSERT INTO counter(agent, name, value) VALUES('alpha','reports_dropped',3)")
                old.set_note("last_ok:alpha", None, NOW)
                if version == 2:
                    old.conn.execute(
                        "INSERT INTO limit_event(event_id, account_alias, body, captured_ms) VALUES(?,?,?,?)",
                        (str(uuid.uuid4()), "claude-main", "{}", NOW))
            old.close()
        finally:
            state_mod.SCHEMA_VERSION, state_mod._DDL = original

    def setUp(self):
        super().setUp()
        self.leased = lease()

    def snapshot(self):
        return [self.raw("SELECT * FROM %s ORDER BY 1" % t)
                for t in ("acquisition", "counter", "note", "meta", "stream", "event", "dispatch", "disposal",
                          "report", "final", "late", "message_status", "agent_state", "limit_event")
                if self.raw("SELECT name FROM sqlite_master WHERE name=?", (t,))]

    def test_a_version_2_file_migrates_with_every_old_row_untouched_and_the_lease_references_kept(self):
        self._make_old(2)
        before = self.snapshot()
        self.assertEqual(self.raw("PRAGMA user_version"), [(2,)])
        self.assertEqual(self.raw("SELECT name FROM sqlite_master WHERE name='gateway_receipt'"), [])
        state = self.open()
        self.assertEqual(state.one("PRAGMA user_version")[0], 3)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.rows(), [])
        # the one acquired lease can be named by a receipt; the one that was never acquired cannot
        self.assertEqual(self.raw("SELECT ref_sha256, lease_id FROM lease_ref"),
                         [(mapper.lease_ref_hash(self.leased), self.leased)])

    def test_a_version_1_file_migrates_straight_to_version_3(self):
        self._make_old(1)
        before = self.snapshot()
        state = self.open()
        self.assertEqual(state.one("PRAGMA user_version")[0], 3)
        self.assertEqual(self.snapshot()[:len(before)], before)
        self.assertEqual(len(self.raw("SELECT name FROM sqlite_master WHERE name IN ('limit_event',"
                                      "'gateway_receipt','lease_ref')")), 3)
        self.assertEqual(len(self.raw("SELECT * FROM lease_ref")), 1)

    def test_a_second_open_is_a_no_op(self):
        self._make_old(2)
        State(self.path).close()
        schema = self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name")
        before = self.snapshot() + [self.raw("SELECT * FROM lease_ref")]
        State(self.path).close()
        self.assertEqual(self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name"), schema)
        self.assertEqual(self.snapshot() + [self.raw("SELECT * FROM lease_ref")], before)
        self.assertEqual(self.raw("PRAGMA user_version"), [(3,)])

    def test_a_migration_that_fails_leaves_the_file_at_version_2_whole(self):
        self._make_old(2)
        before = self.snapshot()
        schema = self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name")
        original = state_mod._RECEIPT_DDL
        state_mod._RECEIPT_DDL = original + ("THIS IS NOT SQL",)
        try:
            with self.assertRaises(StateError):
                State(self.path)
        finally:
            state_mod._RECEIPT_DDL = original
        self.assertEqual(self.raw("PRAGMA user_version"), [(2,)])
        self.assertEqual(self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name"), schema)
        self.assertEqual(self.snapshot(), before)
        self.open()                                           # and the good code still migrates it
        self.assertEqual(self.raw("PRAGMA user_version"), [(3,)])

    def _fingerprint(self):
        main = open(self.path, "rb")
        try:
            digest = hashlib.sha256(main.read()).hexdigest()
        finally:
            main.close()
        return digest, self.raw("PRAGMA user_version"), self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name")

    def test_a_reporter_that_knows_only_version_2_refuses_a_version_3_file_and_changes_nothing(self):
        State(self.path).close()                              # a version-3 file
        before = self._fingerprint()
        _version_2(self)                                      # the code before this card
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


class Rows(_Tmp):

    def test_a_receipt_is_stored_pending_with_its_frozen_canonical_body(self):
        state = self.open()
        lease_id = lease()
        row = receipt(lease_id, 7, outcome="provider_limit", calls=3, input_tokens=10, output_tokens=4,
                      actual_micro_eur=99)
        self.assertEqual(self.store(state, [lease_id], [row]), 1)
        (stored,) = self.rows()
        body = json.loads(stored[7])
        self.assertEqual(body, {
            "event_id": mapper.receipt_event_id(mapper.lease_ref_hash(lease_id), GENERATION),
            "outcome": "provider_limit", "calls": 3, "input_tokens": 10, "cached_input_tokens": 0,
            "output_tokens": 4, "actual_micro_eur": 99, "gateway_id": GATEWAY_ID, "seq": 7, "final": True})
        self.assertEqual(set(body), mapper.RECEIPT_BODY_KEYS)
        self.assertEqual(stored[7], json.dumps(body, sort_keys=True, separators=(",", ":")))   # the wire bytes
        self.assertEqual(stored[1], body["event_id"])
        self.assertEqual((stored[2], stored[3], stored[4], stored[5], stored[6]),
                         (GATEWAY_ALIAS, GENERATION, 7, mapper.lease_ref_hash(lease_id), lease_id))
        self.assertEqual(stored[8:12], ("pending", 0, 0, None))
        self.assertEqual(stored[12], NOW)

    def test_the_event_id_comes_from_the_reference_and_the_generation_only(self):
        one = lease()
        ref = mapper.lease_ref_hash(one)
        a = mapper.receipt_event_id(ref, GENERATION)
        self.assertEqual(a, mapper.receipt_event_id(ref, GENERATION))
        self.assertNotEqual(a, mapper.receipt_event_id(ref, "f" * 32))                    # a new ledger generation
        self.assertNotEqual(a, mapper.receipt_event_id(mapper.lease_ref_hash(lease()), GENERATION))
        uuid.UUID(a)
        # not from the seq, a time or the figures: two receipts of one reference and generation agree
        body_a = mapper.receipt_body(GATEWAY_ID, parsed([receipt(one, 3)])[0])
        body_b = mapper.receipt_body(GATEWAY_ID, parsed([receipt(one, 9, calls=5)])[0])
        self.assertEqual(body_a["event_id"], body_b["event_id"])

    def test_the_same_receipt_twice_is_one_row_and_the_first_bytes_stay(self):
        state = self.open()
        lease_id = lease()
        first = receipt(lease_id, 1)
        self.assertEqual(self.store(state, [lease_id], [first]), 1)
        before = self.rows()
        self.assertEqual(self.store(state, [], [first], now=NOW + 5), 0)
        # the same reference and generation under another seq and other figures: still the first row only
        self.assertEqual(self.store(state, [], [receipt(lease_id, 2, calls=9, actual_micro_eur=1)], now=NOW + 9), 0)
        self.assertEqual(self.rows(), before)

    def test_the_last_seq_moves_with_the_stored_rows_and_never_backwards(self):
        state = self.open()
        a, b = lease(), lease()
        self.assertEqual(state.receipt_cursor(GATEWAY_ALIAS), 0)
        self.store(state, [a, b], [receipt(a, 3), receipt(b, 8)])
        self.assertEqual(state.receipt_cursor(GATEWAY_ALIAS), 8)
        self.store(state, [], [receipt(a, 3)])                         # read again: nothing new
        self.assertEqual(state.receipt_cursor(GATEWAY_ALIAS), 8)
        state.record_receipts(GATEWAY_ALIAS, GATEWAY_ID, (), NOW)      # an empty read moves nothing
        self.assertEqual(state.receipt_cursor(GATEWAY_ALIAS), 8)
        state.record_receipts(GATEWAY_ALIAS, GATEWAY_ID, parsed([receipt(a, 2)]), NOW)
        self.assertEqual(state.receipt_cursor(GATEWAY_ALIAS), 8)
        self.assertEqual(state.receipt_cursor("another-gateway"), 0)    # one cursor per gateway alias

    def test_a_failure_in_the_middle_stores_nothing_and_moves_nothing(self):
        state = self.open()
        a, b = lease(), lease()
        with state.txn():
            state.remember_lease(a)
            state.remember_lease(b)
        real = mapper.receipt_body_text
        calls = []

        def second_fails(body):
            calls.append(1)
            if len(calls) == 2:
                raise ValueError("shape")
            return real(body)
        with mock.patch.object(mapper, "receipt_body_text", second_fails):
            with self.assertRaises(StateError) as caught:
                state.record_receipts(GATEWAY_ALIAS, GATEWAY_ID, parsed([receipt(a, 1), receipt(b, 2)]), NOW)
        self.assertEqual(str(caught.exception), "a receipt body is not valid")
        self.assertEqual(self.rows(), [])
        self.assertEqual(state.receipt_cursor(GATEWAY_ALIAS), 0)
        self.assertEqual(state.counter(state_mod.RECEIPT_AGENT, "stored"), 0)
        self.assertIsNone(state.note("receipt_last_read"))
        self.assertEqual(self.store(state, [], [receipt(a, 1), receipt(b, 2)]), 2)        # the same read works later
        self.assertEqual(state.receipt_cursor(GATEWAY_ALIAS), 2)

    def test_a_reference_that_matches_no_lease_is_stored_failed_and_its_seq_is_still_passed(self):
        state = self.open()
        known = lease()
        self.store(state, [known], [receipt(known, 1), receipt(lease(), 2)])
        rows = self.rows()
        self.assertEqual([(r[4], r[6], r[8], r[11]) for r in rows],
                         [(1, known, "pending", None), (2, None, "failed_permanent", decisions.UNKNOWN_LEASE)])
        self.assertEqual(state.receipt_cursor(GATEWAY_ALIAS), 2)
        item = state.next_receipt_item(NOW)
        self.assertEqual(item.lease_id, known)
        state.apply(item, decisions.Response(200), NOW)
        self.assertIsNone(state.next_receipt_item(NOW))

    def test_a_pending_row_cannot_lack_a_lease(self):
        state = self.open()
        with self.assertRaises(sqlite3.IntegrityError):
            with state.txn():
                state.conn.execute(
                    "INSERT INTO gateway_receipt(event_id, gateway_alias, generation, seq, ref_sha256, lease_id, body,"
                    " stored_ms) VALUES(?,?,?,?,?,NULL,'{}',?)",
                    (str(uuid.uuid4()), GATEWAY_ALIAS, GENERATION, 1, "a" * 64, NOW))

    def test_identity_and_body_cannot_be_changed_or_deleted_but_delivery_columns_can(self):
        state = self.open()
        lease_id = lease()
        self.store(state, [lease_id], [receipt(lease_id, 1)])
        for column, value in (("event_id", "x"), ("gateway_alias", "x"), ("generation", "x"), ("seq", 99),
                              ("ref_sha256", "x"), ("lease_id", str(uuid.uuid4())), ("body", "{}"),
                              ("stored_ms", 1)):
            with self.assertRaises(sqlite3.DatabaseError, msg=column):
                with state.txn():
                    state.conn.execute("UPDATE gateway_receipt SET %s=?" % column, (value,))
        with self.assertRaises(sqlite3.DatabaseError):
            with state.txn():
                state.conn.execute("DELETE FROM gateway_receipt")
        with state.txn():
            state.conn.execute("UPDATE gateway_receipt SET state='sent', tries=2, not_before_ms=5, last_error_code='X'")
        self.assertEqual(self.rows()[0][8:12], ("sent", 2, 5, "X"))

    def test_a_restart_in_the_middle_loses_and_repeats_nothing(self):
        state = self.open()
        a, b, c = lease(), lease(), lease()
        self.store(state, [a, b, c], [receipt(a, 1), receipt(b, 2)])
        state.close()
        again = self.open()
        self.assertEqual(again.receipt_cursor(GATEWAY_ALIAS), 2)
        self.assertEqual(len(self.rows()), 2)
        # the source is asked for what is after seq 2: only the third is new; the first two read again are ignored
        self.assertEqual(again.record_receipts(GATEWAY_ALIAS, GATEWAY_ID,
                                               parsed([receipt(a, 1), receipt(b, 2), receipt(c, 3)]), NOW + 1), 1)
        self.assertEqual([r[4] for r in self.rows()], [1, 2, 3])
        self.assertEqual(again.receipt_cursor(GATEWAY_ALIAS), 3)

    def test_a_read_that_stored_nothing_is_counted_by_its_closed_word_only(self):
        state = self.open()
        state.note_receipt_read(receipts.READ_LATER, "timeout", NOW)
        state.note_receipt_read(receipts.READ_LATER, "timeout", NOW + 1)
        state.note_receipt_read(receipts.REFUSED, "bad_row", NOW + 2)
        agent = state_mod.RECEIPT_AGENT
        self.assertEqual((state.counter(agent, "read_later:timeout"), state.counter(agent, "refused:bad_row")), (2, 1))
        self.assertEqual(state.note("receipt_last_read"), ("refused", NOW + 2))
        self.assertEqual(self.rows(), [])


class Offer(_Tmp):

    def test_the_oldest_due_row_is_offered_with_its_own_path_key_and_kind(self):
        state = self.open()
        a, b = lease(), lease()
        self.store(state, [a, b], [receipt(a, 1), receipt(b, 2)])
        item = state.next_receipt_item(NOW)
        self.assertEqual((item.kind, item.path, item.lease_id), (decisions.RECEIPT, "/v1/leases/%s/settle" % a, a))
        self.assertEqual(item.key, item.body["event_id"])
        self.assertEqual(item.body["seq"], 1)
        self.assertEqual((item.agent, item.message_id), (state_mod.RECEIPT_AGENT, ""))
        self.assertEqual(item.ref[0], "gateway_receipt")

    def test_a_row_that_is_not_due_is_skipped_and_a_sent_one_is_not_offered(self):
        state = self.open()
        a, b = lease(), lease()
        self.store(state, [a, b], [receipt(a, 1), receipt(b, 2)])
        first = state.next_receipt_item(NOW)
        state.apply(first, decisions.Response(None), NOW, 0.5)                 # no answer: back off
        second = state.next_receipt_item(NOW)
        self.assertEqual(second.body["seq"], 2)                               # the first waits out its back-off
        state.apply(second, decisions.Response(200), NOW)
        self.assertIsNone(state.next_receipt_item(NOW))
        self.assertEqual(state.next_receipt_item(NOW + 10**7).body["seq"], 1)

    def test_an_acquired_lease_is_remembered_by_its_reference(self):
        state = self.open()
        lease_id = lease()
        with state.txn():
            state.conn.execute(
                "INSERT INTO acquisition(agent, message_id, turn_uuid, request_id, body_original, body_current)"
                " VALUES('alpha','m1','t1','r1','{}','{}')")
        item = state.next_item(NOW)
        state.apply(item, decisions.Response(201, None, lease_id), NOW)
        self.assertEqual(state.all("SELECT ref_sha256, lease_id FROM lease_ref"),
                         [(mapper.lease_ref_hash(lease_id), lease_id)])
        self.assertEqual(self.store(state, [], [receipt(lease_id, 1)]), 1)
        self.assertEqual(self.rows()[0][6], lease_id)


if __name__ == "__main__":
    unittest.main()
