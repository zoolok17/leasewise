"""Card R1-READER step 1: the reader's pure half (`quota_balancer/reporter/gwreport.py`, `gwcontract.py`, the month version of the receipt page
in `receipts.py`) and the runner's `report` mode (`runner.py`).

What is proven here, with synthetic fixtures AND agenttalk's own documented output (merged head `921c82d4`, `docs/QWEN-OVH-TRIAL.md`; every key
name comes from the one table `gwcontract.py`):

* **The projection** of a snapshot is strict and allowlisted: every field it uses is required with its exact type and bound, an unknown version is
  refused, keys it does not use are ignored, and nothing named (an agent, a message, a path) is kept anywhere.
* **The receipt page** keeps envelope version 1; the reader asks for month rows (the keys plus `charge_period`, a month or null) and the existing
  rows stay as they were.
* **The money** follows the lead's worked accounting example exactly (its five cases, with their figures): `unattributed = committed - covered -
  unreceipted`, receipts only up to the snapshot's own N and generation, a null month in no month, a negative result refused and never clamped.
* **The runner's `report` mode** appends nothing; the receipt mode is unchanged.

Nothing here reads a live gateway or sends anything.
"""

import json
import unittest

from quota_balancer.reporter import gwcontract as c, gwreport, receipts, runner

from tests.test_card7l6a1b_runner import PAGE, WINDOWS, Spy, _Case, slurp

GEN = "0123456789abcdef0123456789abcdef"
GEN2 = "fedcba9876543210fedcba9876543210"
PRICE = "a" * 64
CAP = "b" * 64
P = "2026-10"
LATER = "2026-11"
EARLIER = "2026-09"
NAME = "alice-the-agent"                      # a name the reader must never keep


def snapshot_doc(**over):
    """A good snapshot as the merged contract prints it, with keys and names in places the reader does not use."""
    doc = {
        c.KEY_VERSION: c.ACCEPTED_VERSION, c.KEY_RECEIPT_VERSION: c.ACCEPTED_RECEIPT_VERSION, c.KEY_GENERATION: GEN, c.KEY_PRICE_HASH: PRICE, c.KEY_CAP_HASH: CAP, c.KEY_CAP_READY: True,
        c.KEY_PERIODS: [{c.KEY_PERIOD: P, c.KEY_COMMITTED: 40}],
        c.KEY_UNRESOLVED: [], c.KEY_THROUGH: 0, c.KEY_UNRECEIPTED: [],
        c.KEY_HOLD: False, c.KEY_HOLD_REASON: None,
        "project": r"C:\Users\someone\project", "agent": NAME, "observed_at": "2026-10-03T10:06:00.000000Z", "open_child_turns": 3,
        "open_child_turns_expired": 1, "earliest_open_expiry": "2026-10-04T10:05:00.000000Z", "child_receipts_pending": 1,
    }
    doc.update(over)
    return doc


def raw(doc):
    return json.dumps(doc).encode("utf-8")


def snapshot(**over):
    return gwreport.parse_snapshot(raw(snapshot_doc(**over)))


def receipt(seq, actual, month, calls=1):
    return {"seq": seq, "quota_lease_ref_sha256": "c" * 64, "outcome": "completed", "calls": calls,
            "input_tokens": 0 if calls == 0 else 10, "output_tokens": 0 if calls == 0 else 5, "actual_micro_eur": actual,
            "closed_at": "2026-10-04T00:00:00.000000Z", c.KEY_CHARGE_PERIOD: month}


def page_doc(rows, generation=GEN, after=0, version=1):
    return {"envelope_version": version, "generation": generation, "after_seq": after, "next_seq": after + len(rows), "has_more": False,
            "receipts": rows}


def page(rows, generation=GEN, after=0):
    return receipts.parse_page(raw(page_doc(rows, generation, after)), after, month_rows=True)


def figures(snap, rows=(), month=P, generation=GEN):
    """Read the receipts (up to the snapshot's own N) and compute the figures for `month`."""
    totals = gwreport.empty_totals(generation)
    if rows:
        totals = gwreport.fold_page(totals, page(list(rows), generation), snap)
    return gwreport.compute(snap, totals, month)


def refused(test, call, word):
    with test.assertRaises(gwreport.Refused) as caught:
        call()
    test.assertEqual(caught.exception.word, word)


