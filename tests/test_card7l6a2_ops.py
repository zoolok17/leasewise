"""Card L6a-2, step 2: `recall`, `pending` and `closed` (neutral spec 4.2, 4.3 and 6; DESIGN-6A 8.6 and requirement 5).

* `recall` returns the stored admission, or "unknown", takes no lock and never creates anything;
* `pending` returns the admissions not yet closed, least recently returned first, by a DURABLE return number (never by
  the clock): an entry that cannot be closed yet goes to the back of the line, so an unclosable prefix cannot block the
  closable entries behind it;
* `closed` records the acknowledgement once; a repeat changes nothing; it waits for the key lock at most the bounded wait;
* each operation is strict on both sides, and an operation other than `admit` that cannot complete exits non-zero with no
  answer.
Synthetic data only: generated ids, a scripted fake Leasewise.
"""

import json
import os
import sqlite3
import time
import unittest

from quota_balancer.reporter import admission_lock as lock
from quota_balancer.reporter import admission_store as store
from quota_balancer.reporter import admit
from quota_balancer.reporter import admit_wire as wire

from tests.l6a2_env import AGENT, FakeClock, Rigged, Script, request_bytes

R, M, A = "r" * 64, "m" * 32, "a" * 64


def recall(message_id="msg-0001", agent=AGENT):
    return wire.encode_op(wire.OP_RECALL, agent=agent, message_id=message_id)


def closed(message_id="msg-0001", agent=AGENT):
    return wire.encode_op(wire.OP_CLOSED, agent=agent, message_id=message_id)


def pending(limit=64, agent=AGENT):
    return wire.encode_op(wire.OP_PENDING, agent=agent, limit=limit)


class _Case(unittest.TestCase):
    def setUp(self):
        self.rg = Rigged()
        self.addCleanup(self.rg.cleanup)
        self.clock = FakeClock()
        self.script = Script("GRANT")
        self.script.now_ms = self.clock.now_ms

    def op(self, raw, **seams):
        return self.rg.run(raw, self.script, **self.clock.seams(**seams))

    def answer(self, raw, op):
        code, out, err = self.op(raw)
        self.assertEqual((code, err), (0, ""))
        return wire.parse_op_answer(op, out), out

    def admit_many(self, count, prefix="m-"):
        for number in range(count):
            code, out, _err = self.op(request_bytes(message_id="%s%03d" % (prefix, number)))
            self.assertEqual((code, wire.parse_answer(out)["status"]), (0, "admitted"))

    def pending_ids(self, limit=64):
        doc, _out = self.answer(pending(limit), wire.OP_PENDING)
        return [entry["message_id"] for entry in doc["messages"]]


