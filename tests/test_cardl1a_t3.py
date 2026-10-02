"""Card L1a, turn 3: the safe block clear (`block-clear`), the overrun path
that re-parks a probing block, and the expiry sweep (`expire-sweep`)
(docs/DESIGN.md sections 4-5; docs/STEP-CARD-L1A.md, turn 3).

Rows: block_clear_audited, block_clear_refuses_unresolved_adjustment,
block_clear_not_found, overrun_reparks_probing, expire_sweep, and the
privacy_l1a extension. Stores are built through config-import v2; the only
SQL is test set-up of states no product path creates yet (a probing block,
a resolved adjustment). Synthetic data only.
"""

import json
import sqlite3
import time
import unittest
import uuid
from unittest import mock

from quota_balancer import errors, lease
from quota_balancer.errors import QuotaError

from tests.test_cardl1a import (_Base, _ids, _sized_snapshot, base_doc,
                                run_cli)
from tests.test_cardl1a_t2 import request

ERROR_NOT_FOUND = ('{"error":{"code":"NOT_FOUND","message":"the requested '
                   'object was not found"}}')
ERROR_PARKED = ('{"error":{"code":"ACCOUNT_PARKED","message":"the account '
                'is parked"}}')
ERROR_INVALID_REQUEST = ('{"error":{"code":"INVALID_REQUEST","message":"the '
                         'request is invalid"}}')
REASONS = ("reset_observed", "false_positive", "operator_override")


def doc():
    d = base_doc()
    d["limits"] = []
    return d


class _T3(_Base):
    def setUp(self):
        super().setUp()
        self.import_ok(doc())
        self.account_id = self.rows(
            "SELECT id FROM accounts WHERE alias='meter'")[0][0]

    # -- set-up helpers -------------------------------------------------

    def now(self):
        return int(time.time() * 1000)

    def limit_block(self, model=None, now_ms=None):
        """A provider-usage-limit block (no adjustment), parked."""
        now_ms = now_ms or self.now()
        model_id = self.ids("models")[model] if model else None
        return lease.record_limit_event(self.db, {
            "event_id": str(uuid.uuid4()), "account_id": self.account_id,
            "model_id": model_id, "turn_id": str(uuid.uuid4()),
            "observed_at": now_ms, "evidence_kind": "provider_usage_429",
            "resets_at": None, "exhausted_windows": []}, now_ms)

    def enforcing_lease(self, now_ms=None, turn=None, seat="s3"):
        """An enforcing metered lease (money hold, token): reachable only
        with card R1's gate, patched open for this set-up."""
        now_ms = now_ms or self.now()
        if not getattr(self, "_snapshot_sent", False):
            _sized_snapshot(self)
            self._snapshot_sent = True
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            body = request(_ids(self), seat=seat, now_ms=now_ms)
            if turn:
                body["turn_id"] = turn
            return lease.grant(self.db, body, now_ms)

    def overrun(self, lease_id, extra=500, now_ms=None, event_id=None):
        now_ms = now_ms or self.now()
        hold = self.rows("SELECT money_hold FROM leases WHERE id=?",
                         (lease_id,))[0][0]
        receipt = {
            "event_id": event_id or str(uuid.uuid4()), "lease_id": lease_id,
            "outcome": "completed", "calls": 1, "input_tokens": 500,
            "cached_input_tokens": 0, "output_tokens": 100,
            "actual_micro_eur": hold + extra,
            "gateway_id": self.ids("gateways")["g1"], "seq": 1, "final": 1,
            "observed_at": now_ms, "received_at": now_ms}
        lease.settle(self.db, receipt, now_ms)
        return receipt["event_id"]

    def blocks(self):
        return self.rows("SELECT account_id, model_id, reason, state, "
                         "retry_at, source_event_id, updated_at, id "
                         "FROM blocks ORDER BY id")

    def audit(self, action):
        return self.rows("SELECT actor_alias, object_id, redacted_details "
                         "FROM audit WHERE action=? ORDER BY rowid",
                         (action,))

    def clear(self, *extra, reason="reset_observed", account="meter"):
        return run_cli("--db", self.db, "block-clear", "--account", account,
                       *extra, "--reason", reason)

    def snapshot(self):
        return {t: sorted(self.rows("SELECT * FROM " + t), key=repr)
                for t in ("blocks", "adjustments", "audit", "leases",
                          "holds", "receipts")}


