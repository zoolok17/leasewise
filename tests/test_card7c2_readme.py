"""Card 7c-2, step 4: the README section tells the truth and says the gap in the approved words.

The best-effort sentence appears in `reporter status` and in the README, byte for byte. The section
lists every limit the feature has, and its configuration example is a real, valid configuration.
"""

import os
import re
import unittest

from quota_balancer.reporter import config as cfg, status

from tests.test_card7c2_config import _Tmp as ConfigCase

README = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md")


def section():
    text = open(README, "r", encoding="utf-8", newline="").read().replace("\r\n", "\n")
    start = text.index("## Card 7c-2 - limit events")
    return text[start:]


class Gap(unittest.TestCase):

    def test_the_sentence_is_byte_identical_in_the_readme_and_in_status(self):
        self.assertIn(status.LIMIT_GAP, section())
        self.assertEqual(section().count(status.LIMIT_GAP), 1)
        self.assertTrue(status.LIMIT_GAP.isascii())                           # the status snapshot is written as ASCII

    def test_the_sentence_is_the_approved_one(self):
        self.assertEqual(status.LIMIT_GAP, (
            "A park that starts and ends while the reporter is not running, or between two checks, is never "
            "reported. The seat still parks on its own, and the next refusal after the reporter is back is "
            "reported. This closes when agenttalk keeps a permanent \"parked\" record in its turn journal (a later "
            "agenttalk change, after the turn-journal pull request #300 is merged, together with Codex limit "
            "events). Until then, limit events are best effort."))


class Section(unittest.TestCase):

    def test_there_is_one_section_and_it_is_complete(self):
        text = open(README, "r", encoding="utf-8").read()
        self.assertEqual(text.count("## Card 7c-2"), 1)
        body = " ".join(section().split())
        for needle in ("**Status: built.**", "### What it does, in plain words", "### How to set it up",
                       "### Limits", "R-011", "One outbox, one memory", "more than 256 entries", "No backfill",
                       "No downgrade", "refuses to start", "Claude only", "does not un-park an account by itself",
                       "Advisory until R1", "save its output again", "limit_events.sources",
                       "the reporter never writes into the folder",
                       "the wait between two finished cycles is at most 60 seconds"):
            self.assertIn(needle, body, needle)

    def test_the_wait_cap_is_not_promised_as_a_gap_between_scans(self):
        body = " ".join(section().split())
        for promise in ("never longer than 60 seconds", "never more than 60 seconds apart", "60 seconds apart"):
            self.assertNotIn(promise, body, promise)
        self.assertIn("reading and sending can make the time between two looks longer", body)

    def test_the_account_map_and_counts_are_pointed_to_in_json_not_in_the_plain_text(self):
        body = " ".join(section().split())
        step = body[body.index("**Look at `reporter status"):body.index("### What is read")]
        self.assertTrue(step.startswith("**Look at `reporter status --json`.**"), step[:60])
        self.assertIn("the plain text view shows only the gap sentence and the lines that ask for action", step.lower())

    def test_the_plain_words_come_before_the_technical_detail(self):
        body = section()
        self.assertLess(body.index("### What it does, in plain words"), body.index("### Technical detail"))
        self.assertLess(body.index("### Limits"), body.index("### Technical detail"))

    def test_no_sentence_describes_steps_that_are_now_built_as_missing(self):
        body = section()
        for stale in ("not built yet", "steps 1 to 3", "comes in the next steps", "Reading the park files is step 3",
                      "(step 4)"):
            self.assertNotIn(stale, body)

    def test_the_gap_is_the_first_limit(self):
        body = section()
        limits = body[body.index("### Limits"):]
        self.assertTrue(limits.split("\n- ", 1)[1].startswith("**The gap (best effort).**"))


class Example(ConfigCase):

    def test_the_toml_example_is_a_valid_configuration_block(self):
        match = re.search(r"```toml\n(.*?)```", section(), re.S)
        self.assertIsNotNone(match)
        block = match.group(1)
        block = block.replace("C:/path/to/the/agenttalk/folder", self.bus.replace("\\", "/"))
        config = cfg.parse_config(self.text(raw=block))
        self.assertEqual(len(config.limit_sources), 1)
        self.assertEqual(config.limit_sources[0].provider, "claude")


if __name__ == "__main__":
    unittest.main()
