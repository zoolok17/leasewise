"""Card 3 acceptance tests: the pure policy arithmetic and its named checks.

Run from the repository root:
    python -m unittest discover -s tests -v

Method names carry the card's check names with the unittest "test_" prefix:
round_components, projection_denied, fallback_tier, privacy_3, plus the
arithmetic rules the section-8 row calls out (the request-sized hold at
reserve rates, the fixed reserve as a comparison only, composed limits at
the tightest ceiling, the model input ceiling, and the 80% cold-sweep
deferral). The arithmetic is pure: these tests exercise the functions
directly on fixtures - no store, no CLI, no wrapper.
"""

import copy
import json
import unittest
import uuid

from quota_balancer import errors, policy
from quota_balancer.errors import QuotaError

# --- The card-3 fixture model (a models-table row, closed shape). The rates
# are integer micro-EUR per 1,000,000 tokens; the reserve rates are chosen
# so the section-3 worked example pins exactly:
# C=20000, N=4, G=2000, O=4000, M=5 -> E=92000, I=115000, H_sized=68169 at
# reserve rates, the analogous tariff-rate estimate 56809, H_fixed=1159995.
MODEL = {
    "alias": "claude",
    "context_ceiling": 200000,
    "output_ceiling": 64000,
    "tariff_input_rate": 400000,
    "tariff_output_rate": 2700000,
    "reserve_input_rate": 460000,
    "reserve_output_rate": 3815000,
    "reservation_mode": "sized",
    "policy_hash": "0123456789abcdef" * 4,
    "effort_aliases": "standard,deep",
}

ERROR_INVALID_REQUEST = (
    '{"error":{"code":"INVALID_REQUEST","message":"the request is invalid"}}'
)
ERROR_PROJECTION = (
    '{"error":{"code":"CONTEXT_PROJECTION_EXCEEDED","message":"the projected '
    'context exceeds the model input ceiling"}}'
)
ERROR_DENIED = (
    '{"error":{"code":"POLICY_DENIED","message":"the request is denied by policy"}}'
)

# Deterministic local UUIDs (section 7: IDs are local UUIDs): a stable id
# per label so the limit rows read naturally.
_UID_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # NAMESPACE_URL


def _uid(label):
    return str(uuid.uuid5(_UID_NS, "quota-card3/" + label))


def _limit(scope, scope_id, metric, period, hard, soft=None, window_key=None):
    return {
        "scope": scope,
        "scope_id": scope_id,
        "metric": metric,
        "period": period,
        "window_key": window_key,
        "soft": soft,
        "hard": hard,
    }


def _error_payload(exc):
    """Render the {error:{code,message}} contract the CLI would emit."""
    return json.dumps(
        {"error": {"code": exc.code, "message": exc.message}},
        separators=(",", ":"),
    )