class Sizes(unittest.TestCase):
    """Spec 4.3: the largest legal message of each kind is accepted at its size, one past it is refused."""

    def test_the_largest_legal_requests_of_the_other_operations_have_their_measured_sizes(self):
        keyed = wire.encode_op(wire.OP_RECALL, agent=A, message_id=M)
        self.assertEqual(len(keyed), 144)
        self.assertEqual(len(wire.encode_op(wire.OP_CLOSED, agent=A, message_id=M)), 144)
        self.assertEqual(len(wire.encode_op(wire.OP_PENDING, agent=A, limit=64)), 108)
        for raw in (keyed, wire.encode_op(wire.OP_PENDING, agent=A, limit=64)):
            wire.parse_request(raw)
            wire.parse_request(raw + b" " * (512 - len(raw)))
            with self.assertRaises(wire.WireFailure) as caught:
                wire.parse_request(raw + b" " * (513 - len(raw)))
            self.assertEqual(caught.exception.word, wire.TOO_LARGE)

    def test_the_bounds_of_each_operation_are_checked_at_the_limit_and_one_past_it(self):
        for limit, word in ((1, None), (64, None), (0, wire.OUT_OF_BOUNDS), (65, wire.OUT_OF_BOUNDS),
                            (-1, wire.OUT_OF_BOUNDS), (True, wire.BAD_SHAPE), (5.0, wire.BAD_SHAPE),
                            ("5", wire.BAD_SHAPE), (None, wire.BAD_SHAPE)):
            with self.subTest(limit=limit):
                raw = json.dumps({"v": 1, "op": "pending", "agent": "alpha", "limit": limit}).encode()
                if word is None:
                    self.assertEqual(wire.parse_request(raw).limit, limit)
                else:
                    with self.assertRaises(wire.WireFailure) as caught:
                        wire.parse_request(raw)
                    self.assertEqual(caught.exception.word, word)
        for bad in (dict(message_id="m" * 33), dict(message_id=""), dict(agent="a" * 65), dict(agent="operator"),
                    dict(agent=".x")):
            for op in ("recall", "closed"):
                doc = {"v": 1, "op": op, "agent": "alpha", "message_id": "m1"}
                doc.update(bad)
                with self.subTest(op=op, bad=bad):
                    with self.assertRaises(wire.WireFailure) as caught:
                        wire.parse_request(json.dumps(doc).encode())
                    self.assertEqual(caught.exception.word, wire.OUT_OF_BOUNDS)

    def test_the_other_answers_have_their_measured_sizes(self):
        self.assertEqual(wire.unknown(), b'{"status":"unknown","v":1}')
        self.assertEqual(len(wire.unknown()), 26)
        self.assertEqual(wire.closed_ok(), b'{"status":"ok","v":1}')
        self.assertEqual(len(wire.closed_ok()), 21)
        self.assertEqual(wire.pending_answer([], 5), b'{"messages":[],"status":"ok","v":1}')
        self.assertEqual(len(wire.pending_answer([], 5)), 35)

    def test_64_entries_at_the_maximum_identifier_sizes_are_8290_bytes_and_65_are_refused(self):
        entries = [(M, R)] * 64
        raw = wire.pending_answer(entries, 64)
        self.assertEqual(len(raw), 8290)
        self.assertLessEqual(len(raw), wire.MAX_PENDING_ANSWER_BYTES)
        self.assertEqual(len(wire.parse_op_answer(wire.OP_PENDING, raw)["messages"]), 64)
        with self.assertRaises(wire.WireFailure):
            wire.pending_answer([(M, R)] * 65, 64)
        with self.assertRaises(wire.WireFailure):
            wire.pending_answer([(M, R)] * 3, 2)                       # more than the limit asked for
        with self.assertRaises(wire.WireFailure) as caught:
            wire.parse_op_answer(wire.OP_PENDING, raw + b" " * 1000)
        self.assertEqual(caught.exception.word, wire.TOO_LARGE)

    def test_every_answer_is_read_as_strictly_as_it_is_written(self):
        good = {wire.OP_PENDING: wire.pending_answer([("m1", "ref-1")], 5), wire.OP_CLOSED: wire.closed_ok(),
                wire.OP_RECALL: wire.unknown(), wire.OP_ADMIT: wire.held("refused")}
        for op, raw in good.items():
            wire.parse_op_answer(op, raw)
        bad = [(wire.OP_PENDING, b'{"messages":[{"message_id":"m1"}],"status":"ok","v":1}'),
               (wire.OP_PENDING, b'{"messages":[{"message_id":"m1","reference":"r","x":1}],"status":"ok","v":1}'),
               (wire.OP_PENDING, b'{"messages":[],"status":"ok","v":true}'),
               (wire.OP_PENDING, b'{"messages":{},"status":"ok","v":1}'),
               (wire.OP_PENDING, b'{"status":"ok","v":1}'),
               (wire.OP_PENDING, b'{"messages":[],"status":"held","v":1}'),
               (wire.OP_CLOSED, b'{"status":"ok","v":1,"extra":1}'), (wire.OP_CLOSED, b'{"status":"unknown","v":1}'),
               (wire.OP_CLOSED, b'{"status":"ok","v":1.0}'), (wire.OP_CLOSED, b'{"status":"ok","status":"ok","v":1}'),
               (wire.OP_CLOSED, b'{"status":"ok","v":1}{}'),
               (wire.OP_RECALL, b'{"reason":"refused","status":"held","v":1}'),           # `held` is `admit` alone
               (wire.OP_RECALL, b'{"status":"ok","v":1}'), (wire.OP_RECALL, b'{"status":"unknown","v":1,"x":1}'),
               (wire.OP_ADMIT, b'{"status":"unknown","v":1}'), (wire.OP_ADMIT, b'{"status":"ok","v":1}')]
        for op, raw in bad:
            with self.subTest(op=op, raw=raw[:50]):
                with self.assertRaises(wire.WireFailure):
                    wire.parse_op_answer(op, raw)


