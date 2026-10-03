"""Card L6a-2, step 1: `reporter admit` against a scripted fake Leasewise (DESIGN-6A 8.6, 8.7; neutral spec 4.2, 6).

What is proved: the same answer for the same message; the request and every body saved BEFORE they are sent; at most
3 rebuilds; the winning grant found by replaying saved bodies when a slow answer lands late; a stop at every hand-off
loses nothing; lost standard output is harmless; every `held` word and its back-off; `not_configured` and
`context_unprovable` ask Leasewise nothing; the one 20-second deadline and the progress that survives it.
Synthetic data only: generated ids, made-up bounds, fake answers.
"""

import hashlib
import json
import os
import sqlite3
import time
import unittest
from unittest import mock

from quota_balancer.reporter import admission_lock as lock
from quota_balancer.reporter import admission_store as store
from quota_balancer.reporter import admit, mapper
from quota_balancer.reporter import admit_wire as wire

from tests.l6a1_env import receipt
from tests.l6a2_env import Rigged, Script, FakeClock, error, request_bytes
from tests.reporter_env import Crash

STALE = error("ESTIMATE_STALE", 409)
CONFLICT = error("IDEMPOTENCY_CONFLICT", 409)


def crash_at(stage):
    def fault(name):
        if name == stage:
            raise Crash()
    return fault


class _Case(unittest.TestCase):
    def setUp(self):
        self.rg = Rigged()
        self.addCleanup(self.rg.cleanup)
        self.clock = FakeClock()

    def script(self, *answers, **kw):
        one = Script(*answers, **kw)
        one.now_ms = self.clock.now_ms
        return one

    def admit(self, script, raw=None, **seams):
        code, out, err = self.rg.run(raw or request_bytes(), script, **self.clock.seams(**seams))
        return code, out, err

    def answer(self, script, raw=None, **seams):
        code, out, err = self.admit(script, raw, **seams)
        self.assertEqual((code, err), (0, ""))
        return out

    def held(self, out):
        doc = wire.parse_answer(out)
        self.assertEqual(doc["status"], "held")
        return doc["reason"]


