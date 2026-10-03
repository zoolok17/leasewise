"""Card L6a-1, step 2: the `gateway_receipts` configuration block and the gateway ids.

The block names a gateway by ALIAS and says where its receipts are read from: exactly one of a fixture
file or a command (an argument list). Keys are closed and every error is a fixed sentence. The
gateway's local id comes from `policy-ids` (a new `gateways` list: alias and id, nothing else) and the
reporter refuses to start when the alias is missing, ambiguous or not a UUID, the way card 7c-2 did for
accounts. Without the block nothing changes. Synthetic data only.
"""

import json
import os
import sys
import tempfile
import unittest
import uuid

from quota_balancer import store
from quota_balancer.reporter import config as cfg

from tests.l6a1_env import GATEWAY_ALIAS, GATEWAY_ID
from tests.reporter_env import Rig, agent_table, build_store, config_text

LOUD = "loud-config-value-l6a1"
EXE = os.path.abspath(sys.executable)


def _q(value):
    return json.dumps(value)


class _Tmp(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH") or None
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-l6a1cfg-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.fixture = os.path.join(self.dir, "receipts.json")

    def text(self, raw):
        return config_text("http://127.0.0.1:8443", os.path.join(self.dir, "journal"),
                           os.path.join(self.dir, "state", "r.db"), os.path.join(self.dir, "p.json"),
                           {"alpha": agent_table()}, extra=raw)

    def parse(self, raw):
        return cfg.parse_config(self.text(raw))

    def refused(self, raw):
        with self.assertRaises(cfg.ConfigError) as caught:
            self.parse(raw)
        self.assertEqual(caught.exception.code, cfg.CONFIG_INVALID)
        return caught.exception

    def fixture_block(self, extra=""):
        return "[gateway_receipts]\ngateway = %s\nfixture = %s\n%s" % (_q(GATEWAY_ALIAS), _q(self.fixture), extra)

    def command_block(self, command, extra=""):
        return "[gateway_receipts]\ngateway = %s\ncommand = %s\n%s" % (_q(GATEWAY_ALIAS), _q(command), extra)


class Block(_Tmp):

    def test_a_configuration_without_the_block_is_exactly_as_before(self):
        self.assertIsNone(self.parse("").gateway_receipts)

    def test_a_fixture_block_is_read_with_its_defaults(self):
        source = self.parse(self.fixture_block()).gateway_receipts
        self.assertEqual((source.gateway, source.fixture, source.command), (GATEWAY_ALIAS, self.fixture, None))
        self.assertEqual((source.timeout_seconds, source.max_bytes, source.env, source.gateway_id),
                         (20, 1024 * 1024, (), None))

    def test_a_command_block_is_read_as_an_argument_list(self):
        command = [EXE, "-m", "agenttalk", "ovh-gateway", "receipts", "--json"]
        source = self.parse(self.command_block(command, "timeout_seconds = 5\nmax_bytes = 4096\n"
                                                        "[gateway_receipts.env]\nAGENTTALK_ROOT = \"x\"\n"
                                               )).gateway_receipts
        self.assertEqual((source.command, source.fixture), (tuple(command), None))
        self.assertEqual((source.timeout_seconds, source.max_bytes, source.env), (5, 4096, (("AGENTTALK_ROOT", "x"),)))

    def test_exactly_one_source_is_named(self):
        self.refused("[gateway_receipts]\ngateway = %s\n" % _q(GATEWAY_ALIAS))
        self.refused(self.fixture_block("command = %s\n" % _q([EXE])))

    def test_the_keys_are_closed(self):
        self.refused(self.fixture_block("agent = %s\n" % _q(LOUD)))
        self.refused("[gateway_receipts]\nfixture = %s\n" % _q(self.fixture))          # no gateway
        self.refused("gateway_receipts = 5\n")
        self.refused("gateway_receipts = [%s]\n" % _q(GATEWAY_ALIAS))

    def test_the_gateway_must_be_a_valid_alias(self):
        for bad in ("", "Has Space", "A" * 80, "../x", 5):
            self.refused("[gateway_receipts]\ngateway = %s\nfixture = %s\n" % (_q(bad), _q(self.fixture)))

    def test_the_fixture_must_be_an_absolute_path(self):
        self.refused("[gateway_receipts]\ngateway = %s\nfixture = \"receipts.json\"\n" % _q(GATEWAY_ALIAS))

    def test_the_command_rules(self):
        for bad in ([], ["relative-program"], [EXE, ""], [EXE, 5], [EXE] + ["x"] * 16, [EXE, "--after", "3"],
                    [EXE, "a\x00b"], "not a list"):
            error = self.refused(self.command_block(bad))
            self.assertNotIn(LOUD, error.message)

    def test_the_limits_and_the_environment_rules(self):
        for extra in ("timeout_seconds = 0\n", "timeout_seconds = 61\n", "timeout_seconds = true\n",
                      "timeout_seconds = \"5\"\n", "max_bytes = 10\n", "max_bytes = 5000000\n",
                      "max_bytes = 1.5\n"):
            self.refused(self.fixture_block(extra))
        for env in ("[gateway_receipts.env]\nlower = \"x\"\n", "[gateway_receipts.env]\nA = 5\n",
                    "[gateway_receipts.env]\nA = \"\"\n", "[gateway_receipts.env]\nA = \"x\\u0000y\"\n",
                    "[gateway_receipts.env]\n" + "".join("K%d = \"x\"\n" % i for i in range(9))):
            self.refused(self.command_block([EXE], env))

    def test_an_error_carries_no_value(self):
        error = self.refused(self.fixture_block("agent = %s\n" % _q(LOUD)))
        self.assertNotIn(LOUD, error.message)
        self.assertNotIn(self.fixture, error.message)
        source = self.parse(self.command_block([EXE, LOUD])).gateway_receipts
        self.assertNotIn(LOUD, repr(source) + str(source))
        self.assertNotIn(EXE, repr(source))


def snapshot(gateways=None, **extra):
    base = {"seats": [], "models": [], "task_classes": []}
    if gateways is not None:
        base["gateways"] = gateways
    base.update(extra)
    return base


class PolicyIds(unittest.TestCase):

    def test_the_listing_has_the_gateways_sorted_with_only_alias_and_id(self):
        rig = Rig(server=False)
        self.addCleanup(rig.cleanup)
        db = os.path.join(rig.ws.dir, "inventory.db")
        build_store(db, rig.ws.dir)
        data = store.policy_ids(db)
        aliases = [g["alias"] for g in data["gateways"]]
        self.assertEqual(aliases, sorted(aliases))
        self.assertTrue(aliases)
        for entry in data["gateways"]:
            self.assertEqual(set(entry), {"alias", "id"})
            uuid.UUID(entry["id"])
        self.assertEqual(set(data), {"seats", "models", "task_classes", "accounts", "gateways"})


class Snapshot(_Tmp):

    def load(self, doc):
        path = os.path.join(self.dir, "p-%s.json" % uuid.uuid4().hex)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)
        return cfg.load_snapshot(path)

    def test_the_gateways_are_optional_and_default_to_none(self):
        self.assertEqual(self.load(snapshot())["gateways"], [])

    def test_the_gateways_are_read_as_alias_and_id_pairs(self):
        data = self.load(snapshot([{"alias": "g-a", "id": GATEWAY_ID}]))
        self.assertEqual(data["gateways"], [("g-a", GATEWAY_ID)])

    def test_a_malformed_list_refuses_the_snapshot(self):
        for bad in ("x", {"alias": "a"}, [{"alias": "a", "id": GATEWAY_ID, "extra": 1}], [{"alias": 1, "id": GATEWAY_ID}],
                    [{"alias": "a", "id": 5}], [["a", GATEWAY_ID]], [None]):
            with self.subTest(gateways=bad):
                with self.assertRaises(cfg.ConfigError) as caught:
                    self.load(snapshot(bad))
                self.assertEqual(caught.exception.code, cfg.CONFIG_UNRESOLVED)


