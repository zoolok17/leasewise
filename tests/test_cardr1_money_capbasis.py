"""Card R1-M part 2, fix round 2 (D-177): a shared money cap is refused when its accounts declare different cap bases.

A team, project or platform cap has no tax basis of its own: each account declares the basis ITS caps are counted in (with VAT or
without). If the accounts that share one cap declare different known cap bases, spending counted with VAT would be added to spending
counted without it, and the total would mean nothing. So the grant is refused with the closed code CAP_BASIS_CONFLICT, in advisory
mode too, and for money limits only. An account with no billing policy, or whose cap basis is `unknown`, does not take part (the
accepted fallback). The requester counts even with no spending yet. Neither the requester's basis is picked nor any account's
spending dropped.

The case is Codex's, through the public `grant` and `settle`. The mixed set-up is a store made BEFORE the import check (card R1-M
follow-up), so it is built in two steps: the public imports (`import_config`, `billing_policy_import`) with ONE cap basis, then one
account's declaration is changed directly in the store. Synthetic data only.
"""

import contextlib
import json
import os
import sqlite3
import tempfile
import unittest
import uuid

from quota_balancer import errors, http_api, lease, store
from quota_balancer.errors import QuotaError
from quota_balancer.reporter import decisions

from tests.test_card2b1 import _billing_policy, _policy_doc
from tests.test_card4_seat_kind_fix import NOW, _grant_request
from tests.test_cardl1a import _model, base_doc

SCOPES = ("team", "project", "platform")


class _Case(unittest.TestCase):

    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-r1m2cap-")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def build(self, scope, a_cap="invoice_total", b_cap="ex_vat", hard=1150, metric="money_micro_eur", b_ledger="ex_vat"):
        """Two metered accounts (meter = A, other = B) that share one cap of `hard` at `scope`; both ledgers ex_vat at 20% VAT."""
        self.db = os.path.join(self.root, "db.sqlite")
        if os.path.exists(self.db):
            os.remove(self.db)
        store.init_db(self.db)
        doc = base_doc()
        doc["accounts"][1].update(kind="metered", budget_micro_eur=100_000_000)
        doc["gateways"][0]["share_micro_eur"] = 1000
        doc["gateways"].append(dict(alias="g2", account="other", machine="worker", share_micro_eur=1000, period="utc_month"))
        doc["project_accounts"].append(dict(project="org", account="other"))
        doc["models"].append(_model("other", "other-model", ["opus"]))
        doc["profiles"][0]["candidates"] = ["opus", "other-model", "park"]
        doc["seats"][1]["enforcement"] = "metered_advisory"
        limit = dict(scope=scope, metric=metric, period="month", window_key=None, soft=None, hard=hard)
        if scope in ("team", "project"):
            limit["project"] = "org"
        if scope == "team":
            limit["team"] = "dev"
        doc["limits"] = [limit]
        path = os.path.join(self.root, "config.json")
        self.write(path, doc)
        store.import_config(self.db, path)
        # The imports now refuse a mismatched set-up (card R1-M follow-up), so a store "made before" is built in two steps: the
        # public imports with ONE cap basis, then B's declaration changed in the store directly, as an older version allowed.
        self.policies = _policy_doc([
            _billing_policy(account="meter", cap_basis=a_cap, ledger_basis="ex_vat", vat_bps=2000),
            _billing_policy(account="other", cap_basis=a_cap, ledger_basis="ex_vat", vat_bps=2000)])
        self.import_policies()
        store.import_config(self.db, path)                        # the configuration reconciled after the policies exist
        self.set_b_policy(cap_basis=b_cap, ledger_basis=b_ledger)
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.ids = {table: dict(conn.execute("SELECT alias,id FROM " + table)) for table in ("seats", "models", "gateways")}

    @staticmethod
    def write(path, doc):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)

    def import_policies(self):
        path = os.path.join(self.root, "policies.json")
        self.write(path, self.policies)
        store.billing_policy_import(self.db, path)

    def grant(self, account, when):
        request = _grant_request(self.ids["gateways"]["g1" if account == "meter" else "g2"])
        request["seat_id"] = self.ids["seats"]["s1" if account == "meter" else "s2"]
        request["model_id"] = self.ids["models"]["opus" if account == "meter" else "other-model"]
        request["context"]["observed_at"] = when - 1000
        return lease.grant(self.db, request, when)

    def settle_other(self, amount=1000):
        made = self.grant("other", NOW)
        lease.settle(self.db, dict(event_id=str(uuid.uuid4()), lease_id=made["lease_id"], outcome="completed", calls=1,
                                   input_tokens=100, cached_input_tokens=0, output_tokens=10, actual_micro_eur=amount,
                                   gateway_id=self.ids["gateways"]["g2"], seq=1, final=1, observed_at=NOW + 1000,
                                   received_at=NOW + 1000), NOW + 1000)

    def refused(self, account, when, code):
        with self.assertRaises(QuotaError) as caught:
            self.grant(account, when)
        self.assertEqual(caught.exception.code, code)

    def set_b_policy(self, **changes):
        """Change B's declaration directly in the store (the way a store made before the import check could hold it)."""
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            for column, value in changes.items():
                conn.execute("UPDATE billing_policies SET " + column + "=? WHERE account_id="           # fixed column names
                             "(SELECT id FROM accounts WHERE alias='other')", (value,))
            conn.commit()


