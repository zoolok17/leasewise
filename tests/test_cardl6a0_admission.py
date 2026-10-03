"""Card L6a-0, Codex finding F1: unknown input usage must stay visible to the NEXT quota decision.

Accepting a final receipt with unknown input tokens (test_cardl6a0_null_counts.py) is not enough. The
admission query adds up `input_tokens` over final receipts and a SUM skips nulls, a settled lease's hold no
longer counts, and an advisory lease has no hold at all. So that usage would vanish from the next decision.

The rule (the lead's decision, Codex's smallest conservative option): for every applicable scope and grant
day or month window, if any final receipt of a lease that is not shadow has UNKNOWN input tokens, input
admission for that scope and window is refused with `INPUT_USAGE_UNKNOWN` until the window rolls over. A
receipt with KNOWN input and unknown output keeps its known input in the sum and does not trigger it.
Every test settles first and then asks for the next lease. Synthetic data only.
"""

import time
import unittest
import uuid
from unittest import mock

from quota_balancer import errors, http_api, lease
from quota_balancer.errors import QuotaError
from quota_balancer.reporter import decisions

from tests.test_cardl1a import _Base, _grant_request, _ids, _sized_snapshot, base_doc

OWN = "g1"
DAY_MS = 86_400_000
TEAM = {"project": "org", "team": "dev"}


def _limit(scope, period, hard, **who):
    row = {"scope": scope, "metric": "input_tokens", "period": period, "window_key": None, "soft": None,
           "hard": hard}
    row.update(who)
    return row


class _Next(_Base):
    """A store with the input limits of the subclass, and helpers for settle-then-acquire."""

    limits = ()

    def setUp(self):
        super().setUp()
        doc = base_doc()
        doc["limits"] = [row for row in doc["limits"] if row["metric"] != "input_tokens"] + list(self.limits)
        self.import_ok(doc)
        self.base_ms = int(time.time() * 1000)

    def body(self, seat, at=0):
        body = _grant_request(_ids(self), seat, OWN, self.base_ms + at)
        if seat == "s2":
            body["gateway_id"] = None                              # the shadow seat has no gateway
        return body

    def lease_at(self, seat="s1", at=0):
        return lease.grant(self.db, self.body(seat, at), self.base_ms + at)

    def settle(self, made, at=0, **over):
        now = self.base_ms + at
        doc = {"event_id": str(uuid.uuid4()), "lease_id": made["lease_id"], "outcome": "completed", "calls": 1,
               "input_tokens": None, "cached_input_tokens": 0, "output_tokens": 40, "actual_micro_eur": 5,
               "gateway_id": _ids(self)["gateways"][OWN], "seq": 1, "final": 1, "observed_at": now,
               "received_at": now}
        doc.update(over)
        lease.settle(self.db, doc, now)
        return doc

    def refused(self, seat="s1", at=0):
        with self.assertRaises(QuotaError) as caught:
            self.lease_at(seat, at)
        self.assertEqual(caught.exception.code, errors.CODE_INPUT_USAGE_UNKNOWN)
        self.assertEqual(caught.exception.message, errors.MSG_INPUT_USAGE_UNKNOWN)

    def admitted(self, seat="s1", at=0):
        made = self.lease_at(seat, at)
        self.assertEqual(made["state"], "active")
        return made