class Recall(_Case):

    def test_recall_returns_the_stored_admission_exactly_as_admit_gave_it(self):
        code, said, _err = self.op(request_bytes())
        doc, out = self.answer(recall(), wire.OP_RECALL)
        self.assertEqual((code, out), (0, said))
        self.assertEqual(doc["status"], "admitted")
        self.assertEqual(len(self.script.calls), 1)                                  # no second request

    def test_recall_of_a_message_with_no_admission_is_unknown(self):
        doc, out = self.answer(recall("never-seen"), wire.OP_RECALL)
        self.assertEqual((doc, out), ({"status": "unknown", "v": 1}, b'{"status":"unknown","v":1}'))

    def test_recall_never_creates_anything(self):
        self.answer(recall(), wire.OP_RECALL)                                         # no store file at all
        self.assertFalse(os.path.exists(self.rg.store))
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.rg.store), lock.FOLDER)))
        self.admit_many(2)
        before = (self.rg.sql("SELECT * FROM admission"), self.rg.sql("SELECT * FROM admission_attempt"),
                  self.rg.sql("SELECT * FROM admission_meta"), sorted(os.listdir(os.path.dirname(self.rg.store))))
        for message_id in ("m-000", "m-001", "never-seen"):
            self.answer(recall(message_id), wire.OP_RECALL)
        after = (self.rg.sql("SELECT * FROM admission"), self.rg.sql("SELECT * FROM admission_attempt"),
                 self.rg.sql("SELECT * FROM admission_meta"), sorted(os.listdir(os.path.dirname(self.rg.store))))
        self.assertEqual(before, after)                                                # not a row, a counter or a file

    def test_a_request_that_was_saved_but_not_granted_is_unknown_to_recall(self):
        self.script = Script(Exception("no answer"))
        self.op(request_bytes())
        self.assertEqual(self.rg.row()[0], "requested")
        self.assertEqual(self.answer(recall(), wire.OP_RECALL)[0], {"status": "unknown", "v": 1})

    def test_recall_takes_no_lock_so_a_held_key_does_not_stop_it(self):
        self.op(request_bytes())
        held = lock.KeyLock(self.rg.store, AGENT, "msg-0001")
        self.assertTrue(held.acquire(time.monotonic() + 2))
        self.addCleanup(held.release)
        self.assertEqual(self.answer(recall(), wire.OP_RECALL)[0]["status"], "admitted")

    def test_a_grant_whose_time_is_used_up_is_still_recalled_with_the_least_time_the_wire_allows(self):
        self.script = Script("GRANT", expires_in_ms=500)
        self.script.now_ms = self.clock.now_ms
        self.op(request_bytes())                                                       # `admit` holds it (no time left)
        doc, _out = self.answer(recall(), wire.OP_RECALL)
        self.assertEqual((doc["status"], doc["ttl_seconds"]), ("admitted", 1))
        self.assertEqual(len(doc["reference"]), 36)                                    # the reference is what a close needs

    def test_recall_of_a_store_that_cannot_be_read_is_a_failure_with_no_answer(self):
        self.op(request_bytes())
        from unittest import mock
        for reason in (store.STORE_BUSY, store.STORE_UNREADABLE):
            with mock.patch.object(store, "read_admission", return_value=(store.LATER, reason)):
                code, out, err = self.op(recall())
            self.assertEqual((code, out), (admit.EXIT_FAILED, b""))
            self.assertIn("could not be completed", err)
        with open(self.rg.store, "wb") as handle:                                       # a file that is not a database
            handle.write(b"this is not a database")
        self.assertEqual(self.op(recall())[:2], (admit.EXIT_FAILED, b""))


