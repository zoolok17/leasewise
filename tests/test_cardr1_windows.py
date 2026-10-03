"""Card R1, step R1-1: honest window checks and warn mode (DESIGN-R1 12.1, 12.2).

A grant decides from everything it knows about a usage window, not only the last number:

  evidence                                   subscription_admission     subscription_warn
  known, current, below the ceiling          admit                      admit
  known, at or above the ceiling             refuse WINDOW_LIMIT        refuse WINDOW_LIMIT
  known, below, not current                  admit + METER_STALE        admit + METER_STALE
  explicitly unknown (NULL)                  refuse WINDOW_LIMIT        admit + WINDOW_LIMIT warning
  never observed (no row)                    admit + METER_STALE        admit + METER_STALE

The comparison is inclusive (`used >= ceiling` refuses). Freshness is measured by the observation time: 600 s is
current, 600 s + 1 ms is not, a reset that has passed with no new observation is not current, and an observation up
to 30 s in the future counts as age 0. The answer carries `warnings` and `windows`, built from the same read as the
decision; a replay shows the CURRENT annotations but never re-runs admission. Synthetic data only.
"""

import json
import os
import sqlite3
import tempfile
import unittest
import uuid

from quota_balancer import errors, lease

from tests import test_card4_seat_kind_fix as fx
from tests.test_card4_seat_kind_fix import ACCOUNT, NOW, PROJECT, SEAT, _build_store, _grant_request

MODES = ("subscription_admission", "subscription_warn")
KEYS = {"window_key", "used_bps", "resets_at", "confidence", "source_stale", "age_ms", "current"}