class Projection(unittest.TestCase):

    def test_the_good_snapshot_is_read_and_keeps_only_numbers_states_and_digests(self):
        snap = snapshot(**{c.KEY_UNRESOLVED: [{c.KEY_STATE: "uncertain", c.KEY_RESERVED: 10, c.KEY_ACTUAL: 14, "agent": NAME}],
                           c.KEY_UNRECEIPTED: [{c.KEY_PERIOD: P, c.KEY_UNRECEIPTED_AMOUNT: 14, "message": NAME}]})
        self.assertEqual((snap.generation, snap.price_hash, snap.child_cap_hash, snap.periods, snap.through_seq, snap.hold),
                         (GEN, PRICE, CAP, {P: 40}, 0, False))
        self.assertEqual(snap.unresolved, (("uncertain", 10, 14),))
        self.assertEqual(snap.unreceipted, {P: 14})

    def test_nothing_named_is_kept_anywhere_and_the_object_prints_nothing(self):
        snap = snapshot(**{c.KEY_UNRESOLVED: [{c.KEY_STATE: "reserved", c.KEY_RESERVED: 1, c.KEY_ACTUAL: None, "agent": NAME}]})
        kept = [getattr(snap, slot) for slot in snap.__slots__]
        self.assertNotIn(NAME, repr(kept))
        self.assertNotIn("someone", repr(kept))
        self.assertNotIn(NAME + "x", repr(snap))
        self.assertEqual((repr(snap), str(snap)), ("Snapshot(<hidden>)", "Snapshot(<hidden>)"))

    def test_keys_it_does_not_use_are_ignored_at_every_level(self):
        doc = snapshot_doc(**{"surprise": [1, 2, {"x": NAME}]})
        doc[c.KEY_PERIODS][0]["extra"] = NAME
        doc[c.KEY_UNRESOLVED] = [{c.KEY_STATE: "reserved", c.KEY_RESERVED: 3, c.KEY_ACTUAL: None, "period": "2026-08"}]
        snap = gwreport.parse_snapshot(raw(doc))
        self.assertEqual(snap.unresolved, (("reserved", 3, None),))                  # `period` on an unresolved row is not used
        for dropped in ("project", "agent", "observed_at", "open_child_turns", "open_child_turns_expired", "earliest_open_expiry",
                        "child_receipts_pending"):
            with self.subTest(dropped=dropped):
                doc = snapshot_doc()
                del doc[dropped]
                gwreport.parse_snapshot(raw(doc))                        # nothing the reader does not use is needed (the keys the reader does not use are never needed)

    def test_every_required_field_missing_is_refused(self):
        required = (c.KEY_VERSION, c.KEY_GENERATION, c.KEY_PRICE_HASH, c.KEY_CAP_HASH, c.KEY_CAP_READY, c.KEY_PERIODS, c.KEY_UNRESOLVED,
                    c.KEY_THROUGH, c.KEY_UNRECEIPTED, c.KEY_HOLD, c.KEY_HOLD_REASON, c.KEY_RECEIPT_VERSION)
        for key in required:
            with self.subTest(missing=key):
                doc = snapshot_doc()
                del doc[key]
                refused(self, lambda doc=doc: gwreport.parse_snapshot(raw(doc)), gwreport.STATUS_SHAPE)

    def test_every_field_with_a_wrong_type_or_value_is_refused(self):
        bad = {
            c.KEY_VERSION: (True, 1.0, "1", None, 2, 0),
            c.KEY_GENERATION: (None, 5, GEN.upper(), GEN[:-1], GEN + "0", "g" * 32),
            c.KEY_PRICE_HASH: (None, 5, PRICE.upper(), PRICE[:-1], "g" * 64),
            c.KEY_CAP_HASH: (None, 5, CAP.upper(), CAP[:-1]),
            c.KEY_CAP_READY: (None, 1, "true"),
            c.KEY_PERIODS: (None, {}, "x", [1], [{c.KEY_PERIOD: P}], [{c.KEY_COMMITTED: 1}],
                            [{c.KEY_PERIOD: "2026-13", c.KEY_COMMITTED: 1}], [{c.KEY_PERIOD: "26-10", c.KEY_COMMITTED: 1}],
                            [{c.KEY_PERIOD: P, c.KEY_COMMITTED: -1}], [{c.KEY_PERIOD: P, c.KEY_COMMITTED: 10 ** 12 + 1}],
                            [{c.KEY_PERIOD: P, c.KEY_COMMITTED: 1.5}], [{c.KEY_PERIOD: P, c.KEY_COMMITTED: True}],
                            [{c.KEY_PERIOD: P, c.KEY_COMMITTED: 1}, {c.KEY_PERIOD: P, c.KEY_COMMITTED: 2}]),
            c.KEY_UNRESOLVED: (None, {}, [1], [{c.KEY_STATE: "open", c.KEY_RESERVED: 1, c.KEY_ACTUAL: None}],
                               [{c.KEY_STATE: "reserved", c.KEY_ACTUAL: None}],
                               [{c.KEY_STATE: "reserved", c.KEY_RESERVED: -1, c.KEY_ACTUAL: None}],
                               [{c.KEY_STATE: "reserved", c.KEY_RESERVED: 1, c.KEY_ACTUAL: -1}],
                               [{c.KEY_STATE: "reserved", c.KEY_RESERVED: 1, c.KEY_ACTUAL: "5"}],
                               [{c.KEY_STATE: "reserved", c.KEY_RESERVED: True, c.KEY_ACTUAL: None}]),
            c.KEY_THROUGH: (-1, 1.5, "0", True, 2 ** 63),
            c.KEY_RECEIPT_VERSION: (True, 1.0, "2", 1, 3, 0),
            c.KEY_UNRECEIPTED: (None, {}, [1], [{c.KEY_PERIOD: P}], [{c.KEY_UNRECEIPTED_AMOUNT: 1}],
                                [{c.KEY_PERIOD: P, c.KEY_UNRECEIPTED_AMOUNT: -1}], [{c.KEY_PERIOD: "bad", c.KEY_UNRECEIPTED_AMOUNT: 1}],
                                [{c.KEY_PERIOD: P, c.KEY_UNRECEIPTED_AMOUNT: 1}, {c.KEY_PERIOD: P, c.KEY_UNRECEIPTED_AMOUNT: 1}]),
            c.KEY_HOLD: (None, 0, 1, "false"),
            c.KEY_HOLD_REASON: (5, "somebody_typed_this", True, "over_reservation", []),
        }
        for key, values in bad.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    doc = snapshot_doc(**{key: value})
                    refused(self, lambda doc=doc: gwreport.parse_snapshot(raw(doc)), gwreport.STATUS_SHAPE)

    def test_a_missing_cost_key_in_an_unresolved_row_is_refused_never_read_as_no_cost_yet(self):
        row = {c.KEY_STATE: "uncertain", c.KEY_RESERVED: 6, c.KEY_ACTUAL: 7}
        fig = figures(snapshot(**{c.KEY_UNRESOLVED: [row], c.KEY_HOLD: True, c.KEY_HOLD_REASON: "attempt_over_reservation"}))
        self.assertEqual((fig.uncertain, fig.held_over_reservation), (0, 1))                  # over its reservation: counted as held only
        gone = {key: value for key, value in row.items() if key != c.KEY_ACTUAL}               # the SAME row with only the key deleted
        refused(self, lambda: snapshot(**{c.KEY_UNRESOLVED: [gone]}), gwreport.STATUS_SHAPE)
        for actual in (None, 0):                                                              # an explicit null and an explicit 0 stay valid
            self.assertEqual(snapshot(**{c.KEY_UNRESOLVED: [dict(row, **{c.KEY_ACTUAL: actual})]}).unresolved,
                             (("uncertain", 6, actual),))

    def test_a_missing_key_in_every_nested_row_kind_is_refused(self):
        rows = {
            c.KEY_PERIODS: {c.KEY_PERIOD: P, c.KEY_COMMITTED: 40},
            c.KEY_UNRESOLVED: {c.KEY_STATE: "reserved", c.KEY_RESERVED: 3, c.KEY_ACTUAL: None},
            c.KEY_UNRECEIPTED: {c.KEY_PERIOD: P, c.KEY_UNRECEIPTED_AMOUNT: 5},
        }
        for list_key, row in rows.items():
            self.assertTrue(snapshot(**{list_key: [row]}))
            for key in row:
                with self.subTest(list=list_key, missing=key):
                    refused(self, lambda list_key=list_key, row=row, key=key: snapshot(
                        **{list_key: [{k: v for k, v in row.items() if k != key}]}), gwreport.STATUS_SHAPE)

    def test_months_must_come_in_order_as_agenttalk_prints_them(self):
        two = [{c.KEY_PERIOD: P, c.KEY_COMMITTED: 1}, {c.KEY_PERIOD: EARLIER, c.KEY_COMMITTED: 1}]
        refused(self, lambda: snapshot(**{c.KEY_PERIODS: two}), gwreport.STATUS_SHAPE)
        self.assertEqual(snapshot(**{c.KEY_PERIODS: two[::-1]}).periods, {EARLIER: 1, P: 1})

    def test_the_largest_amount_is_accepted_and_one_more_is_not(self):
        top = 10 ** 12
        snap = snapshot(**{c.KEY_PERIODS: [{c.KEY_PERIOD: P, c.KEY_COMMITTED: top}], c.KEY_THROUGH: 2 ** 63 - 1})
        self.assertEqual((snap.periods, snap.through_seq), ({P: top}, 2 ** 63 - 1))
        refused(self, lambda: snapshot(**{c.KEY_UNRESOLVED: [{c.KEY_STATE: "reserved", c.KEY_RESERVED: top + 1, c.KEY_ACTUAL: None}]}),
                gwreport.STATUS_SHAPE)

    def test_an_unknown_report_version_is_refused_in_both_directions(self):
        for version in (c.ACCEPTED_VERSION + 1, c.ACCEPTED_VERSION - 1, 99):
            with self.subTest(version=version):
                refused(self, lambda version=version: snapshot(**{c.KEY_VERSION: version}), gwreport.STATUS_SHAPE)

    def test_a_ledger_without_child_caps_or_lease_binding_has_its_own_word(self):
        """What agenttalk prints with `child_cap_ready` false, and with schema 3 (no lease binding): the nulls are not damage."""
        not_ready = {c.KEY_CAP_READY: False, c.KEY_CAP_HASH: None, c.KEY_RECEIPT_VERSION: None, c.KEY_THROUGH: None, c.KEY_UNRECEIPTED: []}
        no_binding = {c.KEY_RECEIPT_VERSION: None, c.KEY_THROUGH: None, c.KEY_UNRECEIPTED: []}
        for name, over in (("not ready, as printed", not_ready), ("only the flag false", {c.KEY_CAP_READY: False}),
                           ("no binding (schema 3)", no_binding), ("receipt version null", {c.KEY_RECEIPT_VERSION: None}),
                           ("receipt number null", {c.KEY_THROUGH: None})):
            with self.subTest(name):
                refused(self, lambda over=over: snapshot(**over), gwreport.CHILD_CAP_NOT_READY)

    def test_the_hold_words_are_the_four_agenttalk_prints_and_a_hold_always_has_one(self):
        for word in c.HOLD_REASONS:
            with self.subTest(word=word):
                self.assertTrue(snapshot(**{c.KEY_HOLD: True, c.KEY_HOLD_REASON: word}).hold)
        self.assertEqual(c.HOLD_REASONS, ("attempt_over_reservation", "dashboard_canary_mismatch", "manual", "other"))
        self.assertFalse(snapshot().hold)
        refused(self, lambda: snapshot(**{c.KEY_HOLD: True, c.KEY_HOLD_REASON: None}), gwreport.STATUS_SHAPE)
        refused(self, lambda: snapshot(**{c.KEY_HOLD: False, c.KEY_HOLD_REASON: "manual"}), gwreport.STATUS_SHAPE)

    def test_strict_json_a_repeated_key_a_constant_and_text_that_is_not_json_are_refused(self):
        text = json.dumps(snapshot_doc())
        for bad in (b"not json", b"", b"[]", b"null", b'"x"', b"\xff\xfe", text.replace('"generation"', '"generation": "x", "generation"').encode(),
                    text.replace('"committed_micro_eur": 40', '"committed_micro_eur": NaN').encode(),
                    text.replace('"committed_micro_eur": 40', '"committed_micro_eur": Infinity').encode(),
                    b"[" * 100000):
            with self.subTest(bad=bad[:30]):
                refused(self, lambda bad=bad: gwreport.parse_snapshot(bad), gwreport.STATUS_SHAPE)

    def test_an_empty_report_with_no_spend_is_a_good_snapshot(self):
        snap = snapshot(**{c.KEY_PERIODS: [], c.KEY_UNRESOLVED: [], c.KEY_UNRECEIPTED: []})
        self.assertEqual(figures(snap).settled, 0)


