"""Strict validation of card 2b-1 billing-policy-import documents.

The document shape is closed: {version, policies:[...]} and every policy
carries exactly the 17 keys of its level (docs/DESIGN.md section 11).
Reference resolution (the account and the reporter machine must exist in
the store) is intentionally NOT done here: it depends on the store's
current inventory, so store.billing_policy_import performs it inside the
write transaction. This module validates the self-contained shape and
types, the closed enums, and the B01 allocation invariant (gateway
shares + the non-AI allowance <= the account cap, on one tax basis)
against the per-account share totals the caller resolves.

The tax basis is explicit everywhere (section 11): an unknown basis is
permitted configuration, not permission to compare - it defers the
allocation check, it never approves it, and no implicit VAT default is
ever substituted for a missing rate.
"""

from .errors import (
    CODE_INVALID_CONFIG,
    CODE_SCHEMA_UNSUPPORTED,
    MSG_INVALID_CONFIG,
    MSG_SCHEMA_UNSUPPORTED,
    QuotaError,
)
from .reduce import BASIS_UNKNOWN, INT64_MAX, VAT_BPS_MAX, convert_basis
from .validate import ALIAS_RE

# The card 2b-1 billing-policy document version.
POLICY_DOC_VERSION = 1

_POLICY_DOC_KEYS = frozenset({"version", "policies"})
_POLICY_KEYS = frozenset({
    "account", "reporter_machine", "scope_alias", "scope_revision",
    "dedicated_ai", "non_ai_allowance_micro_eur", "cap_basis",
    "provider_basis", "ledger_basis", "vat_bps", "poll_seconds",
    "max_age_seconds", "lag_seconds", "abs_tolerance_micro_eur",
    "relative_tolerance_bps", "mode", "stale_action",
    "divergence_action",
})

# Closed enums (section 11): an unknown basis is a member of the basis
# set (permitted configuration), never of the action sets.
_BASES = ("unknown", "ex_vat", "invoice_total")
_MODES = ("advisory",)
_STALE_ACTIONS = ("alert",)
_DIVERGENCE_ACTIONS = ("hold",)


def _invalid():
    raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)


def _is_int(value):
    # bool is a subclass of int; the closed shapes reject booleans-as-
    # integers, so the type check is exact (type(...) is int).
    return type(value) is int


def _is_nonneg_int(value):
    # Bounded non-negative signed-64 (section 7: checked arithmetic).
    return _is_int(value) and 0 <= value <= INT64_MAX


def _is_pos_int(value):
    return _is_int(value) and 1 <= value <= INT64_MAX


def _is_alias(value):
    return isinstance(value, str) and ALIAS_RE.fullmatch(value) is not None


def _validate_policy(item):
    if not isinstance(item, dict) or frozenset(item) != _POLICY_KEYS:
        _invalid()
    # Local aliases only: no provider IDs, no free text.
    for field in ("account", "reporter_machine", "scope_alias"):
        if not _is_alias(item[field]):
            _invalid()
    if not _is_pos_int(item["scope_revision"]):
        _invalid()
    # dedicated_ai is exactly true: a non-dedicated project never gets a
    # whole-project cap.
    if item["dedicated_ai"] is not True:
        _invalid()
    if not _is_nonneg_int(item["non_ai_allowance_micro_eur"]):
        _invalid()
    for field in ("cap_basis", "provider_basis", "ledger_basis"):
        if item[field] not in _BASES:
            _invalid()
    # vat_bps: null only when no conversion is needed, otherwise a rate
    # in basis points (10000 == 100%).
    vat_bps = item["vat_bps"]
    if vat_bps is not None and not (
            _is_int(vat_bps) and 0 <= vat_bps <= VAT_BPS_MAX):
        _invalid()
    for field in ("poll_seconds", "max_age_seconds", "lag_seconds"):
        if not _is_pos_int(item[field]):
            _invalid()
    if not _is_nonneg_int(item["abs_tolerance_micro_eur"]):
        _invalid()
    relative = item["relative_tolerance_bps"]
    if not (_is_int(relative) and 0 <= relative <= VAT_BPS_MAX):
        _invalid()
    if item["mode"] not in _MODES:
        _invalid()
    if item["stale_action"] not in _STALE_ACTIONS:
        _invalid()
    if item["divergence_action"] not in _DIVERGENCE_ACTIONS:
        _invalid()


def validate_billing_policies(doc):
    """Validate a billing-policy document; return the policy list.

    The document is closed: version 1, a list of policies, each with
    exactly its 17 keys, and no two policies for the same account (one
    policy per metered account).
    """
    if not isinstance(doc, dict):
        _invalid()

    version = POLICY_DOC_VERSION
    if "version" in doc:
        version = doc["version"]
        if not _is_int(version) or version != POLICY_DOC_VERSION:
            raise QuotaError(CODE_SCHEMA_UNSUPPORTED, MSG_SCHEMA_UNSUPPORTED)

    if frozenset(doc) != _POLICY_DOC_KEYS:
        _invalid()
    policies = doc["policies"]
    if not isinstance(policies, list):
        _invalid()

    accounts = []
    for item in policies:
        _validate_policy(item)
        accounts.append(item["account"])
    if len(set(accounts)) != len(accounts):
        _invalid()
    return policies


def allocation_within_cap(budget, shares_micro_eur, allowance_micro_eur,
                          cap_basis, provider_basis, ledger_basis,
                          vat_bps):
    """B01: gateway shares + the non-AI allowance <= the account cap.

    The comparison runs on the CAP's tax basis: `budget` is the account
    cap on cap_basis, the gateway shares are ledger-basis money, and the
    non-AI allowance is provider-basis money (the provider bills in its
    own basis). A conversion is needed only where a known basis differs
    from the cap basis, and it then requires a usable rate: a missing or
    unusable vat_bps is an invalid document, never a default. An unknown
    (closed) basis DEFERS the check - permitted configuration, not
    permission to compare. The sum is checked: an overflow is invalid.
    """
    def _in_cap_basis(amount, source_basis):
        if source_basis == cap_basis:
            return amount
        if source_basis == BASIS_UNKNOWN or cap_basis == BASIS_UNKNOWN:
            # A comparison on an unknown basis is not possible: defer.
            return None
        if not (_is_int(vat_bps) and 0 <= vat_bps <= VAT_BPS_MAX):
            _invalid()
        # convert_basis raises INVALID_CONFIG on overflow.
        return convert_basis(amount, source_basis, cap_basis, vat_bps)

    shares = _in_cap_basis(shares_micro_eur, ledger_basis)
    allowance = _in_cap_basis(allowance_micro_eur, provider_basis)
    if shares is None or allowance is None:
        return  # deferred: no comparison was possible
    total = shares + allowance
    if total > INT64_MAX or budget is None or total > budget:
        _invalid()
