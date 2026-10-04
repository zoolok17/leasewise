"""Card R1-READER step 3b: the money reader's whole cycle (`reporter/gwcycle.py`, `gwwire.py`, the `gateway_report` configuration table).

What is proven here, with a fake runner (the gateway's program is never started; its output is agenttalk's own documented report and receipt
page, byte for byte) and a REAL in-process Leasewise:

* **End to end.** The reader runs the report command, reads the receipts up to the snapshot's own number, computes the figures, saves the
  version-4 bytes and sends them. Leasewise stores the report; the operator approves the price and cap pair and adopts the report; Leasewise's
  `status` then says the gateway's figures are usable. A second cycle sends report 2, which joins the chain. A restart in between keeps the numbers.
* **The bytes** are checked through Leasewise's real receiving path (`http_api._wire_to_reading`, then `validate_readings`), not only against the
  reader's own checker.
* **Every failure** of the cycle gives no report and one closed word, counted. A waiting report is always sent first, and the gateway is not run
  while one is waiting.
* **The configuration table** has the receipt command's refusals, plus the fixed tail and the same-gateway rule; without it the reader does nothing.

Synthetic values only; no network beyond loopback; nothing real is run.
"""

import contextlib
import datetime
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from quota_balancer import evidence_view, http_api, store, validate_readings
from quota_balancer.reporter import config as cfg, gwcycle, gwreport, gwstate, gwwire, runner, status

from tests import l6a1_env
from tests.reporter_env import Rig, build_store
from tests.test_cardr1reader1_report import AGENTTALK_PAGE, AGENTTALK_REPORT, GEN, GEN2, NAME, receipt as month_receipt

