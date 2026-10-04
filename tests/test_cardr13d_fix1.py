"""Card R1-3d, fix round 1 (Codex's read of the first build).

* **The chain keeps ingest's verdict.** Each version-4 report is stored with `in_order`, written in the same transaction as the report from what
  `evidence.assess` decided when it arrived (migration 11). The usable chain is the adopted report plus the later reports of the same reader,
  epoch, generation and month with `in_order = 1`; it never decides order again, because arrival order cannot be rebuilt later.
* **The adoption clock is read after the write lock.** A report that was fresh when the command started must not be adopted if it expired while
  the command waited for the lock; the month boundary the same way.
* **`--resolve` is bounded** before any conversion: a very long number is bad input (exit 2), not a store error.
* **Two operators at once, for real:** two connections, one waiting for the other's lock; the second re-reads under the lock and is refused.

Synthetic reports in a fixed past month. No seat, lease or admission decision reads any of this.
"""

import contextlib
import copy
import datetime
import io
import json
import sqlite3
import threading
import unittest
from unittest import mock

from quota_balancer import billing_eval, cli, errors, evidence_view, migrate, store
from quota_balancer.errors import QuotaError

from tests import test_cardr13d_operator as op
from tests.test_card2b1 import run_cli
from tests.test_cardr13b_ingest import INC2
from tests.test_card2b2 import _POLICY

rep, NOW, OBS, SEP, OCT = op.rep, op.NOW, op.OBS, op.SEP, op.OCT
AUGUST = int(datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000)


