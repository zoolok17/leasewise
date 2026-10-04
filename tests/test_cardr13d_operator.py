"""Card R1-3d: the operator's side of the gateway evidence (DESIGN-R1-3.md 2.1 to 2.4, 8.1 to 8.3 and 9).

* **`gateway-approve`** records a pair of policy fingerprints, audited; a null or malformed fingerprint and an unknown gateway are the
  existing INVALID_CONFIG sentence and write nothing.
* **`gateway-adopt`** re-reads all ten items of the predicate inside `BEGIN IMMEDIATE`, then runs the steps of 2.3 all or nothing: the
  adoption row, the reader chosen and the others retired, the named items resolved (a retired reader's as retired), the chain state and
  `gateways.state`, one audit row with no amount. A refusal is ADOPTION_REFUSED with a fixed sentence; the reason is only in `status`.
* **`reconcile`** refuses with EVIDENCE_REQUIRED and changes nothing (the whole database is compared before and after).
* **`status`** gets an `evidence` block per gateway (absent without a version-4 report) and **the builder** turns the usable chain into the
  evaluator's input: from the adopted chain only, never from the legacy `gateways` columns, and the real path accepts a version-4 reading.

Synthetic reports in a fixed past month, so the clock of the test is exact. No seat, lease or admission decision reads any of this.
"""

import contextlib
import copy
import datetime
import json
import os
import sqlite3
import unittest
from unittest import mock

from quota_balancer import billing_eval, errors, evidence_view, store
from quota_balancer.errors import QuotaError

from tests.test_card2b1 import VALID_DOC, _config_import, run_cli
from tests.test_cardr13b_ingest import CAP, GEN, GEN2, INC, INC2, PRICE, _Store, report
from tests.test_card2b2 import _POLICY

INC3 = "f" * 32
PRICE2 = "e" * 64
CAP2 = "d" * 64
SEP = int(datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000)
OCT = int(datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000)
OBS = SEP + 15 * 86_400_000                 # the time of every report: noon-ish in the middle of September 2026
NOW = OBS + 5_000                           # "now" of a test, five seconds later: fresh, same month

ALL_ITEMS = ("not_bound", "invalid", "not_current_period", "not_fresh", "not_approved", "identity_conflict", "reader_retired",
             "not_newest", "contested", "gateway_not_ready", "open_transitions_unnamed", "not_quiet")


def rep(machine="desktop", obs=OBS, **over):
    """A version-4 report in September 2026 observed at OBS (unless said otherwise)."""
    body = {"period_start": SEP, "period_end": OCT}
    body.update(over)
    item = report(machine=machine, **body)
    item["observed_at"] = obs
    return item


class _Op(_Store):
    """A store with one gateway `g1` (owner: machine `desktop`, account `meter`) and helpers for the operator's commands."""

    def dump(self):
        """The whole database as text: 'nothing changed' is compared, not assumed."""
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            return "\n".join(conn.iterdump())

    def approve(self, price=PRICE, cap=CAP, gateway="g1"):
        store.gateway_approve(self.db, gateway, price, cap, now_ms=NOW)

    def send(self, item, name=None):
        self.ingest(item, name=name or "r%s.json" % len(os.listdir(self.tmp)))
        return item

    def adopt(self, item, resolve=(), now=NOW, gateway="g1"):
        return store.gateway_adopt(self.db, gateway, item["event_id"], list(resolve), now_ms=now)

    def refuse(self, item, resolve=(), now=NOW, code=errors.CODE_ADOPTION_REFUSED):
        """The adoption is refused with `code`, and the whole database is exactly as it was."""
        before = self.dump()
        with self.assertRaises(QuotaError) as caught:
            self.adopt(item, resolve, now)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.message, errors.MESSAGES[code])
        self.assertEqual(self.dump(), before)

    def why(self, item, named=(), now=NOW):
        """The items of the predicate that fail for this report, as `status` would show them."""
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            return evidence_view.adopt_blockers(conn, self.gateway_id(), item["event_id"], set(named), now)

    def evidence(self, now=NOW, alias="g1"):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            return evidence_view.project(conn, self.gateway_id(alias), now)

    def open_ids(self):
        return [row[0] for row in self.rows("SELECT id FROM gateway_transitions WHERE resolved_by_adoption IS NULL ORDER BY id")]

    def first(self, **over):
        """Approve the pair, ingest a first quiet report and adopt it."""
        self.approve()
        item = self.send(rep(seq=1, **over))
        self.adopt(item)
        return item


# ---------------------------------------------------------------------------------------------------------------------------------
# gateway-approve
# ---------------------------------------------------------------------------------------------------------------------------------