class NamedChecks(unittest.TestCase):
    def test_round_components(self):
        # Given five calls (M=5), when the reserve formula (C=20000, N=4,
        # G=2000, O=4000), then 68169: the section-3 worked example.
        record = policy.estimate(20000, 4, 2000, 4000, 5, copy.deepcopy(MODEL))
        self.assertEqual(record["expected_input"], 92000)  # E
        self.assertEqual(record["input_cap"], 115000)  # I at the 12500 bps default margin
        self.assertEqual(record["hold_sized"], 68169)  # the selected hold, at reserve rates
        # The comparisons: the analogous tariff-rate upper estimate (NOT the
        # reservation) and the legacy fixed reserve (never a selected mode).
        self.assertEqual(record["hold_tariff_comparison"], 56809)
        self.assertEqual(record["hold_fixed_comparison"], 1159995)
        self.assertEqual(record["projected_final_context"], 26000)  # C + G*(N-1)

    def test_projection_denied(self):
        # Given an over-context projection (C + G*(N-1) above the model
        # input ceiling): deny CONTEXT_PROJECTION_EXCEEDED, exit 2, static
        # message. The oversized bound is never clipped - a split or a
        # FRESH/SUMMARY re-estimate is the only way through.
        model = copy.deepcopy(MODEL)  # input ceiling 200000
        # C=250000, N=7, G=0: the projection is 250000 > 200000.
        with self.assertRaises(QuotaError) as cm:
            policy.estimate(250000, 7, 0, 4200, 7, model)
        self.assertEqual(cm.exception.code, errors.CODE_CONTEXT_PROJECTION_EXCEEDED)
        self.assertEqual(errors.EXIT_CODES[cm.exception.code], 2)
        self.assertEqual(_error_payload(cm.exception), ERROR_PROJECTION)
        # Growth-driven: C=30000, N=40, G=5000 -> 30000 + 5000*39 = 205000.
        with self.assertRaises(QuotaError) as cm:
            policy.estimate(30000, 40, 5000, 0, 40, model)
        self.assertEqual(cm.exception.code, errors.CODE_CONTEXT_PROJECTION_EXCEEDED)
        # The boundary is inclusive: the projection at the ceiling is
        # admitted ...
        ok = policy.estimate(100000, 2, 100000, 0, 2, model)
        self.assertEqual(ok["projected_final_context"], 200000)
        # ... and one token over denies.
        with self.assertRaises(QuotaError) as cm:
            policy.estimate(100000, 2, 100001, 0, 2, model)
        self.assertEqual(cm.exception.code, errors.CODE_CONTEXT_PROJECTION_EXCEEDED)

    def test_fallback_tier(self):
        # Given a blocked Codex read / critical review: then the Qwen read /
        # the independent premium, or park (the section-6 profile table).
        # bounded_read: candidate qwen, fallback sonnet then park; a
        # quota-blocked Codex read routes here and lands on the Qwen read.
        plan = policy.select_tier(["qwen"], ["sonnet", "park"], {"codex"})
        self.assertEqual(plan, {"model": "qwen", "tier": "candidate", "index": 0})
        # qwen blocked too: the fallback tier (a new estimate/request/
        # session, never a per-tool hop).
        plan = policy.select_tier(["qwen"], ["sonnet", "park"], {"codex", "qwen"})
        self.assertEqual(plan, {"model": "sonnet", "tier": "fallback", "index": 0})
        # Everything blocked: park.
        plan = policy.select_tier(
            ["qwen"], ["sonnet", "park"], {"codex", "qwen", "sonnet"}
        )
        self.assertEqual(plan, {"model": None, "tier": "park", "index": 1})
        # critical_review: candidate astra, fallback the independent
        # premium reviewer (opus), or park.
        plan = policy.select_tier(["astra"], ["opus", "park"], {"astra"})
        self.assertEqual(plan, {"model": "opus", "tier": "fallback", "index": 0})
        plan = policy.select_tier(["astra"], ["opus", "park"], {"astra", "opus"})
        self.assertEqual(plan, {"model": None, "tier": "park", "index": 1})
        # cold_sweep: a blocked sweep parks - its fallback has no premium
        # tier, so no automatic premium bypass is possible.
        plan = policy.select_tier(["astra"], ["park"], {"astra"})
        self.assertEqual(plan, {"model": None, "tier": "park", "index": 0})

    def test_privacy_3(self):
        # Given injected secret/path/provider-ID sentinels: when the
        # error output is rendered, then none appear - only the static
        # {error:{code,message}} contract. The sentinels are shaped so they
        # can never pass as a local alias (section 8: only local aliases
        # survive), so every placement must be a static INVALID_REQUEST.
        secret = "SK-SECRET-sentinel-0001"
        path = "path-sentinel/card3-2"
        provider_id = "acct_PROVIDER-424242"
        sentinels = (secret, path, provider_id)

        # 1) A sentinel in any model string field: INVALID_REQUEST, static.
        for sentinel in sentinels:
            for field in ("alias", "policy_hash", "effort_aliases",
                          "reservation_mode"):
                bad = copy.deepcopy(MODEL)
                bad[field] = sentinel
                with self.assertRaises(QuotaError) as cm:
                    policy.estimate(20000, 4, 2000, 4000, 5, bad)
                self.assertEqual(cm.exception.code, errors.CODE_INVALID_REQUEST)
                self.assertEqual(_error_payload(cm.exception), ERROR_INVALID_REQUEST)
                for s in sentinels:
                    self.assertNotIn(s, _error_payload(cm.exception))
        # 2) A sentinel in an integer field: INVALID_REQUEST, static.
        bad = copy.deepcopy(MODEL)
        bad["context_ceiling"] = secret
        with self.assertRaises(QuotaError) as cm:
            policy.estimate(20000, 4, 2000, 4000, 5, bad)
        self.assertEqual(_error_payload(cm.exception), ERROR_INVALID_REQUEST)
        # 3) A sentinel as a limit scope_id / window_key: INVALID_REQUEST,
        # static.
        row = _limit("team", _uid("t"), "input_tokens", "day", 1000000)
        bad_row = copy.deepcopy(row)
        bad_row["scope_id"] = secret
        with self.assertRaises(QuotaError) as cm:
            policy.composed_limits([bad_row])
        self.assertEqual(_error_payload(cm.exception), ERROR_INVALID_REQUEST)
        bad_row = copy.deepcopy(row)
        bad_row["window_key"] = path
        with self.assertRaises(QuotaError) as cm:
            policy.composed_limits([bad_row])
        self.assertEqual(_error_payload(cm.exception), ERROR_INVALID_REQUEST)
        # 4) A sentinel in the tier lists or the blocked set: INVALID_
        # REQUEST, static.
        for call in (
            lambda: policy.select_tier([provider_id], ["park"], set()),
            lambda: policy.select_tier(["qwen"], [secret], set()),
            lambda: policy.select_tier(["qwen"], ["park"], {path}),
        ):
            with self.assertRaises(QuotaError) as cm:
                call()
            self.assertEqual(cm.exception.code, errors.CODE_INVALID_REQUEST)
            self.assertEqual(_error_payload(cm.exception), ERROR_INVALID_REQUEST)
            for s in sentinels:
                self.assertNotIn(s, _error_payload(cm.exception))
        # 5) The valid flow renders only fixture numbers; a local alias
        # survives as data, exactly as given.
        record = policy.estimate(20000, 4, 2000, 4000, 5, copy.deepcopy(MODEL))
        self.assertEqual(
            json.dumps(record, separators=(",", ":")),
            '{"expected_input":92000,"input_cap":115000,"hold_sized":68169,'
            '"hold_tariff_comparison":56809,"hold_fixed_comparison":1159995,'
            '"projected_final_context":26000}',
        )
        plan = policy.select_tier(["qwen"], ["sonnet", "park"], {"codex"})
        self.assertEqual(plan["model"], "qwen")  # a local alias survives