class ReceiptPage(unittest.TestCase):
    """The envelope is version 1 for every reader; the caller picks the row shape (existing rows, or month rows)."""

    def test_month_rows_are_accepted_and_each_row_keeps_its_month_or_null(self):
        parsed = page([receipt(1, 16, P), receipt(2, 8, None)])
        self.assertEqual([row.charge_period for row in parsed.receipts], [P, None])

    def test_the_envelope_is_version_1_and_any_other_version_is_refused_for_both_shapes(self):
        for month_rows in (True, False):
            for version in (2, 0, True, "1", None):
                with self.subTest(month_rows=month_rows, version=version):
                    with self.assertRaises(receipts._Refused) as caught:
                        receipts.parse_page(raw(page_doc([], version=version)), 0, month_rows=month_rows)
                    self.assertEqual(caught.exception.args[0], "bad_version")

    def test_existing_rows_are_unchanged_and_have_no_month(self):
        row = {key: value for key, value in receipt(1, 16, P).items() if key != c.KEY_CHARGE_PERIOD}
        parsed = receipts.parse_page(raw(page_doc([row])), 0)
        self.assertIs(type(parsed.receipts[0]), receipts.Receipt)
        self.assertFalse(hasattr(parsed.receipts[0], "charge_period"))

    def test_a_row_of_the_other_shape_is_refused(self):
        with_month = receipt(1, 16, P)
        without = {key: value for key, value in with_month.items() if key != c.KEY_CHARGE_PERIOD}
        for rows, month_rows in (([with_month], False), ([without], True)):         # the existing caller still refuses a month row
            with self.subTest(month_rows=month_rows):
                with self.assertRaises(receipts._Refused) as caught:
                    receipts.parse_page(raw(page_doc(rows)), 0, month_rows=month_rows)
                self.assertEqual(caught.exception.args[0], "bad_row")

    def test_the_keys_stay_closed_and_the_month_is_a_month_or_null(self):
        extra = receipt(1, 16, P)
        extra["agent"] = NAME
        cases = [extra] + [receipt(1, 16, value) for value in ("2026-13", "2026-00", "26-10", "x", "", 5, True, ["2026-10"],
                                                                "2026-10-01", " 2026-10", "\uff12\uff10\uff12\uff16-10")]
        for row in cases:
            with self.subTest(row=row):
                with self.assertRaises(receipts._Refused) as caught:
                    receipts.parse_page(raw(page_doc([row])), 0, month_rows=True)
                self.assertEqual(caught.exception.args[0], "bad_row")

    def test_a_month_row_with_no_calls_must_have_no_month(self):
        def read(row, month_rows=True):
            return receipts.parse_page(raw(page_doc([row])), 0, month_rows=month_rows)
        with self.assertRaises(receipts._Refused) as caught:
            read(receipt(1, 0, P, calls=0))
        self.assertEqual(caught.exception.args[0], "bad_row")
        self.assertIsNone(read(receipt(1, 0, None, calls=0)).receipts[0].charge_period)
        self.assertEqual(read(receipt(1, 0, P)).receipts[0].charge_period, P)                  # calls > 0, cost 0: may still name a month
        plain = {key: value for key, value in receipt(1, 0, P, calls=0).items() if key != c.KEY_CHARGE_PERIOD}
        self.assertEqual(read(plain, month_rows=False).receipts[0].calls, 0)                    # existing-shape callers are unchanged

    def test_the_order_and_gap_checks_still_apply(self):
        for rows, word in (([receipt(2, 1, P)], "bad_order"), ([receipt(1, 1, P), receipt(1, 1, P)], "bad_order")):
            with self.subTest(word=word):
                doc = page_doc(rows)
                doc["next_seq"] = rows[-1]["seq"]
                with self.assertRaises(receipts._Refused) as caught:
                    receipts.parse_page(raw(doc), 0, month_rows=True)
                self.assertEqual(caught.exception.args[0], word)

    def test_the_only_caller_of_the_page_reader_still_reads_existing_rows(self):
        """Compatibility finding (see the step record): `reporter/run.py` reads existing rows and would refuse a gateway on 921c82d4."""
        with open(receipts.__file__.replace("receipts.py", "run.py"), encoding="utf-8") as handle:
            self.assertIn("receipts.read_page(source, after, self.stop)", handle.read())


