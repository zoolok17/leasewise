"""Card L6a-2, step 1: the admission store's writer (DESIGN-6A 8.6): layout and version, WAL and full synchronous
writes, short transactions, the immutability triggers, "request saved before it is sent", "grant saved once", the
durable counter for the order of `pending`, and the reader of card L6a-1 seeing exactly what was committed.
"""

import hashlib
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from quota_balancer.reporter import admission_store as store
from quota_balancer.reporter import mapper


class _Dir(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH") or None
        self._dir = tempfile.TemporaryDirectory(dir=base, prefix="admission-w-")
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "nested", "admission.db")

    def writer(self):
        one = store.Writer(self.path)
        self.addCleanup(one.close)
        return one


def grant(lease="11111111-2222-4333-8444-555555555555", ttl=1800, expires=9_999_999_999_999):
    return {"lease_id": lease, "gateway_id": "7b1e3c52-0d4a-4a8e-9c61-5f2a8d3b9e01",
            "ref_sha256": hashlib.sha256(lease.encode("ascii")).hexdigest(), "max_calls": None,
            "max_micro_eur": None, "ttl_seconds": ttl, "expires_ms": expires}


class Open(_Dir):

    def test_the_first_open_creates_the_folder_and_the_layout_with_wal_and_full_sync(self):
        one = self.writer()
        self.assertEqual(one.conn.execute("PRAGMA user_version").fetchone()[0], store.SCHEMA_VERSION)
        self.assertEqual(one.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(one.conn.execute("PRAGMA synchronous").fetchone()[0], 2)          # FULL
        self.assertEqual(one.conn.execute("PRAGMA busy_timeout").fetchone()[0], store.BUSY_TIMEOUT_MS)
        self.assertLessEqual(store.BUSY_TIMEOUT_MS, 5000)                                   # short
        tables = {row[0] for row in one.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"admission", "admission_attempt", "admission_meta"} <= tables)
        self.assertEqual(one.conn.execute("SELECT value FROM admission_meta WHERE name='return_seq'").fetchone(), (0,))

    def test_a_second_open_changes_nothing_and_a_foreign_version_is_refused_untouched(self):
        self.writer().close()
        again = self.writer()
        self.assertEqual(again.conn.execute("SELECT COUNT(*) FROM admission_meta").fetchone()[0], 2)    # not made twice
        again.close()
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA user_version=99")
        conn.commit()
        conn.close()
        with self.assertRaises(store.StoreUnusable):
            store.Writer(self.path)
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 99)
        conn.close()

    def test_a_file_that_is_not_a_database_is_unusable_and_not_changed(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "wb") as handle:
            handle.write(b"this is not a database at all")
        with self.assertRaises(store.StoreUnusable):
            store.Writer(self.path)
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), b"this is not a database at all")

    def test_a_file_held_by_another_writer_is_busy_after_a_short_wait(self):
        self.writer().close()
        holder = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN EXCLUSIVE")
        with mock.patch.object(store, "BUSY_TIMEOUT_MS", 100):
            one = store.Writer(self.path)
            self.addCleanup(one.close)
            with self.assertRaises(store.StoreBusy):
                one.begin_request("alpha", "m1", "r", "{}", 1)


