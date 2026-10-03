"""Card 7a-3, fix round 2: an answer that echoes the secret, empty and over-limit
bodies, a credential fault cleared in the same transaction as the delivery that
proves recovery, and the closed fallback word.

Rows: echo_f2, bodies_f3, credential_atomic_f5, fallback_word_f6. Synthetic data;
a fake transport; the identity values are obvious fakes.
"""

import os
import unittest
import uuid

from quota_balancer.reporter import config as cfg
from quota_balancer.reporter import decisions
from quota_balancer.reporter import state as state_mod
from quota_balancer.reporter import status as status_mod
from quota_balancer.reporter.client import MAX_ANSWER_BYTES, HttpClient
from quota_balancer.reporter.state import Item

from tests.reporter_env import Crash, Rig
from tests.test_card7a3 import Script, cli, error, granted, one_message

UUID_SECRET = "11111111-2222-4333-8444-555555555555"
BIG = b"x" * (MAX_ANSWER_BYTES + 1)
# valid JSON, but over the limit: only the size check can refuse it
BIG_JSON = b'{"pad": "' + b"x" * MAX_ANSWER_BYTES + b'"}'


def keep_everything(rig):
    """Every byte kept or shown after a run: the state folder (state, its saved
    status copy) and the status report."""
    rig.close()
    blob = b""
    folder = os.path.dirname(rig.ws.state)
    for name in sorted(os.listdir(folder)):
        with open(os.path.join(folder, name), "rb") as handle:
            blob += handle.read()
    code, out, err = cli(["status", "--config", rig.ws.config, "--json"])
    return blob.decode("latin-1") + out + err


def rig_with_identity(identity):
    rig = Rig(server=False)
    rig.ws.write_config("http://127.0.0.1:9", rig.agents_cfg, identity=identity)
    one_message(rig.journal, rig.clock.ms)
    return rig


class EchoF2(unittest.TestCase):
    def test_a_uuid_shaped_identity_passes_the_config_check(self):
        self.assertEqual(cfg._identity(UUID_SECRET), UUID_SECRET)

    def test_an_echoed_identity_is_never_stored_shown_or_put_into_a_url(self):
        for label, echoed in (("equal", UUID_SECRET),
                              ("contained", "pre-" + UUID_SECRET + "-post")):
            with self.subTest(label):
                rig = rig_with_identity(UUID_SECRET)
                self.addCleanup(rig.cleanup)
                script = Script((201, {"lease_id": echoed}), (200, {}), (200, {}))
                rig.open(transport=script)
                result = rig.reporter.cycle()
                self.assertEqual(result.blocked, "unreachable")     # retried
                self.assertEqual(len(script.sent), 1)               # nothing built from it
                self.assertEqual(rig.state.one(
                    "SELECT state, lease_id, tries FROM acquisition"),
                    ("pending", None, 1))
                self.assertNotIn(UUID_SECRET, keep_everything(rig))
                for sent in script.sent:
                    self.assertNotIn(UUID_SECRET, sent["path"])

    def test_the_retry_sends_the_original_bytes_and_a_real_lease_works(self):
        rig = rig_with_identity(UUID_SECRET)
        self.addCleanup(rig.cleanup)
        script = Script((201, {"lease_id": UUID_SECRET}))
        rig.open(transport=script)
        rig.reporter.cycle()
        first = script.sent[0]
        rig.clock.advance(3600)
        lease = str(uuid.uuid4())
        good = Script(granted(lease), (200, {}), (200, {}))
        rig.restart(transport=good)
        result = rig.reporter.cycle()
        self.assertEqual(result.sent, 3)
        self.assertEqual(good.sent[0]["body"], first["body"])
        self.assertEqual(good.sent[0]["headers"]["Idempotency-Key"],
                         first["headers"]["Idempotency-Key"])
        self.assertEqual(good.sent[1]["path"], "/v1/leases/%s/settle" % lease)

    def test_an_echo_in_an_error_answer_is_not_kept_either(self):
        rig = rig_with_identity(UUID_SECRET)
        self.addCleanup(rig.cleanup)
        rig.open(transport=Script((400, {"lease_id": UUID_SECRET,
                                         "error": {"code": UUID_SECRET}})))
        rig.reporter.cycle()
        self.assertNotIn(UUID_SECRET, keep_everything(rig))

    def test_an_echo_of_a_different_identity_value_is_not_a_false_alarm(self):
        rig = rig_with_identity(UUID_SECRET)
        self.addCleanup(rig.cleanup)
        other = str(uuid.uuid4())
        rig.open(transport=Script(granted(other), (200, {}), (200, {})))
        self.assertEqual(rig.reporter.cycle().sent, 3)