class Pending(_Case):

    def test_an_empty_answer_when_nothing_is_pending_even_with_no_store(self):
        self.assertEqual(self.answer(pending(), wire.OP_PENDING)[1], b'{"messages":[],"status":"ok","v":1}')
        self.assertFalse(os.path.exists(self.rg.store))                                # pending never creates the store
        self.admit_many(1)
        self.assertEqual(self.pending_ids(), ["m-000"])
        self.assertEqual(self.pending_ids(5), ["m-000"])

    def test_only_this_agents_unacknowledged_admissions_are_listed(self):
        self.admit_many(2)
        self.op(closed("m-000"))
        self.assertEqual(self.pending_ids(), ["m-001"])
        self.assertEqual(self.answer(pending(agent="beta"), wire.OP_PENDING)[0]["messages"], [])

    def test_never_returned_entries_come_first_then_the_least_recently_returned(self):
        self.admit_many(4)
        self.assertEqual(self.pending_ids(2), ["m-000", "m-001"])
        self.assertEqual(self.pending_ids(2), ["m-002", "m-003"])                      # never returned before
        self.assertEqual(self.pending_ids(2), ["m-000", "m-001"])                      # now the oldest returns
        self.assertEqual(self.pending_ids(3), ["m-002", "m-003", "m-000"])

    def test_the_return_numbers_only_ever_grow_and_every_returned_entry_gets_the_next_one(self):
        self.admit_many(3)
        self.pending_ids(2)
        rows = self.rg.sql("SELECT message_id, return_seq FROM admission ORDER BY id")
        self.assertEqual([r[1] for r in rows], [1, 2, None])
        self.pending_ids(3)
        rows = self.rg.sql("SELECT message_id, return_seq FROM admission ORDER BY id")
        self.assertEqual([r[1] for r in rows], [4, 5, 3])                               # 3 was never returned: it went first
        self.assertEqual(self.rg.sql("SELECT value FROM admission_meta WHERE name='return_seq'"), [(5,)])
        self.assertEqual(len({r[1] for r in rows}), 3)

    def test_tied_time_and_a_clock_that_goes_back_change_nothing(self):
        """Requirement 5: the order is the durable number, never the wall clock."""
        self.admit_many(4)
        seen = []
        for moment in (1_000, 1_000, 1_000, 500, 1, 5_000, 5_000, 2):                  # tied, then back, then forward
            self.clock.ms = moment
            seen.append(self.pending_ids(2))
        self.assertEqual(seen, [["m-000", "m-001"], ["m-002", "m-003"], ["m-000", "m-001"], ["m-002", "m-003"]] * 2)
        times = self.rg.sql("SELECT last_pending_ms FROM admission ORDER BY id")
        self.assertEqual(len({t[0] for t in times}) > 1, True)                         # the clock really did move about

    def test_more_than_64_entries_are_returned_64_at_a_time_and_none_is_lost_or_starved(self):
        self.admit_many(70)
        first = self.pending_ids(64)
        second = self.pending_ids(64)
        self.assertEqual(len(first), 64)
        self.assertEqual(second[:6], ["m-%03d" % n for n in range(64, 70)])            # the six never returned come first
        self.assertEqual(len(second), 64)
        self.assertEqual(set(first) | set(second), {"m-%03d" % n for n in range(70)})

    def test_an_unclosable_prefix_cannot_block_the_closable_entries_behind_it(self):
        self.admit_many(10)
        unclosable = {"m-%03d" % n for n in range(5)}
        closed_so_far = set()
        for _ in range(4):                                                              # a few passes, five at a time
            for message_id in self.pending_ids(5):
                if message_id not in unclosable:
                    self.assertEqual(self.op(closed(message_id))[0], 0)
                    closed_so_far.add(message_id)
        self.assertEqual(closed_so_far, {"m-%03d" % n for n in range(5, 10)})          # every closable one was reached
        self.assertEqual(set(self.pending_ids(64)), unclosable)                        # only the unclosable remain

    def test_a_returned_entrys_time_note_moves(self):
        self.admit_many(1)
        self.clock.ms = 7_000
        self.pending_ids()
        self.assertEqual(self.rg.sql("SELECT last_pending_ms FROM admission"), [(7_000,)])

    def test_a_busy_store_is_a_failure_with_no_answer_and_pending_is_unchanged_after(self):
        self.admit_many(2)
        holder = sqlite3.connect(self.rg.store, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN EXCLUSIVE")
        from unittest import mock
        with mock.patch.object(store, "BUSY_TIMEOUT_MS", 50):
            code, out, _err = self.op(pending(5))
        self.assertEqual((code, out), (admit.EXIT_FAILED, b""))
        holder.execute("ROLLBACK")
        self.assertEqual(self.pending_ids(5), ["m-000", "m-001"])                       # nothing was marked as returned

    def test_a_pending_entry_is_a_grant_never_a_request(self):
        self.script = Script(Exception("no answer"))
        self.op(request_bytes(message_id="never-granted"))
        self.assertEqual(self.pending_ids(), [])


class Closed(_Case):

    def test_closed_records_the_acknowledgement_once_and_the_message_leaves_pending(self):
        self.admit_many(2)
        self.clock.ms = 4_000
        doc, out = self.answer(closed("m-000"), wire.OP_CLOSED)
        self.assertEqual(out, b'{"status":"ok","v":1}')
        self.assertEqual(self.rg.sql("SELECT message_id, closed_ms FROM admission ORDER BY id"),
                         [("m-000", 4_000), ("m-001", None)])
        self.assertEqual(self.pending_ids(), ["m-001"])

    def test_a_repeated_closed_is_a_no_op_that_keeps_the_first_time(self):
        self.admit_many(1)
        self.clock.ms = 4_000
        self.answer(closed("m-000"), wire.OP_CLOSED)
        self.clock.ms = 9_000
        self.assertEqual(self.answer(closed("m-000"), wire.OP_CLOSED)[1], b'{"status":"ok","v":1}')
        self.assertEqual(self.rg.sql("SELECT closed_ms FROM admission"), [(4_000,)])

    def test_the_acknowledgement_cannot_be_changed_afterwards(self):
        self.admit_many(1)
        self.answer(closed("m-000"), wire.OP_CLOSED)
        conn = sqlite3.connect(self.rg.store, isolation_level=None)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE admission SET closed_ms=1")

    def test_closed_for_an_unknown_message_or_with_no_store_is_ok_and_creates_nothing(self):
        self.assertEqual(self.answer(closed("never-seen"), wire.OP_CLOSED)[1], b'{"status":"ok","v":1}')
        self.assertFalse(os.path.exists(self.rg.store))
        self.admit_many(1)
        self.answer(closed("never-seen"), wire.OP_CLOSED)
        self.assertEqual(self.rg.sql("SELECT COUNT(*) FROM admission"), [(1,)])

    def test_closed_does_not_close_a_message_that_was_never_granted(self):
        self.script = Script(Exception("no answer"))
        self.op(request_bytes(message_id="never-granted"))
        self.answer(closed("never-granted"), wire.OP_CLOSED)
        self.assertEqual(self.rg.sql("SELECT state, closed_ms FROM admission"), [("requested", None)])

    def test_closed_waits_for_the_key_for_at_most_the_bounded_wait_then_fails_with_no_answer(self):
        self.admit_many(1)
        held = lock.KeyLock(self.rg.store, AGENT, "m-000")
        self.assertTrue(held.acquire(time.monotonic() + 2))
        self.addCleanup(held.release)
        started = self.clock.mono
        code, out, err = self.op(closed("m-000"))
        waited = self.clock.mono - started
        self.assertEqual((code, out), (admit.EXIT_FAILED, b""))
        self.assertGreaterEqual(waited, admit.LOCK_WAIT_SECONDS)
        self.assertLessEqual(waited, admit.LOCK_WAIT_SECONDS + lock.POLL_SECONDS + 1e-6)
        self.assertEqual(self.rg.sql("SELECT closed_ms FROM admission"), [(None,)])     # nothing was recorded
        held.release()
        self.assertEqual(self.op(closed("m-000"))[0], 0)

    def test_closed_uses_the_same_64_stripe_files_as_admit(self):
        self.admit_many(5)
        for number in range(5):
            self.op(closed("m-%03d" % number))
        folder = os.path.join(os.path.dirname(self.rg.store), lock.FOLDER)
        self.assertLessEqual(len(os.listdir(folder)), 64)


class Failures(_Case):
    """Every operation's strict JSON through the command, and its exit codes."""

    def test_a_request_that_is_not_allowed_is_exit_2_with_no_answer_for_every_operation(self):
        for raw in (b'{"v":1,"op":"recall","agent":"alpha"}', b'{"v":1,"op":"pending","agent":"alpha","limit":65}',
                    b'{"v":1,"op":"closed","agent":"alpha","message_id":"m 1"}', b'{"v":1,"op":"other"}',
                    b'{"v":true,"op":"pending","agent":"alpha","limit":3}',
                    b'{"v":1,"op":"pending","agent":"loud-private","limit":3.0}'):
            code, out, err = self.op(raw)
            with self.subTest(raw=raw[:50]):
                self.assertEqual((code, out), (2, b""))
                self.assertNotIn("loud-private", err)

    def test_held_is_the_answer_of_admit_alone(self):
        for raw in (recall(), pending(), closed()):
            code, out, _err = self.op(raw)
            self.assertEqual(code, 0)
            self.assertNotIn(b"held", out)


if __name__ == "__main__":
    unittest.main()
