"""Card 7c-2, step 3: the `limit_events` block of the reporter configuration (the account map).

The operator says which Leasewise account each agenttalk folder's seats of one provider use:
(machine, OS user, provider) -> account alias. It is a trusted deployment setting: the reporter
proves nothing from the park files. A seat name never decides an account. Everything is synthetic.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

from quota_balancer.reporter import config as cfg

from tests.reporter_env import agent_table, config_text

NT = sys.platform == "win32"
NO_CHECK = lambda path: None            # noqa: E731


class _Tmp(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH") or None
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-7c2cfg-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.state_path = os.path.join(self.dir, "state", "r.db")
        self.bus = os.path.join(self.dir, "bus")
        self.bus2 = os.path.join(self.dir, "bus2")

    def source(self, bus_root=None, os_user="operator-desktop", provider="claude", account="claude-main"):
        return {"bus_root": bus_root or self.bus, "os_user": os_user, "provider": provider, "account": account}

    def text(self, *sources, raw=None):
        lines = []
        for src in sources:
            lines.append("[[limit_events.sources]]")
            lines += ["%s = %s" % (k, _q(v)) for k, v in src.items()]
        return config_text("http://127.0.0.1:8443", os.path.join(self.dir, "journal"), self.state_path,
                           os.path.join(self.dir, "p.json"), {"alpha": agent_table()},
                           extra=raw if raw is not None else "\n".join(lines))

    def parse(self, *sources, **kw):
        return cfg.parse_config(self.text(*sources, **kw))

    def refused(self, *sources, **kw):
        with self.assertRaises(cfg.ConfigError) as caught:
            self.parse(*sources, **kw)
        self.assertEqual(caught.exception.code, cfg.CONFIG_INVALID)
        return caught.exception


def _q(value):
    import json
    return json.dumps(value)


class Block(_Tmp):

    def test_a_configuration_without_the_block_is_exactly_as_before(self):
        config = self.parse(raw="")
        self.assertEqual(config.limit_sources, ())

    def test_one_source_is_read_with_its_four_fields(self):
        config = self.parse(self.source())
        self.assertEqual(len(config.limit_sources), 1)
        src = config.limit_sources[0]
        self.assertEqual((src.bus_root, src.os_user, src.provider, src.account),
                         (self.bus, "operator-desktop", "claude", "claude-main"))

    def test_the_keys_are_closed_and_every_field_is_required(self):
        for key in ("bus_root", "os_user", "provider", "account"):
            with self.subTest(missing=key):
                self.refused({k: v for k, v in self.source().items() if k != key})
        self.refused(dict(self.source(), model="opus"))
        self.refused(raw="[limit_events]\nsources = []\nextra = 1\n")
        self.refused(raw="[limit_events]\n")                                  # no sources key
        self.refused(raw="[limit_events]\nsources = []\n")                    # an empty list is not a map
        self.refused(raw='[limit_events]\nsources = "x"\n')

    def test_values_are_checked(self):
        self.refused(self.source(os_user="Bad User"))
        self.refused(self.source(account="Bad Account"))
        self.refused(self.source(account=""))
        self.refused(self.source(provider="openai"))
        self.refused(self.source(provider="codex"))                           # v1: Claude only
        self.refused(self.source(bus_root="relative/bus"))
        self.refused(dict(self.source(), bus_root=""))

    def test_at_most_eight_sources(self):
        many = [self.source(bus_root=os.path.join(self.dir, "b%d" % i)) for i in range(8)]
        self.assertEqual(len(self.parse(*many).limit_sources), 8)
        self.refused(*many, self.source(bus_root=os.path.join(self.dir, "b9")))

    def test_an_error_never_repeats_a_value_from_the_file(self):
        marker = "PRIVATE-MARKER-bus-folder"
        for bad in (self.source(bus_root=marker), self.source(provider=marker), self.source(os_user=marker.upper()),
                    dict(self.source(), **{marker: "x"})):
            with self.subTest(bad=list(bad.values())[:1]):
                self.assertNotIn(marker, self.refused(bad).message)
                self.assertNotIn(marker.upper(), self.refused(bad).message)


class Uniqueness(_Tmp):
    """One folder, one provider: judged on the RESOLVED folder, never on the text in the file."""

    def spellings(self):
        spelled = [self.bus + os.sep, os.path.join(self.dir, "x", "..", "bus"), self.bus.replace(os.sep, "/")]
        if NT:
            spelled += [self.bus.upper(), self.bus.lower()]
        return spelled

    def test_two_spellings_of_one_folder_with_conflicting_accounts_refuse(self):
        for spelling in self.spellings():
            with self.subTest(spelling=spelling):
                self.refused(self.source(), self.source(bus_root=spelling, account="claude-other"))

    def test_two_spellings_of_one_folder_with_the_same_account_refuse_too(self):
        for spelling in self.spellings():
            with self.subTest(spelling=spelling):
                self.refused(self.source(), self.source(bus_root=spelling))

    def test_one_claude_source_per_folder_works(self):
        self.assertEqual(len(self.parse(self.source(), self.source(bus_root=self.bus2)).limit_sources), 2)

    def test_the_rule_leaves_room_for_a_later_codex_source_on_the_same_folder(self):
        with mock.patch.object(cfg, "LIMIT_PROVIDERS", ("claude", "codex")):
            config = self.parse(self.source(), self.source(provider="codex", account="codex-main"))
        self.assertEqual({s.provider for s in config.limit_sources}, {"claude", "codex"})
        with mock.patch.object(cfg, "LIMIT_PROVIDERS", ("claude", "codex")):
            self.refused(self.source(provider="codex"), self.source(provider="codex", account="codex-main",
                                                                    bus_root=self.bus + os.sep))


class Consistency(_Tmp):

    def test_one_os_user_and_provider_must_name_one_account(self):
        self.refused(self.source(), self.source(bus_root=self.bus2, account="claude-other"))

    def test_the_same_account_from_two_folders_is_fine(self):
        self.assertEqual(len(self.parse(self.source(), self.source(bus_root=self.bus2)).limit_sources), 2)

    def test_another_os_user_may_use_another_account(self):
        config = self.parse(self.source(), self.source(bus_root=self.bus2, os_user="other-user",
                                                       account="claude-other"))
        self.assertEqual(len(config.limit_sources), 2)

    def test_one_os_user_with_two_providers_may_use_two_accounts(self):
        with mock.patch.object(cfg, "LIMIT_PROVIDERS", ("claude", "codex")):
            config = self.parse(self.source(), self.source(provider="codex", account="codex-main"))
        self.assertEqual(len(config.limit_sources), 2)


class Placement(_Tmp):

    def test_the_bus_folder_may_not_contain_the_state_file(self):
        self.state_path = os.path.join(self.bus, "state", "r.db")
        self.refused(self.source())

    def test_a_folder_beside_the_state_file_is_fine_even_with_a_similar_name(self):
        self.state_path = self.bus + "-reporter" + os.sep + "r.db"          # not inside it
        self.assertEqual(len(self.parse(self.source()).limit_sources), 1)

    def test_the_state_file_may_not_be_reached_through_dots_or_case(self):
        self.state_path = os.path.join(self.bus, "x", "..", "r.db")
        self.refused(self.source())
        if NT:
            self.state_path = os.path.join(self.bus.upper(), "r.db")
            self.refused(self.source())

    def test_the_bus_folder_may_not_contain_the_configuration_file(self):
        os.makedirs(self.bus, exist_ok=True)
        path = os.path.join(self.bus, "reporter.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.text(self.source()))
        with self.assertRaises(cfg.ConfigError) as caught:
            cfg.load_config(path, permissions=NO_CHECK)
        self.assertEqual(caught.exception.code, cfg.CONFIG_INVALID)
        outside = os.path.join(self.dir, "reporter.toml")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write(self.text(self.source()))
        self.assertEqual(len(cfg.load_config(outside, permissions=NO_CHECK).limit_sources), 1)

    def test_the_folder_key_is_the_resolved_normalised_folder(self):
        self.assertEqual(cfg.folder_key(self.bus + os.sep), cfg.folder_key(self.bus))
        self.assertEqual(cfg.folder_key(os.path.join(self.dir, "x", "..", "bus")), cfg.folder_key(self.bus))
        self.assertNotEqual(cfg.folder_key(self.bus), cfg.folder_key(self.bus2))


if __name__ == "__main__":
    unittest.main()
