"""Card R1-M follow-up: mismatched cap bases refused at import, `saturate` keeps unknown, one account listing per scope.

1. **Refusal at import, of a NEW conflict only.** Accounts that share a money cap (a team, project, seat or platform cap) must declare
   the same known cap basis. The lease check already refuses such a grant (`CAP_BASIS_CONFLICT`); both imports (`import_config`,
   `billing_policy_import`) refuse an import that ADDS a conflicting scope (one that was not conflicting before the import and is on the
   state it would leave) and write nothing. The word is reused because it means the same and asks the same of the operator. Accounts with
   no policy or an `unknown` basis do not take part. A store that already holds a mismatch (made before; once billing history exists a cap
   basis is frozen, so it may not be repairable by import) stays importable: an unrelated import is accepted, a repairing one is accepted,
   and the lease check keeps refusing leases in the conflicting scope.
2. **`saturate` keeps unknown.** `money.saturate(UNKNOWN)` is UNKNOWN, not an error.
3. **One listing per scope per request.** A grant lists the accounts of a scope, and reads each account's policy, once, however many
   times the guard and the two totals need them. No answer changes.

Synthetic data only.
"""

import contextlib
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from quota_balancer import errors, lease, money, store
from quota_balancer.errors import QuotaError

from tests.test_card2b1 import _billing, _billing_policy, _doc_v3, _eid, _month_bounds_of, _now_ms, _policy_doc
from tests.test_card4_seat_kind_fix import NOW
from tests.test_cardl1a import _model, base_doc
from tests.test_cardr1_money_capbasis import _Case

INV, EX = "invoice_total", "ex_vat"
WORD = errors.CODE_CAP_BASIS_CONFLICT


class _Imports(unittest.TestCase):

    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-r1mfu-")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.db = os.path.join(self.root, "db.sqlite")
        store.init_db(self.db)

    def doc(self, members=("meter", "other"), limit="team"):
        """Two metered accounts; `members` of project `org`; one money cap at `limit` (a scope word or None)."""
        doc = base_doc()
        doc["accounts"][1].update(kind="metered", budget_micro_eur=100_000_000)
        doc["gateways"][0]["share_micro_eur"] = 1000
        doc["gateways"].append(dict(alias="g2", account="other", machine="worker", share_micro_eur=1000, period="utc_month"))
        doc["project_accounts"] = [dict(project="org", account=name) for name in members]
        doc["models"].append(_model("other", "other-model", ["opus"]))
        doc["limits"] = []
        if limit:
            row = dict(scope=limit, metric="money_micro_eur", period="month", window_key=None, soft=None, hard=1150)
            if limit in ("team", "project"):
                row["project"] = "org"
            if limit == "team":
                row["team"] = "dev"
            if limit == "seat":
                row.update(machine="desktop", seat="s1")
            doc["limits"] = [row]
        return doc

    def policies(self, a, b, b_ledger=EX):
        entries = [_billing_policy(account="meter", cap_basis=a, ledger_basis=EX, vat_bps=2000)]
        if b is not None:
            entries.append(_billing_policy(account="other", cap_basis=b, ledger_basis=b_ledger, vat_bps=2000))
        return _policy_doc(entries)

    def write(self, name, doc):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)
        return path

    def import_config(self, doc):
        store.import_config(self.db, self.write("config.json", doc))

    def import_policies(self, doc):
        store.billing_policy_import(self.db, self.write("policies.json", doc))

    def refused(self, call, *args):
        with self.assertRaises(QuotaError) as caught:
            call(*args)
        self.assertEqual(caught.exception.code, WORD)
        self.assertEqual(caught.exception.message, errors.MSG_CAP_BASIS_CONFLICT)       # fixed; names no account

    def state(self):
        """A complete logical dump of the store (every table, every row, the schema), so 'nothing written' is checked, not assumed."""
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            return list(conn.iterdump())

    def add_billing_history(self):
        """Provider billing readings for both accounts, through the public reading import: after them a cap basis is frozen."""
        start, end = _month_bounds_of(_now_ms())
        now = _now_ms()
        readings = [_billing(_eid("fu-history-" + account), "desktop", account, "proj-ai", 1, start, end, 100, False, now)
                    for account in ("meter", "other")]
        store.reading_import(self.db, self.write("history.json", _doc_v3(readings)))

    def older_store(self, limit):
        """A store that already holds a mismatch, made the way an older version allowed it: aligned policies through the public
        imports, then B's cap basis changed in the store directly, then billing history for both accounts."""
        self.import_config(self.doc(limit=limit))
        self.import_policies(self.policies(INV, INV))
        self.force("other", EX)
        self.add_billing_history()

    def force(self, account_alias, cap_basis):
        """Change an account's declared cap basis in the store directly: a store made before the import check."""
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE billing_policies SET cap_basis=? WHERE account_id="
                         "(SELECT id FROM accounts WHERE alias=?)", (cap_basis, account_alias))
            conn.commit()


