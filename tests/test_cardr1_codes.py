"""Card R1, step R1-0: every refusal word the design names is defined, with its status, exit code and message.

docs/DESIGN.md (section 7, the error list) names refusal words that Leasewise did not define. This step defines
them all at once so that later steps only have to emit them. Nothing emits the new words yet, so nothing changes
for a caller. The list in the design file itself is read by these tests, so a change to the design list that the
code does not follow turns a test red. Synthetic data only.
"""

import os
import re
import unittest
import uuid

from quota_balancer import errors, http_api
from quota_balancer.reporter import decisions

DESIGN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "DESIGN.md")

# DESIGN.md section 11, "Later HTTP mapping": scope=409, basis=409, unknown/stale=503, divergence/opening=409.
BILLING = {
    "BILLING_SCOPE_MISMATCH": 409, "BILLING_BASIS_UNKNOWN": 409, "BILLING_UNKNOWN": 503,
    "BILLING_STALE": 503, "BILLING_DIVERGENCE": 409, "OPENING_UNSAFE": 409,
}
# The nine words of the design list that were missing before this step.
NEW = {
    "CONTEXT_ROTATION_REQUIRED": 400, "UNSUPPORTED_EFFORT": 400, "CONTEXT_UNAVAILABLE": 403,
    "ENVELOPE_INCOMPATIBLE": 403, "INPUT_BOUND_UNAVAILABLE": 403, "POLICY_MISMATCH": 409,
    "METER_STALE": 503, "RECONCILIATION_REQUIRED": 503, "SERVICE_UNAVAILABLE": 503,
}


def design_list():
    """{code: status} read from the error paragraph of docs/DESIGN.md."""
    with open(DESIGN, "r", encoding="utf-8") as handle:
        text = handle.read().replace("\r\n", "\n")
    start = text.index("Errors: {error:")
    end = text.index("Section 11 adds billing error mappings", start)
    found = {}
    for status, words in re.findall(r"(\d{3}) ([A-Z_,\s]+?)(?=;|\.|\Z)", text[start:end]):
        for word in re.findall(r"[A-Z][A-Z_]+", words):
            found[word] = int(status)
    return found


def all_codes():
    return {value for name, value in vars(errors).items() if name.startswith("CODE_") and isinstance(value, str)}


class DesignList(unittest.TestCase):

    def test_the_reader_finds_the_whole_design_list(self):
        found = design_list()
        self.assertEqual(found["INVALID_REQUEST"], 400)
        self.assertEqual(found["SERVICE_UNAVAILABLE"], 503)
        self.assertGreaterEqual(len(found), 25)

    def test_every_code_of_the_design_list_exists_with_the_designs_status(self):
        for word, status in sorted(design_list().items()):
            with self.subTest(code=word):
                self.assertIn(word, all_codes(), "not defined in errors.py")
                self.assertEqual(http_api._HTTP_STATUS.get(word), status)

    def test_the_nine_new_words_are_exactly_the_ones_that_were_missing(self):
        self.assertEqual({w for w in design_list() if w not in all_codes() - set(NEW)}, set(NEW))
        for word, status in NEW.items():
            self.assertEqual(http_api._HTTP_STATUS[word], status, word)

    def test_the_six_billing_words_exist_and_have_the_designs_status(self):
        for word, status in BILLING.items():
            with self.subTest(code=word):
                self.assertIn(word, all_codes())
                self.assertEqual(http_api._HTTP_STATUS[word], status)


