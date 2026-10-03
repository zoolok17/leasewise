"""Card L6a-0: a final receipt from the lease's own gateway may carry UNKNOWN token counts.

When a gateway "charges the reserve" for an uncertain attempt it knows the money but not the tokens.
Leasewise used to refuse any receipt with a null count for a lease that is not shadow, so such a lease
could never settle. The rule (docs/DESIGN-6A section 8.2): null input and/or output counts are accepted
only on a FINAL receipt that names the lease's OWN gateway, carries `actual_micro_eur`, has a whole
`cached_input_tokens`, for a `metered` or `metered_advisory` lease. The lease is then `usage_unknown`
(a derived mark, no new column). A null never becomes 0. Synthetic data only.
"""

import json
import sqlite3
import threading
import time
import unittest
import uuid
from unittest import mock

from quota_balancer import errors, http_api, lease
from quota_balancer.errors import QuotaError

from tests import test_cardl1b as l1b
from tests.test_cardl1a import _Base, _call, _grant, _ids, _sized_snapshot, base_doc, run_cli

OWN = "g1"                                   # the gateway alias in the test store (machine desktop)


def _conn(db):
    return sqlite3.connect(db)


class _Settle(l1b._L1b):
    """The standard shadow, subscription and metered-advisory seats of the L1b store."""

    def gateway(self):
        return _ids(self)["gateways"][OWN]

    def advisory(self):
        body, now = self.known("s1")
        made = self.grant(body, now)
        self.assertEqual(made["enforcement"], "metered_advisory")
        return made["lease_id"], now

    def final(self, lease_id, now, **over):
        receipt = self.receipt(lease_id, True, None, now, actual=450, gateway=self.gateway())
        receipt.update(over)
        return receipt

    def settle(self, receipt, now):
        return lease.settle(self.db, receipt, now)

    def refused(self, receipt, now, code):
        with self.assertRaises(QuotaError) as caught:
            self.settle(receipt, now)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def stored(self, lease_id):
        return self.rows("SELECT input_tokens, cached_input_tokens, output_tokens, actual_micro_eur, final "
                         "FROM receipts WHERE lease_id=?", (lease_id,))

    def usage_unknown(self, lease_id):
        conn = _conn(self.db)
        try:
            return lease.usage_unknown(conn, lease_id)
        finally:
            conn.close()

    def audit(self, lease_id):
        rows = self.rows("SELECT redacted_details FROM audit WHERE action='lease_settled' AND object_id=?",
                         (lease_id,))
        return [json.loads(r[0]) for r in rows]


class Accepted(_Settle):

    def test_both_counts_null_with_cost_settles_the_lease_and_marks_it(self):
        lease_id, now = self.advisory()
        done = self.settle(self.final(lease_id, now, input_tokens=None, output_tokens=None), now)
        self.assertEqual(done["state"], "settled")
        self.assertEqual(self.rows("SELECT state, outcome FROM leases WHERE id=?", (lease_id,)),
                         [("settled", "completed")])
        self.assertEqual(self.stored(lease_id), [(None, 0, None, 450, 1)])          # nulls stay null: never 0
        self.assertTrue(self.usage_unknown(lease_id))
        self.assertEqual(self.audit(lease_id), [{"final": 1, "usage_unknown": 1}])

    def test_input_null_with_output_known_and_the_reverse(self):
        for over in ({"input_tokens": None, "output_tokens": 7}, {"input_tokens": 9, "output_tokens": None}):
            with self.subTest(over=over):
                body, now = self.known("s1", turn_id=str(uuid.uuid4()))
                lease_id = self.grant(body, now)["lease_id"]
                self.settle(self.final(lease_id, now, **over), now)
                row = self.stored(lease_id)[0]
                self.assertEqual((row[0], row[2]), (over["input_tokens"], over["output_tokens"]))
                self.assertTrue(self.usage_unknown(lease_id))
                self.assertEqual(self.audit(lease_id), [{"final": 1, "usage_unknown": 1}])

    def test_a_receipt_with_every_count_known_is_not_marked(self):
        lease_id, now = self.advisory()
        self.settle(self.final(lease_id, now), now)
        self.assertFalse(self.usage_unknown(lease_id))
        self.assertEqual(self.audit(lease_id), [{"final": 1}])                       # exactly as before

    def test_the_predicate_is_false_for_a_lease_without_a_final_receipt_or_that_does_not_exist(self):
        lease_id, now = self.advisory()
        self.assertFalse(self.usage_unknown(lease_id))
        self.assertFalse(self.usage_unknown(str(uuid.uuid4())))
        self.settle(self.receipt(lease_id, False, None, now, actual=1, gateway=self.gateway()), now)   # non-final
        self.assertFalse(self.usage_unknown(lease_id))

    def test_a_zero_spend_final_with_unknown_counts_is_accepted_too(self):
        lease_id, now = self.advisory()
        self.settle(self.final(lease_id, now, input_tokens=None, output_tokens=None, actual_micro_eur=0,
                               calls=0), now)
        self.assertEqual(self.stored(lease_id), [(None, 0, None, 0, 1)])


