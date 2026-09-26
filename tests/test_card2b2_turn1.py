"""Card 2b-2 turn 1: the two residuals carried from the card 2b-1 final
review.

Run from the repository root:
    python -m unittest discover -s tests -v

  test_gateway_v3_period_is_one_utc_month  (R2):
      a v3 gateway snapshot period is exactly one UTC month, the same
      rule as the provider period: [Sep 1, Dec 1) is refused,
      [Sep 1, Oct 1) accepted.
  test_config_import_b01_break_refused     (R1a):
      a config-import that breaks B01 (shares + non-AI allowance > cap)
      for an account with a billing policy is refused (static error,
      nothing written); a later billing reading still ingests.
  test_config_import_kind_flip_refused     (R1a):
      the same for a config-import that flips a policy account from
      metered to subscription.
"""

import calendar
import copy
import datetime
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.environ.get("AGENTTALK_PY") or sys.executable

# The standard card-1 inventory: one metered account (a 100 EUR cap with
# the two gateway shares filling it exactly), two machines, two gateways.
VALID_DOC = {
    "version": 1,
    "accounts": [
        {"alias": "meter", "vendor": "ovh", "kind": "metered",
         "budget_micro_eur": 100000000}
    ],
    "machines": [{"alias": "desktop"}, {"alias": "worker"}],
    "gateways": [
        {"alias": "g1", "account": "meter", "machine": "desktop",
         "share_micro_eur": 60000000, "period": "utc_month"},
        {"alias": "g2", "account": "meter", "machine": "worker",
         "share_micro_eur": 40000000, "period": "utc_month"},
    ],
}

ERROR_INVALID_CONFIG = '{"error":{"code":"INVALID_CONFIG","message":"config document is invalid"}}'

_MS_PER_DAY = 86_400_000


def _ms(year, month, day=1):
    return int(datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc)
               .timestamp() * 1000)


SEP = _ms(2026, 9)
OCT = _ms(2026, 10)
DEC = _ms(2026, 12)


def _add_months(ms, months):
    """Add (or subtract) calendar months to a UTC epoch-ms timestamp."""
    dt = datetime.datetime.fromtimestamp(ms // 1000, datetime.timezone.utc)
    idx = dt.year * 12 + dt.month - 1 + months
    year, month = divmod(idx, 12)
    month += 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return int(datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc)
               .timestamp() * 1000)


def _month_bounds_of(ms):
    """The UTC month period [start, end) containing `ms`."""
    dt = datetime.datetime.fromtimestamp(ms // 1000, datetime.timezone.utc)
    start = int(datetime.datetime(dt.year, dt.month, 1,
                                  tzinfo=datetime.timezone.utc)
                .timestamp() * 1000)
    return start, _add_months(start, 1)


def _now_ms():
    return int(time.time() * 1000)


# Section 7: reading IDs are local UUIDs. A deterministic UUID per label.
_EID_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # NAMESPACE_URL


def _eid(label):
    return str(uuid.uuid5(_EID_NS, "quota-card2b2/" + label))


def _write_json(path, doc):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)


