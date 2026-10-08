"""Card 7b, step 7b-2: the `readings` block of the reporter configuration (DESIGN-7B2.md section 2).

The operator copies each agenttalk account text (as `agenttalk capacity` prints it) into a row and names the
Leasewise account alias it belongs to. One exact account text names one alias in the whole file; different
texts may share an alias. Every failure is one fixed sentence that names no value. Everything is synthetic.
"""

import json
import os
import tempfile
import unittest

from quota_balancer.reporter import config as cfg
from tests.reporter_env import agent_table, config_text

NO_CHECK = lambda path: None            # noqa: E731
CLAUDE_X = "claude:<os-user>:home-0123abcd"
CLAUDE_Y = "claude:<os-user>:home-89abcdef"
CODEX_X = "codex:<os-user>:home-4567ef01"
ID_A = "11111111-1111-4111-8111-111111111111"
ID_B = "22222222-2222-4222-8222-222222222222"
MARKERS = ("<os-user>", "home-0123abcd", "home-89abcdef", "home-4567ef01", "SECRET-USER")


def _q(value):
    return json.dumps(value)


class _Tmp(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH") or None
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-7b2cfg-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.state_path = os.path.join(self.dir, "state", "r.db")
        self.bus = os.path.join(self.dir, "bus")
        self.bus2 = os.path.join(self.dir, "bus2")

    def row(self, bus_root=None, provider="claude", agenttalk_account=CLAUDE_X, account="claude-main"):
        return {"bus_root": bus_root or self.bus, "provider": provider, "agenttalk_account": agenttalk_account,
                "account": account}

    def text(self, *rows, limit=(), raw=None):
        lines = []
        for src in limit:
            lines.append("[[limit_events.sources]]")
            lines += ["%s = %s" % (k, _q(v)) for k, v in src.items()]
        for src in rows:
            lines.append("[[readings.sources]]")
            lines += ["%s = %s" % (k, _q(v)) for k, v in src.items()]
        return config_text("http://127.0.0.1:8443", os.path.join(self.dir, "journal"), self.state_path,
                           os.path.join(self.dir, "p.json"), {"alpha": agent_table()},
                           extra=raw if raw is not None else "\n".join(lines))

    def parse(self, *rows, **kw):
        return cfg.parse_config(self.text(*rows, **kw))

    def refused(self, *rows, **kw):
        with self.assertRaises(cfg.ConfigError) as caught:
            self.parse(*rows, **kw)
        self.assertEqual(caught.exception.code, cfg.CONFIG_INVALID)
        for marker in MARKERS:
            self.assertNotIn(marker, caught.exception.message)
        return caught.exception


class Block(_Tmp):
    def test_a_configuration_without_the_block_is_exactly_as_before(self):
        config = self.parse(raw="")
        self.assertEqual(config.reading_sources, ())

    def test_one_row_is_read_with_its_four_fields_and_a_resolved_folder(self):
        config = self.parse(self.row())
        self.assertEqual(len(config.reading_sources), 1)
        row = config.reading_sources[0]
        self.assertEqual((row.bus_root, row.provider, row.agenttalk_account, row.account),
                         (self.bus, "claude", CLAUDE_X, "claude-main"))
        self.assertEqual(row.folder, cfg.folder_key(self.bus))

    def test_the_keys_are_closed_and_every_field_is_required(self):
        for key in ("bus_root", "provider", "agenttalk_account", "account"):
            with self.subTest(missing=key):
                self.refused({k: v for k, v in self.row().items() if k != key})
        self.refused(dict(self.row(), os_user="x"))
        self.refused(raw="[readings]\nsources = []\nextra = 1\n")
        self.refused(raw="[readings]\n")
        self.refused(raw='readings = "yes"\n')

    def test_at_most_sixteen_rows_and_at_least_one(self):
        self.refused(raw="[readings]\nsources = []\n")
        rows = [self.row(agenttalk_account="claude:u:home-%08x" % n, account="a%d" % n) for n in range(16)]
        self.assertEqual(len(self.parse(*rows).reading_sources), 16)
        rows.append(self.row(agenttalk_account="claude:u:home-%08x" % 99, account="a99"))
        self.refused(*rows)

    def test_a_row_must_be_a_table(self):
        self.refused(raw='[readings]\nsources = ["x"]\n')

    def test_the_provider_is_claude_or_codex(self):
        self.assertEqual(self.parse(self.row(provider="codex", agenttalk_account=CODEX_X)).reading_sources[0].provider, "codex")
        for bad in ("gemini", "Claude", "", 3, None):
            with self.subTest(provider=bad):
                self.refused(self.row(provider=bad))

    def test_limit_events_still_reads_claude_only(self):
        self.assertEqual(cfg.LIMIT_PROVIDERS, ("claude",))

    def test_the_bus_root_must_be_an_absolute_path(self):
        self.refused(self.row(bus_root="relative/bus"))
        self.refused(dict(self.row(), bus_root=""))

    def test_the_alias_must_be_a_valid_alias(self):
        for bad in ("Claude Main", "", "a" * 65, "-x", 5):
            with self.subTest(alias=bad):
                self.refused(self.row(account=bad))


class AccountText(_Tmp):
    def test_good_texts(self):
        for provider, text in (("claude", CLAUDE_X), ("codex", CODEX_X), ("claude", "claude:a:home-00000000"),
                               ("claude", "claude:domain:user:home-ffffffff"), ("claude", "claude:" + "u" * 100 + ":home-0123abcd")):
            with self.subTest(text=text):
                self.assertEqual(self.parse(self.row(provider=provider, agenttalk_account=text)).reading_sources[0].agenttalk_account, text)

    def test_bad_texts(self):
        bad = ("", "claude::home-0123abcd", "claude:home-0123abcd", "claude:u:home-0123ABCD", "claude:u:home-0123abc",
               "claude:u:home-0123abcde", "claude:u:home-0123abcg", "codex:u:home-0123abcd", "Claude:u:home-0123abcd",
               "claude:u:home-0123abcd ", " claude:u:home-0123abcd", "claude:u\n:home-0123abcd", "claude:u\x00:home-0123abcd",
               "claude:u\x7f:home-0123abcd", "claude:" + "u" * 160 + ":home-0123abcd", "x", 5, None, ["claude:u:home-0123abcd"])
        for text in bad:
            with self.subTest(text=repr(text)[:40]):
                self.refused(self.row(agenttalk_account=text))

    def test_the_limit_is_160_characters(self):
        user = "u" * (160 - len("claude:") - len(":home-0123abcd"))
        text = "claude:%s:home-0123abcd" % user
        self.assertEqual(len(text), 160)
        self.parse(self.row(agenttalk_account=text))
        self.refused(self.row(agenttalk_account=text.replace("u", "uu", 1)))

    def test_the_provider_word_must_match_the_row(self):
        self.refused(self.row(provider="codex", agenttalk_account=CLAUDE_X))
        self.refused(self.row(provider="claude", agenttalk_account=CODEX_X))


class Rules(_Tmp):
    def test_two_rows_may_not_share_a_folder_and_a_text(self):
        self.refused(self.row(), self.row(account="claude-main"))
        self.refused(self.row(), self.row(bus_root=self.bus + os.sep + ".." + os.sep + "bus"))        # one folder, two spellings

    def test_one_text_names_one_alias_in_the_whole_file(self):
        err = self.refused(self.row(bus_root=self.bus, account="alpha"), self.row(bus_root=self.bus2, account="beta"))
        self.assertIn("one agenttalk account must name one account alias", err.message)
        self.refused(self.row(bus_root=self.bus, account="alpha"), self.row(bus_root=self.bus, provider="claude", account="beta"))

    def test_one_text_one_alias_across_two_folders_is_fine(self):
        config = self.parse(self.row(bus_root=self.bus, account="alpha"), self.row(bus_root=self.bus2, account="alpha"))
        self.assertEqual(len(config.reading_sources), 2)

    def test_two_texts_may_share_an_alias(self):
        config = self.parse(self.row(agenttalk_account=CLAUDE_X, account="alpha"),
                            self.row(agenttalk_account=CLAUDE_Y, account="alpha"))
        self.assertEqual([r.account for r in config.reading_sources], ["alpha", "alpha"])
        config = self.parse(self.row(bus_root=self.bus, agenttalk_account=CLAUDE_X, account="alpha"),
                            self.row(bus_root=self.bus2, agenttalk_account=CLAUDE_Y, account="alpha"))
        self.assertEqual(len(config.reading_sources), 2)

    def test_the_same_alias_for_two_texts_does_not_hide_a_conflict_on_a_third_row(self):
        self.refused(self.row(agenttalk_account=CLAUDE_X, account="alpha"),
                     self.row(agenttalk_account=CLAUDE_Y, account="alpha"),
                     self.row(bus_root=self.bus2, agenttalk_account=CLAUDE_X, account="beta"))

    def test_the_state_file_may_not_be_inside_a_bus_folder(self):
        self.state_path = os.path.join(self.bus, "state", "r.db")
        self.refused(self.row())

    def test_the_configuration_file_may_not_be_inside_a_readings_folder(self):
        path = os.path.join(self.bus, "reporter.toml")
        os.makedirs(self.bus)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.text(self.row()))
        with self.assertRaises(cfg.ConfigError) as caught:
            cfg.load_config(path, permissions=NO_CHECK)
        self.assertEqual(caught.exception.code, cfg.CONFIG_INVALID)
        self.assertIn("readings", caught.exception.message)
        outside = os.path.join(self.dir, "reporter.toml")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write(self.text(self.row()))
        self.assertEqual(len(cfg.load_config(outside, permissions=NO_CHECK).reading_sources), 1)