class BlockClearAudited(_T3):
    """block_clear_audited: a usage-limit block clears, with an audit row
    carrying only the closed reason and the previous state."""

    def test_a_usage_limit_block_clears_and_is_audited(self):
        self.limit_block()
        (account, model, reason, state, retry_at, source, updated,
         block_id) = self.blocks()[0]
        self.assertEqual(state, "parked")
        before = self.now()
        result = self.clear(reason="reset_observed")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {"state": "cleared", "replayed": False})
        self.assertEqual(result.stdout.strip(),
                         '{"state":"cleared","replayed":false}')
        after = self.blocks()[0]
        self.assertEqual(after[3], "cleared")
        self.assertGreaterEqual(after[6], before)
        # everything else about the row is as it was
        self.assertEqual((after[0], after[1], after[2], after[4], after[5],
                          after[7]),
                         (account, model, reason, retry_at, source, block_id))
        self.assertEqual(self.audit("block_cleared"), [(
            "operator", block_id,
            json.dumps({"reason": "reset_observed",
                        "previous_state": "parked"}))])
        keys = set(json.loads(self.audit("block_cleared")[0][2]))
        self.assertEqual(keys, {"reason", "previous_state"})

    def test_a_cleared_account_can_be_granted_again(self):
        self.limit_block()
        body = request(_ids(self))
        with self.assertRaises(QuotaError) as caught:
            lease.grant(self.db, body, self.now())
        self.assertEqual(caught.exception.code, errors.CODE_ACCOUNT_PARKED)
        self.assertEqual(self.clear().returncode, 0)
        self.assertEqual(lease.grant(self.db, request(_ids(self)),
                                     self.now())["state"], "active")

    def test_every_closed_reason_and_a_probing_block(self):
        for reason in REASONS:
            self.limit_block()
            conn = sqlite3.connect(self.db)
            conn.execute("UPDATE blocks SET state='probing' "
                         "WHERE state='parked'")
            conn.commit()
            conn.close()
            result = self.clear(reason=reason)
            self.assertEqual(result.returncode, 0, result.stderr)
            details = json.loads(self.audit("block_cleared")[-1][2])
            self.assertEqual(details, {"reason": reason,
                                       "previous_state": "probing"})
        self.assertEqual(len(self.audit("block_cleared")), 3)

    def test_a_replayed_clear_is_a_noop_without_a_second_audit_row(self):
        self.limit_block()
        self.assertEqual(self.clear().returncode, 0)
        snapshot = self.snapshot()
        again = self.clear(reason="false_positive")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(json.loads(again.stdout),
                         {"state": "cleared", "replayed": True})
        self.assertEqual(self.snapshot(), snapshot)
        self.assertEqual(len(self.audit("block_cleared")), 1)

    def test_a_model_block_and_the_account_wide_block_are_separate(self):
        self.limit_block()
        self.limit_block(model="opus")
        rows = self.blocks()
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.clear("--model", "opus").returncode, 0)
        states = {r[1]: r[3] for r in self.blocks()}
        self.assertEqual(states, {None: "parked",
                                  self.ids("models")["opus"]: "cleared"})
        self.assertEqual(self.clear().returncode, 0)
        self.assertEqual({r[3] for r in self.blocks()}, {"cleared"})
        self.assertEqual(len(self.audit("block_cleared")), 2)

    def test_a_new_block_after_a_clear_can_be_cleared_again(self):
        self.limit_block()
        self.assertEqual(self.clear().returncode, 0)
        self.limit_block()
        result = self.clear()
        self.assertEqual(json.loads(result.stdout)["replayed"], False)
        self.assertEqual(len(self.audit("block_cleared")), 2)