class EveryCode(unittest.TestCase):

    def test_every_code_has_an_exit_code_and_a_fixed_non_empty_message(self):
        for code in sorted(all_codes()):
            with self.subTest(code=code):
                if code not in (errors.CODE_UNAUTHENTICATED, errors.CODE_FORBIDDEN):     # transport-only (card 5)
                    self.assertIn(code, errors.EXIT_CODES)
                    self.assertIn(errors.EXIT_CODES[code], (2, 3))
                message = errors.MESSAGES[code]
                self.assertIsInstance(message, str)
                self.assertTrue(message.strip())

    def test_the_registry_has_no_unknown_codes(self):
        self.assertEqual(set(errors.MESSAGES), all_codes())

    def test_a_store_fault_or_an_unavailable_service_exits_3_and_everything_else_2(self):
        self.assertEqual(errors.EXIT_CODES[errors.CODE_STORE_ERROR], 3)
        self.assertEqual(errors.EXIT_CODES[errors.CODE_SERVICE_UNAVAILABLE], 3)
        for code in all_codes() - {errors.CODE_STORE_ERROR, errors.CODE_SERVICE_UNAVAILABLE,
                                   errors.CODE_UNAUTHENTICATED, errors.CODE_FORBIDDEN}:
            self.assertEqual(errors.EXIT_CODES[code], 2, code)

    def test_no_two_codes_share_a_message(self):
        seen = {}
        for code, message in errors.MESSAGES.items():
            self.assertNotIn(message, seen, "%s and %s say the same thing" % (code, seen.get(message)))
            seen[message] = code

    def test_a_message_never_carries_a_placeholder_or_a_value(self):
        for code, message in errors.MESSAGES.items():
            with self.subTest(code=code):
                for mark in ("{", "}", "%", "<", ">", "$", "\\", "\n"):
                    self.assertNotIn(mark, message)
                self.assertTrue(message.isascii())
                self.assertLessEqual(len(message), 120)

    def test_the_new_messages_say_what_happened_and_what_to_do_in_plain_words(self):
        for word in NEW:
            message = errors.MESSAGES[word]
            self.assertGreaterEqual(len(message.split()), 5, word)


class Wiring(unittest.TestCase):

    def test_the_reporters_closed_list_contains_the_new_words_without_a_change_there(self):
        for word in list(NEW) + list(BILLING):
            self.assertIn(word, decisions.CLOSED_CODES, word)
            self.assertEqual(decisions.closed_code(word), word)

    def test_the_new_words_are_raised_with_their_registry_message(self):
        for word in NEW:
            error = errors.QuotaError(word, errors.MESSAGES[word])
            self.assertEqual((error.code, error.message), (word, errors.MESSAGES[word]))

    def test_unsupported_effort_is_defined_and_mapped_only(self):
        # an unknown effort alias is still INVALID_REQUEST (privacy rule privacy_4): nothing emits the new word
        import inspect
        from quota_balancer import lease
        self.assertNotIn("CODE_UNSUPPORTED_EFFORT", inspect.getsource(lease))
        self.assertNotIn("UNSUPPORTED_EFFORT", inspect.getsource(lease))

    def test_nothing_in_the_product_emits_a_new_word_yet(self):
        """Card R1-1 uses METER_STALE as a grant WARNING (never as a refusal); every other new word is still unused."""
        import inspect
        from quota_balancer import lease, store
        for module in (lease, store):
            source = inspect.getsource(module)
            for word in NEW:
                if word == "METER_STALE" and module is lease:
                    self.assertNotIn("QuotaError(errors.CODE_METER_STALE", source)     # a warning, never a refusal
                    continue
                self.assertNotIn("CODE_" + word, source, (module.__name__, word))


class UsageKind(unittest.TestCase):

    def test_the_usage_reading_kind_is_still_refused_with_its_fixed_error(self):
        body = {"event_id": str(uuid.uuid4()), "account_id": str(uuid.uuid4()), "observed_at": 1, "kind": "usage",
                "payload": {}}
        with self.assertRaises(errors.QuotaError) as caught:
            http_api._wire_to_reading(body, (str(uuid.uuid4()), "desktop"), None)
        self.assertEqual(caught.exception.code, errors.CODE_INVALID_REQUEST)
        self.assertEqual(caught.exception.message, errors.MSG_INVALID_REQUEST)

    def test_the_design_says_the_kind_was_removed_and_keeps_its_old_text_visible(self):
        with open(DESIGN, "r", encoding="utf-8") as handle:
            text = handle.read().replace("\r\n", "\n")
        self.assertIn("Removed 2026-10-03 (lead decision D-116): usage travels in /settle receipts; "
                      "the kind stays refused.", text)
        self.assertIn("~~usage: {turn_id,lease_id?,input_tokens,cached_input_tokens,output_tokens,calls,final,"
                      "uncoordinated};", text)


if __name__ == "__main__":
    unittest.main()
