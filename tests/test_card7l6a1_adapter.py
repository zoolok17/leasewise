"""Card L6a-1 (revised), step 1: the receipt source adapter (`quota_balancer/reporter/receipts.py`).

The adapter is the ONE place that knows how gateway receipts reach us, so everything it refuses is refused here, and a
refused page is refused WHOLE: nothing is stored from it. The unit is the versioned page of DESIGN-6A 5.4: its
version, its closed keys, the ledger generation (even on an empty page), the asked-for number, the receipts with
nullable token counts and an ISO close time, and numbers that rise by exactly one. A source that cannot be read is
"read again later", never a settle. The command source is tried with fake scripts only. Synthetic data only.
"""

import json
import os
import sys
import tempfile
import time
import unittest

from quota_balancer.reporter import receipts

from tests.l6a1_env import (GENERATION, GENERATION_B, command_source, encode, fixture_source, lease, ledger, page,
                            receipt, write_json, write_raw)

LOUD = "loud-secret-l6a1-adapter"


def alive(pid):
    """Is a process with this id still running? (By its id only.)"""
    if sys.platform == "win32":
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)           # query limited information
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return code.value == 259                                   # still active
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def parse(doc, after=0, limit=receipts.MAX_ROWS):
    """The page of `doc`, or the closed (status, reason) the adapter refuses it with."""
    try:
        return receipts.parse_page(encode(doc), after, limit)
    except receipts._Refused as exc:
        return (receipts.REFUSED, exc.args[0])
    except receipts._Later as exc:
        return (receipts.READ_LATER, exc.args[0])