class BlockClearRefusesUnresolvedAdjustment(_T3):
    """block_clear_refuses_unresolved_adjustment (the N3 rule): every
    unresolved adjustment of the account stops a clear."""

    def assert_refused(self):
        before = self.snapshot()
        result = self.clear()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_PARKED)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.snapshot(), before)

    def test_an_overrun_block_cannot_be_cleared(self):
        made = self.enforcing_lease()
        self.overrun(made["lease_id"])
        self.assertEqual(self.rows(
            "SELECT COUNT(*) FROM adjustments WHERE resolved_at IS NULL"),
            [(1,)])
        self.assert_refused()
        self.assertEqual(self.blocks()[0][3], "parked")

    def test_adjustment_of_another_event_than_the_block_source(self):
        made = self.enforcing_lease()
        self.limit_block()                       # block from a usage limit
        event = self.overrun(made["lease_id"])   # overrun while blocked
        block = self.blocks()[0]
        self.assertNotEqual(block[5], event)     # not the block's source
        self.assertEqual(self.rows(
            "SELECT source_event_id FROM adjustments"), [(event,)])
        self.assert_refused()

    def test_two_adjustments_one_resolved_still_refuses(self):
        first = self.enforcing_lease()
        second = self.enforcing_lease(turn=str(uuid.uuid4()))
        self.overrun(first["lease_id"])
        self.overrun(second["lease_id"])
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE adjustments SET resolved_at=? WHERE rowid="
                     "(SELECT MIN(rowid) FROM adjustments)", (self.now(),))
        conn.commit()
        conn.close()
        self.assert_refused()

    def test_a_resolved_adjustment_no_longer_blocks_the_clear(self):
        made = self.enforcing_lease()
        self.overrun(made["lease_id"])
        self.assert_refused()
        # card R1 will resolve adjustments; until then this is set-up SQL.
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE adjustments SET resolved_at=?", (self.now(),))
        conn.commit()
        conn.close()
        self.assertEqual(self.clear().returncode, 0)
        self.assertEqual(self.blocks()[0][3], "cleared")

    def test_an_adjustment_of_another_account_does_not_block(self):
        self.limit_block()
        conn = sqlite3.connect(self.db)
        other = self.rows("SELECT id FROM accounts WHERE alias='other'")[0][0]
        gateway = self.ids("gateways")["g1"]
        conn.execute(
            "INSERT INTO adjustments(id, account_id, gateway_id, "
            "source_event_id, period_start, unattributed_micro_eur, reason, "
            "resolved_at) VALUES(?,?,?,?,?,?,?,NULL)",
            (str(uuid.uuid4()), other, gateway, str(uuid.uuid4()), 1, 5,
             "overrun"))
        conn.commit()
        conn.close()
        self.assertEqual(self.clear().returncode, 0)


class BlockClearNotFound(_T3):
    """block_clear_not_found and the closed inputs."""

    def test_no_block_at_all(self):
        before = self.snapshot()
        result = self.clear()
        self.assertEqual((result.returncode, result.stderr.strip()),
                         (2, ERROR_NOT_FOUND))
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.snapshot(), before)

    def test_scope_must_match_exactly(self):
        self.limit_block()                                  # account-wide
        result = self.clear("--model", "opus")
        self.assertEqual(result.stderr.strip(), ERROR_NOT_FOUND)
        self.limit_block(model="fast")
        result = self.clear()                               # not the model's
        self.assertEqual(result.returncode, 0)
        self.assertEqual([r[3] for r in self.blocks() if r[1] is None],
                         ["cleared"])

    def test_unknown_account_and_model(self):
        self.limit_block()
        self.assertEqual(self.clear(account="ghost").stderr.strip(),
                         ERROR_NOT_FOUND)
        self.assertEqual(self.clear("--model", "ghost").stderr.strip(),
                         ERROR_NOT_FOUND)
        self.assertEqual(self.blocks()[0][3], "parked")

    def test_the_reason_is_a_closed_enum_and_aliases_are_checked(self):
        self.limit_block()
        before = self.snapshot()
        for reason in ("because", "", "RESET_OBSERVED", "reset_observed "):
            result = self.clear(reason=reason)
            self.assertEqual((result.returncode, result.stderr.strip()),
                             (2, ERROR_INVALID_REQUEST), reason)
        for account in ("Meter", "m e", "a" * 65, "1x"):
            result = self.clear(account=account)
            self.assertEqual((result.returncode, result.stderr.strip()),
                             (2, ERROR_INVALID_REQUEST), account)
        result = self.clear("--model", "Opus")
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_REQUEST)
        self.assertEqual(self.snapshot(), before)


