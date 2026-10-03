"""Card R1-M, part 2, fix round 1: a shared cap converts each account's spending with that account's OWN policy (D-172).

A team or project cap is shared by several accounts, and each account's money is in its own ledger basis at its own VAT rate. The
first build converted every account's past spending with the policy of whichever account was asking, so a second account with
another rate could make spending that was already over the cap look below it, and its lease was admitted. Now each contributing
account's receipts and holds are read separately, converted with that account's own policy (an exposure, rounded up), and only then
added; the requester's policy is never applied to another account's spending, and no account is left out.

Synthetic amounts and policies only. The second account is a copy of the first, with its own seat, gateway and model.
"""

import contextlib
import sqlite3
import uuid

from quota_balancer import errors, lease, money
from quota_balancer.errors import QuotaError

from tests.test_card4_seat_kind_fix import ACCOUNT, GATEWAY, MODEL, NOW, SEAT, TEAM
from tests.test_cardr1_money_basis import EX, INV, MONEY, UNK, _Basis


class _Shared(_Basis):

    def clone_row(self, table, where, args, replace):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            row = dict(conn.execute("SELECT * FROM " + table + " WHERE " + where, args).fetchone())
            row.update(replace)
            conn.execute("INSERT INTO " + table + "(" + ",".join(row) + ") VALUES (" + ",".join("?" for _ in row) + ")",
                         tuple(row.values()))
            conn.commit()

    def second_account(self, cap=INV, ledger=EX, vat=1000):
        """Account B on the same team, with its own policy. -> (account, gateway, seat, model)."""
        account, gateway, seat, model = (str(uuid.uuid4()) for _ in range(4))
        self.clone_row("accounts", "id=?", (ACCOUNT,), {"id": account, "alias": "other-account"})
        self.clone_row("gateways", "id=?", (GATEWAY,), {"id": gateway, "alias": "other-gateway", "account_id": account})
        self.clone_row("seats", "id=?", (SEAT,), {"id": seat, "alias": "other-seat"})
        self.clone_row("models", "id=?", (MODEL,), {"id": model, "account_id": account})
        self.clone_row("project_accounts", "account_id=?", (ACCOUNT,), {"account_id": account})
        machine = self.sql("SELECT id FROM machines")[0][0]
        self.sql("INSERT INTO billing_policies VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            account, machine, "proj-ai", 1, 1, 0, cap, "ex_vat", ledger, vat, 900, 7200, 14400, 50000, 100,
            "advisory", "alert", "hold"))
        return account, gateway, seat, model

    def grant_for(self, other, now):
        account, gateway, seat, model = other
        request = self.request(now, gateway=gateway)
        request["seat_id"], request["model_id"] = seat, model
        return lease.grant(self.db, request, now)

    def refused_for(self, other, now):
        with self.assertRaises(QuotaError) as caught:
            self.grant_for(other, now)
        self.assertEqual(caught.exception.code, errors.CODE_MONEY_LIMIT)

    def scope_total(self, requester, requester_basis, other=None):
        """The scope's settled money exactly as admission reads it, for the given requester."""
        conn = sqlite3.connect(self.db)
        try:
            def fetch(extra, more):
                return money.exact_sums(
                    conn, "receipts r JOIN leases l ON l.id=r.lease_id WHERE r.final=1 AND l.enforcement <> 'shadow' AND "
                    + lease._chain_pred("team") + extra, (TEAM,) + more, ["r.actual_micro_eur"]).totals[0]
            return lease._scope_total(conn, "team", TEAM, True, fetch, requester, requester_basis)
        finally:
            conn.close()

    def other_receipt(self, other, amount):
        """A final receipt of `amount` on a lease of account B (the spending that B's own policy must convert)."""
        made = self.grant_for(other, NOW + 500)
        self.sql("INSERT INTO receipts(event_id, lease_id, gateway_id, seq, calls, input_tokens, cached_input_tokens, "
                 "output_tokens, actual_micro_eur, outcome, final, observed_at, received_at) "
                 "VALUES(?,?,?,NULL,1,1,0,1,?, 'completed', 1, ?, ?)",
                 (str(uuid.uuid4()), made["lease_id"], other[1], amount, NOW + 1000, NOW + 1000))


