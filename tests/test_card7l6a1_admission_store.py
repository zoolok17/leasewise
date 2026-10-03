"""Card L6a-1 (revised), step 1: the admission store's layout and the reporter's read-only reader.

`reporter admit` (card L6a-2) will write the admission store; the running reporter only reads it, to learn which
lease a receipt belongs to. The layout lives in ONE module (`reporter/admission_store.py`) that the writer will reuse
and these tests build from. The reader opens the file read-only and a missing, busy or unusable file means "read
again later": it never raises and never writes. Synthetic data only.
"""

import hashlib
import os
import sqlite3
import tempfile
import unittest

from quota_balancer.reporter import admission_store as store

from tests.l6a1_env import GATEWAY_ID, OTHER_GATEWAY_ID, add_binding, grant, lease, make_store


class _Dir(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(dir=os.environ.get("AGENTTALK_SCRATCH") or None,
                                                prefix="admission-l6a1-")
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "admission.db")


class Layout(_Dir):

    def raw(self, sql, params=()):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def test_the_layout_is_one_module_with_a_version(self):
        make_store(self.path)
        self.assertEqual(self.raw("PRAGMA user_version"), [(store.SCHEMA_VERSION,)])
        tables = {r[0] for r in self.raw("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"admission", "admission_attempt"} <= tables)
        columns = [r[1] for r in self.raw("PRAGMA table_info(admission)")]
        self.assertEqual(columns, ["id", "agent", "message_id", "request_id", "body_current", "rebuilds", "state",
                                   "last_error_code", "not_before_ms", "last_pending_ms", "return_seq",
                                   "replay_tried", "grant_seq", "lease_id", "gateway_id",
                                   "reference", "ref_sha256", "max_calls", "max_micro_eur", "ttl_seconds",
                                   "expires_ms", "closed_ms"])

    def test_a_granted_row_needs_its_whole_grant_and_a_requested_one_needs_none(self):
        make_store(self.path)
        conn = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO admission(agent, message_id, request_id, body_current) VALUES('a','m','r','{}')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO admission(agent, message_id, request_id, body_current, state) "
                         "VALUES('a','m2','r2','{}','granted')")

    def test_identity_and_a_grant_cannot_change_and_nothing_is_deleted(self):
        one = lease()
        make_store(self.path, [one])
        conn = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(conn.close)
        for column, value in (("agent", "x"), ("message_id", "x"), ("request_id", "x"), ("lease_id", lease()),
                              ("gateway_id", OTHER_GATEWAY_ID), ("reference", "x"), ("ref_sha256", "0" * 64),
                              ("ttl_seconds", 1), ("expires_ms", 1)):
            with self.assertRaises(sqlite3.DatabaseError, msg=column):
                conn.execute("UPDATE admission SET %s=?" % column, (value,))
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM admission")
        conn.execute("UPDATE admission SET closed_ms=5, last_pending_ms=6, not_before_ms=7")     # the mutable ones
        conn.execute("INSERT INTO admission_attempt(admission_id, body, sent_ms) VALUES(1,'{}',1)")
        for sql in ("UPDATE admission_attempt SET body='x'", "DELETE FROM admission_attempt"):
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute(sql)

    def test_a_reference_names_one_admission_and_a_message_one_admission(self):
        one = lease()
        make_store(self.path, [one])
        conn = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.IntegrityError):
            grant(conn, {"lease_id": one, "message_id": "other"})                     # the same reference again
        first = self.raw("SELECT agent, message_id FROM admission")[0]
        with self.assertRaises(sqlite3.IntegrityError):
            grant(conn, {"lease_id": lease(), "agent": first[0], "message_id": first[1]})   # the same message