class Basics(_Case):

    def test_the_first_admit_answers_admitted_with_null_caps_and_the_lease_id_as_reference(self):
        script = self.script("GRANT")
        code, out, err = self.admit(script)
        self.assertEqual((code, err), (0, ""))
        doc = wire.parse_answer(out)
        lease_id = script.granted[script.calls[0][0]][0]
        self.assertEqual(doc, {"max_calls": None, "max_micro_eur": None, "reference": lease_id,
                               "status": "admitted", "ttl_seconds": 1800, "v": 1})
        self.assertEqual(len(doc["reference"]), 36)
        self.assertNotIn(b"\n", out)                                   # exactly the answer, nothing after it
        self.assertEqual(out, json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("ascii"))

    def test_the_same_message_always_gets_the_same_answer_and_leasewise_is_asked_once(self):
        script = self.script("GRANT")
        first = self.answer(script)
        for _ in range(3):
            self.assertEqual(self.answer(script), first)
        self.assertEqual(len(script.calls), 1)
        self.assertEqual(self.rg.row()[0], "granted")

    def test_a_different_message_is_a_different_admission(self):
        script = self.script("GRANT")
        one = self.answer(script, request_bytes(message_id="m-1"))
        two = self.answer(script, request_bytes(message_id="m-2"))
        self.assertNotEqual(wire.parse_answer(one)["reference"], wire.parse_answer(two)["reference"])
        self.assertEqual(len(self.rg.sql("SELECT 1 FROM admission")), 2)

    def test_the_request_is_saved_before_it_is_sent_and_the_body_sent_is_the_body_stored(self):
        seen = []

        def look(index, key, body):
            seen.append(self.rg.sql("SELECT state, body_current, request_id FROM admission")
                        + self.rg.sql("SELECT body FROM admission_attempt"))
        script = self.script("GRANT", on_call=look)
        self.answer(script)
        state, stored, request_id = seen[0][0]
        self.assertEqual((state, request_id), ("requested", script.calls[0][0]))      # saved, not granted, at send time
        self.assertEqual(stored.encode("utf-8"), script.calls[0][1])                  # the very bytes sent
        self.assertEqual(seen[0][1:], [(stored,)])

    def test_the_body_follows_the_spec_for_a_fresh_turn(self):
        script = self.script("GRANT")
        self.answer(script, request_bytes(parent="", mode="fresh", dispatch=777))
        body = json.loads(script.calls[0][1])
        agent = self.rg.rig.agents_cfg["alpha"]
        self.assertEqual(sorted(body), sorted(["request_id", "turn_id", "seat_id", "task_class", "model_id", "effort",
                                               "gateway_id", "parent_lease_id", "context", "expected_calls",
                                               "max_calls", "output_cap", "per_call_output_cap", "ttl_ms"]))
        self.assertEqual(body["context"], {"mode": "FRESH", "initial_tokens": 1000, "dispatch_tokens": 777,
                                           "source": "conservative_bound", "observed_at": self.clock.ms,
                                           "growth_tokens_per_call": 10})
        self.assertEqual((body["parent_lease_id"], body["task_class"], body["effort"], body["ttl_ms"]),
                         (None, agent["task_class"], agent["effort"], agent["ttl_ms"]))
        self.assertEqual(body["gateway_id"], "7b1e3c52-0d4a-4a8e-9c61-5f2a8d3b9e01")
        self.assertEqual(script.calls[0][1], json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    def test_a_resume_turn_uses_the_resume_bound_and_never_unknown_or_summary(self):
        script = self.script("GRANT")
        self.answer(script, request_bytes(mode="resume", dispatch=0))
        context = json.loads(script.calls[0][1])["context"]
        self.assertEqual((context["mode"], context["initial_tokens"], context["dispatch_tokens"]), ("RESUME", 5000, 0))

    def test_a_parent_request_id_is_checked_but_never_sent(self):
        script = self.script("GRANT")
        self.answer(script, request_bytes(parent="parent-req-7"))
        self.assertNotIn(b"parent-req-7", script.calls[0][1])

    def test_the_reference_hash_is_the_sha256_of_its_ascii_bytes_on_both_sides(self):
        script = self.script("GRANT")
        reference = wire.parse_answer(self.answer(script))["reference"]
        row = self.rg.row()
        expected = hashlib.sha256(reference.encode("ascii")).hexdigest()
        self.assertEqual((row[6], row[7]), (reference, expected))
        self.assertEqual(mapper.lease_ref_hash(reference), expected)
        self.assertEqual(receipt(reference)["quota_lease_ref_sha256"], expected)       # what the gateway writes
        status, found = store.read_bindings(self.rg.store)                             # what the reporter reads back
        self.assertEqual((status, [b.ref_sha256 for b in found], [b.lease_id for b in found]),
                         (store.OK, [expected], [reference]))

    def test_a_metered_enforcing_lease_would_carry_its_numeric_caps(self):
        doc = {"lease_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "state": "active", "enforcement": "metered",
               "money_hold": 12345, "caps": {"max_calls": 7}, "expires_at": self.clock.ms + 600_000}
        out = self.answer(self.script((201, doc)))
        parsed = wire.parse_answer(out)
        self.assertEqual((parsed["max_calls"], parsed["max_micro_eur"], parsed["ttl_seconds"]), (7, 12345, 600))

    def test_a_lease_that_is_not_paid_is_refused_and_no_grant_is_saved(self):
        out = self.answer(self.script("GRANT", enforcement="shadow"))
        self.assertEqual(self.held(out), "refused")
        self.assertEqual(self.rg.row()[0], "requested")                                # nothing is bound to a receipt

    def test_a_lease_with_no_time_left_is_held_but_its_grant_is_kept_for_the_receipt(self):
        script = self.script("GRANT", expires_in_ms=500)
        self.assertEqual(self.held(self.answer(script)), "refused")
        self.assertEqual(self.rg.row()[0], "granted")                                  # the binding must exist
        self.assertEqual(self.held(self.answer(script)), "refused")
        self.assertEqual(len(script.calls), 1)

    def test_the_time_left_is_bounded_to_a_day(self):
        script = self.script("GRANT", expires_in_ms=3 * 86_400_000)
        self.assertEqual(wire.parse_answer(self.answer(script))["ttl_seconds"], 86_400)


class Rebuilds(_Case):

    def test_a_stale_estimate_is_rebuilt_with_a_fresh_time_and_saved_before_it_is_sent(self):
        seen = []

        def look(index, key, body):
            if index == 0:
                self.clock.ms += 130_000                                # the estimate is old by the time it is judged
            seen.append((self.rg.sql("SELECT body_current, rebuilds FROM admission")[0],
                         [row[0] for row in self.rg.sql("SELECT body FROM admission_attempt ORDER BY id")]))
        script = self.script(STALE, "GRANT", on_call=look)
        self.assertEqual(wire.parse_answer(self.answer(script))["status"], "admitted")
        first, second = [json.loads(call[1]) for call in script.calls]
        self.assertEqual(script.calls[0][0], script.calls[1][0])                       # the same request id
        self.assertEqual({k: v for k, v in first.items() if k != "context"},
                         {k: v for k, v in second.items() if k != "context"})
        self.assertGreater(second["context"]["observed_at"], first["context"]["observed_at"])
        stored_at_second_send = seen[1]
        self.assertEqual(stored_at_second_send[0][0].encode("utf-8"), script.calls[1][1])
        self.assertEqual(stored_at_second_send[0][1], 1)
        self.assertEqual(len(stored_at_second_send[1]), 2)                             # both bodies are kept

    def test_three_rebuilds_are_allowed(self):
        script = self.script(STALE, STALE, STALE, "GRANT")
        self.assertEqual(wire.parse_answer(self.answer(script))["status"], "admitted")
        self.assertEqual(len(script.calls), 4)
        self.assertEqual(self.rg.sql("SELECT rebuilds FROM admission"), [(3,)])

    def test_a_fourth_stale_answer_is_a_refusal_and_the_next_episode_starts_afresh(self):
        script = self.script(STALE)
        out = self.answer(script)
        self.assertEqual(self.held(out), "refused")
        self.assertEqual(len(script.calls), 4)                                         # the first body and three rebuilds
        row = self.rg.row()
        self.assertEqual((row[0], row[1], row[3]), ("requested", 0, "ESTIMATE_STALE"))
        self.assertEqual(row[4], self.clock.ms + admit.BACKOFF_MS["refused"])


class Late(_Case):
    """A slow first POST that lands after a rebuild: the winning grant is found by replaying the saved bodies."""

    def test_the_winning_grant_is_found_by_replay_and_nothing_is_overwritten(self):
        landed = {}

        def late(index, key, body):
            if index == 2:                      # while the rebuilt body is on its way, the very first body lands
                lease_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
                landed["lease"] = lease_id
                script.granted[key] = (lease_id, landed["first"])
            if index == 0:
                landed["first"] = body
        script = self.script(Exception("no answer"), STALE, "GRANT", "GRANT", on_call=late)
        first = self.answer(script)
        self.assertEqual(self.held(first), "coordinator_unreachable")                  # run 1: the answer never came
        self.clock.advance(130)                                                        # back-off over, estimate old
        out = self.answer(script)
        doc = wire.parse_answer(out)
        self.assertEqual((doc["status"], doc["reference"]), ("admitted", landed["lease"]))
        bodies = [call[1] for call in script.calls]
        self.assertEqual(bodies[0], bodies[1])                                         # the stored bytes are sent first
        self.assertNotEqual(bodies[2], bodies[0])                                      # then the rebuilt body (a conflict)
        self.assertEqual(bodies[3], bodies[0])                                         # then the earlier body is replayed
        self.assertEqual(len({call[0] for call in script.calls}), 1)
        row = self.rg.row()
        self.assertEqual((row[0], row[5]), ("granted", landed["lease"]))
        self.assertEqual(len(script.granted), 1)                                       # one lease ever existed
        self.assertEqual(self.answer(script), out)                                     # and it is the answer from now on

    def test_when_no_saved_body_replays_nothing_is_guessed(self):
        script = self.script(STALE, CONFLICT, CONFLICT)
        out = self.answer(script)
        self.assertEqual(self.held(out), "refused")
        self.assertEqual(len(script.calls), 3)                                         # first, rebuilt, one replay
        self.assertEqual(self.rg.row()[0], "requested")
        self.assertEqual(self.rg.row()[3], "IDEMPOTENCY_CONFLICT")
        before = len(script.calls)
        self.assertEqual(self.held(self.answer(script)), "refused")                    # still backing off: no request
        self.assertEqual(len(script.calls), before)


class Stops(_Case):
    """A stop at every hand-off loses nothing and creates one lease only. The stop is a BaseException, so nothing in
    the program can swallow it, as a real crash cannot be caught."""

    def stop(self, stage, script):
        with self.assertRaises(Crash):
            self.rg.run(request_bytes(), script, **self.clock.seams(fault=crash_at(stage)))

    def finish(self, script):
        out = self.answer(script)
        self.assertEqual(wire.parse_answer(out)["status"], "admitted")
        self.assertEqual(len(script.granted), 1)                                       # one lease, however often it ran
        self.assertEqual(self.rg.row()[0], "granted")
        return out

    def test_a_stop_after_the_request_is_saved_and_before_it_is_sent_sends_the_stored_bytes(self):
        script = self.script("GRANT")
        self.stop("request_saved", script)
        self.assertEqual(script.calls, [])
        saved = self.rg.sql("SELECT body_current FROM admission")[0][0]
        self.finish(script)
        self.assertEqual(script.calls[0][1], saved.encode("utf-8"))

    def test_a_stop_after_the_answer_and_before_the_grant_is_saved_replays_the_same_bytes(self):
        script = self.script("GRANT")
        self.stop("granted", script)                                                  # Leasewise granted; nothing saved
        self.assertEqual(self.rg.row()[0], "requested")
        out = self.finish(script)
        self.assertEqual(len(script.calls), 2)
        self.assertEqual(script.calls[0][:2], script.calls[1][:2])                     # same key, same bytes
        self.assertEqual(wire.parse_answer(out)["reference"], script.granted[script.calls[0][0]][0])

    def test_a_stop_after_the_grant_is_saved_and_before_it_is_printed_returns_the_stored_answer(self):
        script = self.script("GRANT")
        self.stop("saved", script)
        self.assertEqual(self.rg.row()[0], "granted")
        self.finish(script)
        self.assertEqual(len(script.calls), 1)                                         # no second request at all

    def test_lost_standard_output_is_harmless(self):
        script = self.script("GRANT")
        first = self.rg.run(request_bytes(), script, **self.clock.seams())[1]          # the caller never saw it
        self.assertEqual(self.answer(script), first)
        self.assertEqual(len(script.calls), 1)

    def test_a_stop_after_a_rebuild_is_saved_sends_the_saved_new_body(self):
        script = self.script(STALE, "GRANT")
        stage = {"count": 0}

        def fault(name):
            if name == "posted":
                stage["count"] += 1
                if stage["count"] == 1:
                    raise Crash()
        with self.assertRaises(Crash):
            self.rg.run(request_bytes(), script, **self.clock.seams(fault=fault))
        self.assertEqual(self.rg.sql("SELECT rebuilds FROM admission"), [(0,)])        # the stale answer was not applied
        self.finish(script)


class Holds(_Case):

    def test_every_outage_and_refusal_is_one_closed_word_with_its_own_back_off(self):
        cases = [(Exception("boom"), "coordinator_unreachable"), ((503, {}), "coordinator_unreachable"),
                 (error("STORE_ERROR", 503), "coordinator_unreachable"), (error("UNAUTHENTICATED", 401),
                                                                         "coordinator_unreachable"),
                 ((429, {}), "coordinator_unreachable"), ((200, b"not json"), "coordinator_unreachable"),
                 (error("ACCOUNT_PARKED", 429), "parked"), (error("MONEY_LIMIT", 429), "refused"),
                 (error("INVALID_REQUEST", 400), "refused"), (error("NOT_FOUND", 404), "refused"),
                 (error("CONTEXT_UNKNOWN", 400), "refused"), (error("TOTALLY_MADE_UP", 400), "refused")]
        for answer, word in cases:
            with self.subTest(answer=str(answer)[:50], word=word):
                self.tearDown_rig()
                self.setUp()
                script = self.script(answer)
                self.assertEqual(self.held(self.answer(script)), word)
                self.assertEqual(self.rg.row()[0], "requested")
                row = self.rg.row()
                self.assertEqual(row[4], self.clock.ms + admit.BACKOFF_MS[word])

    def tearDown_rig(self):
        self.rg.cleanup()

    def test_a_held_message_does_not_reach_leasewise_again_until_its_back_off_is_over(self):
        script = self.script(error("ACCOUNT_PARKED", 429), "GRANT")
        self.assertEqual(self.held(self.answer(script)), "parked")
        for _ in range(5):                                                             # the wrapper asks every poll
            self.clock.advance(1)
            self.assertEqual(self.held(self.answer(script)), "parked")
        self.assertEqual(len(script.calls), 1)
        self.clock.advance(admit.BACKOFF_MS["parked"] / 1000)
        self.assertEqual(wire.parse_answer(self.answer(script))["status"], "admitted")
        self.assertEqual(len(script.calls), 2)
        row = self.rg.row()
        self.assertEqual((row[0], row[3], row[4]), ("granted", None, 0))               # the note is cleared by the grant

    def test_a_held_answer_with_a_stored_grant_never_happens_after_the_grant(self):
        script = self.script("GRANT")
        first = self.answer(script)
        self.clock.advance(100_000)
        self.assertEqual(self.answer(script), first)                                   # the stored answer, even much later

    def test_nothing_from_leasewises_answer_or_a_fault_reaches_the_output_or_the_store(self):
        loud = "loud-private-text-l6a2"
        for answer in (error(loud, 429), (400, {"error": {"code": "INVALID_REQUEST", "message": loud}}),
                       Exception(loud), (200, {"lease_id": loud, "message": loud})):
            self.tearDown_rig()
            self.setUp()
            code, out, err = self.admit(self.script(answer))
            self.assertEqual((code, loud in out.decode(), loud in err), (0, False, False))
            self.assertNotIn(loud, json.dumps(self.rg.sql("SELECT * FROM admission")))


class Unconfigured(_Case):

    def test_an_agent_not_in_gateway_agents_is_not_configured_and_nothing_is_asked_or_created(self):
        script = self.script("GRANT")
        out = self.answer(script, request_bytes(agent="beta"))
        self.assertEqual(self.held(out), "not_configured")
        self.assertEqual(script.calls, [])
        self.assertFalse(os.path.exists(self.rg.store))                                # no file, no lock folder
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.rg.store), lock.FOLDER)))

    def test_each_missing_or_unprovable_bound_is_context_unprovable(self):
        cases = [("fresh", dict(fresh_overhead_bound_tokens=None)), ("fresh", dict(growth_bound_tokens=None)),
                 ("resume", dict(resume_history_bound_tokens=None)), ("resume", dict(growth_bound_tokens=None)),
                 ("fresh", dict(context_ceiling_tokens=1400)),                         # 1000 + 500 + 0 > 1400
                 ("resume", dict(context_ceiling_tokens=5400))]                        # 5000 + 500 > 5400
        for mode, bounds in cases:
            with self.subTest(mode=mode, bounds=bounds):
                self.tearDown_rig()
                self.setUp()
                self.rg.configure(**bounds)
                script = self.script("GRANT")
                self.assertEqual(self.held(self.answer(script, request_bytes(mode=mode))), "context_unprovable")
                self.assertEqual(script.calls, [])
                self.assertFalse(os.path.exists(self.rg.store))

    def tearDown_rig(self):
        self.rg.cleanup()

    def test_a_resume_with_no_resume_bound_is_fine_for_a_fresh_turn_and_a_sum_at_the_ceiling_is_provable(self):
        self.rg.configure(resume_history_bound_tokens=None, context_ceiling_tokens=1500)       # 1000 + 500 = 1500
        self.assertEqual(wire.parse_answer(self.answer(self.script("GRANT")))["status"], "admitted")

    def test_a_stored_grant_is_never_rebuilt_from_new_settings(self):
        script = self.script("GRANT")
        first = self.answer(script)
        self.rg.configure(fresh_overhead_bound_tokens=None, growth_bound_tokens=None)
        self.assertEqual(self.answer(script), first)

    def test_a_request_that_is_not_allowed_is_a_failure_with_no_answer_and_no_text_of_the_input(self):
        for raw, word in ((b"not json", "not_json"), (b'{"op":"recall"}', "bad_shape"),
                          (request_bytes(agent="a" * 65), "out_of_bounds"), (b" " * 513, "too_large")):
            code, out, err = self.admit(self.script("GRANT"), raw)
            self.assertEqual((code, out), (2, b""))
            self.assertIn(word, err)
        self.assertEqual(self.admit(self.script("GRANT"), b'{"op":"admit","agent":"loud-input-text"}')[1], b"")


