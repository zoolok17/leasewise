"""Card L6a-2, step 1: the `[admission]` configuration block (checked strictly, resolved against the policy-ids snapshot).

The block names the admission store, the gateway the leases are for, the agents whose turns the gateway admits
(`gateway_agents`), and the operator's token bounds. A bound that is not written stays absent: the request that needs it
is `held` as `context_unprovable`; nothing is invented. Synthetic data only.
"""

import json
import unittest

from quota_balancer.reporter import config as cfg

from tests.l6a2_env import GATEWAY_ID, admission_block
from tests.reporter_env import agent_table, config_text

STORE = "C:/lw-test/admission/admission.db"
SNAPSHOT = {"seats": [("desktop", "dev", "s6", "11111111-1111-4111-8111-111111111111")],
            "models": [("meter", "opus", "22222222-2222-4222-8222-222222222222")], "task_classes": ["bounded_read"],
            "accounts": [], "gateways": [("g1", GATEWAY_ID)]}


def text(extra, agents=None):
    return config_text("http://127.0.0.1:9", "C:/lw-test/journal", "C:/lw-test/state/reporter.db",
                       "C:/lw-test/snapshot.json", {"alpha": agent_table(), "beta": agent_table()} if agents is None
                       else agents, extra=extra)


def block(**over):
    return admission_block(STORE, **over)


class Parse(unittest.TestCase):

    def refused(self, extra, **kw):
        with self.assertRaises(cfg.ConfigError) as caught:
            cfg.parse_config(text(extra, **kw))
        self.assertEqual(caught.exception.code, cfg.CONFIG_INVALID)
        return caught.exception.message

    def test_a_complete_block_is_read(self):
        config = cfg.parse_config(text(block(agents=("beta", "alpha"), context_ceiling_tokens=180000)))
        one = config.admission
        self.assertEqual((one.store, one.gateway, one.gateway_id), (STORE, "g1", None))
        self.assertEqual(one.gateway_agents, ("alpha", "beta"))                          # sorted, a fixed order
        self.assertEqual((one.fresh_overhead_bound_tokens, one.resume_history_bound_tokens, one.growth_bound_tokens,
                          one.context_ceiling_tokens), (1000, 5000, 10, 180000))

    def test_without_the_block_nothing_changes(self):
        self.assertIsNone(cfg.parse_config(text("")).admission)

    def test_a_bound_that_is_not_written_stays_absent(self):
        one = cfg.parse_config(text(block(fresh_overhead_bound_tokens=None, resume_history_bound_tokens=None,
                                          growth_bound_tokens=None))).admission
        self.assertEqual((one.fresh_overhead_bound_tokens, one.resume_history_bound_tokens, one.growth_bound_tokens,
                          one.context_ceiling_tokens), (None, None, None, None))

    def test_the_keys_are_closed_and_the_required_ones_must_be_there(self):
        self.refused('[admission]\nstore = "C:/x/a.db"\ngateway = "g1"\n')                       # no gateway_agents
        self.refused('[admission]\ngateway = "g1"\ngateway_agents = ["alpha"]\n')               # no store
        self.refused(block() + 'surprise = 1\n')

    def test_the_store_must_be_an_absolute_path_and_the_gateway_a_valid_alias(self):
        for store in ("relative/admission.db", "", "a\x00b"):
            with self.subTest(store=store):
                self.refused(admission_block(store))
        self.refused(admission_block(STORE, gateway="not valid!"))

    def test_gateway_agents_are_a_short_list_of_valid_distinct_agents_each_with_a_table(self):
        self.refused(admission_block(STORE, agents=()))
        self.refused(admission_block(STORE, agents=("alpha", "alpha")))
        self.refused(admission_block(STORE, agents=("bad name",)))
        self.refused(admission_block(STORE, agents=tuple("a%d" % n for n in range(65))),
                     agents={"a%d" % n: agent_table() for n in range(65)})
        self.refused(admission_block(STORE, agents=("gamma",)))                                  # no [agents.gamma]
        self.refused(block().replace('["alpha"]', '"alpha"'))

    def test_each_bound_is_a_positive_whole_number_within_a_limit(self):
        for key in ("fresh_overhead_bound_tokens", "resume_history_bound_tokens", "growth_bound_tokens",
                    "context_ceiling_tokens"):
            for value in ("0", "-1", "1.5", "true", '"5"', str(10 ** 12 + 1)):
                with self.subTest(key=key, value=value):
                    self.refused(block(**{key: None}) + "%s = %s\n" % (key, value))
            cfg.parse_config(text(block(**{key: 10 ** 12})))

    def test_the_store_must_be_the_same_file_the_receipt_reader_uses(self):
        receipts = '[gateway_receipts]\ngateway = "g1"\nadmission_store = %s\nfixture = "C:/x/r.json"\n'
        cfg.parse_config(text(block() + receipts % json.dumps(STORE)))
        cfg.parse_config(text(block() + receipts % json.dumps(STORE.replace("/", "\\"))))      # the same file, spelled
        message = self.refused(block() + receipts % json.dumps("C:/lw-test/other/admission.db"))
        self.assertNotIn("other", message)                                                    # no path in the sentence

    def test_nothing_of_the_block_shows_in_its_text_form(self):
        one = cfg.parse_config(text(block())).admission
        self.assertNotIn("admission.db", repr(one) + str(one))


class Resolve(unittest.TestCase):

    def setUp(self):
        self.config = cfg.parse_config(text(block(context_ceiling_tokens=9000)))

    def test_the_gateway_alias_becomes_its_id_and_every_other_value_is_kept(self):
        one = cfg.resolve_admission(self.config, SNAPSHOT)
        self.assertEqual((one.gateway, one.gateway_id, one.store), ("g1", GATEWAY_ID, STORE))
        self.assertEqual((one.gateway_agents, one.fresh_overhead_bound_tokens, one.context_ceiling_tokens),
                         (("alpha",), 1000, 9000))
        self.assertIsNone(self.config.admission.gateway_id)                                  # the parsed one is untouched

    def test_without_the_block_there_is_nothing_to_resolve(self):
        self.assertIsNone(cfg.resolve_admission(cfg.parse_config(text("")), SNAPSHOT))

    def test_a_missing_ambiguous_or_invalid_gateway_refuses_with_a_fixed_sentence(self):
        for gateways in ([], [("g1", GATEWAY_ID), ("g1", "99999999-9999-4999-8999-999999999999")], [("g1", "not-a-uuid")]):
            with self.subTest(gateways=len(gateways)):
                with self.assertRaises(cfg.ConfigError) as caught:
                    cfg.resolve_admission(self.config, dict(SNAPSHOT, gateways=gateways))
                self.assertEqual(caught.exception.code, cfg.CONFIG_UNRESOLVED)
                self.assertNotIn(GATEWAY_ID, caught.exception.message)


if __name__ == "__main__":
    unittest.main()