class EstimateRules(unittest.TestCase):
    def test_margin_bounds(self):
        model = copy.deepcopy(MODEL)
        # margin 10000 bps -> I == E; 20000 bps -> I == 2*E.
        rec = policy.estimate(20000, 4, 2000, 4000, 5, model, margin_bps=10000)
        self.assertEqual(rec["input_cap"], 92000)
        rec = policy.estimate(20000, 4, 2000, 4000, 5, model, margin_bps=20000)
        self.assertEqual(rec["input_cap"], 184000)
        for bad_margin in (9999, 20001, 0, -1):
            with self.assertRaises(QuotaError) as cm:
                policy.estimate(20000, 4, 2000, 4000, 5, model,
                                margin_bps=bad_margin)
            self.assertEqual(cm.exception.code, errors.CODE_INVALID_REQUEST)

    def test_requires_n_le_m_and_bounds(self):
        model = copy.deepcopy(MODEL)
        for c, n, g, o, m in (
            (20000, 6, 2000, 4000, 5),   # N > M
            (20000, 0, 2000, 4000, 5),   # N = 0
            (20000, 4, 2000, 4000, 0),   # M = 0
            (20000, 4, 2000, 4000, 101), # M > 100
        ):
            with self.assertRaises(QuotaError) as cm:
                policy.estimate(c, n, g, o, m, model)
            self.assertEqual(cm.exception.code, errors.CODE_INVALID_REQUEST)

    def test_integer_hygiene(self):
        model = copy.deepcopy(MODEL)
        # Booleans-as-integers, floats, negatives, and results beyond the
        # signed-64 range are all INVALID_REQUEST (section 7 contract).
        overrides = (
            {"c": True},
            {"n": 4.0},
            {"g": -1},
            {"o": -4000},
            {"m": 2 ** 63},
        )
        for override in overrides:
            args = dict(c=20000, n=4, g=2000, o=4000, m=5,
                        model=copy.deepcopy(model))
            args.update(override)
            with self.assertRaises(QuotaError) as cm:
                policy.estimate(**args)
            self.assertEqual(cm.exception.code, errors.CODE_INVALID_REQUEST)
        # Inputs within signed-64 whose E overflows signed-64: rejected.
        with self.assertRaises(QuotaError) as cm:
            policy.estimate(2 ** 62, 100, 2 ** 62, 0, 100, copy.deepcopy(model))
        self.assertEqual(cm.exception.code, errors.CODE_INVALID_REQUEST)

    def test_hold_allowance(self):
        # The 2*M-1 allowance: M=1 adds exactly 1 to the rate part.
        self.assertEqual(
            policy.hold_at_rates(115000, 4000, 1, 460000, 3815000), 68160 + 1
        )
        # The fixed reserve is a comparison: M*231999.
        self.assertEqual(policy.hold_fixed(5), 1159995)
        self.assertEqual(policy.hold_fixed(100), 100 * 231999)

    def test_per_call_output_cap(self):
        model = copy.deepcopy(MODEL)  # output ceiling 64000
        self.assertIsNone(policy.check_per_call_output_cap(model, 64000))
        for bad in (64001, -1):
            with self.assertRaises(QuotaError) as cm:
                policy.check_per_call_output_cap(model, bad)
            self.assertEqual(cm.exception.code, errors.CODE_INVALID_REQUEST)

    def test_model_shape_closed(self):
        # A missing key, an extra key, a short hash, a bad reservation
        # mode, or a non-alias effort: all INVALID_REQUEST.
        cases = []
        missing = copy.deepcopy(MODEL)
        del missing["policy_hash"]
        cases.append(missing)
        extra = copy.deepcopy(MODEL)
        extra["region"] = "eu"
        cases.append(extra)
        short_hash = copy.deepcopy(MODEL)
        short_hash["policy_hash"] = "abc123"
        cases.append(short_hash)
        bad_mode = copy.deepcopy(MODEL)
        bad_mode["reservation_mode"] = "auto"
        cases.append(bad_mode)
        bad_effort = copy.deepcopy(MODEL)
        bad_effort["effort_aliases"] = "Standard"
        cases.append(bad_effort)
        for bad in cases:
            with self.assertRaises(QuotaError) as cm:
                policy.estimate(20000, 4, 2000, 4000, 5, bad)
            self.assertEqual(cm.exception.code, errors.CODE_INVALID_REQUEST)


