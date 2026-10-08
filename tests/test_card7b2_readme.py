"""Card 7b, step 7b-4: the README section and the design record tell the truth.

The gap sentence is byte for byte the one `reporter status` prints; the section lists every limit the feature has; its
configuration example is a real, valid configuration once the placeholders are filled in; it holds placeholders only (no
account text); the present-tense claims of the older sections that readings made untrue are corrected; and `docs/DESIGN.md`
says the enforcement cards are paused and replaces the 7b row with the version 2 contract (spec 10.3).
"""

import os
import re
import unittest

from quota_balancer.reporter import config as cfg, status
from tests.test_card7b2_config import _Tmp as ConfigCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    return open(os.path.join(ROOT, name), "r", encoding="utf-8", newline="").read().replace("\r\n", "\n")


def section():
    text = read("README.md")
    start = text.index("## Card 7b - usage readings")
    return text[start:text.index("## Card L6a-1 - gateway receipts", start)]


class Readme(unittest.TestCase):
    def test_there_is_one_section_between_card_7c_2_and_card_l6a_1(self):
        text = read("README.md")
        self.assertEqual(text.count("## Card 7b - usage readings"), 1)
        self.assertLess(text.index("## Card 7c-2 - limit events"), text.index("## Card 7b - usage readings"))
        self.assertLess(text.index("## Card 7b - usage readings"), text.index("## Card L6a-1 - gateway receipts"))

    def test_the_gap_sentence_is_byte_identical_in_the_readme_and_in_status(self):
        self.assertEqual(section().count(status.READING_GAPS), 1)
        self.assertTrue(status.READING_GAPS.isascii())                        # the status snapshot is written as ASCII
        self.assertTrue(status.READING_GAPS.startswith("Readings are best effort."))

    def test_the_section_is_complete_and_says_every_limit(self):
        body = " ".join(section().split())
        needles = ("**Status: built.**", "### What it does, in plain words", "### How to set it up",
                   "### What is sent, and what never is", "### The new status fields", "### Limits", "agenttalk capacity",
                   "readings.sources", "403 FORBIDDEN", "authorized reporter", "One account text names one alias", "limit_events",
                   "two minutes", "A figure that starts a new reset period goes at once",
                   "The loss of an unsent higher figure is accepted", "highest percentage it received",
                   "A garbled success answer is repeated", "Conflict detection holds only while the row exists",
                   "Deletion counts from the time of capture", "(R-011)", "No downgrade of the state file", "version 7",
                   "`status_version` is 5", "last stored figure", "body_invalid", "readings: off", "never says readings are off",
                   "The status-line source is not used", "Never leaves the machine")
        for needle in needles:
            self.assertIn(needle, body, needle)

    def test_the_configuration_example_is_a_valid_block_once_the_placeholders_are_filled(self):
        block = re.search(r"```toml\n(.*?)```", section(), re.S).group(1)
        self.assertIn("claude:<os-user>:home-<hash>", block)
        filled = block.replace("<os-user>", "operator").replace("<hash>", "0123abcd")

        class Case(ConfigCase):
            def runTest(self):
                pass
        case = Case()
        case.setUp()
        try:
            filled = filled.replace("C:/path/to/the/agenttalk/folder", case.bus.replace("\\", "/"))
            config = cfg.parse_config(case.text(raw=filled))
            self.assertEqual([(r.provider, r.account) for r in config.reading_sources], [("claude", "claude-main")])
        finally:
            case.doCleanups()

    def test_the_section_holds_placeholders_only(self):
        text = section()
        self.assertIsNone(re.search(r"(claude|codex):[^<\s:`]+:home-[0-9a-f]{8}", text))
        self.assertIsNone(re.search(r"home-[0-9a-f]{8}", text))
        self.assertNotIn("\\Users\\", text)

    def test_the_older_sections_no_longer_make_claims_that_readings_made_untrue(self):
        text = read("README.md")
        flat = " ".join(text.split())
        self.assertNotIn("`status_version` is 3", flat)
        self.assertNotIn("`status_version` is 4", flat)
        self.assertIn("and 7 since card 7b;", flat)
        self.assertIn("since card 7b the order is limit, reading, receipt, lease", flat)
        self.assertIn("and 7 since card 7b; `status_version` was 3 then and is 5 since card 7b", flat)

    def test_the_status_line_claims_are_not_present_tense_in_the_readme(self):
        text = read("README.md").lower()
        for claim in ("statusline", "status-line"):
            for match in re.finditer(claim, text):
                around = text[max(0, match.start() - 80):match.end() + 80]
                self.assertIn("not used", around)
        self.assertNotIn("isolated per-seat rollout", text)


class Design(unittest.TestCase):
    def test_the_enforcement_cards_are_paused_as_the_design_says(self):
        text = read("docs/DESIGN.md")
        self.assertIn("**PAUSED by the operator, 2026-10-07** (`C:\\lw\\records\\DECISION-ENFORCEMENT-2026-10-07.md`, register D-288)", text)
        self.assertIn("The first goal is seeing each account's weekly usage (card 7b).", text)
        heading = text.index("### Remaining cards")
        self.assertLess(heading, text.index("**PAUSED by the operator"))
        self.assertLess(text.index("**PAUSED by the operator"), text.index("| Card | Tables/module"))
        for prefix in ("| 6a: gateway child binding | ", "| 6b: required sized-reservation gate | ", "| 7d: context/enforcement switch | "):
            self.assertIn(prefix + "**PAUSED (2026-10-07, D-288).**", text)
        self.assertIn("**PAUSED (2026-10-07, D-288).** The enforcement sub-cards 6a-2a, 6a-2b and 6a-3; the child binding already merged stays.", text)
        self.assertEqual(text.count("**PAUSED (2026-10-07, D-288).**"), 3)

    def test_the_7b_row_is_the_version_2_contract(self):
        text = read("docs/DESIGN.md")
        row = [line for line in text.splitlines() if line.startswith("| 7b:")]
        self.assertEqual(len(row), 1)
        self.assertIn("DESIGN-7B2.md", row[0])
        for name in ("two_accounts", "missing_reset", "privacy_7b", "freshest_per_window", "same_figures_once", "unmapped_account"):
            self.assertIn(name, row[0])
        self.assertNotIn("source parser", row[0])

    def test_section_5_is_marked_superseded_and_the_older_text_stays(self):
        text = read("docs/DESIGN.md")
        self.assertIn("Reject ambiguous shared statusline attribution", text)
        self.assertIn("Codex uses isolated per-seat rollout sources.", text)
        self.assertIn("Add a headless Claude rate_limit_event parser", text)
        sentence = ("Superseded by register D-081/D-082 and `DESIGN-7B2.md`: shared sources are accepted and merged per account; "
                    "the map is the operator's trusted table keyed on agenttalk's account text; the status-line route is not used.")
        self.assertEqual(text.count(sentence), 1)


if __name__ == "__main__":
    unittest.main()
