"""Card 7c-2, step 3: Leasewise's account ids reach the reporter through `policy-ids`.

A limit event names an account by id. `policy-ids` listed seats and models only, with the account as
an alias, so it now also lists `accounts: [{alias, id}]` (local aliases and ids, sorted; the same
closed, read-only rules as the rest). The reporter reads the list from its saved snapshot, treats it
as optional, and refuses to start when a configured account is missing or ambiguous. Synthetic data.
"""

import json
import os
import tempfile
import unittest
import uuid

from quota_balancer import store
from quota_balancer.reporter import config as cfg

from tests.reporter_env import Rig, build_store
from tests.test_card7c2_config import _Tmp as ConfigCase

A1, A2 = str(uuid.uuid4()), str(uuid.uuid4())


def snapshot(accounts=None, **extra):
    base = {"seats": [], "models": [], "task_classes": []}
    if accounts is not None:
        base["accounts"] = accounts
    base.update(extra)
    return base


def write(tmp, doc):
    path = os.path.join(tmp, "p-%s.json" % uuid.uuid4().hex)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)
    return path


class PolicyIds(unittest.TestCase):

    def test_the_listing_has_the_accounts_sorted_with_only_alias_and_id(self):
        rig = Rig(server=False)
        self.addCleanup(rig.cleanup)
        db = os.path.join(rig.ws.dir, "inventory.db")
        build_store(db, rig.ws.dir)
        data = store.policy_ids(db)
        self.assertEqual(set(data), {"seats", "models", "task_classes", "accounts"})
        aliases = [a["alias"] for a in data["accounts"]]
        self.assertEqual(aliases, sorted(aliases))
        self.assertTrue(aliases)
        for entry in data["accounts"]:
            self.assertEqual(set(entry), {"alias", "id"})
            uuid.UUID(entry["id"])
        # every model's account is in the list
        self.assertTrue({m["account"] for m in data["models"]} <= set(aliases))


class Snapshot(unittest.TestCase):

    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH") or None
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-7c2acc-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name

    def load(self, doc):
        return cfg.load_snapshot(write(self.tmp, doc))

    def test_the_accounts_are_optional_and_default_to_none(self):
        self.assertEqual(self.load(snapshot())["accounts"], [])

    def test_the_accounts_are_read_as_alias_and_id_pairs(self):
        data = self.load(snapshot([{"alias": "claude-main", "id": A1}, {"alias": "other", "id": A2}]))
        self.assertEqual(data["accounts"], [("claude-main", A1), ("other", A2)])
        self.assertEqual(data["seats"], [])                          # the rest of the shape is unchanged

    def test_a_malformed_list_refuses_the_snapshot(self):
        bad = ["x", {"alias": "a"}, [{"alias": "a", "id": A1, "extra": 1}], [{"alias": 1, "id": A1}],
               [{"alias": "a", "id": 5}], [["a", A1]], [None]]
        for accounts in bad:
            with self.subTest(accounts=accounts):
                with self.assertRaises(cfg.ConfigError) as caught:
                    self.load(snapshot(accounts))
                self.assertEqual(caught.exception.code, cfg.CONFIG_UNRESOLVED)


class Resolution(ConfigCase):

    def resolve(self, accounts, *sources):
        config = self.parse(*sources)
        return cfg.resolve_limit_sources(config, cfg.load_snapshot(write(self.dir, snapshot(accounts))))

    def unresolved(self, accounts, *sources):
        with self.assertRaises(cfg.ConfigError) as caught:
            self.resolve(accounts, *sources)
        self.assertEqual(caught.exception.code, cfg.CONFIG_UNRESOLVED)
        return caught.exception

    def test_a_source_gets_its_account_id(self):
        sources = self.resolve([{"alias": "claude-main", "id": A1}], self.source())
        self.assertEqual(len(sources), 1)
        src = sources[0]
        self.assertEqual((src.account, src.account_id, src.provider, src.os_user), ("claude-main", A1, "claude",
                                                                                   "operator-desktop"))
        self.assertEqual(src.folder, cfg.folder_key(self.bus))

    def test_no_sources_resolve_to_nothing_even_with_an_old_snapshot(self):
        config = self.parse(raw="")
        self.assertEqual(cfg.resolve_limit_sources(config, cfg.load_snapshot(write(self.dir, snapshot()))), ())

    def test_a_missing_account_refuses_start(self):
        self.unresolved([{"alias": "someone-else", "id": A1}], self.source())

    def test_an_old_snapshot_without_accounts_refuses_start(self):
        self.unresolved(None, self.source())

    def test_an_ambiguous_alias_refuses_start_and_never_picks_the_first(self):
        error = self.unresolved([{"alias": "claude-main", "id": A1}, {"alias": "claude-main", "id": A2}],
                                self.source())
        self.assertNotIn(A1, error.message)
        self.assertNotIn(A2, error.message)

    def test_an_id_that_is_not_a_uuid_refuses_start(self):
        self.unresolved([{"alias": "claude-main", "id": "not-an-id"}], self.source())

    def test_an_error_names_no_value(self):
        error = self.unresolved([{"alias": "someone-else", "id": A1}], self.source(account="claude-main"))
        self.assertNotIn("claude-main", error.message)
        self.assertNotIn(self.bus, error.message)


if __name__ == "__main__":
    unittest.main()