class LimitRules(unittest.TestCase):
    def test_tightest_ceiling_composition(self):
        # Limits compose by their tightest ceiling, never by adding
        # duplicate caps (section 4); periods do not mix.
        caps = policy.composed_limits([
            _limit("platform", _uid("pl"), "input_tokens", "day", 1000000,
                   soft=200000),
            _limit("project", _uid("p"), "input_tokens", "day", 1000000,
                   soft=500000),
            _limit("team", _uid("t"), "input_tokens", "day", 400000),
            _limit("seat", _uid("s"), "input_tokens", "turn", 250000),
            _limit("platform", _uid("pl"), "money_micro_eur", "turn", 5000000),
        ])
        self.assertEqual(
            caps[("input_tokens", "day")], {"hard": 400000, "soft": 200000}
        )
        self.assertEqual(
            caps[("input_tokens", "turn")], {"hard": 250000, "soft": None}
        )
        self.assertEqual(
            caps[("money_micro_eur", "turn")], {"hard": 5000000, "soft": None}
        )

    def test_account_budget_tightens_month_money(self):
        # The account budget is the one money home (F04): it tightens the
        # (money, month) cap, and a tighter money limit still wins.
        caps = policy.composed_limits(
            [_limit("project", _uid("p"), "money_micro_eur", "month", 150000000)],
            account_budget=100000000,
        )
        self.assertEqual(
            caps[("money_micro_eur", "month")], {"hard": 100000000, "soft": None}
        )
        caps = policy.composed_limits(
            [_limit("project", _uid("p"), "money_micro_eur", "month", 50000000)],
            account_budget=100000000,
        )
        self.assertEqual(
            caps[("money_micro_eur", "month")], {"hard": 50000000, "soft": None}
        )
        caps = policy.composed_limits([], account_budget=100000000)
        self.assertEqual(
            caps[("money_micro_eur", "month")], {"hard": 100000000, "soft": None}
        )

    def test_admission_at_cap_ok_over_cap_denied(self):
        # Admission (section 4): settled + unresolved holds + new hold <=
        # each applicable cap; one unit over is POLICY_DENIED, exit 2.
        caps = policy.composed_limits(
            [_limit("team", _uid("t"), "money_micro_eur", "turn", 5000)]
        )
        policy.check_admission(caps, "money_micro_eur", "turn", 1000, 2000, 2000)
        with self.assertRaises(QuotaError) as cm:
            policy.check_admission(caps, "money_micro_eur", "turn", 1000, 2000, 2001)
        self.assertEqual(cm.exception.code, errors.CODE_POLICY_DENIED)
        self.assertEqual(errors.EXIT_CODES[errors.CODE_POLICY_DENIED], 2)
        self.assertEqual(_error_payload(cm.exception), ERROR_DENIED)
        # Input tokens compose the same way.
        caps_in = policy.composed_limits(
            [_limit("seat", _uid("s"), "input_tokens", "turn", 115000)]
        )
        policy.check_admission(caps_in, "input_tokens", "turn", 0, 0, 115000)
        with self.assertRaises(QuotaError) as cm:
            policy.check_admission(caps_in, "input_tokens", "turn", 0, 0, 115001)
        self.assertEqual(cm.exception.code, errors.CODE_POLICY_DENIED)

    def test_admission_unbounded_without_cap(self):
        # A (metric, period) with no cap is unbounded.
        caps = policy.composed_limits([])
        policy.check_admission(caps, "money_micro_eur", "turn", 0, 0, 10 ** 12)

    def test_limit_row_contract(self):
        good = _limit("team", _uid("t"), "input_tokens", "day", 1000000)
        policy.composed_limits([good])  # baseline: accepted
        for scope in ("account", "PLATFORM", ""):
            with self.assertRaises(QuotaError) as cm:
                policy.composed_limits(
                    [_limit(scope, _uid("t"), "input_tokens", "day", 1)])
            self.assertEqual(cm.exception.code, errors.CODE_INVALID_REQUEST)
        for metric in ("tokens", "money"):
            with self.assertRaises(QuotaError) as cm:
                policy.composed_limits(
                    [_limit("team", _uid("t"), metric, "day", 1)])
            self.assertEqual(cm.exception.code, errors.CODE_INVALID_REQUEST)
        for period in ("week", "utc_month"):
            with self.assertRaises(QuotaError) as cm:
                policy.composed_limits(
                    [_limit("team", _uid("t"), "input_tokens", period, 1)])
            self.assertEqual(cm.exception.code, errors.CODE_INVALID_REQUEST)
        # soft > hard is impossible; a non-UUID scope_id and a non-alias
        # window key are not representable; negative and boolean values are
        # rejected.
        for row in (
            _limit("team", _uid("t"), "input_tokens", "day", 100, soft=101),
            _limit("team", "team-alias", "input_tokens", "day", 1),
            _limit("platform", _uid("pl"), "window_bps", "window", 10000,
                   window_key="Week One"),
            _limit("team", _uid("t"), "input_tokens", "day", -1),
            _limit("team", _uid("t"), "input_tokens", "day", 100, soft=True),
        ):
            with self.assertRaises(QuotaError) as cm:
                policy.composed_limits([row])
            self.assertEqual(cm.exception.code, errors.CODE_INVALID_REQUEST)