class Pages(unittest.TestCase):

    def refused(self, doc, reason, after=0):
        self.assertEqual(parse(doc, after), (receipts.REFUSED, reason), reason)

    def test_a_good_page_comes_back_with_every_field_exactly_as_received(self):
        one, two = lease(), lease()
        got = parse(page([receipt(one, 1), receipt(two, 2, outcome="failed", calls=0, input_tokens=0,
                                                   output_tokens=0, actual_micro_eur=0)]))
        self.assertEqual((got.generation, got.after_seq, got.next_seq, got.has_more), (GENERATION, 0, 2, False))
        self.assertEqual([r.seq for r in got.receipts], [1, 2])
        first = got.receipts[0]
        self.assertEqual((first.outcome, first.calls, first.input_tokens, first.output_tokens,
                          first.actual_micro_eur, first.closed_at),
                         ("completed", 2, 1200, 300, 450, "2026-10-03T10:00:00.000000Z"))
        self.assertEqual(set(first.__slots__), {"seq", "ref_sha256", "outcome", "calls", "input_tokens",
                                                "output_tokens", "actual_micro_eur", "closed_at"})
        self.assertNotIn("1200", repr(first) + str(got))

    def test_an_empty_page_still_names_its_generation(self):
        got = parse(page([], after=7, generation=GENERATION_B), after=7)
        self.assertEqual((got.generation, got.after_seq, got.next_seq, got.receipts), (GENERATION_B, 7, 7, ()))
        self.refused(page([], after=7, generation=None), "bad_envelope")
        self.refused({k: v for k, v in page([]).items() if k != "generation"}, "bad_envelope")

    def test_unknown_token_counts_stay_unknown(self):
        got = parse(page([receipt(lease(), 1, input_tokens=None, output_tokens=None),
                          receipt(lease(), 2, input_tokens=5, output_tokens=None),
                          receipt(lease(), 3, input_tokens=None, output_tokens=7)]))
        self.assertEqual([(r.input_tokens, r.output_tokens) for r in got.receipts],
                         [(None, None), (5, None), (None, 7)])

    def test_the_version_is_one(self):
        for bad in (0, 2, "1", 1.0, True, None, [1]):
            self.refused(page([], envelope_version=bad), "bad_version")

    def test_the_envelope_keys_are_closed(self):
        good = page([receipt(lease(), 1)])
        self.refused(dict(good, extra=LOUD), "bad_envelope")
        self.refused({k: v for k, v in good.items() if k != "has_more"}, "bad_envelope")

    def test_the_page_must_be_an_object(self):
        for doc in ([], [page([])], "x", 3, None, True):
            self.refused(doc, "not_an_object")

    def test_the_envelope_values_have_exact_types(self):
        for key, bad in (("generation", "A" * 32), ("generation", "a" * 31), ("generation", "a" * 33),
                         ("generation", "a" * 32 + "\n"), ("generation", 5), ("after_seq", True),
                         ("after_seq", 1.0), ("after_seq", -1), ("next_seq", "1"), ("has_more", 1),
                         ("has_more", "true"), ("receipts", {}), ("receipts", None)):
            self.refused(page([], **{key: bad}), "bad_envelope" if key != "after_seq" else "bad_envelope")

    def test_the_asked_for_number_must_come_back(self):
        self.refused(page([receipt(lease(), 6)], after=5), "wrong_after", after=0)
        self.refused(page([], after=0), "wrong_after", after=4)
        self.assertEqual(parse(page([receipt(lease(), 6)], after=5), after=5).next_seq, 6)

    def test_the_numbers_rise_by_exactly_one_from_the_asked_for_number(self):
        a, b, c = lease(), lease(), lease()
        self.refused(page([receipt(a, 2)], after=0), "bad_order")                            # a gap at the start
        self.refused(page([receipt(a, 1), receipt(b, 3)]), "bad_order")                      # a gap in the middle
        self.refused(page([receipt(a, 1), receipt(b, 1)], next_seq=1), "bad_order")          # a repeat
        self.refused(page([receipt(a, 2), receipt(b, 1)]), "bad_order")                      # backwards
        self.refused(page([receipt(a, 4)], after=5, next_seq=4), "bad_order", after=5)       # at or before the cursor
        self.assertEqual(parse(page([receipt(a, 1), receipt(b, 2), receipt(c, 3)])).next_seq, 3)

    def test_next_seq_and_has_more_must_agree_with_the_receipts(self):
        a = lease()
        self.refused(page([receipt(a, 1)], next_seq=2), "bad_envelope")
        self.refused(page([receipt(a, 1)], next_seq=0), "bad_envelope")
        self.refused(page([], after=3, next_seq=4), "bad_envelope", after=3)
        self.refused(page([], has_more=True), "bad_envelope")
        self.assertTrue(parse(page([receipt(a, 1)], has_more=True)).has_more)

    def test_the_receipt_keys_are_closed(self):
        good = receipt(lease(), 1)
        for doc in (dict(good, agent=LOUD), dict(good, generation=GENERATION), {k: v for k, v in good.items()
                                                                                   if k != "closed_at"},
                    {}, [], "x", None):
            self.refused(page([doc], next_seq=1), "bad_row")

    def test_every_number_must_be_a_plain_whole_number(self):
        for key in ("seq", "calls", "actual_micro_eur"):
            for bad in (True, False, 1.0, 1.5, "1", None, [1], {"a": 1}, -1):
                self.refused(page([receipt(lease(), **{key: bad})], next_seq=1), "bad_row")
        for key in ("input_tokens", "output_tokens"):
            for bad in (True, False, 1.0, 1.5, "1", [1], {"a": 1}, -1):
                self.refused(page([receipt(lease(), **{key: bad})], next_seq=1), "bad_row")

    def test_the_bounds_are_enforced(self):
        top = {"calls": receipts.MAX_CALLS + 1, "input_tokens": receipts.MAX_TOKENS + 1,
               "output_tokens": receipts.MAX_TOKENS + 1, "actual_micro_eur": receipts.MAX_MICRO_EUR + 1}
        for key, bad in top.items():
            self.refused(page([receipt(lease(), **{key: bad})], next_seq=1), "bad_row")
        self.refused(page([receipt(lease(), 0)], next_seq=0), "bad_row")
        edge = receipt(lease(), 1, calls=receipts.MAX_CALLS, input_tokens=receipts.MAX_TOKENS,
                       output_tokens=receipts.MAX_TOKENS, actual_micro_eur=receipts.MAX_MICRO_EUR)
        self.assertEqual(parse(page([edge])).next_seq, 1)

    def test_the_outcome_is_one_of_the_four_words(self):
        for word in ("completed", "cancelled", "failed", "provider_limit"):
            self.assertEqual(parse(page([receipt(lease(), outcome=word)])).next_seq, 1, word)
        for bad in ("done", "COMPLETED", "", " completed", None, 1, ["failed"], LOUD):
            self.refused(page([receipt(lease(), outcome=bad)], next_seq=1), "bad_row")

    def test_the_reference_has_an_exact_shape(self):
        for bad in ("", "A" * 64, "g" * 64, "a" * 63, "a" * 65, "a" * 64 + "\n", None, 7):
            self.refused(page([receipt(lease(), quota_lease_ref_sha256=bad)], next_seq=1), "bad_row")

    def test_the_close_time_is_the_ledgers_own_iso_format(self):
        for bad in (1_788_224_400_000, "2026-10-03", "2026-10-03T10:00:00Z", "2026-10-03T10:00:00.000Z",
                    "2026-10-03 10:00:00.000000Z", "2026-13-03T10:00:00.000000Z", "2026-10-03T25:00:00.000000Z",
                    "2026-10-03T10:00:00.000000", "", None, "2026-10-03T10:00:00.000000Z\n"):
            self.refused(page([receipt(lease(), closed_at=bad)], next_seq=1), "bad_row")

    def test_a_receipt_with_no_calls_may_not_carry_spend(self):
        self.refused(page([receipt(lease(), calls=0, input_tokens=5, output_tokens=0, actual_micro_eur=0)],
                          next_seq=1), "bad_row")
        self.refused(page([receipt(lease(), calls=0, input_tokens=0, output_tokens=0, actual_micro_eur=1)],
                          next_seq=1), "bad_row")
        self.assertEqual(parse(page([receipt(lease(), calls=0, input_tokens=0, output_tokens=0,
                                             actual_micro_eur=0)])).next_seq, 1)

    def test_a_repeated_key_is_refused(self):
        good = encode(page([receipt(lease(), 1)])).decode()
        for text in (good.replace('"has_more": false', '"has_more": false, "has_more": true'),
                     good[:-3] + ', "calls": 9}]}'):
            with self.subTest(text=text[-40:]):
                self.assertEqual(self.raw(text), (receipts.REFUSED, "duplicate_key"))

    def raw(self, text, after=0):
        try:
            receipts.parse_page(text.encode(), after)
        except receipts._Refused as exc:
            return (receipts.REFUSED, exc.args[0])
        except receipts._Later as exc:
            return (receipts.READ_LATER, exc.args[0])

    def test_nan_and_the_infinities_are_refused(self):
        good = encode(page([receipt(lease(), 1)])).decode()
        for token in ("NaN", "Infinity", "-Infinity"):
            self.assertEqual(self.raw(good.replace('"calls": 2', '"calls": ' + token)),
                             (receipts.REFUSED, "bad_row"), token)

    def test_text_that_is_not_json_is_read_later(self):
        for raw in (b'{"envelope_version": 1', b"", b"\xff\xfe\x00", b"not json", b"[1,]"):
            try:
                receipts.parse_page(raw, 0)
            except receipts._Later as exc:
                self.assertEqual(exc.args[0], "not_json", raw)

    def test_too_many_receipts_are_refused(self):
        rows = [receipt(lease(), n) for n in range(1, receipts.MAX_ROWS + 2)]
        self.refused(page(rows), "too_many")

    def test_zero_calls_means_zero_counts_and_zero_money_and_null_is_not_zero(self):
        """F4 (spec 3.4): with no call, both token counts and the money must be the number 0."""
        zero = dict(calls=0, input_tokens=0, output_tokens=0, actual_micro_eur=0)
        self.assertEqual(len(parse(page([receipt(lease(), 1, **zero)])).receipts), 1)
        for over in (dict(input_tokens=None), dict(output_tokens=None), dict(input_tokens=None, output_tokens=None),
                     dict(input_tokens=1), dict(output_tokens=1), dict(actual_micro_eur=1)):
            with self.subTest(over=over):
                self.refused(page([receipt(lease(), 1, **dict(zero, **over))]), "bad_row")
        self.refused(page([receipt(lease(), 1), receipt(lease(), 2, **dict(zero, input_tokens=None))]), "bad_row")

    def test_null_counts_stay_legal_when_there_was_at_least_one_call(self):
        got = parse(page([receipt(lease(), 1, calls=1, input_tokens=None, output_tokens=None)]))
        self.assertEqual((got.receipts[0].input_tokens, got.receipts[0].output_tokens), (None, None))

    def test_a_page_longer_than_the_requested_limit_is_refused_before_anything_is_stored(self):
        """F5: the limit asked for is the limit allowed, not just the 1000 ceiling."""
        rows = [receipt(lease(), n) for n in range(1, 151)]
        self.assertEqual(parse(page(rows), limit=150).next_seq, 150)
        self.assertEqual(parse(page(rows), limit=100), (receipts.REFUSED, "too_many"))     # shorter than 1000, longer than asked
        self.assertEqual(parse(page(rows[:1]), limit=1).next_seq, 1)
        self.assertEqual(parse(page(rows[:2]), limit=1), (receipts.REFUSED, "too_many"))
        big = [receipt(lease(), n) for n in range(1, 1001)]
        self.assertEqual(parse(page(big), limit=1000).next_seq, 1000)
        self.assertEqual(parse(page(big + [receipt(lease(), 1001)]), limit=1000), (receipts.REFUSED, "too_many"))
        self.assertEqual(parse(page([]), limit=1).receipts, ())                              # an empty page is always fine

    def test_one_bad_receipt_refuses_the_whole_page(self):
        got = parse(page([receipt(lease(), 1), receipt(lease(), 2, outcome="nope")]))
        self.assertEqual(got, (receipts.REFUSED, "bad_row"))