class Reproduction(_Shared):
    """Codex's case through the public advisory grant: a team cap of 1150 in the invoice basis; account A (20% VAT) has spent 1000
    excluding VAT, which is 1200 for real; B (10% VAT, no spending) must be refused like A."""

    def setup_case(self):
        self.start()
        self.settle_both(money_amounts=(1000, 0))
        self.policy(cap=INV, ledger=EX, vat=2000)
        other = self.second_account(vat=1000)
        self.limit(MONEY, 1150)
        return other

    def test_account_a_is_refused_and_so_is_account_b(self):
        other = self.setup_case()
        with self.assertRaises(QuotaError) as refused:
            self.grant(NOW + 2000)
        self.assertEqual(refused.exception.code, errors.CODE_MONEY_LIMIT)
        self.refused_for(other, NOW + 2000)                                # B's rate no longer discounts A's spending
        self.assertEqual(self.sql("PRAGMA foreign_key_check"), [])

    def test_a_cap_that_the_spending_does_not_reach_still_admits_both(self):
        self.start()
        self.settle_both(money_amounts=(1000, 0))
        self.policy(cap=INV, ledger=EX, vat=2000)
        other = self.second_account(vat=1000)
        self.limit(MONEY, 1200)                                            # 1000 at 20% is 1200: at the cap, not over it
        self.assertEqual(self.grant(NOW + 2000)["enforcement"], "metered_advisory")
        self.assertEqual(self.grant_for(other, NOW + 2000)["enforcement"], "metered_advisory")

    def test_open_holds_of_another_account_are_converted_with_that_accounts_policy(self):
        self.start()
        self.policy(cap=INV, ledger=EX, vat=2000)
        other = self.second_account(vat=1000)
        limit_id = self.limit(MONEY, 1150)
        for lease_id in self.leases:                                       # A's two open leases hold 500 each = 1000 ex_vat
            self.sql("INSERT INTO holds(lease_id, limit_id, period_start, reserved, settled_actual) VALUES(?,?,?,?,NULL)",
                     (lease_id, limit_id, self.month(), 500))
        self.assertEqual(self.decide(MONEY, new=0), errors.CODE_MONEY_LIMIT)           # 1200 > 1150 for A itself
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            chain = {"platform": lease._PLATFORM_ID, "project": self.project_id(), "team": TEAM, "seat": other[2]}
            rows = conn.execute("SELECT * FROM limits").fetchall()
            with self.assertRaises(QuotaError) as caught:                  # B asks: the same holds, still 1200
                lease._admit_group(conn, (MONEY, "month", None), rows, chain, other[0], None, True, 0, 0, 0, NOW,
                                   basis=lease._lease_basis(conn, other[0]))
            self.assertEqual(caught.exception.code, errors.CODE_MONEY_LIMIT)
        finally:
            conn.close()

    def project_id(self):
        return self.sql("SELECT project_id FROM teams WHERE id=?", (TEAM,))[0][0]


class Invariance(_Shared):

    def test_changing_the_requester_never_changes_the_value_of_historical_spending(self):
        self.start()
        self.settle_both(money_amounts=(1000, 0))
        self.policy(cap=INV, ledger=EX, vat=2000)
        other = self.second_account(vat=1000)
        basis_a, basis_b = lease_basis(self, ACCOUNT), lease_basis(self, other[0])
        self.assertNotEqual(basis_a, basis_b)
        self.assertEqual(self.scope_total(ACCOUNT, basis_a), 1200)
        self.assertEqual(self.scope_total(other[0], basis_b), 1200)           # whoever asks, the same figure
        self.other_receipt(other, 1000)                                       # B's own spending: 1000 at 10% = 1100
        self.assertEqual(self.scope_total(ACCOUNT, basis_a), 1200 + 1100)
        self.assertEqual(self.scope_total(other[0], basis_b), 1200 + 1100)
        self.assertEqual(self.scope_total("some-other-asker", None), 1200 + 1100)      # even an asker with no policy

    def test_equal_bases_are_the_plain_sum_whoever_asks(self):
        self.start()
        self.settle_both(money_amounts=(1000, 0))
        self.policy(cap=EX, ledger=EX, vat=None)
        other = self.second_account(cap=EX, ledger=EX, vat=None)
        self.other_receipt(other, 250)
        self.assertEqual(self.scope_total(ACCOUNT, lease_basis(self, ACCOUNT)), 1250)
        self.assertEqual(self.scope_total(other[0], lease_basis(self, other[0])), 1250)

    def test_an_account_with_no_policy_contributes_its_raw_amount(self):
        self.start()
        self.settle_both(money_amounts=(1000, 0))                             # A has no policy: raw
        other = self.second_account(vat=1000)
        self.other_receipt(other, 1000)                                       # B: 1000 at 10% = 1100
        self.assertEqual(self.scope_total(ACCOUNT, None), 1000 + 1100)
        self.assertEqual(self.scope_total(other[0], lease_basis(self, other[0])), 1000 + 1100)

    def test_an_unconvertible_account_keeps_its_raw_amount_for_its_own_part_only(self):
        self.start()
        self.settle_both(money_amounts=(1000, 0))
        self.policy(cap=INV, ledger=EX, vat=2000)                             # A converts: 1200
        other = self.second_account(cap=INV, ledger=UNK, vat=1000)            # B cannot: its 500 stays 500
        self.other_receipt(other, 500)
        self.assertIsNone(lease_basis(self, other[0]))
        self.assertEqual(self.scope_total(ACCOUNT, lease_basis(self, ACCOUNT)), 1200 + 500)
        self.assertEqual(self.scope_total(other[0], None), 1200 + 500)        # B asking does not reduce A's part to raw

    def test_the_total_of_a_huge_account_stays_above_every_cap(self):
        self.start()
        self.settle_both(money_amounts=(2 ** 63 - 1, 2 ** 63 - 1))
        self.policy(cap=EX, ledger=INV, vat=2000)
        other = self.second_account(vat=1000)
        self.assertEqual(self.scope_total(other[0], lease_basis(self, other[0])), money.SATURATION)


def lease_basis(case, account):
    conn = sqlite3.connect(case.db)
    try:
        return lease._lease_basis(conn, account)
    finally:
        conn.close()