class Refusal(_Imports):

    def test_the_policy_import_refuses_a_mismatch_for_every_scope_and_writes_nothing(self):
        for scope in ("team", "project", "seat", "platform"):
            with self.subTest(scope=scope):
                self.setUp()
                self.import_config(self.doc(limit=scope))
                self.import_policies(self.policies(INV, INV))                  # aligned: accepted
                before = self.state()
                self.refused(self.import_policies, self.policies(INV, EX))      # B changed to the other basis
                self.assertEqual(self.state(), before)

    def test_the_config_import_refuses_a_membership_change_that_creates_a_mismatch_and_writes_nothing(self):
        for scope in ("team", "project", "seat"):
            with self.subTest(scope=scope):
                self.setUp()
                self.import_config(self.doc(members=("meter",), limit=scope))     # B is not in the project yet
                self.import_policies(self.policies(INV, EX))                      # a mismatch, but no shared cap: accepted
                before = self.state()
                self.refused(self.import_config, self.doc(members=("meter", "other"), limit=scope))
                self.assertEqual(self.state(), before)

    def test_the_config_import_refuses_a_new_money_cap_that_shares_mismatched_accounts(self):
        for scope in ("team", "project", "seat", "platform"):
            with self.subTest(scope=scope):
                self.setUp()
                self.import_config(self.doc(limit=None))                          # no money cap: nothing is shared
                self.import_policies(self.policies(INV, EX))
                before = self.state()
                self.refused(self.import_config, self.doc(limit=scope))
                self.assertEqual(self.state(), before)

    def test_the_platform_cap_spans_every_account_even_outside_the_project(self):
        self.setUp()
        self.import_config(self.doc(members=("meter",), limit="platform"))
        before = self.state()
        self.refused(self.import_policies, self.policies(INV, EX))                # B is in no project, but the cap is platform-wide
        self.assertEqual(self.state(), before)

    def test_an_aligned_set_up_is_accepted_by_both_imports(self):
        for scope in ("team", "project", "seat", "platform"):
            with self.subTest(scope=scope):
                self.setUp()
                self.import_config(self.doc(limit=scope))
                self.import_policies(self.policies(EX, EX))
                self.import_policies(self.policies(INV, INV))                     # both moved together: still one basis
                self.import_config(self.doc(limit=scope))                         # and a re-import of the same document

    def test_an_account_with_no_policy_or_an_unknown_basis_never_triggers_the_refusal(self):
        for scope in ("team", "project", "seat", "platform"):
            with self.subTest(scope=scope, case="no policy"):
                self.setUp()
                self.import_config(self.doc(limit=scope))
                self.import_policies(self.policies(INV, None))                    # B has no policy at all
            with self.subTest(scope=scope, case="unknown basis"):
                self.setUp()
                self.import_config(self.doc(limit=scope))
                self.import_policies(self.policies(INV, "unknown"))               # B declares an unknown cap basis
                self.import_policies(self.policies("unknown", EX))                # and so does A, the other way round

    def test_a_cap_on_another_metric_never_triggers_the_refusal(self):
        self.setUp()
        doc = self.doc(limit="team")
        doc["limits"][0]["metric"] = "input_tokens"
        doc["limits"][0].update(period="turn", hard=1_000_000)
        self.import_config(doc)
        self.import_policies(self.policies(INV, EX))                              # mismatched, but no money cap is shared

    def test_a_repairing_import_is_accepted(self):
        self.setUp()
        self.import_config(self.doc(limit="team"))
        self.import_policies(self.policies(INV, INV))
        self.force("other", EX)                                                   # a store made before: one conflicting scope
        self.import_policies(self.policies(INV, INV))                             # aligning shrinks the set: accepted
        self.import_config(self.doc(limit="team"))
        self.setUp()
        self.import_config(self.doc(limit="team"))
        self.import_policies(self.policies(INV, INV))
        self.force("other", EX)
        self.import_config(self.doc(members=("meter",), limit="team"))            # dropping the mismatched member shrinks it too

    def test_an_unrelated_import_of_an_older_store_that_holds_a_mismatch_is_accepted(self):
        for scope in ("team", "project", "seat", "platform"):
            with self.subTest(scope=scope):
                self.setUp()
                self.import_config(self.doc(limit=scope))
                self.import_policies(self.policies(INV, INV))
                self.force("other", EX)                                           # the conflict was there before
                self.import_config(self.doc(limit=scope))                         # the same document: accepted
                changed = self.doc(limit=scope)
                changed["gateways"][0]["share_micro_eur"] = 2000                  # an unrelated change: accepted
                self.import_config(changed)
                self.import_policies(self.policies(INV, EX))                      # the same mismatched policies again: accepted

    def test_a_version_1_import_of_an_older_store_that_holds_a_mismatch_is_accepted(self):
        self.setUp()
        self.import_config(self.doc(limit="platform"))
        self.import_policies(self.policies(INV, INV))
        self.force("other", EX)
        doc = self.doc(limit="platform")
        old = {key: doc[key] for key in ("accounts", "machines", "gateways")}
        old["version"] = 1
        self.import_config(old)

    def test_a_second_conflicting_scope_added_to_an_older_store_is_refused(self):
        self.setUp()
        self.import_config(self.doc(limit="team"))
        self.import_policies(self.policies(INV, INV))
        self.force("other", EX)                                                   # the team scope already conflicts
        before = self.state()
        self.refused(self.import_config, self.doc(limit="platform"))              # a platform cap would be a NEW conflicting scope
        self.assertEqual(self.state(), before)