class Deadline(_Case):
    """Requirement 4: ONE deadline for the whole run, and progress that survives it."""

    def test_the_worst_path_answers_busy_within_20_seconds_and_the_next_run_continues_where_it_stopped(self):
        def slow(index, key, body):
            self.clock.advance(5)                               # every request takes its full 5-second allowance
        script = self.script(STALE, STALE, STALE, CONFLICT, CONFLICT, CONFLICT, "GRANT", on_call=slow)
        started = self.clock.mono
        out = self.answer(script)
        self.assertEqual(self.held(out), "store_busy")
        self.assertLessEqual(self.clock.mono - started, admit.TOTAL_SECONDS)
        self.assertEqual(len(script.calls), 4)                                         # one body, three rebuilds
        self.assertTrue(all(call[2] <= 5.0 for call in script.calls))
        row = self.rg.row()
        self.assertEqual((row[0], row[1], row[2]), ("requested", 3, 1))                # progress is in the store
        # the very first body lands late: it is the winning grant
        first_body = self.rg.sql("SELECT body FROM admission_attempt ORDER BY id")[0][0].encode("utf-8")
        key = script.calls[0][0]
        script.granted[key] = ("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", first_body)
        started = self.clock.mono
        out = self.answer(script)
        self.assertEqual(wire.parse_answer(out)["reference"], "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
        self.assertLessEqual(self.clock.mono - started, admit.TOTAL_SECONDS)
        resumed = script.calls[4:]
        self.assertEqual(len(resumed), 3)                                              # the three older bodies only
        bodies = [row[0].encode("utf-8") for row in self.rg.sql("SELECT body FROM admission_attempt ORDER BY id DESC")]
        self.assertEqual([call[1] for call in resumed], bodies[1:])                    # newest first, never the newest again

    def test_the_wait_for_the_key_is_bounded_and_answers_busy(self):
        script = self.script("GRANT")
        held = lock.KeyLock(self.rg.store, "alpha", "msg-0001")
        self.assertTrue(held.acquire(time.monotonic() + 5))
        self.addCleanup(held.release)
        started = self.clock.mono
        out = self.answer(script)
        self.assertEqual(self.held(out), "store_busy")
        waited = self.clock.mono - started
        self.assertGreaterEqual(waited, admit.LOCK_WAIT_SECONDS)
        self.assertLessEqual(waited, admit.LOCK_WAIT_SECONDS + lock.POLL_SECONDS + 1e-6)
        self.assertEqual(script.calls, [])

    def test_a_busy_store_file_answers_busy_and_nothing_is_lost(self):
        script = self.script("GRANT")
        self.answer(script, request_bytes(message_id="m-other"))                        # the file exists now
        holder = sqlite3.connect(self.rg.store, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN EXCLUSIVE")
        with mock.patch.object(store, "BUSY_TIMEOUT_MS", 50):
            out = self.answer(script)
        self.assertEqual(self.held(out), "store_busy")
        holder.execute("ROLLBACK")
        self.assertEqual(wire.parse_answer(self.answer(script))["status"], "admitted")


if __name__ == "__main__":
    unittest.main()
