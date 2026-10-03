"""Card L6a-1, step 1: the receipt source adapter (`quota_balancer/reporter/receipts.py`).

The adapter is the ONE place that knows how gateway receipts reach us, so everything it refuses is
refused here: a wrong key, type, bound or order, a repeated key, too much. A source that cannot be read
is "read again later", never a settle. The command source is tried with fake scripts only: a time limit,
a size cap, a bad exit, bad JSON, no inherited secrets, no shell. Synthetic data only.
"""

import json
import os
import sys
import tempfile
import unittest

from quota_balancer.reporter import receipts

from tests.l6a1_env import command_source, fixture_source, lease, receipt, write_json, write_raw

LOUD = "loud-secret-l6a1-adapter"


class _Dir(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(dir=os.environ.get("AGENTTALK_SCRATCH") or None,
                                                prefix="receipts-l6a1-")
        self.addCleanup(self._dir.cleanup)
        self.dir = self._dir.name
        self.fixture = os.path.join(self.dir, "receipts.json")

    def read(self, rows, after=0, **over):
        write_json(self.fixture, rows)
        return receipts.read_receipts(fixture_source(self.fixture, **over), after)

    def refused(self, rows, reason, after=0):
        result = self.read(rows, after)
        self.assertEqual((result.status, result.reason, result.receipts), (receipts.REFUSED, reason, ()))


class Fixture(_Dir):

    def test_a_good_list_comes_back_in_order_with_only_the_closed_values(self):
        one, two = lease(), lease()
        result = self.read([receipt(one, 1), receipt(two, 2, outcome="failed", calls=0, input_tokens=0,
                                                       output_tokens=0, actual_micro_eur=0)])
        self.assertEqual((result.status, result.reason), (receipts.OK, None))
        self.assertEqual([r.seq for r in result.receipts], [1, 2])
        self.assertEqual([r.outcome for r in result.receipts], ["completed", "failed"])
        first = result.receipts[0]
        self.assertEqual((first.calls, first.input_tokens, first.output_tokens, first.actual_micro_eur),
                         (2, 1200, 300, 450))
        self.assertEqual(set(first.__slots__), {"seq", "ref_sha256", "outcome", "calls", "input_tokens",
                                                "output_tokens", "actual_micro_eur", "generation"})

    def test_an_empty_list_is_fine(self):
        result = self.read([])
        self.assertEqual((result.status, result.receipts), (receipts.OK, ()))

    def test_only_receipts_after_the_last_seq_are_returned(self):
        rows = [receipt(lease(), n) for n in (1, 2, 3, 4)]
        self.assertEqual([r.seq for r in self.read(rows, after=2).receipts], [3, 4])
        self.assertEqual(self.read(rows, after=4).receipts, ())

    def test_a_missing_file_is_read_later(self):
        result = receipts.read_receipts(fixture_source(os.path.join(self.dir, "nope.json")), 0)
        self.assertEqual((result.status, result.reason), (receipts.READ_LATER, "source_missing"))

    def test_text_that_is_not_json_is_read_later(self):
        for raw in (b'[{"seq": 1', b"", b"\xff\xfe\x00", b"not json", b"[1,]"):
            write_raw(self.fixture, raw)
            result = receipts.read_receipts(fixture_source(self.fixture), 0)
            self.assertEqual((result.status, result.reason), (receipts.READ_LATER, "not_json"), raw)

    def test_a_list_is_required(self):
        for doc in ({}, {"receipts": []}, "x", 3, None, True):
            self.refused(doc, "not_a_list")

    def test_too_many_rows_and_too_large_a_file_are_refused(self):
        self.refused([receipt(lease(), n) for n in range(1, receipts.MAX_ROWS + 2)], "too_many")
        write_json(self.fixture, [receipt(lease(), 1)])
        result = receipts.read_receipts(fixture_source(self.fixture, max_bytes=100), 0)
        self.assertEqual((result.status, result.reason), (receipts.REFUSED, "too_large"))

    def test_the_keys_are_closed(self):
        good = receipt(lease(), 1)
        extra = dict(good, agent=LOUD)
        short = {k: v for k, v in good.items() if k != "generation"}
        for doc in (extra, short, {}, [], "x", None):
            self.refused([doc], "bad_row")

    def test_every_number_must_be_a_plain_whole_number(self):
        for key in ("seq", "calls", "input_tokens", "output_tokens", "actual_micro_eur", "closed_at"):
            for bad in (True, False, 1.0, 1.5, "1", None, [1], {"a": 1}, -1):
                self.refused([receipt(lease(), **{key: bad})], "bad_row")

    def test_the_bounds_are_enforced(self):
        top = {"seq": 2 ** 63, "calls": receipts.MAX_CALLS + 1, "input_tokens": receipts.MAX_TOKENS + 1,
               "output_tokens": receipts.MAX_TOKENS + 1, "actual_micro_eur": receipts.MAX_MICRO_EUR + 1}
        for key, bad in top.items():
            self.refused([receipt(lease(), **{key: bad})], "bad_row")
        self.refused([receipt(lease(), 0)], "bad_row")                      # seq starts at 1
        edge = receipt(lease(), 2 ** 63 - 1, calls=receipts.MAX_CALLS, input_tokens=receipts.MAX_TOKENS,
                       output_tokens=receipts.MAX_TOKENS, actual_micro_eur=receipts.MAX_MICRO_EUR)
        self.assertEqual(self.read([edge]).status, receipts.OK)

    def test_the_outcome_is_one_of_the_four_words(self):
        for word in ("completed", "cancelled", "failed", "provider_limit"):
            self.assertEqual(self.read([receipt(lease(), 1, outcome=word)]).status, receipts.OK, word)
        for bad in ("done", "COMPLETED", "", " completed", None, 1, ["failed"], LOUD):
            self.refused([receipt(lease(), 1, outcome=bad)], "bad_row")

    def test_the_reference_and_the_generation_have_exact_shapes(self):
        for bad in ("", "A" * 64, "g" * 64, "a" * 63, "a" * 65, "a" * 64 + "\n", None, 7):
            self.refused([receipt(lease(), 1, quota_lease_ref_sha256=bad)], "bad_row")
        for bad in ("", "A" * 32, "a" * 31, "a" * 33, "a" * 32 + "\n", None, 7):
            self.refused([receipt(lease(), 1, generation=bad)], "bad_row")

    def test_a_receipt_with_no_calls_may_not_carry_spend(self):
        self.refused([receipt(lease(), 1, calls=0, input_tokens=5, output_tokens=0, actual_micro_eur=0)], "bad_row")
        self.refused([receipt(lease(), 1, calls=0, input_tokens=0, output_tokens=0, actual_micro_eur=1)], "bad_row")
        self.assertEqual(self.read([receipt(lease(), 1, calls=0, input_tokens=0, output_tokens=0,
                                            actual_micro_eur=0)]).status, receipts.OK)

    def test_the_order_must_rise_strictly(self):
        a, b = lease(), lease()
        self.refused([receipt(a, 2), receipt(b, 1)], "bad_order")
        self.refused([receipt(a, 1), receipt(b, 1)], "bad_order")

    def test_a_repeated_key_is_refused(self):
        good = json.dumps(receipt(lease(), 1))
        doubled = good[:-1] + ', "calls": 9}'
        write_raw(self.fixture, ("[%s]" % doubled).encode())
        result = receipts.read_receipts(fixture_source(self.fixture), 0)
        self.assertEqual((result.status, result.reason), (receipts.REFUSED, "duplicate_key"))

    def test_nan_and_the_infinities_are_refused(self):
        for token in ("NaN", "Infinity", "-Infinity"):
            text = json.dumps(receipt(lease(), 1)).replace('"calls": 2', '"calls": ' + token)
            write_raw(self.fixture, ("[%s]" % text).encode())
            result = receipts.read_receipts(fixture_source(self.fixture), 0)
            self.assertEqual(result.status, receipts.REFUSED, token)

    def test_one_bad_row_refuses_the_whole_list(self):
        result = self.read([receipt(lease(), 1), receipt(lease(), 2, outcome="nope")])
        self.assertEqual((result.status, result.receipts), (receipts.REFUSED, ()))

    def test_the_result_and_the_receipt_hide_their_values(self):
        result = self.read([receipt(lease(), 1)])
        self.assertNotIn("1200", repr(result.receipts[0]) + str(result.receipts[0]))


SCRIPT = r"""
import json, os, sys
mode = sys.argv[1]
after = int(sys.argv[sys.argv.index("--after") + 1])
if mode == "good":
    print(json.dumps(json.loads(os.environ["FAKE_ROWS"])))
elif mode == "echo":
    print(json.dumps({"argv": sys.argv[1:], "env": sorted(os.environ)}))
elif mode == "slow":
    import time
    time.sleep(30)
elif mode == "big":
    sys.stdout.write("[" + " " * 5000 + "]")
elif mode == "exit":
    sys.stderr.write("LOUD-STDERR-TEXT")
    sys.stdout.write("[]")
    sys.exit(3)
elif mode == "badjson":
    print("[{oops")
elif mode == "stderr":
    sys.stderr.write("LOUD-STDERR-TEXT")
    print("[]")
"""


class Command(_Dir):

    def setUp(self):
        super().setUp()
        self.script = os.path.join(self.dir, "fake.py")
        with open(self.script, "w", encoding="utf-8") as handle:
            handle.write(SCRIPT)

    def run_mode(self, mode, after=0, env=(), **over):
        source = command_source([sys.executable, self.script, mode], env=tuple(env), **over)
        return receipts.read_receipts(source, after)

    def test_a_good_run_returns_the_receipts_and_is_told_the_last_seq(self):
        rows = [receipt(lease(), 5), receipt(lease(), 6)]
        result = self.run_mode("good", after=4, env=[("FAKE_ROWS", json.dumps(rows))])
        self.assertEqual((result.status, [r.seq for r in result.receipts]), (receipts.OK, [5, 6]))

    def test_an_old_row_in_a_command_answer_is_a_wrong_order(self):
        rows = [receipt(lease(), 3), receipt(lease(), 6)]
        result = self.run_mode("good", after=4, env=[("FAKE_ROWS", json.dumps(rows))])
        self.assertEqual((result.status, result.reason), (receipts.REFUSED, "bad_order"))

    def test_the_command_gets_its_arguments_unchanged_and_no_inherited_secret(self):
        os.environ["LEASEWISE_LOUD_SECRET_L6A1"] = LOUD
        self.addCleanup(os.environ.pop, "LEASEWISE_LOUD_SECRET_L6A1", None)
        source = command_source([sys.executable, self.script, "echo", "a b", "x;y", "$(z)"], env=(("ONE", "1"),))
        proc_result = receipts.read_receipts(source, 7)
        # the echo is not a receipt list: it is refused, which is how this test can see it ran
        self.assertEqual((proc_result.status, proc_result.reason), (receipts.REFUSED, "not_a_list"))
        raw = receipts._read_command(source, 7)
        said = json.loads(raw.decode())
        self.assertEqual(said["argv"], ["echo", "a b", "x;y", "$(z)", "--after", "7"])
        self.assertNotIn("LEASEWISE_LOUD_SECRET_L6A1", said["env"])
        self.assertIn("ONE", said["env"])
        self.assertNotIn(LOUD, raw.decode())

    def test_a_slow_command_is_stopped_and_read_later(self):
        result = self.run_mode("slow", timeout_seconds=1)
        self.assertEqual((result.status, result.reason), (receipts.READ_LATER, "timeout"))

    def test_too_much_output_is_refused(self):
        result = self.run_mode("big", max_bytes=1000)
        self.assertEqual((result.status, result.reason), (receipts.REFUSED, "too_large"))

    def test_a_non_zero_exit_is_read_later_even_if_it_printed_a_list(self):
        result = self.run_mode("exit")
        self.assertEqual((result.status, result.reason), (receipts.READ_LATER, "exit_code"))

    def test_bad_json_is_read_later(self):
        result = self.run_mode("badjson")
        self.assertEqual((result.status, result.reason), (receipts.READ_LATER, "not_json"))

    def test_error_output_is_never_kept(self):
        result = self.run_mode("stderr")
        self.assertEqual((result.status, result.receipts), (receipts.OK, ()))
        self.assertNotIn("LOUD-STDERR-TEXT", repr(vars(result) if hasattr(result, "__dict__") else result.reason))

    def test_a_command_that_cannot_start_is_read_later(self):
        source = command_source([os.path.join(self.dir, "no-such-program")])
        result = receipts.read_receipts(source, 0)
        self.assertEqual((result.status, result.reason), (receipts.READ_LATER, "cannot_run"))


if __name__ == "__main__":
    unittest.main()