class Refused(_Settle):

    def test_without_the_cost_it_is_invalid(self):
        lease_id, now = self.advisory()
        self.refused(self.final(lease_id, now, input_tokens=None, output_tokens=None, actual_micro_eur=None),
                     now, errors.CODE_INVALID_REQUEST)
        self.assertEqual(self.rows("SELECT state FROM leases WHERE id=?", (lease_id,)), [("active",)])
        self.assertEqual(self.stored(lease_id), [])

    def test_a_null_gateway_is_invalid(self):
        lease_id, now = self.advisory()
        self.refused(self.final(lease_id, now, input_tokens=None, output_tokens=None, gateway_id=None),
                     now, errors.CODE_INVALID_REQUEST)
        self.assertEqual(self.stored(lease_id), [])

    def test_another_gateway_is_a_conflict_and_parks_the_account(self):
        lease_id, now = self.advisory()
        self.refused(self.final(lease_id, now, input_tokens=None, output_tokens=None,
                                gateway_id=str(uuid.uuid4())), now, errors.CODE_RECEIPT_CONFLICT)
        self.assertEqual(self.rows("SELECT reason, state FROM blocks"), [("receipt_conflict", "parked")])
        self.assertEqual(self.stored(lease_id), [])
        self.assertEqual(self.rows("SELECT state FROM leases WHERE id=?", (lease_id,)), [("active",)])

    def test_a_non_final_receipt_with_unknown_counts_is_invalid(self):
        lease_id, now = self.advisory()
        partial = self.receipt(lease_id, False, None, now, actual=5, gateway=self.gateway())
        partial.update(input_tokens=None, output_tokens=None)
        self.refused(partial, now, errors.CODE_INVALID_REQUEST)

    def test_an_unknown_cached_count_is_still_invalid(self):
        lease_id, now = self.advisory()
        self.refused(self.final(lease_id, now, input_tokens=None, output_tokens=None, cached_input_tokens=None),
                     now, errors.CODE_INVALID_REQUEST)
        self.refused(self.final(lease_id, now, cached_input_tokens=None), now, errors.CODE_INVALID_REQUEST)

    def strict(self, seat):
        body, now = self.known(seat)
        made = self.grant(body, now)
        receipt = self.receipt(made["lease_id"], True, None, now, actual=450, gateway=None)
        receipt.update(input_tokens=None, output_tokens=None)
        self.refused(receipt, now, errors.CODE_INVALID_REQUEST)
        receipt["gateway_id"] = self.gateway()                               # a gateway it does not have
        self.refused(receipt, now, errors.CODE_RECEIPT_CONFLICT)

    def test_a_subscription_admission_lease_stays_strict(self):
        self.strict("s4")

    def test_a_subscription_warn_lease_stays_strict(self):
        self.strict("s5")

    def test_the_rule_names_its_enforcements_not_just_a_gateway(self):
        """A lease row that has the gateway but a subscription enforcement (a state the grant path does not
        produce today) is still strict: the rule lists metered and metered_advisory, nothing else."""
        lease_id, now = self.advisory()
        conn = _conn(self.db)
        try:
            conn.execute("UPDATE leases SET enforcement='subscription_warn' WHERE id=?", (lease_id,))
            conn.commit()
        finally:
            conn.close()
        self.refused(self.final(lease_id, now, input_tokens=None, output_tokens=None), now,
                     errors.CODE_INVALID_REQUEST)

    def test_a_shadow_lease_is_unchanged(self):
        body, now = self.unknown("s6")
        lease_id = self.grant(body, now)["lease_id"]
        receipt = self.receipt(lease_id, True, None, now, actual=None, gateway=None)
        receipt.update(input_tokens=None, cached_input_tokens=None, output_tokens=None)
        self.settle(receipt, now)
        self.assertEqual(self.stored(lease_id), [(None, None, None, None, 1)])
        self.assertEqual(self.audit(lease_id), [{"final": 1}])                       # no new mark for shadow
        self.assertFalse(self.usage_unknown(lease_id))

    def test_a_metered_lease_still_needs_the_cost_on_a_final_receipt(self):
        _sized_snapshot(self)
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            body, now = self.known("s3")
            made = self.grant(body, now)
        self.assertEqual(made["enforcement"], "metered")
        self.refused(self.final(made["lease_id"], now, input_tokens=None, output_tokens=None,
                                actual_micro_eur=None), now, errors.CODE_INVALID_REQUEST)
        self.settle(self.final(made["lease_id"], now, input_tokens=None, output_tokens=None), now)
        self.assertTrue(self.usage_unknown(made["lease_id"]))


