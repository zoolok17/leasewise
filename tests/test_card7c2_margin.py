"""Card 7c-2, step 1: the 30-second margin on a limit event's reset.

Leasewise holds an account until 30 seconds AFTER the reset a reporter stated, so the provider has
time to reopen. The margin is added once, in `lease.record_limit_event`: to the NEW value only, on a
new block and on a join. The stored `limit_events.resets_at` stays raw and stays in the replay
identity; a replay is a pure read. Existing blocks keep the `retry_at` they have (decision D2: no
backfill). Run from the repository root: python -m unittest discover -s tests -v

All data is synthetic: fixture aliases and generated ids.
"""

import sqlite3
import uuid
import unittest

import test_card5 as c5
from quota_balancer import lease

ACCOUNT, NOW, IDENTITY_A = c5.ACCOUNT, c5.NOW, c5.IDENTITY_A
MARGIN = 30_000
INT64_MAX = 2 ** 63 - 1


def _exec(db, sql, params=()):
    conn = sqlite3.connect(db)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


class MarginRule(c5._ServerCase):

    def post(self, **kw):
        return self.limit_event(**kw)

    def retry_at_of(self, block_id):
        return c5._rows(self.db, "SELECT retry_at FROM blocks WHERE id=?", (block_id,))[0][0]

    def test_the_margin_is_thirty_seconds(self):
        self.assertEqual(lease.LIMIT_RETRY_MARGIN_MS, MARGIN)

    def test_a_new_block_holds_until_the_reset_plus_the_margin(self):
        self.start()
        reset = NOW + 3_600_000
        status, body = self.post(resets_at=reset)
        self.assertEqual(status, 202, body)
        self.assertEqual(body["retry_at"], reset + MARGIN)                  # the 202 answer is margined
        self.assertEqual(self.retry_at_of(body["block_id"]), reset + MARGIN)
        self.assertEqual(c5._rows(self.db, "SELECT resets_at FROM limit_events"), [(reset,)])   # raw evidence

    def test_an_unknown_reset_stays_unknown(self):
        self.start()
        status, body = self.post(resets_at=None)
        self.assertEqual(status, 202, body)
        self.assertIsNone(body["retry_at"])
        self.assertIsNone(self.retry_at_of(body["block_id"]))
        self.assertEqual(c5._rows(self.db, "SELECT resets_at FROM limit_events"), [(None,)])

    def test_a_join_with_a_later_reset_moves_to_the_later_reset_plus_the_margin(self):
        self.start()
        _s, first = self.post(resets_at=NOW + 60_000)
        status, second = self.post(resets_at=NOW + 3_600_000)
        self.assertEqual(status, 202, second)
        self.assertEqual(second["block_id"], first["block_id"])
        self.assertEqual(second["retry_at"], NOW + 3_600_000 + MARGIN)
        self.assertEqual(self.retry_at_of(first["block_id"]), NOW + 3_600_000 + MARGIN)

    def test_a_join_with_an_earlier_reset_does_not_move_the_retry_back(self):
        self.start()
        _s, first = self.post(resets_at=NOW + 3_600_000)
        status, second = self.post(resets_at=NOW + 60_000)
        self.assertEqual(status, 202, second)
        self.assertEqual(second["retry_at"], NOW + 3_600_000 + MARGIN)
        self.assertEqual(self.retry_at_of(first["block_id"]), NOW + 3_600_000 + MARGIN)

    def test_a_join_with_an_unknown_reset_makes_the_retry_unknown(self):
        self.start()
        _s, first = self.post(resets_at=NOW + 60_000)
        self.assertEqual(first["retry_at"], NOW + 60_000 + MARGIN)
        status, second = self.post(resets_at=None)
        self.assertEqual(status, 202, second)
        self.assertIsNone(second["retry_at"])
        self.assertIsNone(self.retry_at_of(first["block_id"]))

    def test_a_known_reset_joining_an_unknown_one_leaves_it_unknown(self):
        self.start()
        _s, first = self.post(resets_at=None)
        status, second = self.post(resets_at=NOW + 60_000)
        self.assertEqual(status, 202, second)
        self.assertIsNone(second["retry_at"])
        self.assertIsNone(self.retry_at_of(first["block_id"]))

    def test_the_margin_is_added_exactly_once_across_insert_join_and_replay(self):
        self.start()
        reset = NOW + 3_600_000
        first_event = c5._limit_event(resets_at=reset)
        second_event = c5._limit_event(resets_at=reset)          # the same raw reset, a different event
        call = lambda e: c5._call(self.base, "POST", "/v1/limit-events", body=e,  # noqa: E731
                                  identity=IDENTITY_A, idempotency_key=e["event_id"])
        _s, a = call(first_event)
        self.assertEqual(a["retry_at"], reset + MARGIN)
        _s, replay_a = call(first_event)
        self.assertEqual(replay_a["retry_at"], reset + MARGIN)
        _s, b = call(second_event)                                # a join: the old value is not margined again
        self.assertEqual(b["block_id"], a["block_id"])
        self.assertEqual(b["retry_at"], reset + MARGIN)
        _s, replay_b = call(second_event)
        self.assertEqual(replay_b["retry_at"], reset + MARGIN)
        self.assertEqual(self.retry_at_of(a["block_id"]), reset + MARGIN)
        self.assertEqual(c5._rows(self.db, "SELECT COUNT(*) FROM audit WHERE action='limit_event_recorded'"),
                         [(2,)])

    def test_the_input_ceiling_keeps_the_sum_inside_the_store(self):
        self.start()
        top = INT64_MAX - MARGIN
        status, body = self.post(resets_at=top)
        self.assertEqual(status, 202, body)
        self.assertEqual(body["retry_at"], INT64_MAX)
        status, body = self.post(resets_at=top + 1)
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")


