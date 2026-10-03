"""Card 7c-2, step 2: the reporter's durable outbox for limit events (state version 2).

A limit event is stored once, with its body frozen, BEFORE anything is sent; retries resend the
stored bytes. The table is immutable in what matters (identity and body) and keyed by `event_id`.
The state file moves from version 1 to version 2 in one transaction. A reporter built before this
card meets a version-2 file with its existing fail-closed error and never opens or changes it.
Everything here is synthetic: fixture aliases and generated ids.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
import uuid

from quota_balancer.reporter import decisions
from quota_balancer.reporter import state as state_mod
from quota_balancer.reporter.state import State, StateError

NOW = 1_788_224_400_000
ALIAS = "claude-main"
WINDOW = "five_hour"


def body(**over):
    value = {"event_id": str(uuid.uuid4()), "account_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()),
             "observed_at": NOW - 60_000, "evidence_kind": "claude_exhausted",
             "resets_at": NOW + 3_600_000, "exhausted_windows": [WINDOW]}
    value.update(over)
    return value


def canonical(doc):
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(dir=os.environ.get("AGENTTALK_SCRATCH") or None,
                                                prefix="reporter-7c2-")
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "r.db")

    current = None

    def open(self):
        """Open the state. While it is open nothing else may read the file (the reporter owns it
        exclusively), so `raw` then asks through this connection."""
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

    def rows(self, table="limit_event"):
        return self.raw("SELECT id, event_id, account_alias, body, state, tries, not_before_ms, "
                        "last_error_code, captured_ms FROM %s ORDER BY id" % table)


class Schema(_Tmp):

    def test_a_fresh_file_has_the_current_version_with_the_limit_table(self):
        state = self.open()
        self.assertEqual(state_mod.SCHEMA_VERSION, 3)                # card L6a-1 moved it from 2 to 3
        self.assertEqual(state.one("PRAGMA user_version")[0], 3)
        columns = [r[1] for r in state.all("PRAGMA table_info(limit_event)")]
        self.assertEqual(columns, ["id", "event_id", "account_alias", "body", "state", "tries",
                                   "not_before_ms", "last_error_code", "captured_ms"])
        triggers = {r[0] for r in state.all("SELECT name FROM sqlite_master WHERE type='trigger'")}
        self.assertTrue({"limit_event_immutable", "limit_event_keep"} <= triggers)

    def _make_version_1(self):
        """A real version-1 file: the code before this card, with lease rows in it."""
        original = (state_mod.SCHEMA_VERSION, state_mod._DDL)
        state_mod.SCHEMA_VERSION, state_mod._DDL = 1, state_mod._DDL_V1
        try:
            old = State(self.path)
            with old.txn():
                old.conn.execute(
                    "INSERT INTO acquisition(agent, message_id, turn_uuid, request_id, body_original, "
                    "body_current) VALUES('alpha','m1','t1','r1','{}','{}')")
                old.conn.execute("INSERT INTO counter(agent, name, value) VALUES('alpha','reports_dropped',3)")
                old.set_note("last_ok:alpha", None, NOW)
            old.close()
        finally:
            state_mod.SCHEMA_VERSION, state_mod._DDL = original

    def lease_snapshot(self):
        return [self.raw("SELECT * FROM %s ORDER BY 1" % t)
                for t in ("acquisition", "counter", "note", "meta", "stream", "event", "dispatch",
                          "disposal", "report", "final", "late", "message_status", "agent_state")]

    def test_a_version_1_file_migrates_with_the_lease_rows_untouched(self):
        self._make_version_1()
        before = self.lease_snapshot()
        self.assertEqual(self.raw("PRAGMA user_version"), [(1,)])
        self.assertEqual(self.raw("SELECT name FROM sqlite_master WHERE name='limit_event'"), [])
        state = self.open()
        self.assertEqual(state.one("PRAGMA user_version")[0], 3)     # 1 -> 3 in one step since card L6a-1
        self.assertEqual(self.lease_snapshot(), before)
        self.assertEqual(self.rows(), [])
        self.assertTrue(state.add_limit_event(ALIAS, body(), NOW))

    def test_a_second_open_is_a_no_op(self):
        self._make_version_1()
        State(self.path).close()
        schema = self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name")
        before = self.lease_snapshot()
        State(self.path).close()
        self.assertEqual(self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name"), schema)
        self.assertEqual(self.lease_snapshot(), before)
        self.assertEqual(self.raw("PRAGMA user_version"), [(3,)])

    def test_a_migration_that_fails_leaves_the_file_at_version_1(self):
        self._make_version_1()
        original = state_mod._LIMIT_DDL
        state_mod._LIMIT_DDL = original + ("THIS IS NOT SQL",)
        try:
            with self.assertRaises(StateError):
                State(self.path)
        finally:
            state_mod._LIMIT_DDL = original
        self.assertEqual(self.raw("PRAGMA user_version"), [(1,)])
        self.assertEqual(self.raw("SELECT name FROM sqlite_master WHERE name='limit_event'"), [])
        self.open()                                            # and the good code still migrates it
        self.assertEqual(self.raw("PRAGMA user_version"), [(3,)])
        self.assertEqual(len(self.raw("SELECT name FROM sqlite_master WHERE name='limit_event'")), 1)

    def _fingerprint(self):
        main = open(self.path, "rb").read()
        return hashlib.sha256(main).hexdigest(), self.raw("PRAGMA user_version"), \
            self.raw("SELECT type, name, sql FROM sqlite_master ORDER BY name")

    def test_a_reporter_built_before_this_card_refuses_a_version_2_file_and_changes_nothing(self):
        State(self.path).close()                              # a version-2 file
        before = self._fingerprint()
        original = (state_mod.SCHEMA_VERSION, state_mod._DDL)
        state_mod.SCHEMA_VERSION, state_mod._DDL = 1, state_mod._DDL_V1      # the code before this card
        try:
            with self.assertRaises(StateError) as caught:
                State(self.path)
        finally:
            state_mod.SCHEMA_VERSION, state_mod._DDL = original
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

    def test_the_same_event_id_twice_gives_one_row_and_the_first_bytes(self):
        state = self.open()
        first = body()
        changed = dict(first, resets_at=first["resets_at"] + 1, exhausted_windows=["seven_day"])
        self.assertTrue(state.add_limit_event(ALIAS, first, NOW))
        self.assertFalse(state.add_limit_event(ALIAS, first, NOW + 5))
        self.assertFalse(state.add_limit_event(ALIAS, changed, NOW + 9))     # same id, different content
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], canonical(first))                       # the first bytes, canonical
        self.assertEqual(rows[0][8], NOW)                                    # captured when first stored
        self.assertEqual(rows[0][4:8], ("pending", 0, 0, None))

    def test_rows_keep_their_order_and_the_account_alias(self):
        state = self.open()
        a, b = body(), body()
        state.add_limit_event("alias-a", a, NOW)
        state.add_limit_event("alias-b", b, NOW + 1)
        self.assertEqual([(r[1], r[2]) for r in self.rows()],
                         [(a["event_id"], "alias-a"), (b["event_id"], "alias-b")])

    def test_an_unknown_reset_is_stored_as_null(self):
        state = self.open()
        doc = body(resets_at=None)
        state.add_limit_event(ALIAS, doc, NOW)
        self.assertEqual(json.loads(self.rows()[0][3])["resets_at"], None)

    def test_identity_and_body_cannot_be_changed_or_deleted(self):
        state = self.open()
        state.add_limit_event(ALIAS, body(), NOW)
        for column, value in (("event_id", str(uuid.uuid4())), ("account_alias", "other"),
                              ("body", "{}"), ("captured_ms", 1)):
            with self.subTest(column=column):
                with self.assertRaises(sqlite3.DatabaseError):
                    state.conn.execute("UPDATE limit_event SET %s=?" % column, (value,))
        with self.assertRaises(sqlite3.DatabaseError):
            state.conn.execute("DELETE FROM limit_event")
        state.conn.execute("UPDATE limit_event SET state='sent'")           # delivery state may change
        self.assertEqual(self.rows()[0][4], "sent")

    def test_a_body_outside_the_allow_list_is_refused_with_a_fixed_sentence(self):
        state = self.open()
        good = body()
        marker = "PRIVATE-MARKER-agent-beta-m-20261003"
        bad = [dict(good, agent=marker), {k: v for k, v in good.items() if k != "turn_id"},
               dict(good, observed_at=True), dict(good, observed_at=0), dict(good, event_id=marker),
               dict(good, account_id=marker), dict(good, evidence_kind=marker),
               dict(good, resets_at=str(NOW)), dict(good, resets_at=2 ** 63 - 1),
               dict(good, exhausted_windows=[marker + " " * 80]), dict(good, exhausted_windows="five_hour"),
               dict(good, exhausted_windows=[1]), dict(good, model_id=None), [], None, "text"]
        for index, doc in enumerate(bad):
            with self.subTest(index=index):
                with self.assertRaises(StateError) as caught:
                    state.add_limit_event(ALIAS, doc, NOW)
                self.assertEqual(str(caught.exception), "a limit event body is not valid")
        for alias in (None, "", "Bad Alias", "x" * 65, marker):
            with self.subTest(alias=alias):
                with self.assertRaises(StateError) as caught:
                    state.add_limit_event(alias, good, NOW)
                self.assertNotIn(marker, str(caught.exception))
        self.assertEqual(self.rows(), [])

    def test_the_ceiling_matches_leasewise_after_the_margin(self):
        state = self.open()
        self.assertTrue(state.add_limit_event(ALIAS, body(resets_at=2 ** 63 - 1 - 30_000), NOW))


class Scheduling(_Tmp):

    def put(self, state, n, **kw):
        docs = [body(**kw) for _ in range(n)]
        for index, doc in enumerate(docs):
            state.add_limit_event(ALIAS, doc, NOW + index)
        return docs

    def test_the_oldest_due_pending_row_comes_first_as_a_limit_item(self):
        state = self.open()
        docs = self.put(state, 3)
        item = state.next_limit_item(NOW)
        self.assertEqual((item.kind, item.agent, item.message_id, item.path, item.key, item.lease_id),
                         (decisions.LIMIT, state_mod.LIMIT_AGENT, "", "/v1/limit-events",
                          docs[0]["event_id"], None))
        self.assertEqual(item.body, docs[0])
        self.assertEqual(item.ref[0], "limit_event")

    def test_agent_names_cannot_collide_with_the_limit_agent_word(self):
        from quota_balancer.reporter.events import _NAME
        self.assertIsNone(_NAME.match(state_mod.LIMIT_AGENT))

    def test_a_row_waiting_out_its_back_off_is_skipped_for_the_next(self):
        state = self.open()
        docs = self.put(state, 2)
        state.conn.execute("UPDATE limit_event SET not_before_ms=? WHERE event_id=?",
                           (NOW + 10_000, docs[0]["event_id"]))
        self.assertEqual(state.next_limit_item(NOW).key, docs[1]["event_id"])
        self.assertEqual(state.next_limit_item(NOW + 10_000).key, docs[0]["event_id"])

    def test_sent_and_failed_rows_are_never_picked_and_none_means_none(self):
        state = self.open()
        self.assertIsNone(state.next_limit_item(NOW))
        docs = self.put(state, 2)
        state.conn.execute("UPDATE limit_event SET state='sent' WHERE event_id=?", (docs[0]["event_id"],))
        state.conn.execute("UPDATE limit_event SET state='failed_permanent' WHERE event_id=?",
                           (docs[1]["event_id"],))
        self.assertIsNone(state.next_limit_item(NOW))

    def test_a_stopped_agent_does_not_hold_a_limit_row(self):
        state = self.open()
        self.put(state, 1)
        with state.txn():
            state.conn.execute("INSERT INTO agent_state(agent, failed_code) VALUES('alpha','FORBIDDEN')")
        self.assertIsNotNone(state.next_limit_item(NOW))

    def test_the_lease_queue_does_not_show_a_limit_row(self):
        state = self.open()
        self.put(state, 1)
        self.assertIsNone(state.next_item(NOW))


class Atomic(_Tmp):
    """The credential notes move in the SAME transaction as the delivery state (fix round 1, 16.6): a
    failure while writing the state leaves the notes as they were."""

    def armed(self):
        state = self.open()
        doc = body()
        state.add_limit_event(ALIAS, doc, NOW)
        item = state.next_limit_item(NOW)
        state.conn.execute("CREATE TEMP TRIGGER boom BEFORE UPDATE ON limit_event BEGIN "
                           "SELECT RAISE(ABORT, 'the delivery state could not be written'); END")
        return state, item

    def test_a_success_clears_the_credential_note_only_together_with_the_row_state(self):
        state, item = self.armed()
        with state.txn():
            state.set_note(state_mod.NOTE_CREDENTIAL_ACTIVE, "UNAUTHENTICATED", NOW)
        with self.assertRaises(sqlite3.DatabaseError):
            state.apply(item, decisions.Response(202), NOW + 1, 0.0)
        self.assertIsNotNone(state.note(state_mod.NOTE_CREDENTIAL_ACTIVE))          # not cleared
        self.assertIsNone(state.note("last_ok:" + state_mod.LIMIT_AGENT))            # not recorded
        self.assertEqual(self.rows()[0][4], "pending")
        state.conn.execute("DROP TRIGGER boom")
        state.apply(item, decisions.Response(202), NOW + 1, 0.0)                     # and it works afterwards
        self.assertIsNone(state.note(state_mod.NOTE_CREDENTIAL_ACTIVE))
        self.assertEqual(self.rows()[0][4], "sent")

    def test_a_401_starts_the_credential_notes_only_together_with_the_retry_state(self):
        state, item = self.armed()
        with self.assertRaises(sqlite3.DatabaseError):
            state.apply(item, decisions.Response(401, "UNAUTHENTICATED"), NOW + 1, 0.0)
        self.assertIsNone(state.note(state_mod.NOTE_CREDENTIAL_ACTIVE))
        self.assertIsNone(state.note(state_mod.NOTE_LAST_401))
        self.assertEqual(self.rows()[0][5], 0)                                       # no try counted either


class Apply(_Tmp):

    def go(self, status, code=None, now=NOW + 1_000):
        self.path = os.path.join(self._dir.name, uuid.uuid4().hex + ".db")      # a fresh file each time
        self.current = None
        state = self.open()
        doc = body()
        state.add_limit_event(ALIAS, doc, NOW)
        item = state.next_limit_item(NOW)
        decision = state.apply(item, decisions.Response(status, code), now, jitter=0.0)
        return state, doc, decision

    def test_202_marks_the_row_sent_and_notes_the_success(self):
        state, _doc, decision = self.go(202)
        self.assertEqual(decision.action, decisions.DONE)
        self.assertEqual(self.rows()[0][4:8], ("sent", 0, 0, None))
        self.assertEqual(state.note("last_ok:" + state_mod.LIMIT_AGENT), (None, NOW + 1_000))

    def test_a_retry_counts_and_backs_off_with_the_same_body(self):
        _state, doc, decision = self.go(503)
        self.assertEqual(decision.action, decisions.RETRY)
        row = self.rows()[0]
        self.assertEqual((row[4], row[5]), ("pending", 1))
        self.assertEqual(row[6], NOW + 1_000 + decisions.backoff_ms(1, 0.0))
        self.assertEqual(row[3], canonical(doc))

    def test_a_refusal_fails_that_row_for_good_and_stops_nothing(self):
        for status, code in ((400, "INVALID_REQUEST"), (403, "FORBIDDEN"), (404, "NOT_FOUND"),
                             (409, "IDEMPOTENCY_CONFLICT")):
            with self.subTest(status=status):
                state, _doc, decision = self.go(status, code)
                self.assertEqual(decision.action, decisions.FAIL)
                self.assertEqual(self.rows()[0][4], "failed_permanent")
                self.assertEqual(self.rows()[0][7], code)
                self.assertEqual(self.raw("SELECT COUNT(*) FROM agent_state"), [(0,)])   # no agent stopped

    def test_401_is_a_credential_retry_with_the_usual_notes(self):
        state, _doc, decision = self.go(401, "UNAUTHENTICATED")
        self.assertEqual(decision, decisions.Decision(decisions.RETRY, "UNAUTHENTICATED"))
        self.assertEqual(self.rows()[0][4], "pending")
        self.assertIsNotNone(state.note(state_mod.NOTE_CREDENTIAL_ACTIVE))
        self.assertIsNotNone(state.note(state_mod.NOTE_LAST_401))


if __name__ == "__main__":
    unittest.main()