# agenttalk's own documented output (docs/QWEN-OVH-TRIAL.md at 921c82d4, "The ledger report" and "Receipts"), copied byte for byte: the
# report as `agenttalk gateway report --json` prints it (one line, no spaces) and the one-receipt page of `gateway receipts --after 0 --json`.
AGENTTALK_REPORT = (
    '{"child_cap_policy_hash":"c5172fff7c21896306d2fae47ec310105e1c3b7ff4cf9e73a5ff3b1e61ce5517","child_cap_ready":true,'
    '"child_receipt_report_version":2,"child_receipts_pending":0,"child_receipts_through_seq":1,'
    '"earliest_open_expiry":"2026-10-04T10:05:00.000000Z","gateway_report_version":1,"generation":"0123456789abcdef0123456789abcdef",'
    '"observed_at":"2026-10-03T10:06:00.000000Z","open_child_turns":1,"open_child_turns_expired":0,'
    '"periods":[{"committed_micro_eur":2630,"period":"2026-10"}],'
    '"policy_hash":"6df40ecdf2c22a9d06a73c2b2d7090b40d722d590e04237b19c903cb034dd5c2","service_hold":false,"service_hold_reason":null,'
    '"unreceipted_bound_actual":[{"micro_eur":670,"period":"2026-10"}],"unresolved":[]}\n').encode("utf-8")