class WithBillingHistory(_Imports):
    """Once both accounts have billing history, a cap basis is frozen: the conflict of an older store cannot be repaired by import."""

    def test_both_alignment_directions_are_refused_invalid_config_and_an_unrelated_import_is_accepted(self):
        self.setUp()
        self.older_store("platform")
        before = self.state()
        for basis in (INV, EX):
            with self.assertRaises(QuotaError) as caught:
                self.import_policies(self.policies(basis, basis))
            self.assertEqual(caught.exception.code, errors.CODE_INVALID_CONFIG)    # frozen, as before
            self.assertEqual(self.state(), before)
        self.import_config(self.doc(limit="platform"))                            # an unrelated import: now ACCEPTED
        changed = self.doc(limit="platform")
        changed["gateways"][0]["share_micro_eur"] = 2000
        self.import_config(changed)

    def test_the_old_cap_can_still_be_removed_and_a_version_1_import_is_accepted(self):
        self.setUp()
        self.older_store("platform")
        self.import_config(self.doc(limit=None))                                  # the cap goes: the conflicting scope goes with it
        doc = self.doc(limit=None)
        self.import_config({key: doc[key] for key in ("accounts", "machines", "gateways")} | {"version": 1})


class HistoricalHold(_Case):
    """A platform cap that a past hold still refers to cannot be removed, and a basis cannot be changed after billing history."""

    def test_an_unrelated_import_is_accepted_and_the_lease_guard_still_refuses(self):
        self.build("platform", a_cap="invoice_total", b_cap="invoice_total", hard=100_000_000)      # aligned, so a lease can be made
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE seats SET enforcement='metered' WHERE alias='s2'")
            conn.execute("UPDATE gateways SET sized_ready=1, state='ok' WHERE alias='g2'")
            conn.commit()
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            self.settle_other(1000)                                               # a historical metered hold exists
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM holds h JOIN limits l ON l.id=h.limit_id").fetchone()[0], 0)
        self.set_b_policy(cap_basis="ex_vat")                                     # the older store now holds the mismatch
        start, end = _month_bounds_of(_now_ms())
        rows = [_billing(_eid("held-fu-" + account), "desktop", account, "proj-ai", 1, start, end, 100, False, _now_ms())
                for account in ("meter", "other")]
        path = os.path.join(self.root, "history.json")
        self.write(path, _doc_v3(rows))
        store.reading_import(self.db, path)
        config = os.path.join(self.root, "config.json")
        store.import_config(self.db, config)                                      # an unrelated import: accepted
        with self.assertRaises(QuotaError) as caught:
            self.grant("meter", NOW + 2000)
        self.assertEqual(caught.exception.code, WORD)                             # and the lease guard still refuses