class OverrunReparksProbing(_T3):
    """overrun_reparks_probing: an overrun turns a live probing block back
    to parked instead of skipping it, and every overrun is traceable."""

    def test_an_existing_probing_block_becomes_parked(self):
        first = self.enforcing_lease()
        self.limit_block()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE blocks SET state='probing', updated_at=1")
        conn.commit()
        conn.close()
        now = self.now()
        event = self.overrun(first["lease_id"], now_ms=now)
        rows = self.blocks()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], "parked")
        self.assertEqual(rows[0][6], now)
        audit = self.audit("overrun_block")
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0][0:2], ("coordinator", rows[0][7]))
        self.assertEqual(json.loads(audit[0][2]),
                         {"event_id": event, "previous_state": "probing"})

    def test_a_parked_block_stays_parked_and_keeps_its_source(self):
        first = self.enforcing_lease()
        self.limit_block()
        before = self.blocks()[0]
        self.overrun(first["lease_id"])
        after = self.blocks()[0]
        self.assertEqual(after[3], "parked")
        self.assertEqual((after[0], after[1], after[2], after[5], after[7]),
                         (before[0], before[1], before[2], before[5],
                          before[7]))
        self.assertEqual(len(self.blocks()), 1)

    def test_two_overruns_are_both_traceable_on_one_block(self):
        first = self.enforcing_lease()
        second = self.enforcing_lease(turn=str(uuid.uuid4()))
        e1 = self.overrun(first["lease_id"])
        e2 = self.overrun(second["lease_id"])
        self.assertEqual(len(self.blocks()), 1)
        self.assertEqual(self.blocks()[0][5], e1)    # first event keys it
        details = [json.loads(r[2]) for r in self.audit("overrun_block")]
        self.assertEqual(details, [
            {"event_id": e1, "previous_state": None},
            {"event_id": e2, "previous_state": "parked"}])
        self.assertEqual(self.rows(
            "SELECT source_event_id FROM adjustments ORDER BY rowid"),
            [(e1,), (e2,)])

    def test_a_model_scoped_block_is_not_the_accounts_overrun_block(self):
        first = self.enforcing_lease()
        self.limit_block(model="opus")
        self.overrun(first["lease_id"])
        scopes = sorted((r[1] is None, r[3]) for r in self.blocks())
        self.assertEqual(scopes, [(False, "parked"), (True, "parked")])


class ExpireSweep(_T3):
    """expire_sweep: expired active leases become uncertain, keeping their
    holds and slot; nothing is settled, refunded or deleted."""

    TTL = 1_800_000

    def setUp(self):
        super().setUp()
        self.t0 = self.now() - 10 * 3_600_000

    def test_expired_leases_become_uncertain_and_keep_everything(self):
        expired = self.enforcing_lease(now_ms=self.t0)
        turn = self.rows("SELECT turn_id FROM leases")[0][0]
        later = self.enforcing_lease(now_ms=self.t0 + 60_000,
                                     turn=str(uuid.uuid4()))
        settled = self.enforcing_lease(now_ms=self.t0,   # also due, but settled
                                       turn=str(uuid.uuid4()))
        lease.settle(self.db, {
            "event_id": str(uuid.uuid4()), "lease_id": settled["lease_id"],
            "outcome": "completed", "calls": 1, "input_tokens": 1,
            "cached_input_tokens": 0, "output_tokens": 1,
            "actual_micro_eur": 5, "gateway_id": self.ids("gateways")["g1"],
            "seq": 1, "final": 1, "observed_at": self.t0 + 2,
            "received_at": self.t0 + 2}, self.t0 + 2)
        holds_before = self.rows("SELECT * FROM holds ORDER BY rowid")
        receipts_before = self.rows("SELECT * FROM receipts")
        lease_count = self.rows("SELECT COUNT(*) FROM leases")
        sweep_at = self.t0 + self.TTL           # exactly expires_at: expired
        result = lease.expire_sweep(self.db, sweep_at)
        self.assertEqual(result, {"expired": 1})   # the settled one is not
        states = dict(self.rows("SELECT id, state FROM leases"))
        self.assertEqual(states[expired["lease_id"]], "uncertain")
        self.assertEqual(states[later["lease_id"]], "active")
        self.assertEqual(states[settled["lease_id"]], "settled")
        self.assertEqual(self.rows("SELECT * FROM holds ORDER BY rowid"),
                         holds_before)
        self.assertEqual(self.rows("SELECT * FROM receipts"), receipts_before)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM leases"), lease_count)
        self.assertEqual(self.rows(
            "SELECT money_hold FROM leases WHERE id=?",
            (expired["lease_id"],)), [(expired["money_hold"],)])
        audit = self.audit("lease_expired")
        self.assertEqual([r[1] for r in audit], [expired["lease_id"]])
        self.assertEqual(audit[0][0], "coordinator")
        # the slot is kept: the same seat and turn still conflicts
        body = request(_ids(self), seat="s3", now_ms=sweep_at)
        body["turn_id"] = turn
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            with self.assertRaises(QuotaError) as caught:
                lease.grant(self.db, body, sweep_at)
        self.assertEqual(caught.exception.code,
                         errors.CODE_IDEMPOTENCY_CONFLICT)

    def test_one_millisecond_early_expires_nothing(self):
        self.enforcing_lease(now_ms=self.t0)
        self.assertEqual(lease.expire_sweep(self.db, self.t0 + self.TTL - 1),
                         {"expired": 0})
        self.assertEqual(self.rows("SELECT state FROM leases"), [("active",)])
        self.assertEqual(self.audit("lease_expired"), [])

    def test_a_second_sweep_changes_nothing(self):
        self.enforcing_lease(now_ms=self.t0)
        self.enforcing_lease(now_ms=self.t0, turn=str(uuid.uuid4()))
        at = self.t0 + self.TTL + 5
        self.assertEqual(lease.expire_sweep(self.db, at), {"expired": 2})
        snapshot = self.snapshot()
        self.assertEqual(lease.expire_sweep(self.db, at + 1000),
                         {"expired": 0})
        self.assertEqual(self.snapshot(), snapshot)

    def test_a_late_final_receipt_still_settles_an_uncertain_lease(self):
        made = self.enforcing_lease(now_ms=self.t0)
        lease.expire_sweep(self.db, self.t0 + self.TTL)
        self.overrun(made["lease_id"], extra=-1, now_ms=self.t0 + self.TTL + 9)
        self.assertEqual(self.rows("SELECT state FROM leases"),
                         [("settled",)])

    def test_advisory_leases_expire_too_without_holds(self):
        now = self.t0
        body = request(_ids(self), seat="s1", now_ms=now)
        made = lease.grant(self.db, body, now)
        self.assertEqual(made["state"], "active")
        self.assertEqual(lease.expire_sweep(self.db, now + self.TTL),
                         {"expired": 1})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM holds"), [(0,)])

    def test_cli_sweep_prints_a_closed_summary(self):
        self.enforcing_lease(now_ms=self.t0)
        result = run_cli("--db", self.db, "expire-sweep")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '{"expired":1}')
        again = run_cli("--db", self.db, "expire-sweep")
        self.assertEqual(again.stdout.strip(), '{"expired":0}')
        self.assertEqual(again.stderr, "")

    def test_the_cli_sweep_needs_an_existing_store(self):
        result = run_cli("--db", self.db + ".missing", "expire-sweep")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")