class Rows(_Dir):

    def test_a_request_is_saved_once_with_its_first_body(self):
        one = self.writer()
        row = one.begin_request("alpha", "m1", "req-1", '{"a":1}', 10)
        self.assertEqual((row["state"], row["body_current"], row["rebuilds"], row["replay_tried"]),
                         ("requested", '{"a":1}', 0, 0))
        again = one.begin_request("alpha", "m1", "req-OTHER", '{"b":2}', 20)          # a second call is a lookup
        self.assertEqual((again["id"], again["request_id"], again["body_current"]), (row["id"], "req-1", '{"a":1}'))
        self.assertEqual(one.attempts(row["id"]), ['{"a":1}'])

    def test_a_rebuild_saves_the_new_body_as_the_current_one_and_as_an_attempt(self):
        one = self.writer()
        row = one.begin_request("alpha", "m1", "req-1", "A", 10)
        one.rebuild(row["id"], "B", 20)
        one.rebuild(row["id"], "C", 30)
        now = one.lookup("alpha", "m1")
        self.assertEqual((now["body_current"], now["rebuilds"]), ("C", 2))
        self.assertEqual(one.attempts(row["id"]), ["C", "B", "A"])                       # newest first
        self.assertEqual(one.attempts(row["id"], 2), ["C", "B"])

    def test_the_grant_is_saved_once_and_a_second_save_returns_the_first(self):
        one = self.writer()
        row = one.begin_request("alpha", "m1", "req-1", "A", 10)
        first = one.save_grant(row["id"], grant())
        self.assertEqual((first["state"], first["reference"], first["lease_id"]),
                         ("granted", grant()["lease_id"], grant()["lease_id"]))
        second = one.save_grant(row["id"], grant(lease="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"))
        self.assertEqual(second["lease_id"], grant()["lease_id"])                         # never overwritten
        self.assertEqual(one.lookup("alpha", "m1")["lease_id"], grant()["lease_id"])

    def test_the_reference_hash_is_the_sha256_of_the_reference_text(self):
        lease = "11111111-2222-4333-8444-555555555555"
        self.assertEqual(mapper.lease_ref_hash(lease), hashlib.sha256(lease.encode("ascii")).hexdigest())

    def test_replay_progress_only_goes_up_and_an_error_note_can_restart_it(self):
        one = self.writer()
        row = one.begin_request("alpha", "m1", "req-1", "A", 10)
        one.rebuild(row["id"], "B", 20)
        one.mark_replayed(row["id"], 2)
        one.mark_replayed(row["id"], 1)
        self.assertEqual(one.lookup("alpha", "m1")["replay_tried"], 2)
        one.note_error(row["id"], "UNREACHABLE", 99, restart_recovery=False)
        now = one.lookup("alpha", "m1")
        self.assertEqual((now["last_error_code"], now["not_before_ms"], now["rebuilds"], now["replay_tried"]),
                         ("UNREACHABLE", 99, 1, 2))
        one.note_error(row["id"], "UNREACHABLE", 100, restart_recovery=True)
        now = one.lookup("alpha", "m1")
        self.assertEqual((now["rebuilds"], now["replay_tried"]), (0, 0))

    def test_a_failure_inside_a_transaction_changes_nothing(self):
        one = self.writer()
        one.begin_request("alpha", "m1", "req-1", "A", 10)
        with mock.patch.object(one, "lookup", side_effect=RuntimeError("fault")):
            with self.assertRaises(RuntimeError):
                one.begin_request("alpha", "m2", "r2", "X", 1)
        self.assertIsNone(one.conn.execute("SELECT 1 FROM admission WHERE message_id='m2'").fetchone())
        self.assertFalse(one.conn.in_transaction)

    def test_the_triggers_keep_the_key_the_grant_and_the_attempts(self):
        one = self.writer()
        row = one.begin_request("alpha", "m1", "req-1", "A", 10)
        one.save_grant(row["id"], grant())
        for sql in ("UPDATE admission SET agent='x'", "UPDATE admission SET message_id='x'",
                    "UPDATE admission SET request_id='x'", "UPDATE admission SET lease_id='x'",
                    "UPDATE admission SET ttl_seconds=1", "DELETE FROM admission",
                    "UPDATE admission_attempt SET body='x'", "DELETE FROM admission_attempt"):
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.DatabaseError):
                    one.conn.execute(sql)

    def test_the_durable_order_counter_and_column_exist_for_pending(self):
        one = self.writer()
        one.begin_request("alpha", "m1", "req-1", "A", 10)
        self.assertIsNone(one.lookup("alpha", "m1")["return_seq"])
        one.conn.execute("UPDATE admission_meta SET value=value+1 WHERE name='return_seq'")          # mutable by design
        one.conn.execute("UPDATE admission SET return_seq=1")
        self.assertEqual(one.lookup("alpha", "m1")["return_seq"], 1)


class ReaderSeesCommits(_Dir):

    def test_the_reporters_reader_sees_a_saved_grant_and_nothing_else(self):
        one = self.writer()
        pending = one.begin_request("alpha", "m1", "req-1", "A", 10)
        self.assertEqual(store.read_bindings(self.path), (store.OK, []))                 # a request is not a binding
        one.save_grant(pending["id"], grant())
        status, found = store.read_bindings(self.path)
        self.assertEqual(status, store.OK)
        self.assertEqual([(b.lease_id, b.ref_sha256, b.agent, b.message_id) for b in found],
                         [(grant()["lease_id"], grant()["ref_sha256"], "alpha", "m1")])

    def test_the_reader_works_while_the_writer_is_in_the_middle_of_a_transaction(self):
        one = self.writer()
        row = one.begin_request("alpha", "m1", "req-1", "A", 10)
        one.save_grant(row["id"], grant())
        other = one.begin_request("alpha", "m2", "req-2", "A", 10)
        one.conn.execute("BEGIN IMMEDIATE")
        second = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        one.conn.execute("UPDATE admission_meta SET value=value+1 WHERE name='grant_seq'")
        one.conn.execute("UPDATE admission SET state='granted', lease_id=?,"
                         " gateway_id='7b1e3c52-0d4a-4a8e-9c61-5f2a8d3b9e01', reference=?, ref_sha256=?,"
                         " ttl_seconds=1, expires_ms=1, grant_seq=(SELECT value FROM admission_meta WHERE name='grant_seq')"
                         " WHERE id=?",
                         (second, second, hashlib.sha256(second.encode("ascii")).hexdigest(), other["id"]))
        status, found = store.read_bindings(self.path)                                    # not committed yet
        self.assertEqual((status, len(found)), (store.OK, 1))
        one.conn.execute("COMMIT")
        self.assertEqual(len(store.read_bindings(self.path)[1]), 2)


if __name__ == "__main__":
    unittest.main()