class Identity(_Settle):

    def test_an_identical_replay_is_a_replay_and_changes_nothing(self):
        lease_id, now = self.advisory()
        receipt = self.final(lease_id, now, input_tokens=None, output_tokens=None)
        first = self.settle(receipt, now)
        before = (self.rows("SELECT * FROM receipts"), self.rows("SELECT * FROM audit"))
        again = self.settle(dict(receipt), now + 5)
        self.assertFalse(first["replayed"])
        self.assertTrue(again["replayed"])
        self.assertEqual((self.rows("SELECT * FROM receipts"), self.rows("SELECT * FROM audit")), before)

    def test_a_changed_body_with_the_same_event_id_is_an_idempotency_conflict(self):
        lease_id, now = self.advisory()
        receipt = self.final(lease_id, now, input_tokens=None, output_tokens=None)
        self.settle(receipt, now)
        for over in ({"input_tokens": 0}, {"output_tokens": 0}, {"actual_micro_eur": 451},
                     {"input_tokens": 5, "output_tokens": None}):
            changed = dict(receipt, **over)
            self.refused(changed, now, errors.CODE_IDEMPOTENCY_CONFLICT)
        self.assertEqual(len(self.stored(lease_id)), 1)


class Holds(_Base):
    """A metered lease that holds tokens and money: unknown tokens keep the token hold whole."""

    def setUp(self):
        super().setUp()
        self.import_ok(base_doc())

    def metered(self):
        _sized_snapshot(self)
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            made = _grant(self, "s3", OWN)
        self.assertEqual(made["enforcement"], "metered")
        return made

    def receipt(self, lease_id, **over):
        now = int(time.time() * 1000)
        doc = {"event_id": str(uuid.uuid4()), "lease_id": lease_id, "outcome": "completed", "calls": 1,
               "input_tokens": None, "cached_input_tokens": 0, "output_tokens": None, "actual_micro_eur": 5,
               "gateway_id": _ids(self)["gateways"][OWN], "seq": 1, "final": 1, "observed_at": now,
               "received_at": now}
        doc.update(over)
        return doc

    def holds(self, lease_id):
        return self.rows("SELECT h.limit_id, h.reserved, h.settled_actual, l.metric FROM holds h "
                         "LEFT JOIN limits l ON l.id=h.limit_id WHERE h.lease_id=? ORDER BY h.limit_id",
                         (lease_id,))

    def test_the_token_hold_is_kept_whole_and_the_money_hold_follows_the_cost(self):
        made = self.metered()
        lease_id, hold = made["lease_id"], made["money_hold"]
        before = self.holds(lease_id)
        self.assertTrue(any(row[3] == "input_tokens" for row in before), before)
        done = lease.settle(self.db, self.receipt(lease_id, actual_micro_eur=5), int(time.time() * 1000))
        self.assertEqual((done["retained_micro_eur"], done["released_micro_eur"]), (5, hold - 5))
        for _limit, reserved, settled, metric in self.holds(lease_id):
            if metric == "input_tokens":
                self.assertEqual(settled, reserved, "the whole token hold is kept: no refund, no invented zero")
            else:
                self.assertEqual(settled, 5)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM adjustments"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM audit WHERE action='overrun_recorded'"), [(0,)])

    def test_spend_over_the_hold_still_records_a_money_overrun_and_no_token_overrun(self):
        made = self.metered()
        lease_id, hold = made["lease_id"], made["money_hold"]
        lease.settle(self.db, self.receipt(lease_id, actual_micro_eur=hold + 9), int(time.time() * 1000))
        self.assertEqual(self.rows("SELECT unattributed_micro_eur, reason FROM adjustments"), [(9, "overrun")])
        self.assertEqual(self.rows("SELECT reason, state FROM blocks"), [("overrun", "parked")])
        metrics = [json.loads(r[0])["metric"] for r in
                   self.rows("SELECT redacted_details FROM audit WHERE action='overrun_recorded'")]
        self.assertEqual(set(metrics), {"money_micro_eur"})                        # never a token overrun
        for _limit, reserved, settled, metric in self.holds(lease_id):
            if metric == "input_tokens":
                self.assertEqual(settled, reserved)

    def test_known_counts_settle_the_token_hold_as_before(self):
        made = self.metered()
        lease.settle(self.db, self.receipt(made["lease_id"], input_tokens=3, output_tokens=2, actual_micro_eur=5),
                     int(time.time() * 1000))
        for _limit, reserved, settled, metric in self.holds(made["lease_id"]):
            if metric == "input_tokens":
                self.assertEqual(settled, 3)

    def test_the_account_budget_sum_ignores_tokens_and_counts_the_cost(self):
        made = self.metered()
        lease.settle(self.db, self.receipt(made["lease_id"], actual_micro_eur=77), int(time.time() * 1000))
        total = self.rows("SELECT COALESCE(SUM(r.actual_micro_eur),0) FROM receipts r JOIN leases l "
                          "ON l.id=r.lease_id WHERE r.final=1 AND l.enforcement <> 'shadow'")
        self.assertEqual(total, [(77,)])