class Reader(_Dir):

    def test_the_granted_rows_come_back_and_requested_ones_do_not(self):
        one, two = lease(), lease()
        make_store(self.path, [one, {"lease_id": two, "gateway_id": OTHER_GATEWAY_ID, "agent": "beta"}])
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("INSERT INTO admission(agent, message_id, request_id, body_current) VALUES('a','pending','r','{}')")
        conn.close()
        status, found = store.read_bindings(self.path)
        self.assertEqual(status, store.OK)
        self.assertEqual([(b.lease_id, b.gateway_id, b.agent) for b in found],
                         [(one, GATEWAY_ID, "alpha"), (two, OTHER_GATEWAY_ID, "beta")])
        self.assertEqual(len(found[0].ref_sha256), 64)
        self.assertNotIn(one, repr(found[0]) + str(found[0]))

    def test_a_row_whose_reference_or_hash_does_not_belong_to_its_lease_is_refused_as_a_whole_batch(self):
        """F3: lease A's reference and hash on lease B's row (the case that could send A's receipt to B)."""
        one, other, good = lease(), lease(), lease()
        make_store(self.path, [good])
        conn = sqlite3.connect(self.path, isolation_level=None)
        grant(conn, {"lease_id": other, "reference": one, "ref_sha256": hashlib.sha256(one.encode()).hexdigest()}, 5)
        conn.close()
        self.assertEqual(store.read_bindings(self.path), (store.LATER, store.STORE_UNREADABLE))      # copies nothing
        for field, value in (("reference", lease()), ("ref_sha256", hashlib.sha256(b"x").hexdigest())):
            with self.subTest(field=field):
                path = make_store(os.path.join(self._dir.name, field + ".db"), [good])
                conn = sqlite3.connect(path, isolation_level=None)
                grant(conn, dict({"lease_id": lease()}, **{field: value}), 6)
                conn.close()
                self.assertEqual(store.read_bindings(path), (store.LATER, store.STORE_UNREADABLE))

    def test_grants_are_read_in_bounded_batches_after_the_last_number_taken(self):
        """D-127 F7: no history size can stop the reader; nothing is read twice."""
        leases = [lease() for _ in range(7)]
        make_store(self.path, leases)
        taken, after = [], 0
        while True:
            status, found = store.read_bindings(self.path, after, 3)
            self.assertEqual(status, store.OK)
            taken += [one.lease_id for one in found]
            if len(found) < 3:
                break
            after = found[-1].grant_seq
        self.assertEqual(taken, leases)                                              # all of them, once, in order
        self.assertEqual(store.read_bindings(self.path, after + 100, 3), (store.OK, []))

    def test_a_grant_saved_late_with_a_lower_row_id_but_a_higher_number_is_still_found(self):
        early, late = lease(), lease()
        make_store(self.path, [early])
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("INSERT INTO admission(agent, message_id, request_id, body_current) VALUES('alpha','slow','r','{}')")
        slow_id = conn.execute("SELECT id FROM admission WHERE message_id='slow'").fetchone()[0]       # id 2, not granted yet
        grant(conn, late, 3)                                                                           # id 3, granted first
        conn.close()
        status, found = store.read_bindings(self.path, 0, 10)
        self.assertEqual([one.lease_id for one in found], [early, late])
        after = found[-1].grant_seq
        conn = sqlite3.connect(self.path, isolation_level=None)                                        # now id 2 is granted
        slow_lease = lease()
        conn.execute("UPDATE admission_meta SET value=value+1 WHERE name='grant_seq'")
        conn.execute("UPDATE admission SET state='granted', lease_id=?, gateway_id=?, reference=?, ref_sha256=?,"
                     " ttl_seconds=1, expires_ms=1, grant_seq=(SELECT value FROM admission_meta WHERE name='grant_seq')"
                     " WHERE id=?", (slow_lease, GATEWAY_ID, slow_lease, hashlib.sha256(slow_lease.encode()).hexdigest(),
                                     slow_id))
        conn.close()
        status, found = store.read_bindings(self.path, after, 10)
        self.assertEqual((status, [one.lease_id for one in found]), (store.OK, [slow_lease]))          # lower id, higher number

    def test_a_mismatching_row_refuses_only_its_own_batch(self):
        good, other, bad_for, bad_lease = lease(), lease(), lease(), lease()
        make_store(self.path, [good, other])
        conn = sqlite3.connect(self.path, isolation_level=None)
        grant(conn, {"lease_id": bad_lease, "reference": bad_for, "ref_sha256": hashlib.sha256(bad_for.encode()).hexdigest()}, 9)
        conn.close()
        self.assertEqual(store.read_bindings(self.path, 0, 3), (store.LATER, store.STORE_UNREADABLE))    # the batch with it
        self.assertEqual(len(store.read_bindings(self.path, 0, 2)[1]), 2)                               # a batch before it

    def test_an_empty_store_is_ok_with_no_bindings(self):
        make_store(self.path)
        self.assertEqual(store.read_bindings(self.path), (store.OK, []))

    def test_a_missing_store_is_read_later(self):
        self.assertEqual(store.read_bindings(self.path), (store.LATER, store.STORE_MISSING))
        self.assertEqual(store.read_bindings(self._dir.name), (store.LATER, store.STORE_MISSING))     # a folder

    def test_a_store_that_is_not_this_layout_is_read_later(self):
        with open(self.path, "wb") as handle:
            handle.write(b"this is not a database at all")
        self.assertEqual(store.read_bindings(self.path), (store.LATER, store.STORE_UNREADABLE))
        make_store(os.path.join(self._dir.name, "v.db"))
        conn = sqlite3.connect(os.path.join(self._dir.name, "v.db"))
        conn.execute("PRAGMA user_version=%d" % (store.SCHEMA_VERSION + 1))
        conn.commit()
        conn.close()
        self.assertEqual(store.read_bindings(os.path.join(self._dir.name, "v.db")),
                         (store.LATER, store.STORE_UNREADABLE))

    def test_a_busy_store_is_read_later_and_never_an_error(self):
        make_store(self.path, [lease()])                                          # the default journal mode
        writer = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN EXCLUSIVE")                                         # a writer holding the file
        try:
            self.assertEqual(store.read_bindings(self.path), (store.LATER, store.STORE_BUSY))
        finally:
            writer.execute("ROLLBACK")
        self.assertEqual(store.read_bindings(self.path)[0], store.OK)

    def test_a_writer_in_the_middle_of_a_transaction_shows_only_what_was_committed(self):
        first = lease()
        make_store(self.path, [first], wal=True)
        writer = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        grant(writer, lease(), 5)                                                 # not committed yet
        status, found = store.read_bindings(self.path)
        self.assertEqual((status, [b.lease_id for b in found]), (store.OK, [first]))
        writer.execute("COMMIT")
        self.assertEqual(len(store.read_bindings(self.path)[1]), 2)

    def bytes(self):
        with open(self.path, "rb") as handle:
            return handle.read()

    def test_reading_writes_nothing(self):
        make_store(self.path, [lease()])
        before = self.bytes()
        mtime = os.stat(self.path).st_mtime_ns
        for _ in range(3):
            self.assertEqual(store.read_bindings(self.path)[0], store.OK)
        self.assertEqual((self.bytes(), os.stat(self.path).st_mtime_ns), (before, mtime))

    def test_the_reader_cannot_write_even_by_accident(self):
        make_store(self.path)
        conn = sqlite3.connect(store._uri(self.path) + "?mode=ro", uri=True)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("INSERT INTO admission_attempt(admission_id, body, sent_ms) VALUES(1,'{}',1)")

    def test_a_damaged_row_means_read_later_and_nothing_is_half_used(self):
        make_store(self.path, [lease()])
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("DROP TRIGGER admission_grant_once")
        conn.execute("UPDATE admission SET ref_sha256='nothex'")
        conn.close()
        self.assertEqual(store.read_bindings(self.path), (store.LATER, store.STORE_UNREADABLE))

    def test_a_path_with_odd_characters_is_read(self):
        odd = os.path.join(self._dir.name, "a b#c%d")
        os.makedirs(odd)
        path = os.path.join(odd, "admission.db")
        one = lease()
        make_store(path, [one])
        self.assertEqual([b.lease_id for b in store.read_bindings(path)[1]], [one])

    def test_a_late_binding_is_seen_by_the_next_read(self):
        make_store(self.path)
        self.assertEqual(store.read_bindings(self.path)[1], [])
        one = lease()
        add_binding(self.path, one)
        self.assertEqual([b.lease_id for b in store.read_bindings(self.path)[1]], [one])


if __name__ == "__main__":
    unittest.main()