def run_cli(*args):
    return subprocess.run(
        [PYTHON, "-m", "quota_balancer", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def _config_import(db, tmp, doc):
    cfg = os.path.join(tmp, "config.json")
    _write_json(cfg, doc)
    return run_cli("--db", db, "config-import", "--file", cfg)


def _readings_import(db, tmp, doc):
    path = os.path.join(tmp, "readings.json")
    _write_json(path, doc)
    return run_cli("--db", db, "reading-import", "--file", path)


def _billing_policy_import(db, tmp, doc):
    path = os.path.join(tmp, "billing-policy.json")
    _write_json(path, doc)
    return run_cli("--db", db, "billing-policy-import", "--file", path)


def _v3_gateway_snapshot(event_id, machine, account, gateway, ledger_epoch,
                         seq, observed_at, settled, reserved, uncertain,
                         unattributed, period_start, period_end):
    """A document version 3 gateway snapshot with explicit period bounds."""
    return {
        "event_id": event_id,
        "machine": machine,
        "account": account,
        "kind": "gateway_snapshot",
        "observed_at": observed_at,
        "payload": {
            "gateway": gateway,
            "ledger_epoch": ledger_epoch,
            "seq": seq,
            "period_start": period_start,
            "period_end": period_end,
            "policy_hashes": None,
            "sized_ready": False,
            "reconciliation_required": False,
            "settled_micro_eur": settled,
            "reserved_micro_eur": reserved,
            "uncertain_micro_eur": uncertain,
            "unattributed_micro_eur": unattributed,
        },
    }


def _billing(event_id, machine, account, scope_alias, scope_revision,
             period_start, period_end, total, non_ai, observed_at):
    """A provider_billing reading: numbers and local aliases only."""
    return {
        "event_id": event_id,
        "machine": machine,
        "account": account,
        "kind": "provider_billing",
        "observed_at": observed_at,
        "payload": {
            "scope_alias": scope_alias,
            "scope_revision": scope_revision,
            "period_start": period_start,
            "period_end": period_end,
            "currency": "EUR",
            "total_micro_eur": total,
            "non_ai_spend_present": non_ai,
        },
    }


def _doc_v3(readings):
    return {"version": 3, "readings": readings}


def _billing_policy(account="meter", reporter="desktop", scope="proj-ai",
                    revision=1, allowance=0):
    """The B06 policy shape: all bases ex_vat, no conversion needed."""
    return {
        "account": account,
        "reporter_machine": reporter,
        "scope_alias": scope,
        "scope_revision": revision,
        "dedicated_ai": True,
        "non_ai_allowance_micro_eur": allowance,
        "cap_basis": "ex_vat",
        "provider_basis": "ex_vat",
        "ledger_basis": "ex_vat",
        "vat_bps": None,
        "poll_seconds": 900,
        "max_age_seconds": 7200,
        "lag_seconds": 14400,
        "abs_tolerance_micro_eur": 50000,
        "relative_tolerance_bps": 100,
        "mode": "advisory",
        "stale_action": "alert",
        "divergence_action": "hold",
    }


def _policy_doc(policies):
    return {"version": 1, "policies": policies}


def _fingerprint(db):
    """Every row of every card-1..3 table: a semantic before/after snapshot
    (the WAL can hold committed pages outside the main file, so file bytes
    are not the right equality for a WAL-mode store)."""
    out = []
    conn = sqlite3.connect(db)
    try:
        for table in ("accounts", "machines", "gateways", "readings",
                      "windows", "billing_policies", "billing_totals"):
            try:
                out.append((table, conn.execute(
                    "SELECT * FROM " + table).fetchall()))
            except sqlite3.OperationalError:
                out.append((table, "<absent>"))
    finally:
        conn.close()
    return out


class _Base(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base,
                                                prefix="quota-card2b2-")
        self.tmp = self._tmp.name
        self.db = os.path.join(self.tmp, "inventory.db")
        run_cli("--db", self.db, "init")
        self.assertEqual(_config_import(self.db, self.tmp,
                                        copy.deepcopy(VALID_DOC)).returncode,
                         0)

    def tearDown(self):
        self._tmp.cleanup()

    def _import_policy(self, **kwargs):
        doc = _policy_doc([_billing_policy(**kwargs)])
        return _billing_policy_import(self.db, self.tmp, doc)


class GatewayV3OneMonthPeriod(_Base):
    """R2: the v3 gateway snapshot period is exactly one UTC month, the
    same rule as the provider period - a multi-month span folds distinct
    months into one report and leaves its period_end stale."""

    def test_gateway_v3_period_is_one_utc_month(self):
        observed = SEP + 2 * _MS_PER_DAY
        # A three-month span [Sep 1, Dec 1): refused.
        bad = _doc_v3([_v3_gateway_snapshot(
            _eid("gw-2b2-multimonth"), "desktop", "meter", "g1", 1, 1,
            observed, settled=0, reserved=0, uncertain=0, unattributed=0,
            period_start=SEP, period_end=DEC)])
        result = _readings_import(self.db, self.tmp, bad)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)

        # Exactly one month [Sep 1, Oct 1): accepted.
        ok = _doc_v3([_v3_gateway_snapshot(
            _eid("gw-2b2-onemonth"), "desktop", "meter", "g1", 1, 2,
            observed, settled=1_000_000, reserved=200_000, uncertain=0,
            unattributed=100_000, period_start=SEP, period_end=OCT)])
        self.assertEqual(_readings_import(self.db, self.tmp, ok).returncode,
                         0)


class ConfigImportPolicyGuard(_Base):
    """R1a: a config-import may not break a billing policy. An account
    that has a policy must stay ingest-able: an import that breaks B01
    for it, or that flips it off the metered kind, is refused (static
    error, nothing written) instead of stranding the account's billing
    readings (every later reading would be refused, and a mixed batch
    would lose its gateway snapshot too). The check mirrors the ingest
    re-check: same kind rule, same allocation on the cap's tax basis."""

    def _set_up_policy_with_headroom(self):
        # Shares 30 + 40 with a 20 allowance leave headroom (70 + 20 <=
        # 100), so the policy imports.
        doc = copy.deepcopy(VALID_DOC)
        doc["gateways"][0]["share_micro_eur"] = 30_000_000
        self.assertEqual(_config_import(self.db, self.tmp, doc).returncode,
                         0)
        self.assertEqual(self._import_policy(
            allowance=20_000_000).returncode, 0)

    def _later_reading(self, label):
        now = _now_ms()
        start, end = _month_bounds_of(now - 1000)
        return _billing(_eid(label), "desktop", "meter", "proj-ai", 1,
                        start, end, 100, False, now - 1000)

    def test_config_import_b01_break_refused(self):
        self._set_up_policy_with_headroom()
        before = _fingerprint(self.db)
        # Raising g1 to 50 keeps the shares card-1-legal (50 + 40 = 90
        # <= 100) but shares + allowance (90 + 20 = 110) exceeds the cap:
        # the import is refused and nothing is written.
        doc = copy.deepcopy(VALID_DOC)
        doc["gateways"][0]["share_micro_eur"] = 50_000_000
        result = _config_import(self.db, self.tmp, doc)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)
        self.assertEqual(_fingerprint(self.db), before)
        # The store still holds the valid allocation: a later billing
        # reading for the account still ingests.
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc_v3([self._later_reading("2b2-b01")])).returncode, 0)

    def test_config_import_kind_flip_refused(self):
        self._set_up_policy_with_headroom()
        before = _fingerprint(self.db)
        # Flipping the policy account from metered to subscription: card 1
        # admits no gateway on a subscription account, so the document
        # drops the gateways and the budget as well. The import is refused
        # and nothing is written.
        doc = copy.deepcopy(VALID_DOC)
        doc["accounts"][0]["kind"] = "subscription"
        doc["accounts"][0]["budget_micro_eur"] = None
        doc["gateways"] = []
        result = _config_import(self.db, self.tmp, doc)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)
        self.assertEqual(_fingerprint(self.db), before)
        # The account is still metered with the intact allocation: a
        # later billing reading still ingests.
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc_v3([self._later_reading("2b2-flip")])).returncode, 0)


if __name__ == "__main__":
    unittest.main()