class PrivacyL1aTurn3(_T3):
    """privacy_l1a extension: the new verbs echo nothing from their input."""

    SENTINELS = ("SENTINEL-SECRET-9f2c", "C:\\Users\\sentinel\\secret.txt",
                 "/etc/sentinel-passwd", "sk-sentinel-key-0001")

    def test_block_clear_outputs_and_errors_carry_no_sentinel(self):
        made = self.enforcing_lease()
        self.limit_block()
        for sentinel in self.SENTINELS:
            for args in (("--account", sentinel, "--reason", "reset_observed"),
                         ("--account", "meter", "--model", sentinel,
                          "--reason", "reset_observed"),
                         ("--account", "meter", "--reason", sentinel)):
                result = run_cli("--db", self.db, "block-clear", *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(sentinel, result.stdout + result.stderr)
        # a refused clear (unresolved adjustment)
        self.overrun(made["lease_id"])
        refused = self.clear()
        self.assertEqual(refused.stderr.strip(), ERROR_PARKED)
        for table in ("audit",):
            text = json.dumps(self.rows("SELECT * FROM " + table), default=str)
            for sentinel in self.SENTINELS:
                self.assertNotIn(sentinel, text)

    def test_the_audit_row_has_only_the_closed_vocabulary(self):
        self.limit_block()
        self.assertEqual(self.clear(reason="operator_override").returncode, 0)
        row = self.audit("block_cleared")[0]
        self.assertEqual(row[0], "operator")
        self.assertEqual(json.loads(row[2]),
                         {"reason": "operator_override",
                          "previous_state": "parked"})

    def test_expire_sweep_prints_no_ids_or_aliases(self):
        made = self.enforcing_lease(now_ms=self.t0_far())
        result = run_cli("--db", self.db, "expire-sweep")
        text = result.stdout + result.stderr
        for needle in (made["lease_id"], "meter", "desktop", "s3", "opus"):
            self.assertNotIn(needle, text)
        self.assertEqual(json.loads(result.stdout), {"expired": 1})

    def t0_far(self):
        return self.now() - 10 * 3_600_000

    def test_the_new_code_does_no_file_or_environment_access(self):
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "quota_balancer", "lease.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        for needle in ("os.environ", "getenv", "print(", "subprocess",
                       "import socket"):
            self.assertFalse(needle in source, needle)


if __name__ == "__main__":
    unittest.main()
