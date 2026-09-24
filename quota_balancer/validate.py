"""Strict validation of version-1 import documents (Card 1).

The document shape is closed: every object carries exactly the keys of its
level, nothing more (docs/DESIGN.md section 8). Validation raises
QuotaError with a static message only - never text from the input document.
"""

import re

from .errors import (
    CODE_INVALID_CONFIG,
    CODE_SCHEMA_UNSUPPORTED,
    MSG_INVALID_CONFIG,
    MSG_SCHEMA_UNSUPPORTED,
    QuotaError,
)

# docs/DESIGN.md section 7: the alias regex is [a-z][a-z0-9_-]{0,63}.
ALIAS_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")

# Section 7: checked signed-64-bit arithmetic; money values must be > 0.
MAX_MICRO_EUR = 2 ** 63 - 1

VENDORS = ("ovh", "openai", "anthropic")
KIND_METERED = "metered"
KIND_SUBSCRIPTION = "subscription"
PERIOD = "utc_month"
SCHEMA_VERSION = 1

_TOP_KEYS = frozenset({"version", "accounts", "machines", "gateways"})
_ACCOUNT_KEYS = frozenset({"alias", "vendor", "kind", "budget_micro_eur"})
_MACHINE_KEYS = frozenset({"alias"})
_GATEWAY_KEYS = frozenset({"alias", "account", "machine", "share_micro_eur", "period"})


def _invalid():
    raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)


def _is_int(value):
    # bool is a subclass of int in Python; the design rejects booleans-as-integers,
    # so the type check is exact (type(...) is int) rather than isinstance.
    return type(value) is int


def _is_money(value):
    return _is_int(value) and 0 < value <= MAX_MICRO_EUR


def _is_alias(value):
    return isinstance(value, str) and ALIAS_RE.fullmatch(value) is not None


def validate(doc):
    """Validate a version-1 document.

    Returns normalized tuples for the store:
      accounts:  [(alias, vendor, kind, budget_micro_eur_or_None), ...]
      machines:  [alias, ...]
      gateways:  [(alias, account_alias, machine_alias, share, period), ...]
    """
    if not isinstance(doc, dict):
        _invalid()

    # The schema version is checked before the full shape check: a document
    # at an unknown or newer version is a schema problem even if its shape
    # has drifted along with it.
    if "version" in doc:
        version = doc["version"]
        if not _is_int(version) or version != SCHEMA_VERSION:
            raise QuotaError(CODE_SCHEMA_UNSUPPORTED, MSG_SCHEMA_UNSUPPORTED)

    if frozenset(doc) != _TOP_KEYS:
        _invalid()

    for key in ("accounts", "machines", "gateways"):
        if not isinstance(doc[key], list):
            _invalid()

    accounts = []
    for item in doc["accounts"]:
        if not isinstance(item, dict) or frozenset(item) != _ACCOUNT_KEYS:
            _invalid()
        alias = item["alias"]
        vendor = item["vendor"]
        kind = item["kind"]
        budget = item["budget_micro_eur"]
        if not _is_alias(alias) or vendor not in VENDORS:
            _invalid()
        if kind == KIND_METERED:
            if not _is_money(budget):
                _invalid()
        elif kind == KIND_SUBSCRIPTION:
            # The key is required at every level; a subscription account
            # carries no budget, so the value must be null (NULL in the store).
            if budget is not None:
                _invalid()
        else:
            _invalid()
        accounts.append((alias, vendor, kind, budget))

    account_aliases = [entry[0] for entry in accounts]
    if len(set(account_aliases)) != len(account_aliases):
        _invalid()
    account_by_alias = {entry[0]: entry for entry in accounts}

    machines = []
    for item in doc["machines"]:
        if not isinstance(item, dict) or frozenset(item) != _MACHINE_KEYS:
            _invalid()
        alias = item["alias"]
        if not _is_alias(alias):
            _invalid()
        machines.append(alias)
    if len(set(machines)) != len(machines):
        _invalid()
    machine_aliases = set(machines)

    gateways = []
    shares_by_account = {}
    for item in doc["gateways"]:
        if not isinstance(item, dict) or frozenset(item) != _GATEWAY_KEYS:
            _invalid()
        alias = item["alias"]
        account = item["account"]
        machine = item["machine"]
        share = item["share_micro_eur"]
        period = item["period"]
        if not _is_alias(alias) or not _is_alias(account) or not _is_alias(machine):
            _invalid()
        if account not in account_by_alias or machine not in machine_aliases:
            _invalid()
        # Metered-only shares: only metered accounts have a budget to allocate.
        if account_by_alias[account][2] != KIND_METERED:
            _invalid()
        if not _is_money(share):
            _invalid()
        if period != PERIOD:
            _invalid()
        gateways.append((alias, account, machine, share, period))
        shares_by_account[account] = shares_by_account.get(account, 0) + share

    gateway_aliases = [entry[0] for entry in gateways]
    if len(set(gateway_aliases)) != len(gateway_aliases):
        _invalid()

    # Share SUM per account <= its budget (design section 8, card 1).
    for account, total in shares_by_account.items():
        if total > account_by_alias[account][3]:
            _invalid()

    return accounts, machines, gateways