class Approve(_Op):

    def test_the_pair_is_recorded_and_audited_once(self):
        self.approve()
        self.assertEqual(self.rows("SELECT price_hash, child_cap_hash, approved_at, operator FROM gateway_approvals"),
                         [(PRICE, CAP, NOW, "operator")])
        self.assertEqual(self.rows("SELECT actor_alias, action, object_id FROM audit WHERE action='gateway_approved'"),
                         [("operator", "gateway_approved", self.gateway_id())])
        before = self.dump()
        self.approve()                                            # approving the same pair again changes nothing, not even the audit
        self.assertEqual(self.dump(), before)

    def test_a_null_or_malformed_fingerprint_is_refused_with_the_existing_sentence_and_writes_nothing(self):
        before = self.dump()
        for price, cap in ((PRICE, None), (None, CAP), (PRICE, "null"), (PRICE, CAP[:-1]), (PRICE, CAP[:-1] + "A"), (PRICE, "g" * 64),
                           ("", CAP), (PRICE, CAP + "0"), (5, CAP), (PRICE, " " + CAP[1:])):
            with self.subTest(price=price, cap=cap):
                with self.assertRaises(QuotaError) as caught:
                    store.gateway_approve(self.db, "g1", price, cap)
                self.assertEqual((caught.exception.code, caught.exception.message),
                                 (errors.CODE_INVALID_CONFIG, errors.MSG_INVALID_CONFIG))
        self.assertEqual(self.dump(), before)

    def test_an_unknown_gateway_is_the_same_sentence_and_writes_nothing(self):
        before = self.dump()
        with self.assertRaises(QuotaError) as caught:
            store.gateway_approve(self.db, "nope", PRICE, CAP)
        self.assertEqual((caught.exception.code, caught.exception.message), (errors.CODE_INVALID_CONFIG, errors.MSG_INVALID_CONFIG))
        self.assertEqual(self.dump(), before)

    def test_a_changed_pair_is_a_new_unapproved_pair(self):
        self.approve()
        self.send(rep(seq=1, policy_hashes={"price": PRICE2, "child_cap": CAP}))
        self.assertFalse(self.evidence()["approved"])
        self.send(rep(seq=2, policy_hashes={"price": PRICE, "child_cap": CAP}))
        self.assertTrue(self.evidence()["approved"])
        self.approve(price=PRICE2)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM gateway_approvals"), [(2,)])

    def test_the_command_prints_one_line_and_a_bad_hash_exits_2_with_the_fixed_sentence(self):
        done = run_cli("--db", self.db, "gateway-approve", "--gateway", "g1", "--price-hash", PRICE, "--child-cap-hash", CAP)
        self.assertEqual((done.returncode, done.stdout.strip(), done.stderr), (0, '{"approved":true}', ""))
        bad = run_cli("--db", self.db, "gateway-approve", "--gateway", "g1", "--price-hash", PRICE, "--child-cap-hash", "null")
        self.assertEqual((bad.returncode, bad.stdout), (2, ""))
        self.assertEqual(json.loads(bad.stderr), {"error": {"code": "INVALID_CONFIG", "message": errors.MSG_INVALID_CONFIG}})
        self.assertNotIn("null", bad.stderr)

    def test_a_move_to_another_machine_deletes_the_approval_and_the_route_back_works(self):
        """The executable route of 13.4: approve the pair, adopt a quiet report of the NEW owner's reader, name `owner_changed`."""
        first = self.first()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM gateway_approvals"), [(1,)])
        moved = copy.deepcopy(VALID_DOC)
        moved["gateways"][0]["machine"] = "worker"
        self.assertEqual(_config_import(self.db, self.tmp, moved).returncode, 0)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM gateway_approvals"), [(0,)])
        self.assertEqual(self.evidence()["adopt_blockers"], ["not_bound"])          # the old owner's last report is not this owner's
        new = self.send(rep(machine="worker", seq=1, reader_incarnation=INC2, obs=OBS + 1000))
        self.assertIn("open_transitions_unnamed", self.why(new))
        self.refuse(new, self.open_ids())                                           # not approved yet
        self.approve()
        self.adopt(new, self.open_ids())
        self.assertEqual(self.open_ids(), [])
        self.assertEqual(self.rows("SELECT reader_incarnation, state FROM gateway_readers ORDER BY reader_incarnation"),
                         [(INC, "retired"), (INC2, "current")])
        self.assertEqual(first["event_id"] in [row[0] for row in self.rows("SELECT event_id FROM gateway_adoptions")], True)


# ---------------------------------------------------------------------------------------------------------------------------------
# gateway-adopt: the predicate, one item at a time
# ---------------------------------------------------------------------------------------------------------------------------------