class Month(_Next):
    limits = (_limit("seat", "month", 5_000_000, machine="desktop", seat="s1"),
              _limit("seat", "month", 5_000_000, machine="desktop", seat="s3"))

    def test_an_advisory_lease_with_unknown_input_blocks_the_next_input_admission_of_its_scope(self):
        made = self.lease_at("s1")
        self.assertEqual(made["enforcement"], "metered_advisory")
        self.assertIsNone(made["money_hold"])                       # no hold at all: a hold could not carry it
        self.settle(self.admitted("s1"))                            # (admitted before: nothing unknown yet)
        self.refused("s1")

    def test_a_metered_lease_with_holds_blocks_it_too(self):
        _sized_snapshot(self)
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            made = self.lease_at("s3")
        self.assertEqual(made["enforcement"], "metered")
        self.settle(made)
        self.refused("s3")                                          # the settled hold no longer reaches admission

    def test_a_scope_without_unknown_usage_is_not_affected(self):
        self.settle(self.lease_at("s1"))
        self.refused("s1")
        self.assertEqual(self.admitted("s3")["state"], "active")    # its own seat limit has no unknown usage

    def test_a_replay_of_the_receipt_and_of_an_earlier_grant_changes_nothing(self):
        body = self.body("s1")
        made = lease.grant(self.db, body, self.base_ms)
        doc = self.settle(made)
        lease.settle(self.db, dict(doc), self.base_ms + 5)          # the same receipt again: a replay
        self.refused("s1")
        again = lease.grant(self.db, body, self.base_ms)            # the same request again: a replay, not re-admitted
        self.assertEqual((again["lease_id"], again["replayed"]), (made["lease_id"], True))
        self.refused("s1")

    def test_known_input_with_unknown_output_keeps_its_input_and_does_not_trigger_the_rule(self):
        first = self.lease_at("s1")
        self.settle(first, input_tokens=3_000_000, output_tokens=None)
        second = self.admitted("s1")                                # admitted: no unknown INPUT in this window
        self.settle(second, input_tokens=3_000_000, output_tokens=None)
        with self.assertRaises(QuotaError) as caught:               # 6,000,000 known is over 5,000,000
            self.lease_at("s1")
        self.assertEqual(caught.exception.code, errors.CODE_INPUT_LIMIT)

    def test_a_shadow_lease_is_still_admitted_and_its_unknown_usage_never_counts(self):
        self.settle(self.lease_at("s1"))
        self.refused("s1")
        shadow = self.lease_at("s2")
        self.assertEqual(shadow["enforcement"], "shadow")
        now = self.base_ms
        lease.settle(self.db, {"event_id": str(uuid.uuid4()), "lease_id": shadow["lease_id"],
                               "outcome": "completed", "calls": 1, "input_tokens": None,
                               "cached_input_tokens": None, "output_tokens": None, "actual_micro_eur": None,
                               "gateway_id": None, "seq": 1, "final": 1, "observed_at": now,
                               "received_at": now}, now)
        self.assertEqual(self.admitted("s3")["state"], "active")    # a shadow receipt never blocks anyone

    def test_the_month_window_rolls_over(self):
        self.settle(self.lease_at("s1"))
        self.refused("s1", at=0)
        self.refused("s1", at=5 * 60_000)                           # the same month, a little later
        self.assertEqual(self.admitted("s1", at=62 * DAY_MS)["state"], "active")      # a later month

    def test_the_refusal_has_its_own_word_status_and_exit_code(self):
        self.assertEqual(errors.CODE_INPUT_USAGE_UNKNOWN, "INPUT_USAGE_UNKNOWN")
        self.assertEqual(http_api._HTTP_STATUS[errors.CODE_INPUT_USAGE_UNKNOWN], 429)
        self.assertEqual(errors.EXIT_CODES[errors.CODE_INPUT_USAGE_UNKNOWN], 2)
        self.assertIn("INPUT_USAGE_UNKNOWN", decisions.CLOSED_CODES)            # the reporter shows it as a closed word


class Wider(_Next):
    limits = (_limit("team", "month", 9_000_000, **TEAM),)

    def test_a_team_limit_makes_every_seat_of_the_team_wait(self):
        self.settle(self.lease_at("s1"))
        self.refused("s1")
        self.refused("s3")

    def shadow_receipt_with_unknown_usage(self):
        shadow = self.lease_at("s2")
        self.assertEqual(shadow["enforcement"], "shadow")
        now = self.base_ms
        lease.settle(self.db, {"event_id": str(uuid.uuid4()), "lease_id": shadow["lease_id"],
                               "outcome": "completed", "calls": 1, "input_tokens": None,
                               "cached_input_tokens": None, "output_tokens": None, "actual_micro_eur": None,
                               "gateway_id": None, "seq": 1, "final": 1, "observed_at": now,
                               "received_at": now}, now)

    def test_a_shadow_receipt_with_unknown_usage_blocks_nobody_under_the_same_team_limit(self):
        self.shadow_receipt_with_unknown_usage()
        self.assertEqual(self.admitted("s1")["state"], "active")

    def test_a_shadow_request_is_admitted_while_the_team_waits(self):
        self.settle(self.lease_at("s1"))
        self.refused("s3")
        self.assertEqual(self.lease_at("s2")["enforcement"], "shadow")      # observe-only: never refused for usage


class Platform(_Next):
    limits = (_limit("platform", "month", 9_000_000),)

    def test_a_platform_limit_makes_every_seat_wait(self):
        self.settle(self.lease_at("s1"))
        self.refused("s3")


class Day(_Next):
    limits = (_limit("team", "day", 9_000_000, **TEAM),)

    def test_the_day_window_rolls_over(self):
        self.settle(self.lease_at("s1"))
        self.refused("s3")
        self.assertEqual(self.admitted("s3", at=DAY_MS)["state"], "active")      # the next grant day


class NoLimit(_Next):
    limits = ()

    def test_with_no_input_limit_nothing_changes(self):
        self.settle(self.lease_at("s1"))
        self.assertEqual(self.admitted("s1")["state"], "active")


if __name__ == "__main__":
    unittest.main()
