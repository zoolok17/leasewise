"""Card L6a-1, step 5: `reporter status` shows gateway receipts (status version 3).

The report gains a top-level `gateway_receipts` object: whether it is configured, the last `seq`, the
rows by state, the failures by closed word, how often a read had to wait or was refused (by closed
reason), and the plain note that cached input is reported as 0. Closed words and counts only: never a
path, a command, a lease id, a reference, a figure from a receipt or exception text. Synthetic data only.
"""

import json
import os
import sqlite3
import unittest
from unittest import mock

from quota_balancer.reporter import admission_store, receipts, status

from quota_balancer.reporter import mapper

from tests.l6a1_env import (GATEWAY_ALIAS, GATEWAY_ID, GENERATION_B, OTHER_GATEWAY_ID, add_binding, configure, grant, ledger, lease,
                            make_store, receipt, write_json)
from tests.reporter_env import Rig
from tests.test_card7a3 import cli
from tests.test_card7l6a1_delivery import Net, error

NOTE = ("Cached input is reported as 0: the gateway records none, so 0 is not proof that none was used.")
OBJECT_KEYS = {"configured", "gateway", "last_seq", "last_read", "last_read_result", "stored", "rows", "failed",
               "quarantine", "quarantine_action", "admission_store", "journal_suppression", "generation_changes", "awaiting_receipt", "overdue", "read_later",
               "refused", "read_fault", "last_ok", "cached_input", "last_read_reason", "source_kind", "cleanup_incomplete"}