class Resolution(_Tmp):

    def resolve(self, gateways):
        config = self.parse(self.fixture_block())
        return cfg.resolve_gateway_receipts(config, self.load(snapshot(gateways)))

    load = Snapshot.load

    def unresolved(self, gateways):
        with self.assertRaises(cfg.ConfigError) as caught:
            self.resolve(gateways)
        self.assertEqual(caught.exception.code, cfg.CONFIG_UNRESOLVED)
        return caught.exception

    def test_the_gateway_gets_its_id(self):
        source = self.resolve([{"alias": GATEWAY_ALIAS, "id": GATEWAY_ID}, {"alias": "other", "id": str(uuid.uuid4())}])
        self.assertEqual((source.gateway, source.gateway_id, source.fixture), (GATEWAY_ALIAS, GATEWAY_ID, self.fixture))

    def test_without_the_block_nothing_resolves_even_with_an_old_snapshot(self):
        config = self.parse("")
        self.assertIsNone(cfg.resolve_gateway_receipts(config, self.load(snapshot())))

    def test_a_missing_gateway_refuses_start(self):
        self.unresolved([{"alias": "someone-else", "id": GATEWAY_ID}])
        self.unresolved(None)                                           # an old snapshot without the list

    def test_an_ambiguous_alias_refuses_start_and_never_picks_the_first(self):
        other = str(uuid.uuid4())
        error = self.unresolved([{"alias": GATEWAY_ALIAS, "id": GATEWAY_ID}, {"alias": GATEWAY_ALIAS, "id": other}])
        self.assertNotIn(GATEWAY_ID, error.message)
        self.assertNotIn(other, error.message)

    def test_an_id_that_is_not_a_uuid_refuses_start(self):
        for bad in ("not-an-id", GATEWAY_ID.upper(), GATEWAY_ID + "0", ""):
            self.unresolved([{"alias": GATEWAY_ALIAS, "id": bad}])

    def test_an_error_names_no_value(self):
        error = self.unresolved([{"alias": "someone-else", "id": GATEWAY_ID}])
        self.assertNotIn(GATEWAY_ALIAS, error.message)
        self.assertNotIn(self.fixture, error.message)

    def test_the_resolved_copy_is_new_and_the_original_is_untouched(self):
        config = self.parse(self.fixture_block())
        resolved = cfg.resolve_gateway_receipts(
            config, self.load(snapshot([{"alias": GATEWAY_ALIAS, "id": GATEWAY_ID}])))
        self.assertIsNot(resolved, config.gateway_receipts)
        self.assertIsNone(config.gateway_receipts.gateway_id)


if __name__ == "__main__":
    unittest.main()