AGENTTALK_PAGE = (
    '{"after_seq":0,"envelope_version":1,"generation":"0123456789abcdef0123456789abcdef","has_more":false,"next_seq":1,'
    '"receipts":[{"actual_micro_eur":1290,"calls":1,"charge_period":"2026-10","closed_at":"2026-10-03T10:03:00.000000Z",'
    '"input_tokens":1200,"outcome":"completed","output_tokens":300,'
    '"quota_lease_ref_sha256":"d8b85cf28e9743fe6abc53d28753a832f037530e484455cdff76f473d69b5433","seq":1}]}\n').encode("utf-8")


class AgenttalkOwnOutput(unittest.TestCase):
    """The strict reader on the real thing, not only on fixtures made to fit it."""

    def test_the_documented_report_and_page_are_read_and_the_unowned_money_is_670(self):
        snap = gwreport.parse_snapshot(AGENTTALK_REPORT)
        parsed = receipts.parse_page(AGENTTALK_PAGE, 0, month_rows=True)
        self.assertEqual((snap.generation, snap.through_seq, snap.periods, snap.unreceipted, snap.hold),
                         (GEN, 1, {P: 2630}, {P: 670}, False))
        self.assertEqual((parsed.generation, parsed.receipts[0].charge_period, parsed.receipts[0].actual_micro_eur), (GEN, P, 1290))
        totals = gwreport.fold_page(gwreport.empty_totals(GEN), parsed, snap)
        fig = gwreport.compute(snap, totals, P)
        self.assertEqual((fig.settled, fig.unattributed, fig.reserved, fig.uncertain, fig.held_over_reservation, fig.quiet),
                         (2630, 2630 - 1290 - 670, 0, 0, 0, True))
        self.assertEqual(fig.unattributed, 670)

    def test_the_existing_row_reader_refuses_that_page_so_the_month_shape_is_needed(self):
        with self.assertRaises(receipts._Refused) as caught:
            receipts.parse_page(AGENTTALK_PAGE, 0)
        self.assertEqual(caught.exception.args[0], "bad_row")

    def test_every_documented_key_the_reader_does_not_use_is_present_and_ignored(self):
        doc = json.loads(AGENTTALK_REPORT)
        for key in ("observed_at", "child_receipts_pending", "open_child_turns", "open_child_turns_expired", "earliest_open_expiry"):
            self.assertIn(key, doc)
        self.assertEqual(sorted(doc), sorted(["child_cap_policy_hash", "child_cap_ready", "child_receipt_report_version",
                                              "child_receipts_pending", "child_receipts_through_seq", "earliest_open_expiry",
                                              "gateway_report_version", "generation", "observed_at", "open_child_turns",
                                              "open_child_turns_expired", "periods", "policy_hash", "service_hold",
                                              "service_hold_reason", "unreceipted_bound_actual", "unresolved"]))

    def test_the_documented_report_without_lease_binding_is_the_not_ready_word(self):
        doc = json.loads(AGENTTALK_REPORT)
        doc.update(child_receipt_report_version=None, child_receipts_through_seq=None, child_receipts_pending=None, unreceipted_bound_actual=[])
        refused(self, lambda: gwreport.parse_snapshot(raw(doc)), gwreport.CHILD_CAP_NOT_READY)
        doc.update(child_cap_ready=False, child_cap_policy_hash=None, open_child_turns=None, open_child_turns_expired=None,
                   earliest_open_expiry=None)
        refused(self, lambda: gwreport.parse_snapshot(raw(doc)), gwreport.CHILD_CAP_NOT_READY)