class Saturate(unittest.TestCase):

    def test_saturate_keeps_unknown_and_changes_nothing_else(self):
        self.assertIs(money.saturate(money.UNKNOWN), money.UNKNOWN)
        self.assertIs(money.saturate(money.add(1, money.UNKNOWN)), money.UNKNOWN)
        self.assertEqual(money.saturate(5), 5)
        self.assertEqual(money.saturate(money.SATURATION - 1), money.SATURATION - 1)
        self.assertEqual(money.saturate(2 ** 70), money.SATURATION)

    def test_a_converted_total_that_cannot_be_converted_stays_unknown_through_both_helpers(self):
        self.assertIs(money.convert_total(5, "unknown", EX, 2000, money.EXPOSURE), money.UNKNOWN)
        self.assertIs(money.saturate(money.convert_total(5, "unknown", EX, 2000, money.EXPOSURE)), money.UNKNOWN)


class OneListing(_Case):

    def count_queries(self):
        """Run one grant for A and count the statements that list a scope's accounts and that read an account's policy."""
        log = []
        real = lease._connect

        def traced(*args, **kwargs):
            conn = real(*args, **kwargs)
            conn.set_trace_callback(log.append)
            return conn
        with mock.patch.object(lease, "_connect", traced):
            made = self.grant("meter", NOW + 2000)
        listings = sum(1 for text in log if text.lstrip().startswith("SELECT DISTINCT l.account_id"))
        policies = sum(1 for text in log if "FROM billing_policies WHERE account_id" in text)
        return made, listings, policies

    def test_a_grant_lists_each_scope_once_and_reads_each_policy_once_and_answers_as_before(self):
        self.build("team", a_cap=INV, b_cap=INV, hard=10_000)
        self.settle_other()                                                       # B has a lease and spending in the shared scopes
        made, listings, policies = self.count_queries()
        self.assertEqual(made["enforcement"], "metered_advisory")
        # four scopes in the chain (platform, project, team, seat), each listed ONCE however many times it is used;
        # two accounts (A asking, B with a lease), each policy read ONCE
        self.assertEqual(listings, 4)
        self.assertEqual(policies, 2)

    def test_the_answers_are_the_same_with_the_shared_listing(self):
        self.build("team", a_cap=INV, b_cap=INV, hard=1150)
        self.settle_other(1000)                                                   # 1000 at 20% is 1200 in the shared basis
        with self.assertRaises(QuotaError) as caught:
            self.grant("meter", NOW + 2000)
        self.assertEqual(caught.exception.code, errors.CODE_MONEY_LIMIT)


if __name__ == "__main__":
    unittest.main()