class _Status(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        self.leases = []

    def reporter(self, rows=None, net=None, count=1):
        self.leases = [lease() for _ in range(count)]
        if rows is None:
            rows = [receipt(one, n + 1) for n, one in enumerate(self.leases)]
        self.fixture = configure(self.rig, rows, bindings=self.leases)
        return self.rig.open(transport=net or Net())


class Report(_Status):

    def test_the_status_version_is_3_and_the_object_has_a_closed_shape(self):
        report = self.reporter().report()
        self.assertEqual(report["status_version"], 3)
        self.assertEqual(set(report["gateway_receipts"]), OBJECT_KEYS)
        self.assertEqual(set(report["gateway_receipts"]["read_later"]), set(receipts.LATER_REASONS))
        self.assertEqual(set(report["gateway_receipts"]["refused"]), set(receipts.REFUSED_REASONS))

    def test_without_the_block_it_says_not_configured_and_shows_nothing_more(self):
        rig = Rig(server=False)
        self.addCleanup(rig.cleanup)
        object_ = rig.open(transport=Net()).report()["gateway_receipts"]
        self.assertEqual((object_["configured"], object_["gateway"], object_["last_seq"], object_["cached_input"]),
                         (False, None, 0, None))
        self.assertEqual(object_["rows"], {"pending": 0, "sent": 0, "failed_permanent": 0, "quarantined": 0})
        self.assertNotIn("gateway receipts", status.render_text(rig.reporter.report()))

    def test_configured_before_any_read_it_says_so_with_the_note(self):
        object_ = self.reporter().report()["gateway_receipts"]
        self.assertEqual((object_["configured"], object_["gateway"], object_["last_seq"]), (True, GATEWAY_ALIAS, 0))
        self.assertEqual((object_["last_read"], object_["last_read_result"], object_["stored"]), (None, None, 0))
        self.assertEqual(object_["cached_input"], NOTE)
        self.assertEqual(status.CACHED_INPUT_NOTE, NOTE)

    def test_counts_after_a_read_and_a_delivery(self):
        reporter = self.reporter(count=3)
        reporter.cycle()
        object_ = reporter.report()["gateway_receipts"]
        self.assertEqual((object_["last_seq"], object_["stored"]), (3, 3))
        self.assertEqual(object_["rows"], {"pending": 0, "sent": 3, "failed_permanent": 0, "quarantined": 0})
        self.assertEqual((object_["last_read_result"], object_["failed"], object_["read_fault"]), ("ok", {}, 0))
        self.assertIsNotNone(object_["last_read"])
        self.assertIsNotNone(object_["last_ok"])
        self.assertEqual(reporter.report()["operator_actions"], [])

    def test_the_text_has_one_line_with_the_counts_and_the_note(self):
        reporter = self.reporter(count=2)
        reporter.cycle()
        lines = [line for line in status.render_text(reporter.report()).splitlines()
                 if line.startswith("gateway receipts")]
        self.assertEqual(lines, ["gateway receipts: last seq 2, pending 0, delivered 2, failed 0. " + NOTE])

    def test_a_failed_row_is_counted_by_its_closed_word_and_asks_for_attention(self):
        net = Net(receipt=[error("RECEIPT_CONFLICT", 409)])
        reporter = self.reporter(net=net)
        reporter.cycle()
        object_ = reporter.report()["gateway_receipts"]
        self.assertEqual(object_["rows"], {"pending": 0, "sent": 0, "failed_permanent": 1, "quarantined": 0})
        self.assertEqual(object_["failed"], {"RECEIPT_CONFLICT": 1})
        self.assertTrue(any("gateway receipt" in line for line in reporter.report()["operator_actions"]))

    def test_a_pending_row_is_counted_while_leasewise_is_down(self):
        reporter = self.reporter(net=Net(receipt=[(503, {})]))
        reporter.cycle()
        self.assertEqual(reporter.report()["gateway_receipts"]["rows"]["pending"], 1)

    def test_a_reference_with_no_binding_is_quarantined_with_its_word_and_asks_for_attention(self):
        reporter = self.reporter(rows=[receipt(lease(), 1)])
        reporter.cycle()
        object_ = reporter.report()["gateway_receipts"]
        self.assertEqual((object_["failed"], object_["last_seq"]), ({}, 1))
        self.assertEqual(object_["rows"]["quarantined"], 1)
        self.assertEqual(object_["quarantine"], {"unknown_reference": 1, "wrong_gateway": 0, "duplicate_reference": 0})
        self.assertTrue(any("quarantined" in line for line in reporter.report()["operator_actions"]))
        self.assertEqual(self.rig.net.calls, [])

    def test_a_zero_call_receipt_with_unknown_counts_refuses_the_whole_page_and_the_cursor_stays(self):
        """F4: with no call, null is not zero. Nothing is captured, nothing moves, and the word is counted."""
        reporter = self.reporter(rows=[receipt(lease(), 1), receipt(lease(), 2, calls=0, input_tokens=None,
                                                                    output_tokens=None, actual_micro_eur=0)])
        reporter.cycle()
        object_ = reporter.report()["gateway_receipts"]
        self.assertEqual((object_["last_seq"], object_["stored"], object_["refused"]["bad_row"]), (0, 0, 1))
        self.assertEqual(self.rig.state.one("SELECT COUNT(*) FROM receipt_capture")[0], 0)
        self.assertEqual(self.rig.state.current_cursor(1), (None, 0))

    def test_a_source_that_is_not_there_is_counted_as_read_later_and_a_bad_one_as_refused(self):
        reporter = self.reporter()
        os.remove(self.fixture)
        reporter.cycle()
        reporter.cycle()
        object_ = reporter.report()["gateway_receipts"]
        self.assertEqual((object_["read_later"]["source_missing"], object_["last_read_result"]), (2, "read_later"))
        self.assertEqual(reporter.report()["operator_actions"], [])         # a wait is not a fault
        write_json(self.fixture, ledger([receipt(lease(), 1, outcome="nope")]))
        reporter.cycle()
        object_ = reporter.report()["gateway_receipts"]
        self.assertEqual((object_["refused"]["bad_row"], object_["last_read_result"]), (1, "refused"))
        self.assertEqual((object_["last_seq"], object_["rows"]["pending"], object_["stored"]), (0, 0, 0))
        self.assertTrue(any("refused" in line for line in reporter.report()["operator_actions"]))
        # fixed: the next good read clears the attention
        write_json(self.fixture, ledger([receipt(self.leases[0], 1)]))
        reporter.cycle()
        self.assertEqual(reporter.report()["gateway_receipts"]["last_read_result"], "ok")
        self.assertEqual(reporter.report()["operator_actions"], [])

    def test_a_fault_in_reading_is_a_count_and_never_stops_sending(self):
        reporter = self.reporter(count=1)
        with mock.patch.object(receipts, "read_page", side_effect=RuntimeError("LOUD-FAULT-TEXT")):
            reporter.cycle()
        report = reporter.report()
        self.assertEqual(report["gateway_receipts"]["read_fault"], 1)
        self.assertTrue(any("reading gateway receipts" in line for line in report["operator_actions"]))
        self.assertNotIn("LOUD-FAULT-TEXT", json.dumps(report) + status.render_text(report))
        reporter.cycle()                                                      # and the next cycle reads and sends
        self.assertEqual(reporter.report()["gateway_receipts"]["rows"]["sent"], 1)

    def test_the_text_of_an_older_snapshot_without_the_object_still_renders(self):
        """A reporter of card 7c-2 (status version 2) that still owns the state leaves a snapshot with no
        `gateway_receipts`; the new text view shows it and says nothing about receipts."""
        report = self.reporter().report()
        del report["gateway_receipts"]
        report["status_version"] = 2
        text = status.render_text(report)
        self.assertNotIn("gateway receipts", text)
        self.assertNotIn("Cached input", text)

    def test_the_counts_survive_a_restart(self):
        reporter = self.reporter(count=2)
        reporter.cycle()
        self.rig.close()
        object_ = self.rig.open(transport=Net()).report()["gateway_receipts"]
        self.assertEqual((object_["last_seq"], object_["rows"]["sent"], object_["stored"]), (2, 2, 2))


class Custody(_Status):
    """Step 2 (DESIGN-6A 8.3, 8.5, 8.8 item 2): what is waiting, set aside or overdue. Counts and fixed words only."""

    def waiting(self, bound, **over):
        """A reporter whose admission store binds `bound` leases (with the expiry in `over`) and whose ledger is empty."""
        self.leases = [lease() for _ in range(bound)]
        rows = [dict(lease_id=one, **over) for one in self.leases]
        self.fixture = configure(self.rig, [], bindings=rows)
        return self.rig.open(transport=Net())

    def test_a_quarantine_says_what_to_do_in_one_fixed_sentence_per_reason(self):
        object_ = self.reporter().report()["gateway_receipts"]
        self.assertEqual(set(object_["quarantine_action"]), set(object_["quarantine"]))
        for sentence in object_["quarantine_action"].values():
            self.assertIsInstance(sentence, str)
            self.assertNotIn(GATEWAY_ID, sentence)
        self.assertEqual(object_["quarantine_action"], status.QUARANTINE_ACTIONS)

    def test_bindings_with_no_receipt_are_awaiting_with_the_age_of_the_oldest(self):
        reporter = self.waiting(2, expires_ms=self.rig.clock.ms + 3_600_000)
        reporter.cycle()
        self.rig.clock.advance(90)
        object_ = reporter.report()["gateway_receipts"]
        self.assertEqual(object_["awaiting_receipt"], {"count": 2, "oldest_age_seconds": 90})
        self.assertEqual(object_["overdue"], 0)
        self.assertEqual(reporter.report()["operator_actions"], [])          # waiting is not yet a fault

    def test_an_awaiting_binding_past_its_own_expiry_is_overdue_and_asks_for_attention(self):
        reporter = self.waiting(2, expires_ms=self.rig.clock.ms + 60_000)
        reporter.cycle()
        self.rig.clock.advance(61)
        object_ = reporter.report()["gateway_receipts"]
        self.assertEqual((object_["awaiting_receipt"]["count"], object_["overdue"]), (2, 2))
        self.assertTrue(any("overdue" in line for line in reporter.report()["operator_actions"]))
        self.assertNotIn(self.leases[0], json.dumps(reporter.report()))

    def test_a_receipt_ends_the_wait_even_when_it_is_still_quarantined_or_failed(self):
        one, two = lease(), lease()
        soon = self.rig.clock.ms + 1000
        self.fixture = configure(self.rig, [receipt(one, 1), receipt(two, 2)],
                                 bindings=[{"lease_id": one, "expires_ms": soon}, {"lease_id": two, "expires_ms": soon}])
        reporter = self.rig.open(transport=Net(receipt=[error("RECEIPT_CONFLICT", 409)]))
        reporter.cycle()
        self.rig.clock.advance(5)
        object_ = reporter.report()["gateway_receipts"]
        self.assertEqual((object_["awaiting_receipt"], object_["overdue"]), ({"count": 0, "oldest_age_seconds": None}, 0))

    def test_a_binding_for_another_gateway_is_not_this_sources_to_wait_for(self):
        self.fixture = configure(self.rig, [], bindings=[{"lease_id": lease(), "gateway_id": OTHER_GATEWAY_ID}])
        reporter = self.rig.open(transport=Net())
        reporter.cycle()
        self.assertEqual(reporter.report()["gateway_receipts"]["awaiting_receipt"]["count"], 0)

    def test_without_the_block_nothing_is_awaiting(self):
        rig = Rig(server=False)
        self.addCleanup(rig.cleanup)
        object_ = rig.open(transport=Net()).report()["gateway_receipts"]
        self.assertEqual((object_["awaiting_receipt"], object_["overdue"], object_["generation_changes"]),
                         ({"count": 0, "oldest_age_seconds": None}, 0, []))

    def test_a_new_ledger_generation_is_counted_with_when_it_was_first_seen(self):
        one, two = lease(), lease()
        self.fixture = configure(self.rig, [receipt(one, 1)], bindings=[one, two])
        reporter = self.rig.open(transport=Net())
        reporter.cycle()
        self.assertEqual(reporter.report()["gateway_receipts"]["generation_changes"],
                         [{"gateway": GATEWAY_ALIAS, "count": 0, "latest_first_seen": None}])
        self.rig.clock.advance(120)
        write_json(self.fixture, ledger([receipt(two, 1)], generation=GENERATION_B))
        reporter.cycle()
        changes = reporter.report()["gateway_receipts"]["generation_changes"]
        self.assertEqual([(c["gateway"], c["count"]) for c in changes], [(GATEWAY_ALIAS, 1)])
        self.assertEqual(changes[0]["latest_first_seen"], status.iso(self.rig.clock.ms))

    def test_a_changed_gateway_mapping_is_a_new_source_and_both_show(self):
        reporter = self.reporter()
        reporter.cycle()
        self.rig.state.ensure_source(GATEWAY_ALIAS, OTHER_GATEWAY_ID, self.rig.clock.ms)
        changes = reporter.report()["gateway_receipts"]["generation_changes"]
        self.assertEqual([c["gateway"] for c in changes], [GATEWAY_ALIAS, GATEWAY_ALIAS])

    def test_none_of_it_names_a_lease_a_reference_a_gateway_id_or_a_path(self):
        reporter = self.waiting(1, expires_ms=self.rig.clock.ms - 1)
        reporter.cycle()
        shown = json.dumps(reporter.report(), sort_keys=True) + status.render_text(reporter.report())
        for private in (self.leases[0], mapper.lease_ref_hash(self.leases[0]), GATEWAY_ID, self.rig.ws.dir):
            self.assertNotIn(private, shown)

    def test_a_missing_or_busy_admission_store_keeps_the_receipts_and_is_read_again_later(self):
        """Requirement 7: an unreadable store is never proof of anything. Receipts are still captured (quarantined as
        unknown), nothing is sent, and the first read that works releases them."""
        one = lease()
        store = os.path.join(self.rig.ws.dir, "admission.db")
        self.fixture = configure(self.rig, [receipt(one, 1)], store=store, bindings=[one])
        os.rename(store, store + ".away")                                      # missing
        reporter = self.rig.open(transport=Net())
        reporter.cycle()
        object_ = reporter.report()["gateway_receipts"]
        self.assertEqual((object_["rows"]["quarantined"], object_["rows"]["sent"], object_["last_seq"]), (1, 0, 1))
        self.assertEqual(self.rig.net.calls, [])
        self.assertEqual(self.rig.state.counter("_gateway_receipts", "store:store_missing"), 1)
        os.rename(store + ".away", store)
        writer = sqlite3.connect(store, isolation_level=None)                  # busy: a writer holds the file
        self.addCleanup(writer.close)
        writer.execute("BEGIN EXCLUSIVE")
        reporter.cycle()
        self.assertEqual(self.rig.state.counter("_gateway_receipts", "store:store_busy"), 1)
        self.assertEqual(reporter.report()["gateway_receipts"]["rows"]["quarantined"], 1)
        writer.execute("ROLLBACK")
        reporter.cycle()                                                       # readable again: released and sent
        self.assertEqual(reporter.report()["gateway_receipts"]["rows"], {"pending": 0, "sent": 1, "failed_permanent": 0,
                                                                         "quarantined": 0})

    def test_a_snapshot_from_the_first_build_without_the_new_words_still_renders(self):
        report = self.reporter().report()
        for key in ("quarantine_action", "generation_changes", "awaiting_receipt", "overdue"):
            del report["gateway_receipts"][key]
        self.assertIn("gateway receipts: last seq 0", status.render_text(report))


class StoreTrouble(_Status):
    """F6 and F7: trouble with the admission store is visible, as a closed current state and counts of past failures,
    in the JSON and in the text. No path, no exception text. The state goes back to `ok` after a good read."""

    def setUp(self):
        super().setUp()
        self.one = lease()
        self.store = os.path.join(self.rig.ws.dir, "admission.db")
        self.fixture = configure(self.rig, [receipt(self.one, 1)], store=self.store, bindings=[self.one])
        self.reporter = self.rig.open(transport=Net())

    def shown(self):
        report = self.reporter.report()
        text = status.render_text(report)
        return report["gateway_receipts"]["admission_store"], text, report

    def test_before_the_first_read_there_is_no_state_and_after_a_good_read_it_is_ok(self):
        state, text, _report = self.shown()
        self.assertEqual(state["state"], None)
        self.assertIn("admission store: not read yet", text)
        self.reporter.cycle()
        state, text, report = self.shown()
        self.assertEqual(state, {"state": "ok", "failures": {"missing": 0, "busy": 0, "unreadable": 0},
                                 "at_body_cap": 0})
        self.assertIn("admission store: ok", text)
        self.assertEqual(report["operator_actions"], [])

    def test_a_missing_store_is_shown_then_recovers(self):
        os.rename(self.store, self.store + ".away")
        self.reporter.cycle()
        state, text, report = self.shown()
        self.assertEqual((state["state"], state["failures"]["missing"]), ("missing", 1))
        self.assertIn("admission store: missing", text)
        self.assertTrue(any("not there" in line for line in report["operator_actions"]))
        os.rename(self.store + ".away", self.store)
        self.reporter.cycle()
        state, text, report = self.shown()
        self.assertEqual((state["state"], state["failures"]["missing"]), ("ok", 1))        # back to ok; the past stays
        self.assertIn("admission store: ok", text)
        self.assertEqual(report["operator_actions"], [])

    def test_a_busy_store_is_shown_as_a_wait_not_a_fault_then_recovers(self):
        writer = sqlite3.connect(self.store, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN EXCLUSIVE")
        self.reporter.cycle()
        state, text, report = self.shown()
        self.assertEqual((state["state"], state["failures"]["busy"]), ("busy", 1))
        self.assertIn("admission store: busy", text)
        self.assertFalse(set(status.STORE_ACTIONS.values()) & set(report["operator_actions"]))      # a wait asks for nothing
        writer.execute("ROLLBACK")
        self.reporter.cycle()
        self.assertEqual(self.shown()[0]["state"], "ok")

    def test_a_malformed_store_is_shown_then_recovers(self):
        with open(self.store, "wb") as handle:
            handle.write(b"this is not a database")
        self.reporter.cycle()
        state, text, report = self.shown()
        self.assertEqual((state["state"], state["failures"]["unreadable"]), ("unreadable", 1))
        self.assertIn("admission store: unreadable", text)
        self.assertTrue(any("cannot be used" in line for line in report["operator_actions"]))
        os.remove(self.store)
        make_store(self.store, [self.one])
        self.reporter.cycle()
        self.assertEqual(self.shown()[0]["state"], "ok")

    def test_a_row_that_does_not_belong_to_its_lease_makes_the_store_unreadable_and_nothing_is_ever_sent(self):
        """F3 through the whole reporter: one lease's reference and hash on another lease's row."""
        other = lease()
        os.remove(self.store)
        make_store(self.store, [])
        conn = sqlite3.connect(self.store, isolation_level=None)
        grant(conn, {"lease_id": other, "reference": self.one, "ref_sha256": mapper.lease_ref_hash(self.one)}, 1)
        conn.close()
        self.reporter.cycle()
        self.reporter.cycle()
        self.assertEqual(self.shown()[0]["state"], "unreadable")
        self.assertEqual(self.rig.net.calls, [])                                          # no settle, to anyone
        self.assertEqual(self.rig.state.one("SELECT COUNT(*) FROM receipt_binding")[0], 0)
        self.assertEqual(self.rig.state.one("SELECT COUNT(*) FROM receipt_delivery")[0], 0)

    def test_no_history_size_stops_the_reporter_grants_are_copied_in_batches_across_cycles_and_a_restart(self):
        """D-127 F7: the receipt reader copies new grants a few at a time; there is no lifetime limit."""
        extra = [lease() for _ in range(10)]
        for number, one in enumerate(extra):
            add_binding(self.store, {"lease_id": one, "message_id": "bulk-%d" % number})
        from quota_balancer.reporter import run as run_mod
        with mock.patch.object(admission_store, "BATCH", 3), mock.patch.object(run_mod, "MAX_GRANT_BATCHES", 1):
            seen = []
            for _ in range(5):
                self.reporter.cycle()
                seen.append(self.rig.state.one("SELECT COUNT(*) FROM receipt_binding")[0])
                self.rig.close()                                                           # a restart between cycles
                self.reporter = self.rig.open(transport=Net())
        self.assertEqual(seen, [3, 6, 9, 11, 11])                                           # 3 a cycle, then the last 2
        self.assertEqual(self.shown()[0]["state"], "ok")
        self.assertEqual(self.rig.state.grant_cursor(), 11)
        self.assertEqual(self.rig.state.one("SELECT COUNT(DISTINCT ref_sha256) FROM receipt_binding")[0], 11)

    def test_nothing_in_the_state_names_a_path_or_a_fault(self):
        os.rename(self.store, self.store + ".away")
        self.reporter.cycle()
        _state, text, report = self.shown()
        shown = json.dumps(report, sort_keys=True) + text
        for private in (self.rig.ws.dir, "admission.db", "sqlite", "Traceback"):
            self.assertNotIn(private, shown)
        self.assertEqual(set(report["gateway_receipts"]["admission_store"]), {"state", "failures", "at_body_cap"})

    def test_the_command_shows_it_too(self):
        os.rename(self.store, self.store + ".away")
        self.reporter.cycle()
        self.rig.close()
        code, out, err = cli(["status", "--config", self.rig.ws.config, "--json"], permissions=lambda p: None)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["gateway_receipts"]["admission_store"]["state"], "missing")
        code, text, _err = cli(["status", "--config", self.rig.ws.config], permissions=lambda p: None)
        self.assertIn("admission store: missing", text)

    def test_a_receipt_conflict_has_its_own_action_that_names_the_parked_account(self):
        self.rig.close()
        net = Net(receipt=[error("RECEIPT_CONFLICT", 409)])
        reporter = self.rig.open(transport=net)
        reporter.cycle()
        report = reporter.report()
        lines = [line for line in report["operator_actions"] if "conflict" in line]
        self.assertTrue(lines)
        self.assertIn("parked account", lines[0])


class Command(_Status):
    """`reporter status` (not running): built from the state file and the configuration."""

    def test_the_command_reports_configured_and_the_note(self):
        reporter = self.reporter(count=1)
        reporter.cycle()
        self.rig.close()
        code, out, err = cli(["status", "--config", self.rig.ws.config, "--json"], permissions=lambda p: None)
        self.assertEqual((code, err), (0, ""))
        object_ = json.loads(out)["gateway_receipts"]
        self.assertEqual((object_["configured"], object_["last_seq"], object_["cached_input"]), (True, 1, NOTE))
        code, text, _err = cli(["status", "--config", self.rig.ws.config], permissions=lambda p: None)
        self.assertIn("gateway receipts: last seq 1, pending 0, delivered 1, failed 0. " + NOTE, text)

    def test_the_command_without_the_block_says_not_configured(self):
        path = self.rig.ws.write_config("http://127.0.0.1:9", self.rig.agents_cfg)
        code, out, err = cli(["status", "--config", path, "--json"], permissions=lambda p: None)
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["gateway_receipts"]["configured"])
        code, text, _err = cli(["status", "--config", path], permissions=lambda p: None)
        self.assertNotIn("gateway receipts", text)

    def test_a_running_reporters_snapshot_carries_the_object(self):
        reporter = self.reporter(count=1)
        reporter.cycle()
        snap = status.read_snapshot(status.snapshot_path(self.rig.state.path))
        self.assertEqual(snap["status_version"], 3)
        self.assertEqual(snap["gateway_receipts"]["rows"]["sent"], 1)
        self.assertEqual(snap["gateway_receipts"]["cached_input"], NOTE)


if __name__ == "__main__":
    unittest.main()