class WithLimitEvents(_Tmp):
    def limit(self, bus_root=None, provider="claude", account="claude-main"):
        return {"bus_root": bus_root or self.bus, "os_user": "operator-desktop", "provider": provider, "account": account}

    def test_the_same_folder_and_provider_must_name_the_same_alias(self):
        err = self.refused(self.row(account="beta"), limit=[self.limit(account="alpha")])
        self.assertIn("an account differs from limit_events for the same folder and provider", err.message)
        self.parse(self.row(account="alpha"), limit=[self.limit(account="alpha")])

    def test_the_check_is_per_folder_and_provider(self):
        self.parse(self.row(bus_root=self.bus2, account="beta"), limit=[self.limit(account="alpha")])
        self.parse(self.row(provider="codex", agenttalk_account=CODEX_X, account="beta"), limit=[self.limit(account="alpha")])

    def test_two_spellings_of_one_folder_are_one_folder(self):
        self.refused(self.row(bus_root=self.bus + os.sep + ".." + os.sep + "bus", account="beta"),
                     limit=[self.limit(account="alpha")])

    def test_the_rule_binds_every_row_of_the_folder(self):
        self.refused(self.row(agenttalk_account=CLAUDE_X, account="alpha"), self.row(agenttalk_account=CLAUDE_Y, account="beta"),
                     limit=[self.limit(account="alpha")])

    def test_the_message_points_at_the_documentation_and_names_no_value(self):
        err = self.refused(self.row(account="beta"), limit=[self.limit(account="alpha")])
        self.assertNotIn("alpha", err.message)
        self.assertNotIn("beta", err.message)