class _Fix(op._Op):

    def chain_ids(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            return [r["event_id"] for r in evidence_view.usable_chain(conn, self.gateway_id())]

    def verdicts(self):
        return dict(self.rows("SELECT seq, in_order FROM gateway_totals ORDER BY seq"))


class StoredVerdict(_Fix):

    def test_codexs_case_a_late_report_is_not_trusted_through_the_real_path(self):
        """Adopt September 1; August 10 arrives (same reader and ledger); September 2 arrives with a hold. Arrival ignored 2 as late."""
        one = self.first()
        self.send(rep(seq=10, period_start=AUGUST, period_end=SEP, obs=OBS + 10))
        two = self.send(rep(seq=2, reconciliation_required=True, obs=OBS + 20))
        self.assertEqual(self.chain_ids(), [one["event_id"]])                                  # report 2 is not in the chain
        got = store.evidence_inputs(self.db, "meter", NOW + 100)
        self.assertEqual(got["states"]["g1"], "usable")                                        # on the strength of report 1 alone
        self.assertEqual([r["event_id"] for r in got["readings"]], [one["event_id"]])
        self.assertNotIn(two["event_id"], [r["event_id"] for r in got["readings"]])
        context = {"account": "meter", "period_start": SEP, "period_end": OCT, "cap_micro_eur": 100_000_000,
                   "gateways": [g for g in got["gateways"] if g["gateway"] == "g1"], "policy": copy.deepcopy(_POLICY),
                   "readings": got["readings"], "unresolved_event_ids": got["unresolved_event_ids"],
                   "boundary_exposure_micro_eur": None, "boundary_event_ids": []}
        request = {"version": 1, "account": "meter", "now_ms": NOW + 100, "requested_micro_eur": 0, "coverage": []}
        self.assertNotEqual(billing_eval.evaluate_billing(context, request)["reason"], "gateway_unusable")
        # and status shows the chain's newest report (1) as the one the verdict rests on: the adopted report is still the base
        block = self.evidence(now=NOW + 100)
        self.assertEqual((block["adopted"]["seq"], block["state"]), (1, "usable"))
        self.assertEqual(block["old_chain_reports"], 1)
        self.assertEqual(block["late_reports"], 1)

    def test_a_late_lower_report_after_a_higher_one_in_the_same_month_is_not_in_the_chain(self):
        one = self.first()
        high = self.send(rep(seq=5, obs=OBS + 10))
        self.send(rep(seq=3, obs=OBS + 20))
        self.assertEqual(self.chain_ids(), [one["event_id"], high["event_id"]])
        self.assertEqual(self.verdicts(), {1: 0, 3: 0, 5: 1})

    def test_the_normal_in_order_sequence_is_in_the_chain_by_number(self):
        one = self.first()
        later = [self.send(rep(seq=n, settled_micro_eur=5000 + n, obs=OBS + n)) for n in (2, 3, 4)]
        self.assertEqual(self.chain_ids(), [one["event_id"]] + [i["event_id"] for i in later])
        self.assertEqual(self.verdicts(), {1: 0, 2: 1, 3: 1, 4: 1})          # the adopted report itself is the base, whatever it was stored as

    def test_ingest_writes_the_verdict_of_assess_for_every_standing(self):
        self.approve()
        self.send(rep(seq=1))                                                   # no adoption yet: kept, in no chain
        self.adopt(self.send(rep(seq=2)))                                       # the newest is adopted; it too arrived before any adoption
        self.send(rep(seq=3, obs=OBS + 10))                                     # in order
        self.send(rep(seq=1, reader_incarnation=INC2, obs=OBS + 20))            # a new reader: pending, not this chain
        self.send(rep(seq=4, period_start=AUGUST, period_end=SEP, obs=OBS + 30))   # an older month
        with self.assertRaises(QuotaError):                                     # collides with seq 2: refused, nothing of it stored
            self.send(rep(seq=2, obs=OBS + 40, settled_micro_eur=5001))
        rows = self.rows("SELECT reader_incarnation, period_start, seq, in_order FROM gateway_totals ORDER BY rowid")
        self.assertEqual([r[3] for r in rows], [0, 0, 1, 0, 0])

    def test_the_chain_does_not_depend_on_rowid_or_arrival_clock(self):
        """Renumber the rowids and reverse the receive times: the chain is the same, because it reads the stored verdict and the number."""
        one = self.first()
        a = self.send(rep(seq=2, obs=OBS + 10))
        b = self.send(rep(seq=3, obs=OBS + 20))
        before = self.chain_ids()
        self.run_sql("UPDATE gateway_totals SET received_at = 1000000 - seq")
        self.run_sql("UPDATE gateway_totals SET rowid = 100 - seq")
        self.assertEqual(self.chain_ids(), before)
        self.assertEqual(before, [one["event_id"], a["event_id"], b["event_id"]])


class MigrationEleven(_Fix):

    def downgrade_to_ten(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("ALTER TABLE gateway_totals DROP COLUMN in_order")
            conn.execute("PRAGMA user_version=10")
            conn.commit()
        finally:
            conn.close()

    def schema(self):
        return sorted(self.rows("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))

    def test_a_version_10_store_with_rows_reads_them_as_not_in_order_and_nothing_else_changes(self):
        self.first()
        self.send(rep(seq=2, obs=OBS + 10))
        fresh = self.schema()
        before = {t: self.rows("SELECT * FROM " + t) for t in ("readings", "gateway_adoptions", "gateway_readers", "gateway_chain_state",
                                                                "gateway_transitions", "gateway_approvals", "audit")}
        columns = [r[1] for r in self.rows("PRAGMA table_info(gateway_totals)")][:-1]
        totals = self.rows("SELECT " + ", ".join(columns) + " FROM gateway_totals ORDER BY seq")             # fixed names from the table
        self.downgrade_to_ten()
        self.assertEqual(self.rows("PRAGMA user_version"), [(10,)])
        self.assertEqual(run_cli("--db", self.db, "init").returncode, 0)
        self.assertEqual(self.rows("PRAGMA user_version"), [(11,)])
        self.assertEqual(self.rows("SELECT seq, in_order FROM gateway_totals ORDER BY seq"), [(1, 0), (2, 0)])      # conservative
        self.assertEqual(self.rows("SELECT " + ", ".join(columns) + " FROM gateway_totals ORDER BY seq"), totals)
        for table, rows in before.items():
            self.assertEqual(self.rows("SELECT * FROM " + table), rows, table)
        self.assertEqual(self.schema(), fresh)
        # and the chain of that store is only the adopted report, until newer reports arrive
        self.assertEqual(len(self.chain_ids()), 1)

    def test_a_store_at_version_11_is_refused_by_the_version_10_reader_and_the_column_cannot_be_forged(self):
        with mock.patch.object(migrate, "SCHEMA_VERSION", 10):
            with self.assertRaises(QuotaError) as caught:
                store.status(self.db)
        self.assertEqual(caught.exception.code, errors.CODE_SCHEMA_UNSUPPORTED)
        self.first()
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_sql("UPDATE gateway_totals SET in_order=2")


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class _Spy:
    """A connection whose `BEGIN IMMEDIATE` runs a callback AFTER the lock is held (the moment the command stops waiting)."""

    def __init__(self, conn, after_lock):
        self._conn, self._after_lock = conn, after_lock

    def execute(self, sql, *args):
        result = self._conn.execute(sql, *args)
        if sql == "BEGIN IMMEDIATE":
            self._after_lock()
        return result

    def __getattr__(self, name):
        return getattr(self._conn, name)


class ClockAfterTheLock(_Fix):

    def adopt_with_clock(self, item, before, after):
        """Adopt with the production clock: it reads `before` until the write lock is held, and `after` from then on."""
        clock = _Clock(before)
        real = store._connect
        with mock.patch.object(store, "_now_ms", clock), \
                mock.patch.object(store, "_connect", lambda path, must_exist: _Spy(real(path, must_exist), lambda: setattr(clock, "t", after))):
            return store.gateway_adopt(self.db, "g1", item["event_id"], [])

    def test_a_report_fresh_before_the_lock_and_expired_after_it_is_refused_and_nothing_changes(self):
        self.approve()
        item = self.send(rep(seq=1))
        before = self.dump()
        with self.assertRaises(QuotaError) as caught:
            self.adopt_with_clock(item, OBS + 119_999, OBS + 120_001)
        self.assertEqual(caught.exception.code, errors.CODE_ADOPTION_REFUSED)
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.adopt_with_clock(item, OBS + 119_000, OBS + 120_000), 1)       # the boundary itself is still fresh

    def test_a_report_of_this_month_before_the_lock_and_of_the_next_after_it_is_refused(self):
        self.approve()
        item = self.send(rep(seq=1, obs=OCT - 5_000))
        before = self.dump()
        with self.assertRaises(QuotaError) as caught:
            self.adopt_with_clock(item, OCT - 1_000, OCT + 1_000)
        self.assertEqual(caught.exception.code, errors.CODE_ADOPTION_REFUSED)
        self.assertEqual(self.dump(), before)

    def test_the_clock_is_not_read_before_the_lock_by_the_commands_that_check_it(self):
        reads = []
        real = store._connect
        with mock.patch.object(store, "_now_ms", lambda: reads.append("clock") or OBS), \
                mock.patch.object(store, "_connect", lambda path, must_exist: _Spy(real(path, must_exist), lambda: reads.append("lock"))):
            self.approve_default()
            with self.assertRaises(QuotaError):
                store.gateway_adopt(self.db, "g1", "no-such-report", [])
        self.assertEqual(reads, ["lock", "clock", "lock", "clock"])

    def approve_default(self):
        store.gateway_approve(self.db, "g1", op.PRICE, op.CAP)


class ResolveBound(_Fix):

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--db", self.db] + list(args))
        return code, out.getvalue(), err.getvalue()

    def test_a_4301_digit_number_is_bad_input_with_the_fixed_sentence_and_the_store_unchanged(self):
        self.approve()
        item = self.send(rep(seq=1))
        before = self.dump()
        code, out, err = self.run_main("gateway-adopt", "--gateway", "g1", "--evidence", item["event_id"], "--resolve", "9" * 4301)
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(json.loads(err), {"error": {"code": "INVALID_CONFIG", "message": errors.MSG_INVALID_CONFIG}})
        self.assertNotIn("999", err)
        self.assertEqual(self.dump(), before)

    def test_only_a_whole_number_that_fits_a_sqlite_integer_is_an_id(self):
        self.approve()
        item = self.send(rep(seq=1))
        for text in ("9" * 20, str(2 ** 63), "0" * 20, "1" * 4301, "-1", "1.5", "", " 1"):
            with self.subTest(text=text[:12]):
                self.refuse(item, [text], code=errors.CODE_INVALID_CONFIG)
        for number in (2 ** 63, -1, 10 ** 30):
            with self.subTest(number=number):
                self.refuse(item, [number], code=errors.CODE_INVALID_CONFIG)
        self.refuse(item, [str(2 ** 63 - 1)])                                    # fits: a well-formed id that is simply not open
        self.refuse(item, ["1"])                                                 # the same: a naming mistake, not bad input


class TwoOperators(_Fix):

    def test_the_second_operator_waits_for_the_first_then_re_reads_under_the_lock_and_is_refused(self):
        self.approve()
        newest = self.send(rep(seq=2))
        results, waiting, at_step = {}, threading.Event(), threading.Event()
        real_connect = store._connect

        def connect(path, must_exist):
            conn = real_connect(path, must_exist)
            if threading.current_thread().name != "B":
                return conn
            return _Waiting(conn, waiting)

        def hook(conn, step):
            if threading.current_thread().name == "A" and step == 2:             # A holds the write lock here
                at_step.set()
                self.assertTrue(waiting.wait(30))                              # B is about to ask for the lock...
                threading.Event().wait(0.3)                                    # ...and is now blocked in it, behind A

        def run(name):
            try:
                results[name] = store.gateway_adopt(self.db, "g1", newest["event_id"], [], now_ms=NOW)
            except QuotaError as exc:
                results[name] = exc.code

        with mock.patch.object(store, "_connect", connect), mock.patch.object(store, "_after_adopt_step", hook):
            a = threading.Thread(target=run, args=("A",), name="A")
            b = threading.Thread(target=run, args=("B",), name="B")
            a.start()
            self.assertTrue(at_step.wait(30))
            b.start()
            a.join(60)
            b.join(60)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(results, {"A": 1, "B": errors.CODE_ADOPTION_REFUSED})
        self.assertEqual(self.rows("SELECT id, event_id FROM gateway_adoptions"), [(1, newest["event_id"])])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM audit WHERE action='gateway_adopted'"), [(1,)])
        self.assertEqual(self.rows("SELECT state FROM gateway_readers"), [("current",)])

    def test_the_same_report_adopted_twice_is_a_clean_refusal_not_a_store_error(self):
        self.approve()
        item = self.send(rep(seq=1))
        self.adopt(item)
        self.refuse(item)                                                         # was an unhandled integrity error before the guard
        self.assertEqual(self.evidence()["adopt_blockers"], ["not_newest"])


class _Waiting:
    """A connection that tells a test it is about to ask for the write lock."""

    def __init__(self, conn, event):
        self._conn, self._event = conn, event

    def execute(self, sql, *args):
        if sql == "BEGIN IMMEDIATE":
            self._event.set()
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


if __name__ == "__main__":
    unittest.main()