EXE = r"C:\path\to\agenttalk.exe"                          # a placeholder: nothing is ever started
T0 = int(datetime.datetime(2026, 10, 4, 12, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
DOC_PRICE = json.loads(AGENTTALK_REPORT)["policy_hash"]    # the pair agenttalk's documented report carries
DOC_CAP = json.loads(AGENTTALK_REPORT)["child_cap_policy_hash"]
_TEMPLATE = {}


def setUpModule():
    base = os.environ.get("AGENTTALK_SCRATCH")
    if base:
        os.makedirs(base, exist_ok=True)
    folder = tempfile.mkdtemp(dir=base, prefix="quota-r1r3b-tpl-")
    _TEMPLATE["dir"] = folder
    _TEMPLATE["db"] = os.path.join(folder, "template.db")
    build_store(_TEMPLATE["db"], folder)


def tearDownModule():
    shutil.rmtree(_TEMPLATE["dir"], ignore_errors=True)


class FakeGateway:
    """Stands in for `runner.run`: the report mode answers `report`, the receipts mode answers the page after `after` made from `rows`."""

    def __init__(self):
        self.report = AGENTTALK_REPORT
        self.rows = json.loads(AGENTTALK_PAGE)["receipts"]
        self.generation = GEN
        self.report_word = runner.OK                        # what the runner says about the report command
        self.report_confirmed = True                        # whether the end of the report command's job was confirmed
        self.page_word = runner.OK
        self.page_edit = None                               # a function that changes the page document before it is printed
        self.calls = []

    def __call__(self, command, after=None, limit=None, *, timeout_seconds, max_bytes, env=(), stop=None, mode=runner.MODE_RECEIPTS, **_kw):
        self.calls.append((mode, after, tuple(command)))
        if mode == runner.MODE_REPORT:
            return runner.Result(self.report_word, self.report if self.report_word == runner.OK else None, self.report_confirmed)
        shown = [row for row in self.rows if row["seq"] > after][:limit]
        doc = {"envelope_version": 1, "generation": self.generation, "after_seq": after,
               "next_seq": shown[-1]["seq"] if shown else after, "has_more": False, "receipts": shown}
        if self.page_edit:
            doc = self.page_edit(doc)
        return runner.Result(self.page_word, json.dumps(doc).encode("utf-8") if self.page_word == runner.OK else None, True)

    def reports(self):
        return [call for call in self.calls if call[0] == runner.MODE_REPORT]


def toml_blocks(admission_store, report_extra="", receipts_extra=""):
    return ("[gateway_receipts]\ngateway = \"g1\"\nadmission_store = %s\ncommand = %s\n%s\n"
            "[gateway_report]\ngateway = \"g1\"\naccount = \"meter\"\ncommand = %s\n%s\n") % (
        json.dumps(admission_store), json.dumps([EXE, "gateway", "receipts"]), receipts_extra,
        json.dumps([EXE, "gateway", "report", "--json"]), report_extra)


class Env(unittest.TestCase):
    """A real in-process Leasewise (a store copied from one template), a reporter configured with the money reader, and a fake gateway."""

    def setUp(self):
        self.rig = Rig(template=_TEMPLATE["db"])
        self.addCleanup(self.rig.cleanup)
        self.rig.clock.ms = T0
        self.gateway = FakeGateway()
        patcher = mock.patch.object(runner, "run", self.gateway)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.admission = l6a1_env.make_store(os.path.join(self.rig.ws.dir, "admission.db"))
        self.mono = [0.0]                                   # the reporter's monotonic clock, driven by the test
        self.write_config()
        self.reporter = self.attach(self.rig.open())

    def attach(self, reporter):
        reporter.monotonic = lambda: self.mono[0]
        return reporter

    def write_config(self, **kw):
        self.rig.ws.write_config(self.rig.lw.url, self.rig.agents_cfg, extra=toml_blocks(self.admission, **kw))

    def restart(self):
        self.reporter = self.attach(self.rig.restart())

    # --- what the real Leasewise holds -------------------------------------------------------------------------------------------
    def rows(self, sql, params=()):
        return self.rig.lw.rows(sql, params)

    def totals(self):
        return self.rows("SELECT seq, ledger_epoch, reader_incarnation, settled_micro_eur, unattributed_micro_eur, receipts_seq, quiet "
                         "FROM gateway_totals ORDER BY seq")

    def reader(self, sql):
        return self.rig.state.all(sql)

    def counts(self):
        return {name: self.rig.state.counter(gwstate.READER_AGENT, name) for name in status.MONEY_READER_COUNTS}

    def lane(self, wait=100):
        """One cycle of the reporter; then the clock moves a little (so a second report is a different instant) and the monotonic clock moves by
        `wait` seconds (the default is past the report interval, so every cycle is due unless a test says otherwise)."""
        result = self.reporter.cycle()
        self.rig.clock.advance(1)
        self.mono[0] += wait
        return result

    def evidence(self):
        """The evidence block of Leasewise's `status` for the gateway, at the test's clock (`status` itself reads the real one)."""
        with contextlib.closing(sqlite3.connect(self.rig.lw.db)) as conn:
            gateway_id = conn.execute("SELECT id FROM gateways WHERE alias='g1'").fetchone()[0]
            return evidence_view.project(conn, gateway_id, self.rig.clock.ms)

    def open_ids(self):
        return [row[0] for row in self.rows("SELECT id FROM gateway_transitions WHERE resolved_by_adoption IS NULL ORDER BY id")]


class EndToEnd(Env):

    def test_a_cycle_sends_a_report_leasewise_stores_the_operator_adopts_it_and_status_says_usable(self):
        result = self.lane()
        self.assertEqual((result.sent, result.blocked), (1, None))
        (row,) = self.totals()
        self.assertEqual(row[:2], (1, 1))                                      # report 1 of epoch 1
        self.assertEqual(row[3:], (2630, 670, 1, 1))                           # the documented example: unowned money 670, N = 1, quiet
        self.assertEqual(self.reader("SELECT minted_seq, acked_seq FROM gwr_seq"), [(1, 1)])
        self.assertEqual(self.reader("SELECT * FROM gwr_pending"), [])
        self.assertEqual(self.evidence()["state"], "unadopted")
        store.gateway_approve(self.rig.lw.db, "g1", DOC_PRICE, DOC_CAP, now_ms=self.rig.clock.ms)
        event_id = self.rows("SELECT event_id FROM gateway_totals")[0][0]
        store.gateway_adopt(self.rig.lw.db, "g1", event_id, self.open_ids(), now_ms=self.rig.clock.ms)
        self.assertEqual(self.evidence()["state"], "usable")

    def test_a_second_cycle_sends_report_2_which_joins_the_chain_and_a_restart_keeps_the_numbers(self):
        self.lane()
        event_id = self.rows("SELECT event_id FROM gateway_totals")[0][0]
        store.gateway_approve(self.rig.lw.db, "g1", DOC_PRICE, DOC_CAP, now_ms=self.rig.clock.ms)
        store.gateway_adopt(self.rig.lw.db, "g1", event_id, self.open_ids(), now_ms=self.rig.clock.ms)
        incarnation = self.reader("SELECT reader_incarnation FROM gwr_identity")
        self.restart()                                                         # the reader stops and starts again
        self.assertEqual(self.reader("SELECT reader_incarnation FROM gwr_identity"), incarnation)
        self.assertEqual(self.reader("SELECT minted_seq, acked_seq FROM gwr_seq"), [(1, 1)])
        self.lane()
        self.assertEqual([row[:3] for row in self.totals()], [(1, 1, incarnation[0][0]), (2, 1, incarnation[0][0])])
        self.assertEqual(self.reader("SELECT minted_seq, acked_seq FROM gwr_seq"), [(2, 2)])
        self.assertEqual(self.evidence()["state"], "usable")
        self.assertEqual(self.evidence()["latest"]["seq"], 2)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM gateway_epochs"), [(1,)])           # one epoch, two reports


def report_with(**over):
    """The documented report with some fields changed (as the bytes `gateway report --json` would print)."""
    doc = json.loads(AGENTTALK_REPORT)
    doc.update(over)
    return json.dumps(doc).encode("utf-8")


class Sends(Env):
    """A report that cannot be sent now is kept as the same bytes, and a waiting report is sent before the gateway is read again."""

    def paths(self):
        return [call[0] for call in self.rig.net.calls]

    def test_a_waiting_report_is_sent_first_as_the_same_bytes_and_the_gateway_is_not_run_while_it_waits(self):
        self.rig.net.down = True
        self.lane()
        self.assertEqual(len(self.gateway.reports()), 1)
        (saved,) = self.reader("SELECT seq, body FROM gwr_pending")
        for _ in range(3):
            result = self.lane()
            self.assertEqual((result.sent, result.blocked), (0, "unreachable"))
        self.assertEqual(len(self.gateway.reports()), 1)                       # not run again: a report is waiting
        self.assertEqual(self.reader("SELECT seq, body FROM gwr_pending"), [saved])        # the same bytes, the same number
        self.assertEqual(self.reader("SELECT minted_seq, acked_seq FROM gwr_seq"), [(1, 0)])
        self.assertEqual(self.counts()["send_failed"], 4)
        self.rig.net.down = False
        result = self.lane()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM readings"), [(2,)])   # report 1 accepted, and report 2 made and sent in the same cycle
        self.assertEqual(len(self.gateway.reports()), 2)
        self.assertEqual(result.sent, 2)
        stored = self.rows("SELECT payload FROM readings ORDER BY observed_at")
        self.assertEqual(json.loads(stored[0][0]), {k: v for k, v in json.loads(bytes(saved[1]))["payload"].items() if k != "gateway_id"} | {"gateway": "g1"})
        self.assertEqual(self.reader("SELECT minted_seq, acked_seq FROM gwr_seq"), [(2, 2)])

    def test_a_halted_reader_does_nothing_and_does_not_read_the_gateway(self):
        self.lane()
        with self.rig.state.txn():
            self.rig.state.conn.execute("UPDATE gwr_identity SET halt='continuity_conflict'")
        runs = len(self.gateway.reports())
        self.lane()
        self.assertEqual(len(self.gateway.reports()), runs)
        self.assertEqual(self.counts()["reader_halted"], 1)
        self.assertEqual(self.reader("SELECT minted_seq, acked_seq FROM gwr_seq"), [(1, 1)])

    def test_a_damaged_state_does_not_read_the_gateway_either(self):
        self.lane()
        self.rig.state.conn.execute("PRAGMA ignore_check_constraints=ON")
        self.rig.state.conn.execute("UPDATE gwr_seq SET acked_seq=5")
        self.rig.state.conn.execute("PRAGMA ignore_check_constraints=OFF")
        runs = len(self.gateway.reports())
        self.lane()
        self.assertEqual(len(self.gateway.reports()), runs)
        self.assertEqual(self.reader("SELECT halt FROM gwr_identity"), [("state_unavailable",)])
        self.assertEqual(self.counts()["state_unavailable"], 1)


class Failures(Env):
    """Every failure of steps 4 to 8 gives NO report and ONE closed word, counted; nothing is numbered, saved or sent."""

    def assert_no_report(self, word):
        self.assertEqual(self.reader("SELECT * FROM gwr_pending"), [])
        self.assertEqual(self.reader("SELECT * FROM gwr_seq"), [])                         # no number was taken
        self.assertEqual(self.rows("SELECT COUNT(*) FROM readings"), [(0,)])
        self.assertNotIn("/v1/readings", [call[0] for call in self.rig.net.calls])
        self.assertEqual({name: count for name, count in self.counts().items() if count}, {word: 1})

    def test_the_runner_failing_in_any_way_is_status_failed(self):
        for word in (runner.CANNOT_RUN, runner.CANNOT_CONTAIN, runner.TIMEOUT, runner.EXIT_CODE, runner.LEFTOVER, runner.TOO_LARGE,
                     runner.READ_FAILED, runner.CLEANUP_INCOMPLETE, runner.INTERRUPTED):
            with self.subTest(word):
                self.setUp()
                self.gateway.report_word = word
                self.lane()
                self.assert_no_report(gwcycle.STATUS_FAILED)

    def test_an_end_of_the_job_that_was_not_confirmed_is_status_failed_even_with_output(self):
        self.gateway.report_confirmed = False
        self.lane()
        self.assert_no_report(gwcycle.STATUS_FAILED)

    def test_a_snapshot_the_projection_refuses_is_status_shape(self):
        doc = json.loads(AGENTTALK_REPORT)
        bad = {"not an object": b"[]", "not json": b"{", "a repeated key": b'{"generation":"a","generation":"a"}',
               "a constant": b'{"gateway_report_version":NaN}', "an unknown version": report_with(gateway_report_version=2),
               "a missing field": json.dumps({k: v for k, v in doc.items() if k != "periods"}).encode(),
               "a mistyped field": report_with(child_receipts_through_seq="1")}
        for label, raw in bad.items():
            with self.subTest(label):
                self.setUp()
                self.gateway.report = raw
                self.lane()
                self.assert_no_report(gwreport.STATUS_SHAPE)

    def test_a_gateway_without_lease_binding_or_caps_is_child_cap_not_ready(self):
        for label, raw in (("not ready", report_with(child_cap_ready=False, child_cap_policy_hash=None)),
                           ("no binding", report_with(child_receipt_report_version=None, child_receipts_through_seq=None))):
            with self.subTest(label):
                self.setUp()
                self.gateway.report = raw
                self.lane()
                self.assert_no_report(gwreport.CHILD_CAP_NOT_READY)

    def test_a_page_naming_another_generation_stops_the_read_with_generation_changed(self):
        self.gateway.generation = GEN2
        self.lane()
        self.assert_no_report(gwreport.GENERATION_CHANGED)

    def test_a_gap_a_repeat_a_closed_key_violation_and_a_wrong_version_are_receipts_failed(self):
        def gap(doc):
            doc["receipts"] = [dict(doc["receipts"][0], seq=2)]
            doc["next_seq"] = 2
            return doc

        def repeat(doc):
            doc["after_seq"] = 5
            return doc

        def extra_key(doc):
            doc["receipts"][0]["agent"] = NAME
            return doc

        def wrong_version(doc):
            doc["envelope_version"] = 2
            return doc
        for label, edit in (("a gap", gap), ("a wrong asked-for number", repeat), ("a key the page may not carry", extra_key),
                            ("an envelope version", wrong_version)):
            with self.subTest(label):
                self.setUp()
                self.gateway.page_edit = edit
                self.lane()
                self.assert_no_report(gwreport.RECEIPTS_FAILED)

    def test_a_receipt_command_that_fails_is_receipts_failed(self):
        for word in (runner.TIMEOUT, runner.EXIT_CODE, runner.TOO_LARGE):
            with self.subTest(word):
                self.setUp()
                self.gateway.page_word = word
                self.lane()
                self.assert_no_report(gwreport.RECEIPTS_FAILED)

    def test_a_gateway_that_promises_more_receipts_than_it_gives_is_receipts_failed(self):
        self.gateway.report = report_with(child_receipts_through_seq=5)
        self.lane()
        self.assert_no_report(gwreport.RECEIPTS_FAILED)

    def test_receipts_above_n_are_not_used_and_the_saved_position_stops_at_n(self):
        self.gateway.rows = json.loads(AGENTTALK_PAGE)["receipts"] + [month_receipt(2, 9999, "2026-10")]
        self.lane()
        self.assertEqual(self.reader("SELECT generation, read_through_seq FROM gwr_receipts"), [(GEN, 1)])
        self.assertEqual(self.reader("SELECT period, receipts, total_micro_eur FROM gwr_receipt_months"), [("2026-10", 1, 1290)])
        self.assertEqual(self.totals()[0][4], 670)                              # the unowned money counts the one receipt up to N only

    def test_an_n_below_the_saved_position_of_the_same_generation_is_receipts_rolled_back(self):
        self.lane()
        self.assertEqual(self.reader("SELECT minted_seq, acked_seq FROM gwr_seq"), [(1, 1)])
        self.gateway.report = report_with(child_receipts_through_seq=0)
        self.lane()
        self.assertEqual(self.counts()[gwreport.RECEIPTS_ROLLED_BACK], 1)
        self.assertEqual(self.reader("SELECT minted_seq, acked_seq FROM gwr_seq"), [(1, 1)])      # nothing new was numbered
        self.assertEqual(self.reader("SELECT * FROM gwr_pending"), [])

    def test_receipts_that_exceed_what_is_settled_are_refused_and_never_clamped(self):
        self.gateway.report = report_with(periods=[{"committed_micro_eur": 100, "period": "2026-10"}])
        self.lane()
        self.assert_no_report(gwreport.RECEIPTS_EXCEED_SETTLED)

    def test_a_later_period_than_the_readers_month_is_period_mismatch(self):
        self.gateway.report = report_with(periods=[{"committed_micro_eur": 2630, "period": "2026-10"},
                                                   {"committed_micro_eur": 5, "period": "2026-11"}])
        self.lane()
        self.assert_no_report(gwreport.PERIOD_MISMATCH)

    def test_the_words_are_counts_in_the_status_and_hold_no_value_from_the_report(self):
        self.gateway.report = report_with(agent=NAME, project=r"C:\Users\someone\project", generation="g" * 32)
        self.lane()
        report = self.reporter.report()
        self.assertEqual(report["money_reader"]["counts"][gwreport.STATUS_SHAPE], 1)
        text = json.dumps(report)
        for value in (NAME, "someone", GEN):
            self.assertNotIn(value, text)
        for word in gwcycle.CYCLE_WORDS:
            self.assertIn(word, report["money_reader"]["counts"])


class Generations(Env):

    def test_a_change_of_generation_takes_a_new_epoch_while_the_numbers_keep_growing(self):
        self.lane()
        self.gateway.generation = GEN2
        self.gateway.report = report_with(generation=GEN2)
        self.lane()
        self.gateway.generation = GEN                                           # a return to the first generation: a restored ledger
        self.gateway.report = report_with()
        self.lane()
        self.assertEqual(self.reader("SELECT ledger_epoch, generation FROM gwr_epoch ORDER BY ledger_epoch"), [(1, GEN), (2, GEN2), (3, GEN)])
        self.assertEqual(self.reader("SELECT generation, minted_seq, acked_seq FROM gwr_seq ORDER BY generation"), [(GEN, 2, 2), (GEN2, 1, 1)])
        self.assertEqual(sorted(self.rows("SELECT ledger_epoch, generation, seq FROM gateway_totals")), [(1, GEN, 1), (2, GEN2, 1), (3, GEN, 2)])


class Lane(Env):

    def test_an_unexpected_failure_of_the_gateway_program_call_never_stops_the_other_uploads(self):
        def boom(*_args, **_kw):
            raise RuntimeError("synthetic fault")
        with mock.patch.object(runner, "run", boom):
            result = self.lane()
        self.assertEqual((result.sent, result.blocked), (0, None))
        self.assertEqual(self.counts()["state_unavailable"], 1)
        self.assertEqual(self.reader("SELECT * FROM gwr_pending"), [])

    def test_a_reporter_without_the_table_is_unchanged_and_runs_nothing(self):
        self.rig.ws.write_config(self.rig.lw.url, self.rig.agents_cfg,
                                 extra="[gateway_receipts]\ngateway = \"g1\"\nadmission_store = %s\ncommand = %s\n" % (
                                     json.dumps(self.admission), json.dumps([EXE, "gateway", "receipts"])))
        self.reporter = self.attach(self.rig.restart())
        self.lane()
        self.assertEqual(self.gateway.reports(), [])
        self.assertEqual(self.reader("SELECT * FROM gwr_identity"), [])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM readings"), [(0,)])
        self.assertEqual(self.reporter.report()["money_reader"]["halt"], None)


class Interval(Env):
    """Lead decision D-217: a new report at most once per `interval_seconds` (default 60), on a monotonic clock kept in memory."""

    def minted(self):
        return self.reader("SELECT minted_seq FROM gwr_seq")

    def test_two_cycles_inside_the_interval_make_one_report(self):
        self.lane(wait=10)
        self.lane(wait=10)
        self.assertEqual(len(self.gateway.reports()), 1)
        self.assertEqual(self.minted(), [(1,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM readings"), [(1,)])

    def test_a_cycle_that_waits_for_the_interval_writes_nothing_and_counts_no_word(self):
        self.lane(wait=10)
        before = ({name: self.reader("SELECT * FROM " + name) for name in gwstate.TABLES}, self.counts())      # fixed names
        self.lane(wait=10)
        self.assertEqual(({name: self.reader("SELECT * FROM " + name) for name in gwstate.TABLES}, self.counts()), before)

    def test_the_next_cycle_after_the_interval_makes_a_report(self):
        self.lane(wait=59)
        self.lane(wait=1)                                                       # 59 s since the first: not yet
        self.assertEqual(len(self.gateway.reports()), 1)
        self.lane(wait=0)                                                       # 60 s: due
        self.assertEqual(len(self.gateway.reports()), 2)
        self.assertEqual(self.minted(), [(2,)])

    def test_a_waiting_report_is_resent_inside_the_interval_and_no_new_one_is_made(self):
        self.rig.net.down = True
        self.lane(wait=5)
        self.rig.net.down = False
        result = self.lane(wait=5)                                              # 5 s later: the same bytes go out, nothing new is read
        self.assertEqual((result.sent, len(self.gateway.reports())), (1, 1))
        self.assertEqual(self.reader("SELECT minted_seq, acked_seq FROM gwr_seq"), [(1, 1)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM readings"), [(1,)])

    def test_a_restart_makes_a_report_at_once(self):
        self.lane(wait=1)
        self.restart()
        self.lane(wait=1)
        self.assertEqual(len(self.gateway.reports()), 2)
        self.assertEqual(self.minted(), [(2,)])

    def test_a_failed_read_does_not_start_the_interval(self):
        self.gateway.report_word = runner.TIMEOUT
        self.lane(wait=1)
        self.gateway.report_word = runner.OK
        self.lane(wait=1)                                                       # one second later: the failure did not use up the interval
        self.assertEqual(self.minted(), [(1,)])

    def test_the_configured_interval_is_the_one_used(self):
        self.write_config(report_extra="interval_seconds = 30\n")
        self.reporter = self.attach(self.rig.restart())
        self.assertEqual(self.reporter.pacer.interval, 30)
        self.lane(wait=30)
        self.lane(wait=0)
        self.assertEqual(self.minted(), [(2,)])


def receipts_page():
    from quota_balancer.reporter import receipts
    return receipts.parse_page(AGENTTALK_PAGE, 0, month_rows=True)


class Wire(Env):
    """The bytes, through Leasewise's own receiving path, and what they hold."""

    def figures(self, **over):
        doc = json.loads(AGENTTALK_REPORT)
        doc.update(over)
        snapshot = gwreport.parse_snapshot(json.dumps(doc).encode())
        totals = gwreport.fold_page(gwreport.empty_totals(GEN), receipts_page(), snapshot)
        return gwreport.compute(snapshot, totals, "2026-10")

    def ids(self):
        with contextlib.closing(sqlite3.connect(self.rig.lw.db)) as conn:
            machine = conn.execute("SELECT id, alias FROM machines WHERE alias='desktop'").fetchone()
            return machine, conn.execute("SELECT id FROM gateways WHERE alias='g1'").fetchone()[0], \
                conn.execute("SELECT id FROM accounts WHERE alias='meter'").fetchone()[0]

    def through_leasewise(self, body):
        """The wire body as Leasewise's receiving path reads it: `_wire_to_reading`, then `validate_readings`."""
        machine, _gateway, _account = self.ids()
        with contextlib.closing(sqlite3.connect(self.rig.lw.db)) as conn:
            _account_id, document = http_api._wire_to_reading(json.loads(body), machine, conn)
        return document, validate_readings.validate_readings(document, T0)

    def build(self, **over):
        _machine, gateway, account = self.ids()
        return gwwire.build_report(self.figures(**over), gateway, account, "d" * 32, 3, 7, T0)

    def test_the_bytes_are_accepted_by_leasewises_real_receiving_path_with_every_figure_in_its_field(self):
        document, (reading,) = self.through_leasewise(self.build())
        self.assertEqual(document["version"], validate_readings.DOC_VERSION_V4)
        self.assertEqual(reading["event_id"], validate_readings.derive_event_id("d" * 32, GEN, 7))
        for key, value in (("gateway", "g1"), ("ledger_epoch", 3), ("seq", 7), ("settled_micro_eur", 2630), ("reserved_micro_eur", 0),
                           ("uncertain_micro_eur", 0), ("unattributed_micro_eur", 670), ("generation", GEN), ("receipts_seq", 1), ("quiet", True),
                           ("held_over_reservation", 0), ("reader_incarnation", "d" * 32), ("sized_ready", False),
                           ("reconciliation_required", False), ("policy_hashes", {"price": DOC_PRICE, "child_cap": DOC_CAP})):
            self.assertEqual(reading[key], value, key)
        self.assertEqual((reading["period_start"], reading["period_end"]), gwreport.month_bounds("2026-10"))
        self.assertEqual(reading["account"], "meter")

    def test_the_identity_facts_sit_where_the_state_reads_them(self):
        facts = gwstate.report_facts(self.build())
        self.assertEqual(facts, (validate_readings.derive_event_id("d" * 32, GEN, 7), "d" * 32, GEN, 7, 3))

    def test_the_same_inputs_give_the_same_bytes_and_the_keys_are_closed(self):
        self.assertEqual(self.build(), self.build())
        body = json.loads(self.build())
        self.assertEqual(set(body), set(http_api._WIRE_READING_REQUIRED))
        expected = (set(validate_readings._GATEWAY_SNAPSHOT_KEYS_V4) - {"gateway"}) | {"gateway_id"}
        self.assertEqual(set(body["payload"]), expected)

    def test_a_hold_busy_attempts_and_an_over_limit_attempt_map_to_their_fields(self):
        unresolved = [{"state": "reserved", "reserved_micro_eur": 40, "actual_micro_eur": None, "period": "2026-10"},
                      {"state": "uncertain", "reserved_micro_eur": 30, "actual_micro_eur": None, "period": "2026-10"},
                      {"state": "uncertain", "reserved_micro_eur": 20, "actual_micro_eur": 25, "period": "2026-10"}]
        _document, (reading,) = self.through_leasewise(self.build(unresolved=unresolved, service_hold=True, service_hold_reason="manual"))
        self.assertEqual((reading["reserved_micro_eur"], reading["uncertain_micro_eur"], reading["held_over_reservation"],
                          reading["reconciliation_required"]), (40, 30, 1, True))

    def test_nothing_named_by_an_agent_a_message_or_a_path_and_no_hold_word_is_in_the_bytes(self):
        raw = self.build(agent=NAME, project=r"C:\Users\someone\project", service_hold=True, service_hold_reason="manual")
        text = raw.decode("utf-8")
        for value in (NAME, "someone", "manual"):
            self.assertNotIn(value, text)


def minimal_text(extra):
    return ('version = 1\nbase_url = "http://127.0.0.1:9"\nidentity = "FAKE-IDENTITY-0001"\nmachine = "desktop"\njournal_root = "C:/j"\n'
            'state_path = "C:/s/r.db"\npolicy_ids = "C:/p.json"\n' + extra)


RECEIPTS = ('[gateway_receipts]\ngateway = "g1"\nadmission_store = "C:/a.db"\n'
            'command = ["C:\\\\path\\\\to\\\\agenttalk.exe", "gateway", "receipts"]\n')


def report_table(command=None, gateway="g1", account="meter", extra=""):
    command = [EXE, "gateway", "report", "--json"] if command is None else command
    return '[gateway_report]\ngateway = %s\naccount = %s\ncommand = %s\n%s' % (json.dumps(gateway), json.dumps(account), json.dumps(command), extra)


class Configuration(unittest.TestCase):

    def refused(self, text, fragment=None):
        with self.assertRaises(cfg.ConfigError) as caught:
            cfg.parse_config(minimal_text(text))
        self.assertEqual(caught.exception.code, cfg.CONFIG_INVALID)
        if fragment:
            self.assertIn(fragment, caught.exception.message)
        return caught.exception.message

    def test_a_good_table_is_read_and_hides_itself(self):
        config = cfg.parse_config(minimal_text(RECEIPTS + report_table(extra='timeout_seconds = 30\nmax_bytes = 2048\nenv = { A_B = "1" }\n')))
        row = config.gateway_report
        self.assertEqual((row.gateway, row.account, row.command, row.timeout_seconds, row.max_bytes, row.env),
                         ("g1", "meter", (EXE, "gateway", "report", "--json"), 30, 2048, (("A_B", "1"),)))
        self.assertEqual(repr(row), "GatewayReport(<hidden>)")
        self.assertEqual(cfg.parse_config(minimal_text(RECEIPTS)).gateway_report, None)         # without the table the reader does nothing

    def test_the_interval_defaults_to_60_and_takes_30_to_100(self):
        def interval(extra=""):
            return cfg.parse_config(minimal_text(RECEIPTS + report_table(extra=extra))).gateway_report.interval_seconds
        self.assertEqual(interval(), 60)
        self.assertEqual((interval("interval_seconds = 30\n"), interval("interval_seconds = 100\n")), (30, 100))
        for label, value in (("29", "29"), ("101", "101"), ("0", "0"), ("negative", "-60"), ("a string", '"60"'), ("a float", "60.0"), ("a boolean", "true")):
            with self.subTest(label):
                message = self.refused(RECEIPTS + report_table(extra="interval_seconds = %s\n" % value), "interval_seconds")
                self.assertNotIn("60", message.replace("30 to 100", ""))

    def test_the_receipt_commands_refusals_apply(self):
        for label, command in (("a relative path", ["agenttalk.exe", "gateway", "report", "--json"]),
                               ("not an .exe", [r"C:\path\to\agenttalk.cmd", "gateway", "report", "--json"]),
                               ("a network path", [r"\\server\share\agenttalk.exe", "gateway", "report", "--json"]),
                               ("a quote in an argument", [EXE, 'a"b', "gateway", "report", "--json"]),
                               ("--after named", [EXE, "--after", "gateway", "report", "--json"]),
                               ("--limit named", [EXE, "--limit=3", "gateway", "report", "--json"])):
            with self.subTest(label):
                self.refused(RECEIPTS + report_table(command))

    def test_a_shell_string_or_a_missing_or_misplaced_tail_is_refused(self):
        for label, command in (("a shell string", EXE + " gateway report --json"), ("no tail", [EXE]), ("the tail only", ["gateway", "report", "--json"]),
                               ("no --json", [EXE, "gateway", "report"]), ("a tail that is not last", [EXE, "gateway", "report", "--json", "x"]),
                               ("--json twice", [EXE, "--json", "gateway", "report", "--json"])):
            with self.subTest(label):
                self.refused(RECEIPTS + report_table(command))

    def test_the_table_needs_a_command_receipt_source_of_the_same_gateway(self):
        fixture = '[gateway_receipts]\ngateway = "g1"\nadmission_store = "C:/a.db"\nfixture = "C:/f.json"\n'
        self.refused(report_table(), "gateway_receipts")                                        # no receipt source at all
        self.refused(fixture + report_table(), "command")                                      # a fixture file is not a command
        self.refused(RECEIPTS + report_table(gateway="g2"), "gateway_receipts")                # another gateway

    def test_the_keys_are_closed_and_the_bounds_are_the_receipt_commands(self):
        self.refused(RECEIPTS + report_table(extra='shell = true\n'))
        self.refused(RECEIPTS + report_table(extra='timeout_seconds = 61\n'))
        self.refused(RECEIPTS + report_table(extra='max_bytes = 10\n'))
        self.refused(RECEIPTS + report_table(extra='env = { "a b" = "1" }\n'))
        self.refused(RECEIPTS + '[gateway_report]\ngateway = "g1"\ncommand = ["C:\\\\x.exe", "gateway", "report", "--json"]\n', "account")

    def test_the_messages_never_hold_a_value_from_the_file(self):
        message = self.refused(RECEIPTS + report_table([r"C:\secret-place\agenttalk.exe", "x\ty", "gateway", "report"]))
        self.assertNotIn("secret-place", message)

    def test_the_gateway_and_account_must_each_be_in_the_snapshot_exactly_once(self):
        config = cfg.parse_config(minimal_text(RECEIPTS + report_table()))
        snapshot = {"gateways": [("g1", "7b1e3c52-0d4a-4a8e-9c61-5f2a8d3b9e01")], "accounts": [("meter", "c4a2e9f0-3b7d-4c1e-8a55-0d9b2f6e7a12")]}
        row = cfg.resolve_gateway_report(config, snapshot)
        self.assertEqual((row.gateway_id, row.account_id), ("7b1e3c52-0d4a-4a8e-9c61-5f2a8d3b9e01", "c4a2e9f0-3b7d-4c1e-8a55-0d9b2f6e7a12"))
        for label, edit in (("no gateway", {"gateways": []}), ("two gateways", {"gateways": snapshot["gateways"] * 2}),
                            ("no account", {"accounts": []}), ("a bad id", {"accounts": [("meter", "not-an-id")]})):
            with self.subTest(label), self.assertRaises(cfg.ConfigError) as caught:
                cfg.resolve_gateway_report(config, dict(snapshot, **edit))
            self.assertEqual(caught.exception.code, cfg.CONFIG_UNRESOLVED)
        self.assertIsNone(cfg.resolve_gateway_report(cfg.parse_config(minimal_text(RECEIPTS)), snapshot))


if __name__ == "__main__":
    unittest.main()