class Hiding(_Tmp):
    def test_the_text_is_hidden_from_repr_and_str(self):
        config = self.parse(self.row())
        row = config.reading_sources[0]
        for text in (repr(row), str(row), repr(config), str(config), repr(config.reading_sources)):
            for marker in MARKERS + (self.bus, "claude-main"):
                self.assertNotIn(marker, text)

    def test_every_error_names_no_value(self):
        samples = [dict(self.row(), account="Bad Alias"), dict(self.row(), agenttalk_account="claude:SECRET-USER"),
                   dict(self.row(), provider="SECRET-USER"), dict(self.row(), bus_root="SECRET-USER/x"),
                   dict(self.row(), extra="SECRET-USER")]
        for sample in samples:
            self.refused(sample)


class Resolution(_Tmp):
    def snapshot(self, accounts):
        return {"seats": [], "models": [], "task_classes": [], "accounts": accounts, "gateways": []}

    def test_a_row_resolves_to_the_local_id(self):
        config = self.parse(self.row(account="alpha"), self.row(bus_root=self.bus2, agenttalk_account=CLAUDE_Y, account="beta"))
        resolved = cfg.resolve_reading_sources(config, self.snapshot([("alpha", ID_A), ("beta", ID_B)]))
        self.assertEqual([(r.provider, r.account, r.account_id) for r in resolved], [("claude", "alpha", ID_A), ("claude", "beta", ID_B)])
        self.assertEqual(resolved[0].agenttalk_account, CLAUDE_X)
        self.assertEqual(resolved[0].folder, cfg.folder_key(self.bus))

    def test_without_the_block_nothing_resolves(self):
        self.assertEqual(cfg.resolve_reading_sources(self.parse(raw=""), self.snapshot([])), ())

    def test_an_account_missing_from_the_snapshot_is_unresolved(self):
        config = self.parse(self.row(account="alpha"))
        for accounts, word in (([], "is not in the policy-ids snapshot"), ([("alpha", ID_A), ("alpha", ID_B)], "is ambiguous"),
                               ([("alpha", "not-a-uuid")], "is not valid"), ([("alpha", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA")], "is not valid")):
            with self.subTest(word=word):
                with self.assertRaises(cfg.ConfigError) as caught:
                    cfg.resolve_reading_sources(config, self.snapshot(accounts))
                self.assertEqual(caught.exception.code, cfg.CONFIG_UNRESOLVED)
                self.assertTrue(caught.exception.message.startswith("readings: "))
                self.assertIn(word, caught.exception.message)
                for marker in MARKERS + ("alpha", "not-a-uuid"):
                    self.assertNotIn(marker, caught.exception.message)

    def test_the_resolved_row_hides_its_text_too(self):
        config = self.parse(self.row(account="alpha"))
        row = cfg.resolve_reading_sources(config, self.snapshot([("alpha", ID_A)]))[0]
        for text in (repr(row), str(row)):
            for marker in MARKERS + (self.bus, "alpha", ID_A):
                self.assertNotIn(marker, text)

    def test_the_same_sentences_as_the_limit_events_resolution(self):
        config = self.parse(self.row(account="alpha"), limit=[{"bus_root": self.bus, "os_user": "u", "provider": "claude", "account": "alpha"}])
        for accounts in ([], [("alpha", ID_A), ("alpha", ID_B)], [("alpha", "bad")]):
            with self.assertRaises(cfg.ConfigError) as limit:
                cfg.resolve_limit_sources(config, self.snapshot(accounts))
            with self.assertRaises(cfg.ConfigError) as reading:
                cfg.resolve_reading_sources(config, self.snapshot(accounts))
            self.assertEqual(reading.exception.message, limit.exception.message.replace("limit_events:", "readings:"))


if __name__ == "__main__":
    unittest.main()