class Predicate(_Op):

    def test_an_adoptable_report_has_no_blocker(self):
        self.approve()
        item = self.send(rep(seq=1))
        self.assertEqual(self.why(item), [])

    def test_1_bound_a_report_that_is_not_stored_for_this_gateway_is_refused(self):
        self.approve()
        self.send(rep(seq=1))
        unknown = {"event_id": "0" * 8 + "-0000-0000-0000-" + "0" * 12}
        self.assertEqual(self.why(unknown), ["not_bound"])
        self.refuse(unknown)
        other = self.send(rep(seq=1, gateway="g2", machine="worker", generation=GEN2))                  # a report of g2, named on g1
        self.assertEqual(self.why(other), ["not_bound"])
        self.refuse(other)
        self.refuse({"event_id": "not an id at all"})                                  # nothing is echoed or tried as an id

    def test_1_bound_a_report_whose_sender_is_no_longer_the_owner_is_refused(self):
        self.approve()
        item = self.send(rep(seq=1))
        moved = copy.deepcopy(VALID_DOC)
        moved["gateways"][0]["machine"] = "worker"
        self.assertEqual(_config_import(self.db, self.tmp, moved).returncode, 0)
        self.approve()
        self.assertEqual(self.why(item), ["not_bound"])
        self.assertEqual(self.evidence()["adopt_blockers"], ["not_bound"])
        self.refuse(item, self.open_ids())

    def test_2_valid_every_stored_figure_is_checked_again(self):
        good = {"settled_micro_eur": 5000, "reserved_micro_eur": 1, "uncertain_micro_eur": 2, "unattributed_micro_eur": 7,
                "price_hash": PRICE, "child_cap_hash": CAP, "generation": GEN, "reader_incarnation": INC}
        self.assertTrue(evidence_view._valid(good))
        for key, value in (("settled_micro_eur", -1), ("settled_micro_eur", 2 ** 63), ("reserved_micro_eur", 1.5), ("uncertain_micro_eur", True),
                           ("unattributed_micro_eur", 5001), ("unattributed_micro_eur", None), ("price_hash", PRICE.upper()),
                           ("child_cap_hash", CAP[:-1]), ("child_cap_hash", None), ("generation", GEN + "0"), ("generation", "G" * 32),
                           ("reader_incarnation", INC[:-1]), ("reader_incarnation", None)):
            with self.subTest(key=key, value=value):
                bad = dict(good)
                bad[key] = value
                self.assertFalse(evidence_view._valid(bad))

    def test_3_current_period_an_earlier_month_is_refused(self):
        self.approve()
        august = int(datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000)
        item = self.send(rep(seq=1, period_start=august, period_end=SEP))
        self.assertEqual(self.why(item), ["not_current_period"])
        self.refuse(item)
        self.assertEqual(self.why(item, now=august + 5000, ), ["not_fresh"])           # judged in its own month it is only too old

    def test_4_fresh_the_boundary_is_120_s_and_the_future_is_refused(self):
        self.approve()
        item = self.send(rep(seq=1))
        self.assertEqual(self.why(item, now=OBS + 120_000), [])
        self.assertEqual(self.why(item, now=OBS + 120_001), ["not_fresh"])
        self.assertEqual(self.why(item, now=OBS - 1), ["not_fresh"])
        self.refuse(item, now=OBS + 120_001)
        self.adopt(item, now=OBS + 120_000)

    def test_5_approved_the_pair_must_be_approved(self):
        item = self.send(rep(seq=1))
        self.assertEqual(self.why(item), ["not_approved"])
        self.refuse(item)
        self.approve(price=PRICE2)                                           # another pair is not this pair
        self.refuse(item)

    def test_6_identity_a_generation_that_disagrees_with_the_recorded_one_is_refused(self):
        self.approve()
        self.send(rep(seq=1))
        clash = self.send(rep(seq=2, generation=GEN2))                       # same reader and epoch, another generation
        self.assertEqual(self.why(clash, self.open_ids()), ["identity_conflict", "contested"])
        self.refuse(clash, self.open_ids())
        self.assertEqual([r[0] for r in self.rows("SELECT kind FROM gateway_transitions")], ["epoch_generation_mismatch"])

    def test_6_a_retired_reader_is_refused(self):
        self.first()
        other = self.send(rep(seq=1, reader_incarnation=INC2))
        self.adopt(other, self.open_ids())                                   # INC2 current, INC retired
        later = self.send(rep(seq=2, reader_incarnation=INC))
        self.assertEqual(self.why(later), ["reader_retired"])
        self.refuse(later)

    def test_7_newest_only_the_highest_number_of_its_reader_and_epoch(self):
        self.approve()
        old = self.send(rep(seq=1))
        self.send(rep(seq=2))
        self.assertEqual(self.why(old), ["not_newest"])
        self.refuse(old)

    def test_7_contested_a_report_at_or_below_an_open_collision_is_refused_until_a_later_one(self):
        self.approve()
        item = self.send(rep(seq=1))
        rival = rep(seq=1, settled_micro_eur=5001, event_id=item["event_id"])      # the same id, other content: a collision
        with self.assertRaises(QuotaError) as caught:
            self.ingest(rival, name="rival.json")
        self.assertEqual(caught.exception.code, errors.CODE_IDEMPOTENCY_CONFLICT)
        self.assertEqual(self.why(item, self.open_ids()), ["contested"])
        self.refuse(item, self.open_ids())
        later = self.send(rep(seq=2))
        self.assertEqual(self.why(later, self.open_ids()), [])               # "neither is trusted" until an uncontested report
        self.adopt(later, self.open_ids())

    def test_8_the_gateway_says_it_is_ready(self):
        self.approve()
        item = self.send(rep(seq=1, reconciliation_required=True))
        self.assertEqual(self.why(item), ["gateway_not_ready"])
        self.refuse(item)

    def test_9_every_open_item_must_be_named_and_one_left_out_is_refused(self):
        first = self.first()
        self.send(rep(seq=2, settled_micro_eur=4000))                         # settled falls: figure_fell
        self.send(rep(seq=3, reader_incarnation=INC2))                        # a new reader: reader_changed
        self.send(rep(seq=4, settled_micro_eur=3000))                         # falls again: another figure_fell (an event kind)
        ids = self.open_ids()
        self.assertGreaterEqual(len(ids), 3)
        newest = self.send(rep(seq=5, settled_micro_eur=6000))
        self.assertEqual(self.why(newest), ["open_transitions_unnamed"])
        self.refuse(newest)                                                   # nothing named
        self.refuse(newest, ids[:-1])                                         # one left out
        self.refuse(newest, ids + [max(ids) + 100])                           # an id that is not open is a naming mistake too
        self.assertEqual(self.open_ids(), ids)
        self.adopt(newest, ids)
        self.assertEqual(self.open_ids(), [])
        self.assertIsNotNone(first)

    def test_10_quiet_a_busy_report_is_refused(self):
        self.approve()
        item = self.send(rep(seq=1, quiet=False))
        self.assertEqual(self.why(item), ["not_quiet"])
        self.refuse(item)

    def test_status_names_every_failed_item_of_the_newest_report_in_the_designs_order(self):
        item = self.send(rep(seq=1, quiet=False, reconciliation_required=True))
        got = self.evidence()["adopt_blockers"]
        self.assertEqual(got, ["not_approved", "gateway_not_ready", "not_quiet"])
        self.assertTrue(set(got) <= set(ALL_ITEMS))
        self.assertEqual(self.why(item), got)

    def test_the_malformed_naming_of_an_id_is_the_existing_sentence(self):
        self.approve()
        item = self.send(rep(seq=1))
        for bad in ("x", "1.5", "-1", "", "١", None, True):
            with self.subTest(bad=bad):
                self.refuse(item, [bad], code=errors.CODE_INVALID_CONFIG)

    def test_an_unknown_gateway_is_invalid_config(self):
        item = self.send(rep(seq=1))
        before = self.dump()
        with self.assertRaises(QuotaError) as caught:
            store.gateway_adopt(self.db, "nope", item["event_id"], [], now_ms=NOW)
        self.assertEqual(caught.exception.code, errors.CODE_INVALID_CONFIG)
        self.assertEqual(self.dump(), before)


# ---------------------------------------------------------------------------------------------------------------------------------
# gateway-adopt: the transaction
# ---------------------------------------------------------------------------------------------------------------------------------