class WorkedExample(unittest.TestCase):
    """The lead's accounting example: baseline month P with 40 of spend that no lease owns, N = 0, U = 0."""

    def u(self, amount, month=P):
        return {c.KEY_UNRECEIPTED: [{c.KEY_PERIOD: month, c.KEY_UNRECEIPTED_AMOUNT: amount}]} if amount else {c.KEY_UNRECEIPTED: []}

    def committed(self, amount, **over):
        return snapshot(**dict({c.KEY_PERIODS: [{c.KEY_PERIOD: P, c.KEY_COMMITTED: amount}]}, **over))

    def test_the_baseline(self):
        fig = figures(self.committed(40))
        self.assertEqual((fig.settled, fig.unattributed, fig.reserved, fig.uncertain, fig.held_over_reservation), (40, 40, 0, 0, 0))

    def test_case_1_a_same_month_leased_turn_keeps_the_unowned_spend_at_40_at_all_three_moments(self):
        first = figures(self.committed(50, **self.u(10)))                                  # after the first call (10)
        second = figures(self.committed(56, **self.u(16)))                                 # after the second call (6)
        ended = figures(self.committed(56, **{c.KEY_THROUGH: 1, **self.u(0)}), [receipt(1, 16, P)])   # the receipt moves the 16 out of U
        self.assertEqual([f.unattributed for f in (first, second, ended)], [40, 40, 40])
        self.assertEqual([f.settled for f in (first, second, ended)], [50, 56, 56])

    def test_case_2_a_turn_spanning_two_months_rises_by_the_part_in_this_month_when_its_receipt_is_written(self):
        open_ = figures(self.committed(45, **self.u(5)))                                   # 3 in September, 5 in October; still open
        ended = figures(self.committed(45, **{c.KEY_THROUGH: 1, **self.u(0)}), [receipt(1, 8, None)])   # one receipt of 8, no month
        self.assertEqual((open_.unattributed, ended.unattributed), (40, 45))               # the agreed limit: rises by 5, once

    def test_case_3_a_receipt_with_no_charges_changes_nothing(self):
        fig = figures(self.committed(40, **{c.KEY_THROUGH: 1}), [receipt(1, 0, None, calls=0)])
        self.assertEqual(fig.unattributed, 40)

    def test_case_4_an_uncertain_attempt_with_a_cost_is_counted_once_and_sets_the_hold(self):
        over = {c.KEY_UNRESOLVED: [{c.KEY_STATE: "uncertain", c.KEY_RESERVED: 10, c.KEY_ACTUAL: 14}], c.KEY_HOLD: True,
                c.KEY_HOLD_REASON: "attempt_over_reservation", **self.u(14)}
        fig = figures(self.committed(54, **over))
        self.assertEqual((fig.unattributed, fig.uncertain, fig.reserved, fig.held_over_reservation, fig.reconciliation_required),
                         (40, 0, 0, 1, True))
        reconciled = figures(self.committed(54, **{c.KEY_THROUGH: 1, **self.u(0)}), [receipt(1, 14, P)])
        self.assertEqual((reconciled.unattributed, reconciled.held_over_reservation, reconciled.reconciliation_required),
                         (40, 0, False))

    def test_case_5_a_charge_landing_between_the_report_and_the_page_read_is_excluded_by_the_boundary(self):
        t1 = self.committed(50, **self.u(10))                                              # N = 0; the page read later returns receipt 1
        totals = gwreport.fold_page(gwreport.empty_totals(GEN), page([receipt(1, 12, P)]), t1)
        self.assertEqual(totals.read_through, 0)                                           # the receipt above N is ignored
        self.assertEqual(gwreport.covered(totals, P), 0)
        self.assertEqual(gwreport.compute(t1, totals, P).unattributed, 40)                 # not 28: the money is not subtracted twice
        t2 = self.committed(52, **{c.KEY_THROUGH: 1, **self.u(0)})                         # the next report includes it
        totals = gwreport.fold_page(totals, page([receipt(1, 12, P)]), t2)
        self.assertEqual(gwreport.compute(t2, totals, P).unattributed, 40)

    def test_what_the_reserved_and_uncertain_balances_count(self):
        rows = [{c.KEY_STATE: "reserved", c.KEY_RESERVED: 7, c.KEY_ACTUAL: None},
                {c.KEY_STATE: "reserved", c.KEY_RESERVED: 3, c.KEY_ACTUAL: None},
                {c.KEY_STATE: "uncertain", c.KEY_RESERVED: 20, c.KEY_ACTUAL: None},
                {c.KEY_STATE: "uncertain", c.KEY_RESERVED: 10, c.KEY_ACTUAL: 0}]          # an actual of 0 is a recorded cost, not unknown
        fig = figures(self.committed(40, **{c.KEY_UNRESOLVED: rows}))
        self.assertEqual((fig.reserved, fig.uncertain, fig.held_over_reservation), (10, 20, 1))


class ReceiptBoundary(unittest.TestCase):

    def snap(self, n, **over):
        return snapshot(**dict({c.KEY_THROUGH: n, c.KEY_PERIODS: [{c.KEY_PERIOD: P, c.KEY_COMMITTED: 1000}]}, **over))

    def test_receipts_above_n_are_ignored_even_when_the_page_returns_them(self):
        snap = self.snap(2)
        totals = gwreport.fold_page(gwreport.empty_totals(GEN), page([receipt(1, 10, P), receipt(2, 20, P), receipt(3, 400, P)]), snap)
        self.assertEqual((totals.read_through, gwreport.covered(totals, P)), (2, 30))
        self.assertEqual(gwreport.compute(snap, totals, P).unattributed, 1000 - 30)

    def test_another_generation_stops_the_read(self):
        snap = self.snap(1)
        refused(self, lambda: gwreport.fold_page(gwreport.empty_totals(GEN), page([receipt(1, 10, P)], GEN2), snap),
                gwreport.GENERATION_CHANGED)
        refused(self, lambda: gwreport.fold_page(gwreport.empty_totals(GEN2), page([receipt(1, 10, P)]), snap), gwreport.GENERATION_CHANGED)

    def test_a_page_read_with_the_existing_row_shape_is_refused_not_counted_as_no_month(self):
        row = {key: value for key, value in receipt(1, 5, P).items() if key != c.KEY_CHARGE_PERIOD}
        old_page = receipts.parse_page(raw(page_doc([row])), 0)
        refused(self, lambda: gwreport.fold_page(gwreport.empty_totals(GEN), old_page, self.snap(1)), gwreport.STATUS_SHAPE)

    def test_a_null_month_and_a_zero_charge_receipt_are_in_no_months_coverage(self):
        snap = self.snap(3)
        totals = gwreport.fold_page(gwreport.empty_totals(GEN),
                                    page([receipt(1, 8, None), receipt(2, 0, None, calls=0), receipt(3, 5, P)]), snap)
        self.assertEqual((gwreport.covered(totals, P), totals.no_month, totals.read_through), (5, 2, 3))

    def test_a_month_other_than_p_is_not_p_coverage(self):
        snap = self.snap(2)
        totals = gwreport.fold_page(gwreport.empty_totals(GEN), page([receipt(1, 9, EARLIER), receipt(2, 4, P)]), snap)
        self.assertEqual((gwreport.covered(totals, P), gwreport.covered(totals, EARLIER), gwreport.covered(totals, LATER)), (4, 9, 0))

    def test_pages_add_up_across_calls_and_the_input_is_never_changed(self):
        snap = self.snap(3)
        first = gwreport.fold_page(gwreport.empty_totals(GEN), page([receipt(1, 10, P)]), snap)
        before = tuple(first)
        second = gwreport.fold_page(first, page([receipt(2, 5, P), receipt(3, 1, P)], after=1), snap)
        self.assertEqual(tuple(first), before)
        self.assertEqual((second.read_through, gwreport.covered(second, P)), (3, 16))
        self.assertEqual(second.by_period, ((P, 3, 16),))

    def test_a_gap_or_a_repeat_against_the_saved_position_is_damage(self):
        snap = self.snap(5)
        totals = gwreport.fold_page(gwreport.empty_totals(GEN), page([receipt(1, 1, P), receipt(2, 1, P)]), snap)
        refused(self, lambda: gwreport.fold_page(totals, page([receipt(1, 1, P)], after=0), snap), gwreport.RECEIPTS_FAILED)    # a repeat
        refused(self, lambda: gwreport.fold_page(totals, page([receipt(4, 1, P)], after=3), snap), gwreport.RECEIPTS_FAILED)    # a gap

    def test_a_watermark_of_zero_needs_no_receipts(self):
        snap = self.snap(0)
        totals = gwreport.empty_totals(GEN)
        self.assertEqual(gwreport.compute(snap, totals, P).unattributed, 1000)

    def test_a_snapshot_with_a_lower_n_in_the_same_generation_is_a_rollback(self):
        totals = gwreport.fold_page(gwreport.empty_totals(GEN), page([receipt(1, 1, P), receipt(2, 1, P)]), self.snap(2))
        refused(self, lambda: gwreport.compute(self.snap(1), totals, P), gwreport.RECEIPTS_ROLLED_BACK)
        refused(self, lambda: gwreport.fold_page(totals, page([], after=2), self.snap(1)), gwreport.RECEIPTS_ROLLED_BACK)

    def test_computing_before_every_receipt_up_to_n_is_read_is_refused(self):
        refused(self, lambda: gwreport.compute(self.snap(2), gwreport.empty_totals(GEN), P), gwreport.RECEIPTS_FAILED)

    def test_totals_of_another_generation_are_never_used(self):
        refused(self, lambda: gwreport.compute(self.snap(0), gwreport.empty_totals(GEN2), P), gwreport.GENERATION_CHANGED)


