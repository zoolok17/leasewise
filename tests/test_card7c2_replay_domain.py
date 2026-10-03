"""Card 7c-2, fix round 1: a repeat delivery keeps the OLD reset domain.

Before the margin, Leasewise accepted a limit event whose reset was anything up to INT64_MAX. The
margin needs room (a reset above INT64_MAX - 30 000 cannot be stored once 30 seconds are added), so
that smaller ceiling applies to NEW events only: an insert or a join. An event Leasewise already
holds is a replay, and a replay must behave exactly as before: no conflict, no margin, nothing
written, the block unchanged. The old code is run from the repository's own history (`da91e1f`),
so each event is recorded the old way. Synthetic data only.
"""

import subprocess
import types
import unittest
import uuid
import os
import sqlite3

import test_card5 as c5
from quota_balancer import lease

INT64_MAX = 2 ** 63 - 1
MARGIN = 30_000
NOW, ACCOUNT, IDENTITY_A = c5.NOW, c5.ACCOUNT, c5.IDENTITY_A
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDGES = (INT64_MAX, INT64_MAX - 29_999, INT64_MAX - 1)          # all above INT64_MAX - 30 000


def old_lease():
    """The limit-event code as it was before the margin (da91e1f), loaded as a module."""
    raw = subprocess.check_output(["git", "-C", REPO, "show", "da91e1f:quota_balancer/lease.py"])
    module = types.ModuleType("quota_balancer.baseline_lease_da91e1f")
    module.__package__ = "quota_balancer"
    exec(compile(raw, "baseline_lease.py", "exec"), module.__dict__)
    return module


def rows(db):
    conn = sqlite3.connect(db)
    try:
        return [conn.execute(q).fetchall() for q in (
            "SELECT * FROM blocks ORDER BY id", "SELECT * FROM limit_events ORDER BY event_id",
            "SELECT COUNT(*) FROM audit")]
    finally:
        conn.close()


class ReplayKeepsTheOldDomain(c5._ServerCase):

    def setUp(self):
        super().setUp()
        self.old = old_lease()

    def recorded_the_old_way(self, reset, **kw):
        self.db = c5._build_store(self.tmp, name=uuid.uuid4().hex + ".db")
        event = c5._limit_event(resets_at=reset, **kw)
        before = self.old.record_limit_event(self.db, event, NOW)
        self.assertEqual(before["retry_at"], reset)                    # the old code stored it raw
        return event, before

    def test_an_event_accepted_before_the_change_replays_unchanged_at_the_edges(self):
        for reset in EDGES:
            with self.subTest(reset=reset):
                event, before = self.recorded_the_old_way(reset)
                snapshot = rows(self.db)
                self.assertEqual(self.old.record_limit_event(self.db, event, NOW), before)   # the old replay
                self.assertEqual(lease.record_limit_event(self.db, event, NOW), before)      # the new one: the same
                self.assertEqual(rows(self.db), snapshot)                                    # nothing written
                self.assertEqual(snapshot[0][0][4], reset)                                   # raw retry_at: no margin

    def test_the_same_replay_over_http_is_a_202_with_the_stored_state(self):
        for reset in EDGES:
            with self.subTest(reset=reset):
                event, before = self.recorded_the_old_way(reset)
                self.start_with_db(self.db)
                status, body = c5._call(self.base, "POST", "/v1/limit-events", body=event,
                                        identity=IDENTITY_A, idempotency_key=event["event_id"])
                self.assertEqual(status, 202)
                self.assertEqual({k: body[k] for k in ("block_id", "retry_at", "state")}, before)   # + the envelope
                self.server.shutdown()
                self.server.server_close()
                self.thread.join(timeout=5)
                self.server = None

    def test_a_changed_reset_under_an_old_event_id_is_still_a_conflict(self):
        event, _before = self.recorded_the_old_way(INT64_MAX)
        changed = dict(event, resets_at=INT64_MAX - 29_999)
        with self.assertRaises(lease.QuotaError) as caught:
            lease.record_limit_event(self.db, changed, NOW)
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_a_replay_above_the_old_domain_is_still_invalid(self):
        event, _before = self.recorded_the_old_way(INT64_MAX)
        with self.assertRaises(lease.QuotaError) as caught:
            lease.record_limit_event(self.db, dict(event, resets_at=INT64_MAX + 1), NOW)
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")

    def legacy_record(self, reset):
        """A legacy row (migration v8): its creator identity from before limit_events existed."""
        self.db = c5._build_store(self.tmp, name=uuid.uuid4().hex + ".db")
        event = c5._limit_event(resets_at=reset)
        block_id = str(uuid.uuid4())
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("INSERT INTO blocks(id, account_id, model_id, reason, retry_at, state, source_event_id,"
                         " updated_at) VALUES(?,?,?,?,?,?,?,?)",
                         (block_id, ACCOUNT, None, event["evidence_kind"], reset, "parked", event["event_id"], NOW))
            conn.execute("INSERT INTO limit_events(event_id, account_id, model_id, turn_id, observed_at,"
                         " evidence_kind, resets_at, exhausted_windows, block_id, received_at, legacy)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                         (event["event_id"], ACCOUNT, None, str(uuid.uuid4()), NOW, event["evidence_kind"], reset,
                          "[]", block_id, NOW))
            conn.commit()
        finally:
            conn.close()
        return event, block_id

    def test_a_legacy_record_replays_unchanged_at_the_edges(self):
        for reset in EDGES:
            with self.subTest(reset=reset):
                event, block_id = self.legacy_record(reset)
                snapshot = rows(self.db)
                answer = lease.record_limit_event(self.db, event, NOW)
                self.assertEqual(answer, {"block_id": block_id, "retry_at": reset, "state": "parked"})
                self.assertEqual(rows(self.db), snapshot)

    def test_a_NEW_event_at_those_values_is_still_refused_and_nothing_is_written(self):
        self.start()
        snapshot = rows(self.db)
        for reset in EDGES + (INT64_MAX - MARGIN + 1,):
            with self.subTest(reset=reset):
                status, body = self.limit_event(resets_at=reset)
                self.assertEqual(status, 400, body)
                self.assertEqual(body["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(rows(self.db), snapshot)
        status, body = self.limit_event(resets_at=INT64_MAX - MARGIN)       # the new ceiling itself is accepted
        self.assertEqual(status, 202, body)
        self.assertEqual(body["retry_at"], INT64_MAX)

    def test_a_new_event_JOINING_a_live_block_at_those_values_is_refused_and_the_block_unchanged(self):
        self.start()
        _s, first = self.limit_event(resets_at=NOW + 3_600_000)
        snapshot = rows(self.db)
        for reset in EDGES:
            with self.subTest(reset=reset):
                status, body = self.limit_event(resets_at=reset)
                self.assertEqual(status, 400, body)
        self.assertEqual(rows(self.db), snapshot)
        self.assertEqual(snapshot[0][0][4], NOW + 3_600_000 + MARGIN)
        self.assertEqual(first["retry_at"], NOW + 3_600_000 + MARGIN)

    def test_a_new_event_that_joins_an_old_block_at_the_edge_is_judged_by_the_new_ceiling(self):
        event, _before = self.recorded_the_old_way(INT64_MAX)               # an old block at the very top
        self.start_with_db(self.db)
        status, body = self.limit_event(resets_at=NOW + 60_000)             # a new, ordinary event joins it
        self.assertEqual(status, 202, body)
        self.assertEqual(body["retry_at"], INT64_MAX)                        # max(old raw, new + margin)


if __name__ == "__main__":
    unittest.main()