class Adoption(_Op):

    def test_the_adoption_writes_the_baseline_the_reader_the_chain_and_one_audit_row_without_amounts(self):
        self.approve()
        item = self.send(rep(seq=1))
        adoption = self.adopt(item)
        gateway = self.gateway_id()
        self.assertEqual(self.rows("SELECT id, gateway_id, event_id, reader_incarnation, ledger_epoch, generation, period_start, seq, "
                                   "settled_micro_eur, reserved_micro_eur, uncertain_micro_eur, unattributed_micro_eur, price_hash, "
                                   "child_cap_hash, adopted_at, operator, kind, prev_adoption_id FROM gateway_adoptions"),
                         [(adoption, gateway, item["event_id"], INC, 1, GEN, SEP, 1, 5000, 200, 30, 7, PRICE, CAP, NOW, "operator",
                           "operator", None)])
        self.assertEqual(self.rows("SELECT reader_incarnation, state, decided_by_adoption FROM gateway_readers"), [(INC, "current", adoption)])
        self.assertEqual(self.rows("SELECT current_adoption_id, last_quiet_event_id, last_quiet_unattributed FROM gateway_chain_state"),
                         [(adoption, item["event_id"], 7)])
        self.assertEqual(self.rows("SELECT state FROM gateways WHERE alias='g1'"), [("ok",)])
        (audit,) = self.rows("SELECT actor_alias, action, object_id, at, redacted_details FROM audit WHERE action='gateway_adopted'")
        self.assertEqual(audit[:4], ("operator", "gateway_adopted", item["event_id"], NOW))
        details = json.loads(audit[4])
        self.assertEqual(details, {"adoption_id": adoption, "chosen": INC, "retired": [], "resolved": []})
        for amount in ("5000", "200", "30"):
            self.assertNotIn(amount, audit[4])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM gateway_epochs"), [(1,)])

    def test_a_fault_after_each_step_leaves_the_database_exactly_as_it_was(self):
        self.approve()
        self.send(rep(seq=1))
        self.send(rep(seq=2, reader_incarnation=INC2, obs=OBS + 10))
        target = self.send(rep(seq=3, reader_incarnation=INC2, obs=OBS + 20))
        before = self.dump()
        for step in range(2, 8):
            with self.subTest(step=step):
                def stop(conn, number, step=step):
                    if number == step:
                        raise RuntimeError("fault after step %d" % step)
                with mock.patch.object(store, "_after_adopt_step", stop):
                    with self.assertRaises(RuntimeError):
                        self.adopt(target)
                self.assertEqual(self.dump(), before)
        self.adopt(target)                                                      # and without a fault it goes through
        self.assertNotEqual(self.dump(), before)

    def test_two_pending_readers_one_adoption_chooses_one_and_retires_the_other(self):
        """D2 (13.10): no adoption yet, so no transition; adopting one reader's report retires the other for good."""
        self.approve()
        a = self.send(rep(seq=1, reader_incarnation=INC))
        b = self.send(rep(seq=1, reader_incarnation=INC2, obs=OBS + 10))
        self.assertEqual(self.open_ids(), [])
        self.assertEqual(self.rows("SELECT reader_incarnation, state FROM gateway_readers ORDER BY first_seen_at"), [(INC, "pending"), (INC2, "pending")])
        adoption = self.adopt(b)
        self.assertEqual(self.rows("SELECT reader_incarnation, state, decided_by_adoption FROM gateway_readers ORDER BY reader_incarnation"),
                         [(INC, "retired", adoption), (INC2, "current", adoption)])
        self.assertEqual(json.loads(self.rows("SELECT redacted_details FROM audit WHERE action='gateway_adopted'")[0][0])["retired"], [INC])
        self.assertEqual(self.why(a), ["reader_retired"])

    def test_the_chosen_reader_is_current_the_others_are_retired_and_reader_changed_is_resolved_as_retired(self):
        """I1 adopted; I2 and I3 each arrive (reader_changed); adopting I3 naming both: I3 current, I2 and I1 retired (13.10)."""
        self.first()
        self.send(rep(seq=1, reader_incarnation=INC2, obs=OBS + 10))
        self.send(rep(seq=1, reader_incarnation=INC3, obs=OBS + 20))
        self.assertEqual(self.rows("SELECT kind FROM gateway_transitions ORDER BY id"), [("reader_changed",), ("reader_changed",)])
        third = self.send(rep(seq=2, reader_incarnation=INC3, obs=OBS + 30))
        ids = self.open_ids()
        adoption = self.adopt(third, ids)
        self.assertEqual(self.rows("SELECT reader_incarnation, state FROM gateway_readers ORDER BY reader_incarnation"),
                         [(INC, "retired"), (INC2, "retired"), (INC3, "current")])
        # every reader_changed row is resolved by this adoption; the reader table says which were retired and which was chosen
        self.assertEqual(self.rows("SELECT t.target_incarnation, t.resolved_by_adoption, r.state FROM gateway_transitions t "
                                   "JOIN gateway_readers r ON r.reader_incarnation=t.target_incarnation ORDER BY t.id"),
                         [(INC2, adoption, "retired"), (INC3, adoption, "current")])
        details = json.loads(self.rows("SELECT redacted_details FROM audit WHERE action='gateway_adopted' ORDER BY rowid DESC")[0][0])
        self.assertEqual((details["chosen"], details["retired"], details["resolved"]), (INC3, [INC, INC2], ids))
        self.assertEqual(self.rows("SELECT prev_adoption_id FROM gateway_adoptions WHERE id=?", (adoption,)), [(1,)])

    def test_a_retired_reader_is_stored_and_counted_never_current_and_never_re_opens_anything(self):
        self.first()
        other = self.send(rep(seq=1, reader_incarnation=INC2, obs=OBS + 10))
        self.adopt(other, self.open_ids())
        for n in (2, 3, 4):                                                    # three more reports of the retired reader
            self.send(rep(seq=n, reader_incarnation=INC, obs=OBS + 20 + n))
        self.assertEqual(self.rows("SELECT retired_reader_reports FROM gateway_chain_state"), [(3,)])
        self.assertEqual(self.open_ids(), [])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM gateway_transitions WHERE kind='reader_changed'"), [(1,)])      # one per identity
        self.assertEqual(self.rows("SELECT state FROM gateway_readers WHERE reader_incarnation=?", (INC,)), [("retired",)])

    def test_a_resolved_conflict_replayed_after_the_adoption_does_not_re_open(self):
        self.approve()
        first = self.send(rep(seq=1))
        rival = rep(seq=1, settled_micro_eur=5001, event_id=first["event_id"])
        for attempt in range(2):                                               # the collision is recorded once, however often it is sent
            with self.assertRaises(QuotaError):
                self.ingest(rival, name="rival%d.json" % attempt)
        self.assertEqual(len(self.open_ids()), 1)
        later = self.send(rep(seq=2))
        self.adopt(later, self.open_ids())
        self.assertEqual(self.open_ids(), [])
        with self.assertRaises(QuotaError):
            self.ingest(rival, name="rival-again.json")
        self.assertEqual(self.open_ids(), [])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM gateway_transitions"), [(1,)])

    def test_an_epoch_change_that_set_reconciliation_required_is_cleared_only_by_adopting_a_named_report(self):
        """The legacy sticky state of `gateways.state` (it used to be cleared by `reconcile`), now cleared by `gateway-adopt`."""
        self.first()
        self.send(rep(seq=1, ledger_epoch=2, generation=GEN2, obs=OBS + 10))
        self.send(rep(seq=2, ledger_epoch=2, generation=GEN2, obs=OBS + 20))
        self.assertEqual(self.rows("SELECT state FROM gateways WHERE alias='g1'"), [("reconciliation_required",)])
        self.assertEqual(self.open_ids() != [], True)
        latest = self.send(rep(seq=3, ledger_epoch=2, generation=GEN2, obs=OBS + 30))
        self.assertEqual(self.rows("SELECT state FROM gateways WHERE alias='g1'"), [("reconciliation_required",)])      # no self-clear
        self.refuse(latest)                                                                                              # nothing named
        self.adopt(latest, self.open_ids())
        self.assertEqual(self.rows("SELECT state FROM gateways WHERE alias='g1'"), [("ok",)])

    def test_the_hash_change_sticky_state_is_cleared_the_same_way_after_the_new_pair_is_approved(self):
        self.first()
        h = {"price": PRICE2, "child_cap": CAP2}
        self.send(rep(seq=2, policy_hashes=h, obs=OBS + 10))
        later = self.send(rep(seq=3, policy_hashes=h, obs=OBS + 20))
        self.assertEqual(self.rows("SELECT state FROM gateways WHERE alias='g1'"), [("reconciliation_required",)])
        self.refuse(later, self.open_ids())                                   # the new pair is not approved yet
        self.approve(PRICE2, CAP2)
        self.adopt(later, self.open_ids())
        self.assertEqual(self.rows("SELECT state FROM gateways WHERE alias='g1'"), [("ok",)])

    def test_an_adoption_changes_no_lease_no_receipt_and_no_adjustment(self):
        self.approve()
        item = self.send(rep(seq=1))
        tables = [row[0] for row in self.rows("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        untouched = [t for t in tables if t in ("leases", "receipts", "adjustments", "windows", "accounts", "machines", "billing_policies")]
        self.assertIn("leases", untouched)
        before = {t: self.rows("SELECT * FROM " + t) for t in untouched}                  # fixed names read from sqlite_master
        self.adopt(item)
        self.assertEqual({t: self.rows("SELECT * FROM " + t) for t in untouched}, before)

    def test_the_command_prints_the_adoption_id_and_a_refusal_exits_2_with_the_fixed_sentence(self):
        # the CLI uses the real clock, so a report fresh in real time is needed
        from tests.test_cardr13b_ingest import report as live_report
        live = live_report(seq=1)
        self.ingest(live, name="live.json")
        refused = run_cli("--db", self.db, "gateway-adopt", "--gateway", "g1", "--evidence", live["event_id"])
        self.assertEqual((refused.returncode, refused.stdout), (2, ""))
        self.assertEqual(json.loads(refused.stderr), {"error": {"code": "ADOPTION_REFUSED", "message": errors.MSG_ADOPTION_REFUSED}})
        before = self.dump()
        run_cli("--db", self.db, "gateway-approve", "--gateway", "g1", "--price-hash", PRICE, "--child-cap-hash", CAP)
        done = run_cli("--db", self.db, "gateway-adopt", "--gateway", "g1", "--evidence", live["event_id"])
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(json.loads(done.stdout), {"adoption_id": 1})
        self.assertNotEqual(self.dump(), before)
        bad = run_cli("--db", self.db, "gateway-adopt", "--gateway", "g1", "--evidence", live["event_id"], "--resolve", "secret-value")
        self.assertEqual(bad.returncode, 2)
        self.assertNotIn("secret-value", bad.stderr + bad.stdout)

    def test_an_older_report_is_refused_after_a_newer_one_was_adopted_in_a_row(self):          # one call after the other: the real race is in test_cardr13d_fix1
        self.approve()
        first = self.send(rep(seq=1))
        second = self.send(rep(seq=2))
        self.adopt(second)
        self.refuse(first)                                                    # no longer the newest, and its reader's state moved on
        self.assertEqual(self.rows("SELECT COUNT(*) FROM gateway_adoptions"), [(1,)])


# ---------------------------------------------------------------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------------------------------------------------------------

class Reconcile(_Op):

    def test_it_exits_2_prints_the_fixed_sentence_and_changes_nothing(self):
        self.first()
        self.send(rep(seq=2, settled_micro_eur=10))                            # something to clear: figure_fell, a sticky state
        self.run_sql("UPDATE gateways SET state='reconciliation_required'")
        with open(self.db, "rb") as handle:
            file_before = handle.read()
        before = self.dump()
        for gateway in ("g1", "g2", "nope"):
            with self.subTest(gateway=gateway):
                done = run_cli("--db", self.db, "reconcile", "--gateway", gateway)
                self.assertEqual((done.returncode, done.stdout), (2, ""))
                self.assertEqual(json.loads(done.stderr), {"error": {"code": "EVIDENCE_REQUIRED", "message": errors.MSG_EVIDENCE_REQUIRED}})
        self.assertEqual(self.dump(), before)
        with open(self.db, "rb") as handle:
            self.assertEqual(handle.read(), file_before)                       # not even a byte of the file moved

    def test_the_store_function_refuses_without_opening_the_store(self):
        with self.assertRaises(QuotaError) as caught:
            store.reconcile(os.path.join(self.tmp, "does-not-exist.db"), "g1")
        self.assertEqual(caught.exception.code, errors.CODE_EVIDENCE_REQUIRED)

    def test_the_two_new_words_are_in_the_registry_with_exit_code_2(self):
        for code in (errors.CODE_ADOPTION_REFUSED, errors.CODE_EVIDENCE_REQUIRED):
            self.assertEqual(errors.EXIT_CODES[code], 2)
            self.assertIn(code, errors.MESSAGES)
        self.assertIn("gateway-adopt", errors.MSG_EVIDENCE_REQUIRED)
        self.assertIn("status", errors.MSG_ADOPTION_REFUSED)


# ---------------------------------------------------------------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------------------------------------------------------------

class Status(_Op):

    KEYS = {"state", "approved", "adopted", "latest", "readers", "last_quiet", "open_transitions", "adopt_blockers", "late_reports",
            "old_chain_reports", "duplicate_reports", "retired_reader_reports", "action"}

    def test_the_block_is_absent_for_a_gateway_with_no_version_4_report(self):
        self.assertTrue(all("evidence" not in g for g in store.status(self.db)["gateways"]))
        self.approve()
        self.assertTrue(all("evidence" not in g for g in store.status(self.db)["gateways"]))
        self.send(rep(seq=1))
        by_alias = {g["alias"]: g for g in store.status(self.db)["gateways"]}
        self.assertIn("evidence", by_alias["g1"])
        self.assertNotIn("evidence", by_alias["g2"])

    def test_the_keys_are_closed_and_the_two_keys_of_r1_3e_are_not_there_yet(self):
        self.send(rep(seq=1))
        block = self.evidence()
        self.assertEqual(set(block), self.KEYS)
        self.assertNotIn("unreceipted_micro_eur", block)
        self.assertNotIn("receipts_exceed_settled", block)
        self.assertIn(block["state"], evidence_view.STATES)

    def test_the_states_in_the_order_an_operator_meets_them(self):
        item = self.send(rep(seq=1))
        self.assertEqual(self.evidence()["state"], "unadopted")            # nothing is adopted: unapproved is reported once there is a chain
        self.approve()
        self.assertEqual(self.evidence()["state"], "unadopted")
        self.adopt(item)
        self.assertEqual(self.evidence()["state"], "usable")
        self.assertEqual(self.evidence(now=OBS + 120_000)["state"], "usable")
        self.assertEqual(self.evidence(now=OBS + 120_001)["state"], "stale")
        self.send(rep(seq=2, settled_micro_eur=100))                           # a fall
        self.assertEqual(self.evidence()["state"], "transition_open")
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("DELETE FROM gateway_approvals")
            conn.commit()
        self.assertEqual(self.evidence()["state"], "unapproved")

    def test_a_month_that_changed_after_the_adoption_is_period_mismatch_while_the_report_is_still_fresh(self):
        item = self.first()
        late = OCT + 1000
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE gateway_totals SET observed_at=? WHERE event_id=?", (OCT - 5000, item["event_id"]))
            conn.commit()
        self.assertEqual(self.evidence(now=OCT - 4000)["state"], "usable")
        self.assertEqual(self.evidence(now=OCT + 100)["state"], "period_mismatch")
        self.assertEqual(self.evidence(now=late + 200_000)["state"], "stale")

    def test_every_block_after_an_adoption_and_a_busy_report(self):
        first = self.first()
        self.send(rep(seq=2, quiet=False, held_over_reservation=3, reconciliation_required=True, obs=OBS + 2000))
        block = self.evidence(now=OBS + 4000)
        self.assertEqual(block["adopted"], {"event_id": first["event_id"], "reader_incarnation": INC, "seq": 1, "epoch": 1,
                                            "period_start": SEP, "adopted_at": NOW, "kind": "operator"})
        self.assertEqual(block["latest"], {"seq": 2, "observed_at": OBS + 2000, "age_ms": 2000, "quiet": False, "held_over_reservation": 3})
        self.assertEqual(block["readers"], [{"reader_incarnation": INC, "state": "current", "first_seen_at": block["readers"][0]["first_seen_at"]}])
        self.assertEqual(block["last_quiet"], {"event_id": first["event_id"], "unattributed": 7})
        self.assertEqual([t["kind"] for t in block["open_transitions"]], ["reported_by_gateway"])
        self.assertEqual(block["adopt_blockers"], ["gateway_not_ready", "open_transitions_unnamed", "not_quiet"])
        self.assertEqual(block["action"], evidence_view.ACTION_SENTENCE)
        self.assertEqual((block["late_reports"], block["old_chain_reports"], block["duplicate_reports"], block["retired_reader_reports"]),
                         (0, 0, 0, 0))

    def test_the_action_sentence_appears_while_something_is_open_or_the_gateway_says_so_and_not_otherwise(self):
        self.first()
        self.assertIsNone(self.evidence()["action"])
        self.send(rep(seq=2, reconciliation_required=True, obs=OBS + 10))
        self.assertEqual(self.evidence()["action"], evidence_view.ACTION_SENTENCE)
        self.send(rep(seq=3, obs=OBS + 20))                                    # the flag is gone but the open item stays open
        self.assertEqual(self.evidence()["action"], evidence_view.ACTION_SENTENCE)
        self.adopt(self.send(rep(seq=4, obs=OBS + 30)), self.open_ids())
        self.assertIsNone(self.evidence(now=OBS + 40)["action"])

    def test_the_counters_show_late_reports(self):
        self.first()
        self.send(rep(seq=3, obs=OBS + 10))
        self.send(rep(seq=2, obs=OBS + 20))                                    # late
        block = self.evidence()
        self.assertEqual((block["late_reports"], block["duplicate_reports"], block["old_chain_reports"]), (1, 0, 0))

    def test_nothing_secret_is_echoed_no_hash_no_generation_no_amount_but_the_quiet_figure(self):
        self.first()
        self.send(rep(seq=2, reconciliation_required=True, obs=OBS + 10))
        block = self.evidence()
        text = json.dumps(block)
        for value in (PRICE, CAP, GEN):
            self.assertNotIn(value, text)

        def numbers(node):
            if isinstance(node, dict):
                return [n for v in node.values() for n in numbers(v)]
            if isinstance(node, list):
                return [n for v in node for n in numbers(v)]
            return [node] if isinstance(node, int) and not isinstance(node, bool) else []
        for amount in (5000, 200, 30):                                      # settled, reserved, uncertain of the fixture reports
            self.assertNotIn(amount, numbers(block))
        for sentence in (errors.MSG_ADOPTION_REFUSED, errors.MSG_EVIDENCE_REQUIRED, evidence_view.ACTION_SENTENCE):
            self.assertTrue(sentence.isascii())
            for value in (PRICE, CAP, INC):
                self.assertNotIn(value, sentence)

    def test_status_keeps_working_through_the_cli(self):
        self.send(rep(seq=1))
        done = run_cli("--db", self.db, "status", "--json")
        self.assertEqual(done.returncode, 0)
        by_alias = {g["alias"]: g for g in json.loads(done.stdout)["gateways"]}
        self.assertEqual(by_alias["g1"]["evidence"]["state"], "unadopted")
        self.assertNotIn("evidence", by_alias["g2"])


# ---------------------------------------------------------------------------------------------------------------------------------
# the read-only context builder
# ---------------------------------------------------------------------------------------------------------------------------------

class Builder(_Op):

    def inputs(self, now=NOW):
        return store.evidence_inputs(self.db, "meter", now)

    def g1(self, now=NOW):
        got = self.inputs(now)
        return got, {g["gateway"]: g for g in got["gateways"]}["g1"]

    def test_usable_right_after_adoption_the_chain_is_the_adopted_report_and_never_empty(self):
        item = self.first()
        got, g1 = self.g1()
        self.assertEqual(got["states"]["g1"], "usable")
        self.assertEqual(g1["current_chain_event_ids"], [item["event_id"]])
        self.assertEqual([r["event_id"] for r in got["readings"]], [item["event_id"]])
        envelope = got["readings"][0]
        self.assertEqual((envelope["machine"], envelope["account"], envelope["kind"], envelope["observed_at"]),
                         ("desktop", "meter", "gateway_snapshot", OBS))
        self.assertEqual(envelope["payload"]["reader_incarnation"], INC)

    def test_the_chain_is_the_adoption_and_the_later_in_order_reports_and_excludes_the_history_before_it(self):
        self.approve()
        before = self.send(rep(seq=1))
        adopted = self.send(rep(seq=2))
        self.adopt(adopted)
        after = [self.send(rep(seq=n, settled_micro_eur=5000 + n)) for n in (3, 4)]
        got, g1 = self.g1()
        self.assertEqual(g1["current_chain_event_ids"], [adopted["event_id"]] + [i["event_id"] for i in after])
        self.assertNotIn(before["event_id"], g1["current_chain_event_ids"])

    def test_a_late_report_is_not_in_the_chain(self):
        self.first()
        high = self.send(rep(seq=5, obs=OBS + 10))
        self.send(rep(seq=3, obs=OBS + 20))
        _, g1 = self.g1()
        self.assertEqual(g1["current_chain_event_ids"][-1], high["event_id"])
        self.assertEqual(len(g1["current_chain_event_ids"]), 2)

    def test_each_reason_a_gateway_is_not_usable_empties_its_chain_and_says_why(self):
        self.approve()
        item = self.send(rep(seq=1))
        got, g1 = self.g1()
        self.assertEqual((got["states"]["g1"], g1["current_chain_event_ids"], got["readings"]), ("unadopted", [], []))
        self.adopt(item)
        self.assertEqual(self.inputs(OBS + 120_001)["states"]["g1"], "stale")
        self.assertEqual({g["gateway"]: g["current_chain_event_ids"] for g in self.inputs(OBS + 120_001)["gateways"]}["g1"], [])
        self.send(rep(seq=2, settled_micro_eur=100, obs=OBS + 10))                      # a fall: transition_open
        got, g1 = self.g1()
        self.assertEqual((got["states"]["g1"], g1["current_chain_event_ids"]), ("transition_open", []))
        self.assertTrue(got["unresolved_event_ids"])                                   # the open item's report is handed over as unresolved
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("DELETE FROM gateway_approvals")
            conn.commit()
        self.assertEqual(self.inputs()["states"]["g1"], "unapproved")

    def test_a_gateway_with_no_report_is_listed_unusable(self):
        got = self.inputs()
        self.assertEqual({g["gateway"]: g["current_chain_event_ids"] for g in got["gateways"]}, {"g1": [], "g2": []})
        self.assertEqual(got["states"], {"g1": "unadopted", "g2": "unadopted"})

    def test_the_legacy_gateway_columns_cannot_make_a_report_usable(self):
        """6 (i): the evidence is the adopted chain, never the `gateways` columns the old reducer still writes."""
        self.approve()
        self.send(rep(seq=1))
        # the legacy columns say everything is fine and fresh: ok, the newest sequence, the report time, the hashes
        self.run_sql("UPDATE gateways SET state='ok', last_report_at=?, last_seq=1, ledger_epoch=1 WHERE alias='g1'", (OBS,))
        got, g1 = self.g1()
        self.assertEqual((got["states"]["g1"], g1["current_chain_event_ids"]), ("unadopted", []))
        # an adopted chain, then a report that is outside it moves the legacy columns on; it still cannot supply the evidence
        item = self.first_after_legacy()
        outside = self.send(rep(seq=9, reader_incarnation=INC2, obs=OBS + 50))
        self.run_sql("UPDATE gateways SET state='ok' WHERE alias='g1'")
        got, g1 = self.g1(NOW + 100)
        self.assertEqual(got["states"]["g1"], "transition_open")
        self.assertEqual(g1["current_chain_event_ids"], [])
        self.assertNotIn(outside["event_id"], [r["event_id"] for r in got["readings"] if r["event_id"] in g1["current_chain_event_ids"]])
        self.assertIsNotNone(item)

    def first_after_legacy(self):
        item = self.send(rep(seq=2, obs=OBS + 5))
        self.adopt(item)
        return item

    def test_a_report_of_another_epoch_or_generation_or_reader_is_never_in_the_chain(self):
        self.first()
        for n, over in enumerate(({"ledger_epoch": 2, "generation": GEN2}, {"reader_incarnation": INC2}, {"generation": GEN2})):
            self.send(rep(seq=10 + n, obs=OBS + 10 + n, **over))
        self.run_sql("UPDATE gateway_transitions SET resolved_by_adoption=1")           # even with every item "resolved"
        _, g1 = self.g1()
        self.assertEqual(len(g1["current_chain_event_ids"]), 1)

    def test_the_builder_reads_only_and_nothing_in_the_grant_path_calls_it(self):
        self.first()
        before = self.dump()
        self.inputs()
        self.assertEqual(self.dump(), before)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("lease.py", "http_api.py", "money.py", "reduce.py", "billing_eval.py"):
            with open(os.path.join(root, "quota_balancer", name), encoding="utf-8") as handle:
                text = handle.read()
            self.assertNotIn("evidence_inputs", text, name)
            self.assertNotIn("evidence_view", text, name)

    # --- 6 (ii): the real path, from stored readings to the evaluator ------------------------------------------------------

    def context(self, got, only=("g1",), **over):
        ctx = {"account": "meter", "period_start": SEP, "period_end": OCT, "cap_micro_eur": 100_000_000,
               "gateways": [g for g in got["gateways"] if g["gateway"] in only], "policy": copy.deepcopy(_POLICY),
               "readings": got["readings"], "unresolved_event_ids": got["unresolved_event_ids"],
               "boundary_exposure_micro_eur": None, "boundary_event_ids": []}
        ctx.update(over)
        return ctx

    def request(self, now=NOW):
        return {"version": 1, "account": "meter", "now_ms": now, "requested_micro_eur": 0, "coverage": []}

    def test_a_stored_version_4_reading_passes_the_evaluators_reading_check(self):
        item = self.first()
        got = self.inputs()
        envelope = billing_eval._validate_reading(got["readings"][0], "meter")
        self.assertEqual(envelope["payload"]["reader_incarnation"], INC)
        self.assertEqual(envelope["payload"]["quiet"], True)
        self.assertEqual(envelope["event_id"], item["event_id"])

    def test_the_evaluator_still_refuses_a_version_4_payload_that_is_not_exactly_the_closed_shape(self):
        self.first()
        envelope = self.inputs()["readings"][0]
        for change in (lambda p: p.pop("generation"), lambda p: p.update(extra=1), lambda p: p.update(quiet="yes"),
                       lambda p: p.update(held_over_reservation=-1), lambda p: p.update(reader_incarnation="x"),
                       lambda p: p.update(policy_hashes=None), lambda p: p.pop("settled_micro_eur")):
            with self.subTest():
                bad = copy.deepcopy(envelope)
                change(bad["payload"])
                with self.assertRaises(QuotaError) as caught:
                    billing_eval._validate_reading(bad, "meter")
                self.assertEqual(caught.exception.code, errors.CODE_INVALID_CONFIG)

    def test_from_stored_readings_through_the_builder_into_evaluate_billing(self):
        self.first()
        self.send(rep(seq=2, settled_micro_eur=5100, obs=OBS + 1000))
        got = self.inputs(OBS + 2000)
        result = billing_eval.evaluate_billing(self.context(got), self.request(OBS + 2000))
        self.assertEqual(result["reason"], "billing_unknown")                   # the gateway chain was accepted; there is no provider sample
        self.assertNotEqual(result["reason"], "gateway_unusable")

    def test_a_gateway_that_is_not_usable_makes_the_evaluator_say_gateway_unusable(self):
        self.first()
        stale = self.inputs(OBS + 120_001)                                     # the same stored readings, a minute too late
        result = billing_eval.evaluate_billing(self.context(stale), self.request(OBS + 120_001))
        self.assertEqual(result["reason"], "gateway_unusable")
        both = self.inputs(OBS + 2000)                                         # g2 has no report: the account as a whole is unusable
        self.assertEqual(billing_eval.evaluate_billing(self.context(both, only=("g1", "g2")), self.request(OBS + 2000))["reason"],
                         "gateway_unusable")

    def test_the_open_items_report_goes_in_as_unresolved_and_the_evaluator_accepts_the_context(self):
        self.first()
        self.send(rep(seq=2, settled_micro_eur=100, obs=OBS + 10))
        got = self.inputs(OBS + 2000)
        self.assertEqual(len(got["unresolved_event_ids"]), 1)
        result = billing_eval.evaluate_billing(self.context(got), self.request(OBS + 2000))
        self.assertEqual(result["reason"], "gateway_unusable")


if __name__ == "__main__":
    unittest.main()