class BodiesF3(unittest.TestCase):
    def send(self, kind, status, raw):
        client = HttpClient("http", "127.0.0.1", 9, "FAKE-IDENTITY-0001",
                            transport=lambda *a: (status, raw))
        item = Item(kind, "alpha", "m", "/v1/leases", {}, "k", ("report", 1),
                    None)
        return decisions.decide(kind, client.send(item))

    def test_an_empty_or_over_limit_success_is_a_retry_for_every_kind(self):
        for kind in (decisions.REPORT, decisions.FINAL):
            for label, raw in (("empty", b""), ("over the limit", BIG),
                               ("over the limit, valid JSON", BIG_JSON)):
                for status in (200, 201):
                    with self.subTest(kind=kind, body=label, status=status):
                        self.assertEqual(self.send(kind, status, raw).action,
                                         decisions.RETRY)

    def test_the_other_retry_statuses_stay_retries_with_an_empty_or_big_body(self):
        for status in (401, 429, 500, 502, 503):
            for raw in (b"", BIG):
                with self.subTest(status=status, size=len(raw)):
                    self.assertEqual(
                        self.send(decisions.REPORT, status, raw).action,
                        decisions.RETRY)

    def test_any_other_4xx_with_such_a_body_keeps_the_permanent_handling(self):
        for status in (400, 403, 404, 409, 410, 422):
            for raw in (b"", BIG):
                with self.subTest(status=status, size=len(raw)):
                    decision = self.send(decisions.FINAL, status, raw)
                    self.assertIn(decision.action,
                                  (decisions.FAIL, decisions.FAIL_AGENT,
                                   decisions.DROP))
                    self.assertNotEqual(decision.action, decisions.RETRY)

    def test_an_over_limit_answer_that_is_valid_json_is_refused_on_its_size(self):
        self.assertGreater(len(BIG_JSON), MAX_ANSWER_BYTES)
        client = HttpClient("http", "127.0.0.1", 9, "FAKE-IDENTITY-0001",
                            transport=lambda *a: (
                                201, BIG_JSON[:-2] + b'", "lease_id": "'
                                + str(uuid.uuid4()).encode() + b'"}'))
        item = Item(decisions.ACQUIRE, "alpha", "m", "/v1/leases", {}, "k",
                    ("acquisition", 1), None)
        answer = client.send(item)
        self.assertIsNone(answer.lease_id)
        self.assertEqual(decisions.decide(item.kind, answer).action,
                         decisions.RETRY)

    def test_a_report_and_a_final_stay_pending_after_such_a_200(self):
        for which, answers in (
                ("report", [granted(), (200, b""), (200, {})]),
                ("report", [granted(), (200, BIG), (200, {})]),
                ("final", [granted(), (200, {}), (200, b"")]),
                ("final", [granted(), (200, {}), (200, BIG)])):
            with self.subTest(which=which, size=len(answers[-1][1])
                              if isinstance(answers[-1][1], bytes) else 0):
                rig = Rig(server=False)
                self.addCleanup(rig.cleanup)
                one_message(rig.journal, rig.clock.ms)
                rig.open(transport=Script(*answers))
                result = rig.reporter.cycle()
                self.assertEqual(result.blocked, "unreachable")
                table = "report" if which == "report" else "final"
                state, tries = rig.state.one(
                    "SELECT state, tries FROM %s" % table)
                self.assertEqual((state, tries), ("pending", 1))

    def test_an_empty_4xx_answer_on_an_acquisition_fails_it_without_a_loop(self):
        rig = Rig(server=False)
        self.addCleanup(rig.cleanup)
        one_message(rig.journal, rig.clock.ms)
        script = Script((404, b""))
        rig.open(transport=script)
        rig.reporter.cycle()
        rig.reporter.cycle()
        self.assertEqual(len(script.sent), 1)
        self.assertEqual(rig.state.one(
            "SELECT state FROM acquisition")[0], "failed_permanent")