class Conflict(_Case):

    def test_mixed_cap_bases_refuse_the_requester_with_the_new_code_for_every_scope(self):
        for scope in SCOPES:
            with self.subTest(scope=scope):
                self.build(scope)
                self.settle_other()                                    # B has spent 1000
                self.refused("meter", NOW + 2000, errors.CODE_CAP_BASIS_CONFLICT)       # A has spent nothing: still counts
                # B asking is not refused: A has no spending in the scope and is not asking, so only one cap basis takes part
                self.assertEqual(self.grant("other", NOW + 3000)["enforcement"], "metered_advisory")

    def test_the_refusal_is_the_same_wherever_the_spending_is(self):
        for scope in SCOPES:
            with self.subTest(scope=scope):
                self.build(scope, a_cap="ex_vat", b_cap="invoice_total")             # the other way round
                self.settle_other()
                self.refused("meter", NOW + 2000, errors.CODE_CAP_BASIS_CONFLICT)

    def test_with_one_cap_basis_the_scope_is_evaluated_normally(self):
        for scope in SCOPES:
            with self.subTest(scope=scope):
                self.build(scope, hard=1150)
                self.set_b_policy(cap_basis="invoice_total")
                self.settle_other()                                    # 1000 at 20% is 1200 in the shared basis
                self.refused("meter", NOW + 2000, errors.CODE_MONEY_LIMIT)
                self.build(scope, hard=1200)
                self.set_b_policy(cap_basis="invoice_total")
                self.settle_other()
                self.assertEqual(self.grant("meter", NOW + 2000)["enforcement"], "metered_advisory")

    def test_the_conflict_is_found_again_when_it_is_introduced_later(self):
        self.build("team", b_cap="invoice_total")                       # same basis: fine
        self.settle_other()
        self.refused("meter", NOW + 2000, errors.CODE_MONEY_LIMIT)
        self.set_b_policy(cap_basis="ex_vat")                           # the operator changes B: now they differ
        self.refused("meter", NOW + 3000, errors.CODE_CAP_BASIS_CONFLICT)
        self.set_b_policy(cap_basis="invoice_total")                    # and aligns them again
        self.refused("meter", NOW + 4000, errors.CODE_MONEY_LIMIT)

    def test_once_both_accounts_have_a_lease_in_the_scope_both_are_refused(self):
        self.build("team", b_cap="invoice_total", hard=10_000)           # the same basis: both are granted
        self.settle_other(100)
        self.grant("meter", NOW + 1500)                                  # A has a lease in the scope now
        self.set_b_policy(cap_basis="ex_vat")                            # then the declarations differ
        self.refused("meter", NOW + 2000, errors.CODE_CAP_BASIS_CONFLICT)
        self.refused("other", NOW + 3000, errors.CODE_CAP_BASIS_CONFLICT)

    def test_nothing_is_written_by_a_refused_grant(self):
        self.build("team")
        self.settle_other()
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            before = conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
        self.refused("meter", NOW + 2000, errors.CODE_CAP_BASIS_CONFLICT)
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0], before)


class DoesNotTakePart(_Case):

    def test_an_account_with_no_policy_never_triggers_the_refusal(self):
        for scope in SCOPES:
            with self.subTest(scope=scope):
                self.build(scope, hard=10_000)
                with contextlib.closing(sqlite3.connect(self.db)) as conn:
                    conn.execute("DELETE FROM billing_policies WHERE account_id=?",
                                 (conn.execute("SELECT id FROM accounts WHERE alias='other'").fetchone()[0],))
                    conn.commit()
                self.settle_other()
                self.assertEqual(self.grant("meter", NOW + 2000)["enforcement"], "metered_advisory")

    def test_an_account_whose_cap_basis_is_unknown_never_triggers_the_refusal(self):
        for scope in SCOPES:
            with self.subTest(scope=scope):
                self.build(scope, hard=10_000, b_cap="unknown", b_ledger="ex_vat")
                self.settle_other()
                self.assertEqual(self.grant("meter", NOW + 2000)["enforcement"], "metered_advisory")

    def test_the_requester_with_no_policy_does_not_take_part_either(self):
        self.build("team", hard=10_000, a_cap="ex_vat", b_cap="ex_vat")
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("DELETE FROM billing_policies WHERE account_id=?",
                         (conn.execute("SELECT id FROM accounts WHERE alias='meter'").fetchone()[0],))
            conn.commit()
        self.settle_other()
        self.assertEqual(self.grant("meter", NOW + 2000)["enforcement"], "metered_advisory")

    def test_a_cap_that_only_one_account_uses_has_no_conflict(self):
        self.build("team", hard=10_000)                                  # B never leases: only A takes part
        self.assertEqual(self.grant("meter", NOW + 2000)["enforcement"], "metered_advisory")

    def test_token_limits_are_unaffected(self):
        for scope in SCOPES:
            with self.subTest(scope=scope):
                self.build(scope, metric="input_tokens", hard=10 ** 9)
                self.settle_other()
                self.assertEqual(self.grant("meter", NOW + 2000)["enforcement"], "metered_advisory")


class TheCode(unittest.TestCase):

    def test_the_new_code_is_defined_everywhere_a_code_must_be(self):
        code = errors.CODE_CAP_BASIS_CONFLICT
        self.assertEqual(code, "CAP_BASIS_CONFLICT")
        self.assertEqual(errors.EXIT_CODES[code], 2)                     # a request condition
        self.assertEqual(http_api._HTTP_STATUS[code], 409)               # a conflict of configuration
        self.assertIn(code, decisions.CLOSED_CODES)
        message = errors.MESSAGES[code]
        self.assertEqual(message, errors.MSG_CAP_BASIS_CONFLICT)
        for word in ("same", "cap basis"):
            self.assertIn(word, message)                                 # it says what to do
        self.assertEqual(sum(1 for text in errors.MESSAGES.values() if text == message), 1)


if __name__ == "__main__":
    unittest.main()