class _Case(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-r1w-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name

    def store(self, mode="subscription_admission", kind="subscription"):
        self.db = _build_store(self.tmp, kind, mode)
        self.tmp = tempfile.mkdtemp(dir=self._tmp.name)           # a further store in the same case gets its own folder
        return self.db

    def sql(self, text, params=()):
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(text, params).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def limit(self, hard=4000, key="wk", scope="project", scope_id=PROJECT):
        self.sql("INSERT INTO limits(id, scope, scope_id, metric, period, window_key, soft, hard) VALUES(?,?,?,?,?,?,?,?)",
                 (str(uuid.uuid4()), scope, scope_id, "window_bps", "window", key, None, hard))

    def window(self, used, observed=NOW, resets=NOW + 3_600_000, key="wk", confidence="observed", stale=0):
        self.sql("INSERT OR REPLACE INTO windows(account_id, window_key, used_bps, resets_at, observed_at, confidence,"
                 " source_stale, covered_seq, source_event_id) VALUES(?,?,?,?,?,?,?,?,?)",
                 (ACCOUNT, key, used, resets, observed, confidence, stale, 1, "ev-secret-source"))

    def grant(self, now=NOW, **over):
        request = _grant_request()
        request.update(over)
        return lease.grant(self.db, request, now)

    def refused(self, code, now=NOW):
        with self.assertRaises(errors.QuotaError) as caught:
            self.grant(now)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM leases"), [(0,)])

    def warned(self, made):
        return sorted((w["code"], w["window_key"]) for w in made["warnings"])


class TheTable(_Case):

    def each_mode(self, check):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.setUp()
                self.store(mode)
                self.limit()
                check(mode)

    def test_known_current_and_below_the_ceiling_is_admitted_without_a_warning(self):
        def check(mode):
            self.window(3999)
            made = self.grant()
            self.assertEqual(made["warnings"], [])
            self.assertEqual(made["windows"], [{"window_key": "wk", "used_bps": 3999, "resets_at": NOW + 3_600_000,
                                                "confidence": "observed", "source_stale": False, "age_ms": 0,
                                                "current": True}])
        self.each_mode(check)

    def test_known_at_the_ceiling_refuses_and_so_does_known_above_it(self):
        def check(mode):
            for used in (4000, 4001, 10000):
                self.sql("DELETE FROM windows")
                self.window(used)
                self.refused(errors.CODE_WINDOW_LIMIT)
        self.each_mode(check)

    def test_known_at_the_ceiling_refuses_even_when_the_reading_is_old(self):
        def check(mode):
            self.window(4000, observed=NOW - 3_000_000)
            self.refused(errors.CODE_WINDOW_LIMIT)
        self.each_mode(check)

    def test_exhaustion_is_not_forgotten_after_the_reset_time_passes(self):
        """Time alone proves no recovery: a new epoch needs a new observation."""
        def check(mode):
            self.window(5000, observed=NOW - 120_000, resets=NOW - 60_000)
            self.refused(errors.CODE_WINDOW_LIMIT)
        self.each_mode(check)

    def test_known_below_the_ceiling_but_old_is_admitted_with_a_stale_warning(self):
        def check(mode):
            self.window(100, observed=NOW - 700_000)
            made = self.grant()
            self.assertEqual(self.warned(made), [("METER_STALE", "wk")])
            self.assertEqual((made["windows"][0]["confidence"], made["windows"][0]["current"]), ("observed", False))
        self.each_mode(check)

    def test_an_explicit_unknown_refuses_an_admission_seat_and_warns_a_warn_seat(self):
        self.store("subscription_admission")
        self.limit()
        self.window(None, confidence="unknown")
        self.refused(errors.CODE_WINDOW_LIMIT)
        self.setUp()
        self.store("subscription_warn")
        self.limit()
        self.window(None, confidence="unknown")
        made = self.grant()
        self.assertEqual(self.warned(made), [("WINDOW_LIMIT", "wk")])
        self.assertEqual((made["windows"][0]["confidence"], made["windows"][0]["used_bps"]), ("unknown", None))

    def test_a_window_never_observed_is_admitted_with_a_stale_warning_and_is_never_an_observed_zero(self):
        def check(mode):
            made = self.grant()
            self.assertEqual(self.warned(made), [("METER_STALE", "wk")])
            record = made["windows"][0]
            self.assertEqual((record["confidence"], record["used_bps"], record["current"]), ("missing", None, False))
            self.assertIsNone(record["age_ms"])
        self.each_mode(check)

    def test_a_shadow_request_is_never_refused_by_a_window(self):
        self.store("shadow")
        self.limit()
        self.window(9000)
        made = self.grant()
        self.assertEqual((made["enforcement"], made["warnings"]), ("shadow", []))

    def test_a_metered_advisory_lease_follows_the_admission_rules(self):
        """A window limit applies to a metered seat's advisory lease too, and it is treated like admission mode."""
        self.store("metered", kind="metered")
        self.limit()
        self.window(4000)
        with self.assertRaises(errors.QuotaError) as caught:
            self.grant(gateway_id=fx.GATEWAY)
        self.assertEqual(caught.exception.code, errors.CODE_WINDOW_LIMIT)
        self.window(None, confidence="unknown")
        with self.assertRaises(errors.QuotaError) as caught:
            self.grant(gateway_id=fx.GATEWAY)
        self.assertEqual(caught.exception.code, errors.CODE_WINDOW_LIMIT)
        self.sql("DELETE FROM windows")
        made = self.grant(gateway_id=fx.GATEWAY)
        self.assertEqual((made["enforcement"], self.warned(made)), ("metered_advisory", [("METER_STALE", "wk")]))


class Freshness(_Case):

    def test_600_seconds_is_current_and_600_seconds_and_a_millisecond_is_not(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.setUp()
                self.store(mode)
                self.limit()
                self.window(100, observed=NOW - 600_000)
                made = self.grant()
                self.assertEqual((made["warnings"], made["windows"][0]["current"], made["windows"][0]["age_ms"]),
                                 ([], True, 600_000))
                self.window(100, observed=NOW - 600_001)
                self.sql("DELETE FROM leases")
                made = self.grant()
                self.assertEqual((self.warned(made), made["windows"][0]["current"]), ([("METER_STALE", "wk")], False))

    def test_a_recent_sample_whose_reset_has_passed_is_not_current(self):
        self.store()
        self.limit()
        self.window(100, observed=NOW - 1000, resets=NOW - 1)
        made = self.grant()
        self.assertEqual((self.warned(made), made["windows"][0]["current"]), ([("METER_STALE", "wk")], False))

    def test_a_sample_taken_after_the_reset_is_current_even_though_the_reset_time_has_passed(self):
        self.store()
        self.limit()
        self.window(100, observed=NOW - 1000, resets=NOW - 5000)
        made = self.grant()
        self.assertEqual((made["warnings"], made["windows"][0]["current"]), ([], True))

    def test_an_observation_up_to_30_seconds_in_the_future_counts_as_age_zero(self):
        self.store()
        self.limit()
        self.window(100, observed=NOW + 30_000)
        made = self.grant()
        self.assertEqual((made["warnings"], made["windows"][0]["age_ms"], made["windows"][0]["current"]),
                         ([], 0, True))

    def test_an_observation_further_in_the_future_is_not_trusted_as_current(self):
        self.store()
        self.limit()
        self.window(100, observed=NOW + 30_001)
        made = self.grant()
        self.assertEqual((self.warned(made), made["windows"][0]["current"]), ([("METER_STALE", "wk")], False))


class Refusals(_Case):

    def test_a_parked_block_refuses_in_both_modes(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.setUp()
                self.store(mode)
                self.limit()
                self.window(100)
                self.sql("INSERT INTO blocks(id, account_id, model_id, reason, retry_at, state, source_event_id,"
                         " updated_at) VALUES(?,?,?,?,?,?,?,?)",
                         (str(uuid.uuid4()), ACCOUNT, fx.MODEL, "codex_exhausted", NOW + 60_000, "parked", "ev-b", NOW))
                self.refused(errors.CODE_ACCOUNT_PARKED)

    def test_the_concurrency_limit_refuses_in_both_modes(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.setUp()
                self.store(mode)
                self.sql("INSERT INTO limits(id, scope, scope_id, metric, period, window_key, soft, hard)"
                         " VALUES(?,?,?,?,?,?,?,?)",
                         (str(uuid.uuid4()), "seat", SEAT, "concurrency", "turn", None, None, 1))
                self.grant()
                with self.assertRaises(errors.QuotaError) as caught:
                    self.grant()
                self.assertEqual(caught.exception.code, errors.CODE_CONCURRENCY_LIMIT)


class Annotations(_Case):

    def test_two_limits_on_one_window_give_one_record_and_one_warning(self):
        self.store()
        self.limit(hard=4000)
        self.limit(hard=3500, scope="seat", scope_id=SEAT)
        self.window(100, observed=NOW - 700_000)
        made = self.grant()
        self.assertEqual(len(made["windows"]), 1)
        self.assertEqual(made["warnings"], [{"code": "METER_STALE", "window_key": "wk"}])

    def test_the_tightest_ceiling_of_any_applicable_limit_decides(self):
        self.store()
        self.limit(hard=4000)
        self.limit(hard=3000, scope="seat", scope_id=SEAT)
        self.window(3000)
        self.refused(errors.CODE_WINDOW_LIMIT)

    def test_several_windows_are_listed_by_key_each_with_its_own_warning(self):
        self.store()
        self.limit(key="a")
        self.limit(key="b")
        self.window(10, key="b", observed=NOW - 900_000)
        self.window(10, key="a")
        made = self.grant()
        self.assertEqual([w["window_key"] for w in made["windows"]], ["a", "b"])
        self.assertEqual(self.warned(made), [("METER_STALE", "b")])

    def test_a_record_has_exactly_the_closed_keys_and_no_source_identity(self):
        self.store()
        self.limit()
        self.window(10)
        made = self.grant()
        self.assertEqual(set(made["windows"][0]), KEYS)
        shown = json.dumps(made)
        for private in ("ev-secret-source", ACCOUNT, "covered_seq", "source_event_id"):
            self.assertNotIn(private, shown)

    def test_a_lease_with_no_window_limit_has_empty_lists(self):
        self.store()
        made = self.grant()
        self.assertEqual((made["windows"], made["warnings"]), ([], []))

    def test_the_codes_are_only_the_two_closed_words(self):
        self.store("subscription_warn")
        self.limit(key="a")
        self.limit(key="b")
        self.limit(key="c")
        self.window(None, key="a", confidence="unknown")
        self.window(10, key="b", observed=NOW - 900_000)
        made = self.grant()
        self.assertEqual(self.warned(made), [("METER_STALE", "b"), ("METER_STALE", "c"), ("WINDOW_LIMIT", "a")])
        self.assertTrue({w["code"] for w in made["warnings"]} <= {"WINDOW_LIMIT", "METER_STALE"})


class Replay(_Case):

    def test_a_replay_shows_the_current_annotations_but_never_reruns_admission(self):
        self.store()
        self.limit()
        self.window(100)
        request = _grant_request()
        first = lease.grant(self.db, dict(request), NOW)
        self.assertEqual(first["warnings"], [])
        before = self.sql("SELECT * FROM leases")
        self.window(9999, observed=NOW + 5000)                       # the window is now used up
        again = lease.grant(self.db, dict(request), NOW + 10_000)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["lease_id"], first["lease_id"])
        self.assertEqual(again["expires_at"], first["expires_at"])
        self.assertEqual(self.warned(again), [("WINDOW_LIMIT", "wk")])
        self.assertEqual(again["windows"][0]["used_bps"], 9999)
        self.assertEqual(self.sql("SELECT * FROM leases"), before)          # the stored grant is untouched

    def test_a_replay_is_never_refused_because_the_state_changed(self):
        self.store("subscription_admission")
        self.limit()
        self.window(100)
        request = _grant_request()
        lease.grant(self.db, dict(request), NOW)
        self.window(None, confidence="unknown")
        again = lease.grant(self.db, dict(request), NOW + 1000)
        self.assertEqual((again["replayed"], self.warned(again)), (True, [("WINDOW_LIMIT", "wk")]))
        self.sql("DELETE FROM windows")
        self.assertEqual(self.warned(lease.grant(self.db, dict(request), NOW + 2000)), [("METER_STALE", "wk")])

    def test_a_shadow_replay_has_empty_lists(self):
        self.store("shadow")
        self.limit()
        request = _grant_request()
        lease.grant(self.db, dict(request), NOW)
        again = lease.grant(self.db, dict(request), NOW + 1)
        self.assertEqual((again["windows"], again["warnings"]), ([], []))


if __name__ == "__main__":
    unittest.main()