class _Dir(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(dir=os.environ.get("AGENTTALK_SCRATCH") or None,
                                                prefix="receipts-l6a1-")
        self.addCleanup(self._dir.cleanup)
        self.dir = self._dir.name
        self.fixture = os.path.join(self.dir, "receipts.json")


class Fixture(_Dir):

    def read(self, doc, after=0, **over):
        write_json(self.fixture, doc)
        return receipts.read_page(fixture_source(self.fixture, **over), after)

    def test_the_fixture_page_has_the_configured_length_and_says_there_is_more(self):
        rows = [receipt(lease(), n) for n in range(1, 151)]
        for limit, expected in ((1, 1), (100, 100), (150, 150), (1000, 150)):
            with self.subTest(limit=limit):
                result = self.read(ledger(rows), page_limit=limit)
                self.assertEqual(len(result.page.receipts), expected)
                self.assertEqual(result.page.has_more, expected < 150)

    def test_a_ready_made_fixture_page_longer_than_the_configured_length_is_refused(self):
        rows = [receipt(lease(), n) for n in range(1, 151)]
        result = self.read(page(rows), page_limit=100)
        self.assertEqual((result.status, result.reason), (receipts.REFUSED, "too_many"))
        self.assertEqual(self.read(page(rows), page_limit=150).status, receipts.OK)

    def test_a_ledger_makes_the_page_after_the_asked_for_number(self):
        rows = [receipt(lease(), n) for n in (1, 2, 3, 4)]
        result = self.read(ledger(rows), after=2)
        self.assertEqual((result.status, result.reason), (receipts.OK, None))
        self.assertEqual([r.seq for r in result.page.receipts], [3, 4])
        self.assertEqual((result.page.generation, result.page.after_seq, result.page.next_seq),
                         (GENERATION, 2, 4))
        self.assertEqual(self.read(ledger(rows), after=4).page.receipts, ())

    def test_a_long_ledger_is_paged_with_has_more(self):
        rows = [receipt(lease(), n) for n in range(1, 251)]
        first = self.read(ledger(rows)).page
        self.assertEqual((len(first.receipts), first.next_seq, first.has_more), (100, 100, True))
        last = self.read(ledger(rows), after=200).page
        self.assertEqual((len(last.receipts), last.next_seq, last.has_more), (50, 250, False))

    def test_a_made_page_is_served_as_it_is_and_still_checked(self):
        result = self.read(page([receipt(lease(), 1)]))
        self.assertEqual(result.status, receipts.OK)
        bad = self.read(page([receipt(lease(), 1)], envelope_version=2))
        self.assertEqual((bad.status, bad.reason), (receipts.REFUSED, "bad_version"))
        wrong = self.read(page([receipt(lease(), 6)], after=5), after=0)
        self.assertEqual((wrong.status, wrong.reason), (receipts.REFUSED, "wrong_after"))

    def test_a_ledger_with_a_gap_is_refused(self):
        result = self.read(ledger([receipt(lease(), 1), receipt(lease(), 3)]))
        self.assertEqual((result.status, result.reason), (receipts.REFUSED, "bad_order"))

    def test_a_missing_file_is_read_later(self):
        result = receipts.read_page(fixture_source(os.path.join(self.dir, "nope.json")), 0)
        self.assertEqual((result.status, result.reason), (receipts.READ_LATER, "source_missing"))

    def test_text_that_is_not_json_is_read_later_and_too_large_is_refused(self):
        write_raw(self.fixture, b'{"envelope_version": 1, "genera')
        result = receipts.read_page(fixture_source(self.fixture), 0)
        self.assertEqual((result.status, result.reason), (receipts.READ_LATER, "not_json"))
        write_json(self.fixture, ledger([receipt(lease(), 1)]))
        result = receipts.read_page(fixture_source(self.fixture, max_bytes=100), 0)
        self.assertEqual((result.status, result.reason), (receipts.REFUSED, "too_large"))


SCRIPT = r"""
import json, os, sys
mode = sys.argv[1]
after = int(sys.argv[sys.argv.index("--after") + 1])
if mode == "good":
    print(os.environ["FAKE_PAGE"])
elif mode == "echo":
    print(json.dumps({"argv": sys.argv[1:], "env": sorted(os.environ)}))
elif mode == "slow":
    import time
    time.sleep(30)
elif mode == "big":
    sys.stdout.write("[" + " " * 5000 + "]")
elif mode == "exit":
    sys.stderr.write("LOUD-STDERR-TEXT")
    sys.stdout.write("{}")
    sys.exit(3)
elif mode == "orphan_good":
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)    # a child that holds nothing open
    with open(os.environ["PIDFILE"], "w") as handle:
        handle.write("%d %d" % (os.getpid(), child.pid))
    print(os.environ["FAKE_PAGE"])
elif mode in ("orphan", "hang_tree", "flood"):
    import subprocess, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])      # inherits our output
    with open(os.environ["PIDFILE"], "w") as handle:
        handle.write("%d %d" % (os.getpid(), child.pid))
    if mode == "orphan":
        sys.exit(0)                                   # the program is gone; its child still holds the output open
    if mode == "hang_tree":
        time.sleep(120)
    while True:
        sys.stdout.write("x" * 65536)
        sys.stdout.flush()
elif mode == "badjson":
    print("{oops")
elif mode == "stderr":
    sys.stderr.write("LOUD-STDERR-TEXT")
    print(os.environ["FAKE_PAGE"])
"""


class Command(_Dir):

    def setUp(self):
        super().setUp()
        self.script = os.path.join(self.dir, "fake.py")
        with open(self.script, "w", encoding="utf-8") as handle:
            handle.write(SCRIPT)

    def run_mode(self, mode, after=0, env=(), **over):
        source = command_source([sys.executable, self.script, mode], env=tuple(env), **over)
        return receipts.read_page(source, after)

    def test_a_good_run_returns_the_page_and_is_told_the_last_number(self):
        doc = page([receipt(lease(), 5), receipt(lease(), 6)], after=4)
        result = self.run_mode("good", after=4, env=[("FAKE_PAGE", json.dumps(doc))])
        self.assertEqual((result.status, [r.seq for r in result.page.receipts]), (receipts.OK, [5, 6]))

    def test_a_page_for_another_number_than_asked_is_refused(self):
        doc = page([receipt(lease(), 3)], after=2)
        result = self.run_mode("good", after=4, env=[("FAKE_PAGE", json.dumps(doc))])
        self.assertEqual((result.status, result.reason), (receipts.REFUSED, "wrong_after"))

    def test_the_command_gets_its_arguments_unchanged_and_no_inherited_secret(self):
        os.environ["LEASEWISE_LOUD_SECRET_L6A1"] = LOUD
        self.addCleanup(os.environ.pop, "LEASEWISE_LOUD_SECRET_L6A1", None)
        source = command_source([sys.executable, self.script, "echo", "a b", "x;y", "$(z)"], env=(("ONE", "1"),))
        refused = receipts.read_page(source, 7)
        self.assertEqual((refused.status, refused.reason), (receipts.REFUSED, "bad_version"))   # it ran: not a page
        raw = receipts._read_command(source, 7)
        said = json.loads(raw.decode())
        self.assertEqual(said["argv"], ["echo", "a b", "x;y", "$(z)", "--after", "7", "--limit", "100"])
        self.assertNotIn("LEASEWISE_LOUD_SECRET_L6A1", said["env"])
        self.assertIn("ONE", said["env"])
        self.assertNotIn(LOUD, raw.decode())

    def test_a_slow_command_is_stopped_and_read_later(self):
        result = self.run_mode("slow", timeout_seconds=1)
        self.assertEqual((result.status, result.reason), (receipts.READ_LATER, "timeout"))

    def test_too_much_output_is_refused(self):
        result = self.run_mode("big", max_bytes=1000)
        self.assertEqual((result.status, result.reason), (receipts.REFUSED, "too_large"))

    def test_a_non_zero_exit_is_read_later_even_if_it_printed_a_page(self):
        result = self.run_mode("exit")
        self.assertEqual((result.status, result.reason), (receipts.READ_LATER, "exit_code"))

    def test_bad_json_is_read_later(self):
        result = self.run_mode("badjson")
        self.assertEqual((result.status, result.reason), (receipts.READ_LATER, "not_json"))

    def test_error_output_is_never_kept(self):
        doc = page([])
        result = self.run_mode("stderr", env=[("FAKE_PAGE", json.dumps(doc))])
        self.assertEqual((result.status, result.page.receipts), (receipts.OK, ()))
        self.assertNotIn("LOUD-STDERR-TEXT", repr(result.page) + str(result.reason))

    # --- fix round 1, F2: ONE deadline bounds reading, exit and cleanup, and no process is left running ---

    def tree_run(self, mode, **over):
        pidfile = os.path.join(self.dir, "pids-%s.txt" % mode)
        started = time.monotonic()
        result = self.run_mode(mode, env=[("PIDFILE", pidfile)], **over)
        elapsed = time.monotonic() - started
        with open(pidfile, "r", encoding="ascii") as handle:
            parent, child = (int(number) for number in handle.read().split())
        return result, elapsed, parent, child

    def assertGone(self, *pids):
        for pid in pids:
            for _ in range(40):                                  # a killed process needs a moment to be reaped
                if not alive(pid):
                    break
                time.sleep(0.1)
            self.assertFalse(alive(pid), "process %d is still running" % pid)

    def test_a_program_that_exits_while_its_child_still_holds_the_output_is_bounded_and_leaves_nothing(self):
        result, elapsed, parent, child = self.tree_run("orphan", timeout_seconds=2)
        self.assertEqual((result.status, result.reason), (receipts.READ_LATER, "timeout"))
        self.assertLess(elapsed, 2 + receipts.CLEANUP_SECONDS + 2)
        self.assertGone(parent, child)

    def test_a_program_that_never_stops_is_ended_with_its_children_at_the_deadline(self):
        result, elapsed, parent, child = self.tree_run("hang_tree", timeout_seconds=2)
        self.assertEqual((result.status, result.reason), (receipts.READ_LATER, "timeout"))
        self.assertLess(elapsed, 2 + receipts.CLEANUP_SECONDS + 2)
        self.assertGone(parent, child)

    def test_a_program_that_writes_too_much_is_ended_at_once_with_its_children(self):
        result, elapsed, parent, child = self.tree_run("flood", timeout_seconds=20, max_bytes=100_000)
        self.assertEqual((result.status, result.reason), (receipts.REFUSED, "too_large"))
        self.assertLess(elapsed, 10)                                 # long before the deadline: the cap ended it
        self.assertGone(parent, child)

    def test_a_good_run_leaves_no_process_behind_either(self):
        doc = page([receipt(lease(), 1)], after=0)
        pidfile = os.path.join(self.dir, "pids-good.txt")
        result = self.run_mode("orphan_good", env=[("FAKE_PAGE", json.dumps(doc)), ("PIDFILE", pidfile)])
        self.assertEqual((result.status, [r.seq for r in result.page.receipts]), (receipts.OK, [1]))
        with open(pidfile, "r", encoding="ascii") as handle:
            parent, child = (int(number) for number in handle.read().split())
        self.assertGone(parent, child)

    def test_the_command_is_asked_for_the_configured_page_length(self):
        source = command_source([sys.executable, self.script, "echo"], page_limit=7)
        said = json.loads(receipts._read_command(source, 3).decode())
        self.assertEqual(said["argv"][-4:], ["--after", "3", "--limit", "7"])

    def test_a_command_that_cannot_start_is_read_later(self):
        result = receipts.read_page(command_source([os.path.join(self.dir, "no-such-program")]), 0)
        self.assertEqual((result.status, result.reason), (receipts.READ_LATER, "cannot_run"))


if __name__ == "__main__":
    unittest.main()