class CredentialAtomicF5(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        one_message(self.rig.journal, self.rig.clock.ms)
        # acquisition and report accepted, the final answered 401
        self.script = Script(granted(), (200, {}), error(401, "UNAUTHENTICATED"))
        self.rig.open(transport=self.script)
        self.rig.reporter.cycle()
        self.assertTrue(self.rig.reporter.report()["last_401"]["active"])
        self.rig.clock.advance(3600)
        self.script.answers = [(200, {})]

    def active(self):
        return self.rig.reporter.report()["last_401"]["active"]

    def test_a_crash_right_after_the_accepted_final_leaves_it_cleared(self):
        def die(stage):
            if stage == "applied":
                raise Crash()
        self.rig.restart(transport=self.script, fault=die)
        with self.assertRaises(Crash):
            self.rig.reporter.cycle()
        self.rig.restart(transport=self.script)       # a new process
        result = self.rig.reporter.cycle()            # a quiet cycle
        self.assertEqual(result.sent, 0)
        self.assertEqual(self.rig.state.one("SELECT state FROM final")[0], "sent")
        self.assertFalse(self.active())
        self.assertIsNotNone(self.rig.reporter.report()["last_401"])   # history

    def test_a_crash_inside_the_transaction_loses_the_delivery_and_the_clear_together(
            self):
        original = self.rig.state.clear_note

        def boom(key):
            if key == state_mod.NOTE_CREDENTIAL_ACTIVE:
                raise RuntimeError("synthetic crash inside the transaction")
            return original(key)
        self.rig.state.clear_note = boom
        with self.assertRaises(RuntimeError):
            self.rig.reporter.cycle()
        self.assertEqual(self.rig.state.one("SELECT state FROM final")[0],
                         "pending")                   # rolled back as a whole
        self.assertTrue(self.active())
        self.rig.restart(transport=self.script)       # the final is sent again
        self.script.answers = [(200, {})]
        self.rig.reporter.cycle()
        self.assertEqual(self.rig.state.one("SELECT state FROM final")[0], "sent")
        self.assertFalse(self.active())

    def test_a_new_401_records_its_history_and_flag_with_the_retry_it_causes(self):
        rig = Rig(server=False)
        self.addCleanup(rig.cleanup)
        one_message(rig.journal, rig.clock.ms)
        script = Script(error(401, "UNAUTHENTICATED"))
        rig.open(transport=script)
        original = rig.state.set_note

        def boom(name, code, at):
            if name == state_mod.NOTE_CREDENTIAL_ACTIVE:
                raise RuntimeError("synthetic crash inside the transaction")
            return original(name, code, at)
        rig.state.set_note = boom
        with self.assertRaises(RuntimeError):
            rig.reporter.cycle()
        # nothing of that answer survived: no retry count, no history, no flag
        self.assertEqual(rig.state.one(
            "SELECT tries, last_error_code FROM acquisition"), (0, None))
        self.assertIsNone(rig.state.note(state_mod.NOTE_LAST_401))
        self.assertIsNone(rig.state.note(state_mod.NOTE_CREDENTIAL_ACTIVE))
        rig.state.set_note = original
        rig.restart(transport=script)
        rig.reporter.cycle()
        self.assertEqual(rig.state.one("SELECT tries FROM acquisition")[0], 1)
        self.assertTrue(rig.reporter.report()["last_401"]["active"])

    def test_the_two_notes_and_the_answer_share_one_commit(self):
        """No code path writes the credential notes outside apply()."""
        import inspect
        from quota_balancer.reporter import run
        source = inspect.getsource(run)
        self.assertNotIn("credential_active", source)
        self.assertNotIn("last_401", source)


class FallbackWordF6(unittest.TestCase):
    def test_an_unreadable_answer_stores_a_member_of_the_closed_set(self):
        answers = ((400, b"[[["), (400, b""), (403, BIG), (409, b"\xff\xfe"),
                   (422, {"error": {"code": "whatever"}}))
        for answer in answers:
            with self.subTest(status=answer[0]):
                rig = Rig(server=False)
                self.addCleanup(rig.cleanup)
                one_message(rig.journal, rig.clock.ms)
                rig.open(transport=Script(answer))
                rig.reporter.cycle()
                code = rig.state.one(
                    "SELECT last_error_code FROM acquisition")[0]
                self.assertIn(code, decisions.CLOSED_CODES)
                self.assertEqual(code, decisions.UNKNOWN_CODE)
                shown = rig.reporter.report()["agents"]["alpha"]
                for item in shown["permanent_failures"]:
                    self.assertIn(item["code"], decisions.CLOSED_CODES)

    def test_no_stored_word_anywhere_is_outside_the_closed_set(self):
        rig = Rig(server=False)
        self.addCleanup(rig.cleanup)
        one_message(rig.journal, rig.clock.ms)
        rig.open(transport=Script(granted(), (400, b"[["), (422, b"")))
        rig.reporter.cycle()
        for table in ("acquisition", "report", "final"):
            for (code,) in rig.state.all(
                    "SELECT last_error_code FROM %s WHERE last_error_code "
                    "IS NOT NULL" % table):
                self.assertIn(code, decisions.CLOSED_CODES, table)
        self.assertNotIn("UNKNOWN", [
            c for (c,) in rig.state.all(
                "SELECT last_error_code FROM report UNION SELECT "
                "last_error_code FROM final UNION SELECT last_error_code "
                "FROM acquisition")])


if __name__ == "__main__":
    unittest.main()