class SweepRules(unittest.TestCase):
    def test_sweep_defer_above_80_percent(self):
        # "Defer above 80%" (section 6): strictly above 8000 bps defers; at
        # exactly 8000 the sweep still runs.
        self.assertFalse(policy.sweep_deferred(0))
        self.assertFalse(policy.sweep_deferred(8000))
        self.assertTrue(policy.sweep_deferred(8001))
        self.assertTrue(policy.sweep_deferred(10000))
        for bad in (-1, 10001, True, 50.0):
            with self.assertRaises(QuotaError) as cm:
                policy.sweep_deferred(bad)
            self.assertEqual(cm.exception.code, errors.CODE_INVALID_REQUEST)

    def test_cold_sweep_no_premium_bypass(self):
        # A blocked or deferred cold sweep parks: its fallback list has no
        # premium tier, so the arithmetic cannot route it to one.
        plan = policy.select_tier(["astra"], ["park"], {"astra"})
        self.assertEqual(plan["tier"], "park")
        self.assertIsNone(plan["model"])
        self.assertTrue(policy.sweep_deferred(8001))


class ErrorContract(unittest.TestCase):
    def test_codes_messages_and_exit_codes(self):
        # The three card-3 codes: static messages, exit 2 (client-side
        # conditions, the card-1 "invalid request" class).
        for code, message in (
            (errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST),
            (errors.CODE_CONTEXT_PROJECTION_EXCEEDED,
             errors.MSG_CONTEXT_PROJECTION_EXCEEDED),
            (errors.CODE_POLICY_DENIED, errors.MSG_POLICY_DENIED),
        ):
            self.assertEqual(errors.EXIT_CODES[code], 2)
            self.assertIsInstance(message, str)
            self.assertTrue(message)