class Store(_Base):

    def test_the_live_stores_receipt_token_columns_are_nullable(self):
        self.import_ok(base_doc())
        info = {row[1]: row[3] for row in self.rows("PRAGMA table_info(receipts)")}      # name -> notnull
        for column in ("input_tokens", "cached_input_tokens", "output_tokens"):
            self.assertEqual(info[column], 0, column)
        self.assertEqual(info["calls"], 1)                                              # calls stays required

    def test_a_fresh_store_before_any_import_has_the_same_columns(self):
        info = {row[1]: row[3] for row in self.rows("PRAGMA table_info(receipts)")}
        self.assertEqual([info[c] for c in ("input_tokens", "cached_input_tokens", "output_tokens")], [0, 0, 0])


class OverHttp(l1b._L1b):
    """The real handler, over loopback."""

    def setUp(self):
        super().setUp()
        self.assertEqual(run_cli("--db", self.db, "identity-set", "--machine", "desktop",
                                 "--identity-ref", "identity-desktop", "--enabled", "true").returncode, 0)
        self.server = http_api.make_server(self.db, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def wire(self, **over):
        doc = {"event_id": str(uuid.uuid4()), "outcome": "completed", "calls": 2, "input_tokens": None,
               "cached_input_tokens": 0, "output_tokens": None, "actual_micro_eur": 450,
               "gateway_id": _ids(self)["gateways"][OWN], "seq": 4, "final": True}
        doc.update(over)
        return doc

    def post(self, lease_id, body):
        return _call(self.base, "/v1/leases/%s/settle" % lease_id, body, body["event_id"])

    def grant_advisory(self):
        body, now = self.known("s1")
        return self.grant(body, now)["lease_id"]

    def test_a_null_count_final_receipt_settles_over_http(self):
        lease_id = self.grant_advisory()
        status, answer = self.post(lease_id, self.wire())
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["state"], "settled")
        self.assertEqual(self.rows("SELECT input_tokens, output_tokens, actual_micro_eur FROM receipts "
                                   "WHERE lease_id=?", (lease_id,)), [(None, None, 450)])
        conn = _conn(self.db)
        try:
            self.assertTrue(lease.usage_unknown(conn, lease_id))
        finally:
            conn.close()

    def test_the_replay_is_200_and_a_changed_body_is_409(self):
        lease_id = self.grant_advisory()
        body = self.wire()
        self.assertEqual(self.post(lease_id, body)[0], 200)
        self.assertEqual(self.post(lease_id, dict(body))[0], 200)
        status, answer = self.post(lease_id, dict(body, actual_micro_eur=451))
        self.assertEqual((status, answer["error"]["code"]), (409, "IDEMPOTENCY_CONFLICT"))

    def test_refusals_over_http(self):
        lease_id = self.grant_advisory()
        for over, status_expected, code in (
                ({"actual_micro_eur": None}, 400, "INVALID_REQUEST"),
                ({"gateway_id": None}, 400, "INVALID_REQUEST"),
                ({"final": False}, 400, "INVALID_REQUEST"),
                ({"cached_input_tokens": None}, 400, "INVALID_REQUEST")):
            status, answer = self.post(lease_id, self.wire(**over))
            self.assertEqual((status, answer["error"]["code"]), (status_expected, code), over)
        status, answer = self.post(lease_id, self.wire(gateway_id=str(uuid.uuid4())))
        self.assertEqual((status, answer["error"]["code"]), (409, "RECEIPT_CONFLICT"))
        self.assertEqual(self.rows("SELECT state FROM leases WHERE id=?", (lease_id,)), [("active",)])


if __name__ == "__main__":
    unittest.main()