class BeforeTheChange(c5._ServerCase):
    """A store written by the code before the margin: raw `retry_at` on its block and raw `resets_at`
    on its event. After the change a replay of that event must not conflict and must not add the margin;
    a block that already exists keeps its stored `retry_at` (no backfill)."""

    def old_style_block(self, *, reset, state="parked", legacy=0):
        self.start()
        block_id, event_id, turn_id = (str(uuid.uuid4()) for _ in range(3))
        _exec(self.db, "INSERT INTO blocks(id, account_id, model_id, reason, retry_at, state, source_event_id,"
                       " updated_at) VALUES(?,?,?,?,?,?,?,?)",
              (block_id, ACCOUNT, None, "provider_usage_429", reset, state, event_id, NOW))
        _exec(self.db, "INSERT INTO limit_events(event_id, account_id, model_id, turn_id, observed_at,"
                       " evidence_kind, resets_at, exhausted_windows, block_id, received_at, legacy)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (event_id, ACCOUNT, None, turn_id, NOW, "provider_usage_429", reset, "[]", block_id, NOW, legacy))
        return block_id, event_id, turn_id

    def replay(self, event_id, turn_id, reset):
        event = c5._limit_event(event_id=event_id, turn_id=turn_id, resets_at=reset)
        return c5._call(self.base, "POST", "/v1/limit-events", body=event,
                        identity=IDENTITY_A, idempotency_key=event_id)

    def snapshot(self):
        return (c5._rows(self.db, "SELECT id, retry_at, state, updated_at FROM blocks ORDER BY id"),
                c5._rows(self.db, "SELECT COUNT(*) FROM limit_events"),
                c5._rows(self.db, "SELECT COUNT(*) FROM audit"))

    def test_a_duplicate_stored_before_the_change_is_a_pure_read_with_no_margin(self):
        reset = NOW + 3_600_000
        block_id, event_id, turn_id = self.old_style_block(reset=reset)
        before = self.snapshot()
        status, body = self.replay(event_id, turn_id, reset)
        self.assertEqual(status, 202, body)
        self.assertEqual(body["block_id"], block_id)
        self.assertEqual(body["retry_at"], reset)                 # raw: no margin added to an old block
        self.assertEqual(self.snapshot(), before)

    def test_a_changed_reset_under_an_old_event_id_is_still_a_conflict(self):
        reset = NOW + 3_600_000
        _block, event_id, turn_id = self.old_style_block(reset=reset)
        status, body = self.replay(event_id, turn_id, reset + MARGIN)    # the margined value is not the raw one
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "IDEMPOTENCY_CONFLICT")

    def test_a_new_event_joining_an_old_block_gets_the_margin_on_its_own_value_only(self):
        reset = NOW + 3_600_000
        block_id, _e, _t = self.old_style_block(reset=reset)
        later = NOW + 7_200_000
        status, body = self.limit_event(resets_at=later)
        self.assertEqual(status, 202, body)
        self.assertEqual(body["block_id"], block_id)
        self.assertEqual(body["retry_at"], later + MARGIN)
        earlier = NOW + 60_000
        status, body = self.limit_event(resets_at=earlier)
        self.assertEqual(body["retry_at"], later + MARGIN)        # the old raw value did not win; nor was it re-margined

    def test_an_old_raw_value_later_than_the_new_margined_one_still_wins(self):
        reset = NOW + 3_600_000
        block_id, _e, _t = self.old_style_block(reset=reset)
        status, body = self.limit_event(resets_at=reset - 60_000)    # new value + margin = reset - 30 000, below the old
        self.assertEqual(status, 202, body)
        self.assertEqual(body["block_id"], block_id)
        self.assertEqual(body["retry_at"], reset)                 # max(old raw, new + margin)

    def test_an_operator_cleared_block_is_never_re_parked_by_a_replay(self):
        self.start()
        reset = NOW + 3_600_000
        event = c5._limit_event(resets_at=reset)
        call = lambda: c5._call(self.base, "POST", "/v1/limit-events", body=event,  # noqa: E731
                                identity=IDENTITY_A, idempotency_key=event["event_id"])
        _s, first = call()
        _exec(self.db, "UPDATE blocks SET state='cleared' WHERE id=?", (first["block_id"],))
        before = self.snapshot()
        status, replayed = call()
        self.assertEqual(status, 202, replayed)
        self.assertEqual(replayed["state"], "cleared")
        self.assertEqual(replayed["retry_at"], reset + MARGIN)    # as stored; nothing recomputed
        self.assertEqual(self.snapshot(), before)

    def test_a_legacy_row_is_left_untouched(self):
        reset = NOW + 3_600_000
        block_id, event_id, turn_id = self.old_style_block(reset=reset, legacy=1)
        before = self.snapshot()
        # a legacy row's content is never compared: even different-looking content is a pure read
        status, body = self.replay(event_id, str(uuid.uuid4()), NOW + 5_000_000)
        self.assertEqual(status, 202, body)
        self.assertEqual(body["block_id"], block_id)
        self.assertEqual(body["retry_at"], reset)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