class Figures(unittest.TestCase):

    def test_settled_is_zero_only_when_every_period_row_is_earlier(self):
        fig = figures(snapshot(**{c.KEY_PERIODS: [{c.KEY_PERIOD: EARLIER, c.KEY_COMMITTED: 900}]}))
        self.assertEqual((fig.settled, fig.unattributed), (0, 0))

    def test_a_later_period_row_is_a_clock_mismatch(self):
        snap = snapshot(**{c.KEY_PERIODS: [{c.KEY_PERIOD: P, c.KEY_COMMITTED: 1}, {c.KEY_PERIOD: LATER, c.KEY_COMMITTED: 1}]})
        refused(self, lambda: figures(snap), gwreport.PERIOD_MISMATCH)

    def test_a_negative_unattributed_is_refused_and_never_clamped(self):
        for committed, unreceipted, rows in ((10, [], [receipt(1, 11, P)]),
                                             (10, [{c.KEY_PERIOD: P, c.KEY_UNRECEIPTED_AMOUNT: 11}], []),
                                             (10, [{c.KEY_PERIOD: P, c.KEY_UNRECEIPTED_AMOUNT: 6}], [receipt(1, 5, P)])):
            with self.subTest(committed=committed):
                snap = snapshot(**{c.KEY_PERIODS: [{c.KEY_PERIOD: P, c.KEY_COMMITTED: committed}], c.KEY_UNRECEIPTED: unreceipted,
                                   c.KEY_THROUGH: len(rows)})
                refused(self, lambda snap=snap, rows=rows: figures(snap, rows), gwreport.RECEIPTS_EXCEED_SETTLED)

    def test_a_computed_zero_is_sent_as_zero_and_an_exact_match_is_not_damage(self):
        snap = snapshot(**{c.KEY_PERIODS: [{c.KEY_PERIOD: P, c.KEY_COMMITTED: 10}],
                           c.KEY_UNRECEIPTED: [{c.KEY_PERIOD: P, c.KEY_UNRECEIPTED_AMOUNT: 4}], c.KEY_THROUGH: 1})
        fig = figures(snap, [receipt(1, 6, P)])
        self.assertEqual(fig.unattributed, 0)
        self.assertIs(type(fig.unattributed), int)

    def test_a_sum_that_is_not_an_amount_is_refused_and_never_clamped(self):
        """No report can reach this (each amount is at most 10^12), so the guard is proven on a snapshot built by hand."""
        snap = snapshot()
        top = 2 ** 63 - 1
        snap.unresolved = (("reserved", top, None), ("reserved", 1, None))
        refused(self, lambda: figures(snap), gwreport.AMOUNT_INVALID)

    def test_quiet_comes_from_the_figure_being_present_not_from_a_constant(self):
        snap = snapshot()
        self.assertTrue(figures(snap).quiet)
        without = gwreport.Snapshot()
        for slot in without.__slots__:
            setattr(without, slot, getattr(snap, slot))
        without.unreceipted = None                                                          # a future version that lacks the figure
        refused(self, lambda: gwreport.compute(without, gwreport.empty_totals(GEN), P), gwreport.UNATTRIBUTED_UNAVAILABLE)

    def test_the_other_fields_come_from_the_snapshot_and_the_reader_never_claims_readiness(self):
        snap = snapshot(**{c.KEY_THROUGH: 0, c.KEY_HOLD: True, c.KEY_HOLD_REASON: "manual"})
        fig = figures(snap)
        start, end = gwreport.month_bounds(P)
        self.assertEqual((fig.period_start, fig.period_end, fig.receipts_seq, fig.generation, fig.price_hash, fig.child_cap_hash,
                          fig.sized_ready, fig.reconciliation_required), (start, end, 0, GEN, PRICE, CAP, False, True))

    def test_the_month_bounds_and_the_month_of_a_time(self):
        self.assertEqual(gwreport.month_bounds("2026-10"), (1790812800000, 1793491200000))
        self.assertEqual(gwreport.month_bounds("2026-12")[1], gwreport.month_bounds("2027-01")[0])
        self.assertEqual(gwreport.month_of(1790812800000), "2026-10")
        self.assertEqual(gwreport.month_of(1790812800000 - 1), "2026-09")
        self.assertEqual(gwreport.month_of(1793491200000), "2026-11")

    def test_a_month_that_is_not_a_month_is_refused(self):
        refused(self, lambda: gwreport.compute(snapshot(), gwreport.empty_totals(GEN), "2026-13"), gwreport.STATUS_SHAPE)

    def test_the_module_touches_no_database_clock_file_or_process(self):
        for name in ("sqlite3", "time", "os", "subprocess", "socket", "runner"):
            self.assertNotIn(name, vars(gwreport), name)

    def test_the_contract_names_live_in_one_table_that_says_where_they_come_from(self):
        for module in (gwreport, receipts):
            with open(module.__file__, encoding="utf-8") as handle:
                source = handle.read()
            for literal in ('"gateway_report_version"', '"unreceipted_bound_actual"', '"child_receipts_through_seq"', '"committed_micro_eur"',
                            '"service_hold"', '"child_receipt_report_version"', '"attempt_over_reservation"'):
                self.assertNotIn(literal, source, literal)
        self.assertEqual(receipts.ENVELOPE_VERSION, 1)
        with open(c.__file__, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("921c82d4d5b94d133dd954f223c55e663c132cc1", text)
        self.assertNotIn("guess", text.lower())
        self.assertFalse(hasattr(c, "RECEIPT_PAGE_VERSION"))


@unittest.skipUnless(WINDOWS, "the contained runner is Windows only")
class ReportMode(_Case):
    """`gateway report --json` takes no `--after` or `--limit`: the command line is exactly the configured one, which must end with the
    mode's fixed tail (`gateway report --json`)."""

    def report(self, program, *args, spy=None, **over):
        spy = spy or Spy()
        self.spies.append(spy)
        return runner.run(self.command(program, *args, *runner.REPORT_TAIL), timeout_seconds=15, max_bytes=1024 * 1024, win=spy,
                          mode=runner.MODE_REPORT, **over)

    def test_nothing_is_appended(self):
        marker = self.path("argv.json")
        awkward = ["a b", "", "tail\\", "-m", "--root", "\u00e4 \u65e5"]
        result = self.report("argv", marker, *awkward)
        self.assertEqual(result.word, runner.OK)
        self.assertEqual(json.loads(slurp(marker)), awkward + list(runner.REPORT_TAIL))   # nothing after the fixed tail
        self.assertEqual(result.data, PAGE.encode("utf-8"))

    def test_a_report_run_needs_no_numbers_and_ignores_any_it_is_given(self):
        marker = self.path("argv2.json")
        result = self.report("argv", marker, after=7, limit=9)
        self.assertEqual((result.word, json.loads(slurp(marker))), (runner.OK, list(runner.REPORT_TAIL)))

    def test_the_receipt_mode_is_unchanged_by_name_and_by_default(self):
        for explicit in (True, False):
            with self.subTest(explicit=explicit):
                marker = self.path("rec%d.json" % explicit)
                extra = {"mode": runner.MODE_RECEIPTS} if explicit else {}
                result = runner.run(self.command("argv", marker, "x"), 7, 25, timeout_seconds=15, max_bytes=1024 * 1024, **extra)
                self.assertEqual(result.word, runner.OK)
                self.assertEqual(json.loads(slurp(marker)), ["x", "--after", "7", "--limit", "25", "--json"])

    def test_an_unknown_mode_or_a_receipt_run_without_whole_numbers_starts_nothing(self):
        for kwargs, args in (({"mode": "status"}, (0, 100)), ({"mode": None}, (0, 100)), ({"mode": runner.MODE_RECEIPTS}, ()),
                             ({"mode": runner.MODE_RECEIPTS}, (7, None)), ({"mode": runner.MODE_RECEIPTS}, ("7", 100)),
                             ({"mode": runner.MODE_RECEIPTS}, (True, 100))):
            with self.subTest(kwargs=kwargs, args=args):
                spy = Spy()
                self.spies.append(spy)
                result = runner.run(self.command("page"), *args, timeout_seconds=15, max_bytes=1024, win=spy, **kwargs)
                self.assertEqual(result.word, runner.CANNOT_RUN)
                self.assertNotIn("create_process", spy.calls)

    def test_a_command_without_the_fixed_tail_starts_nothing(self):
        for tail in ((), ("gateway", "report"), ("gateway", "status", "--json"), ("report", "--json"), ("gateway", "report", "--json", "x")):
            with self.subTest(tail=tail):
                spy = Spy()
                self.spies.append(spy)
                result = runner.run(self.command("page", *tail), timeout_seconds=15, max_bytes=1024, win=spy, mode=runner.MODE_REPORT)
                self.assertEqual(result.word, runner.CANNOT_RUN)
                self.assertNotIn("create_process", spy.calls)

    def test_a_failing_report_command_is_the_existing_exit_code_word_and_keeps_nothing(self):
        result = self.report("exit1")
        self.assertEqual((result.word, result.data), (runner.EXIT_CODE, None))

    def test_a_report_run_is_contained_like_a_receipt_run(self):
        spy = Spy()
        self.assertEqual(self.report("page", spy=spy).word, runner.OK)
        self.assertLess(spy.calls.index("update_attribute"), spy.calls.index("create_process"))
        self.assert_clean(spy)
        result = runner.run(self.command("big", "2000", *runner.REPORT_TAIL), timeout_seconds=15, max_bytes=100, mode=runner.MODE_REPORT)
        self.assertEqual(result.word, runner.TOO_LARGE)                               # the byte limit is the same bounded read


if __name__ == "__main__":
    unittest.main()
