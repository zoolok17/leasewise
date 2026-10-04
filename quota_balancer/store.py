"""SQLite store for the inventory (accounts, machines, gateways) and the
card-2 reading state (readings, windows, gateway report columns).

Connections are autocommit (isolation_level=None); every transaction is
explicit. The import replacement takes the write lock (BEGIN IMMEDIATE)
before it reads the old alias->UUID maps, so a concurrent import cannot
slip between the read and the write. A reading import also runs under the
write lock: idempotency by event_id is checked against the store inside the
same transaction as the inserts/reductions, so a replay is a no-op and a
collision aborts the whole batch. The status projection is a single read
transaction: the version check and all projections share one snapshot. Read
paths never switch journal mode and never write, so a store whose version is
rejected is left byte-identical.
"""

import json
import os
import re
import sqlite3
import time
import uuid

from .errors import (
    CODE_ADOPTION_REFUSED,
    CODE_BILLING_SCOPE_MISMATCH,
    CODE_CAP_BASIS_CONFLICT,
    CODE_EVIDENCE_REQUIRED,
    CODE_IDEMPOTENCY_CONFLICT,
    CODE_INVALID_CONFIG,
    CODE_STORE_ERROR,
    MSG_ADOPTION_REFUSED,
    MSG_BILLING_SCOPE_MISMATCH,
    MSG_CAP_BASIS_CONFLICT,
    MSG_EVIDENCE_REQUIRED,
    MSG_IDEMPOTENCY_CONFLICT,
    MSG_INVALID_CONFIG,
    MSG_STORE_FAILED,
    MSG_STORE_NOT_FOUND,
    QuotaError,
)
from .migrate import SCHEMA_VERSION, migrate, require_supported, rollback_quietly
from .reduce import (
    BASIS_EX_VAT,
    BASIS_INVOICE_TOTAL,
    GATEWAY_STATE_OK,
    GATEWAY_STATE_RECONCILE,
    billing_freshness,
    gateway_report_state,
    reduce_billing_total,
    reduce_gateway_report,
    reduce_window,
    window_freshness,
)
from . import evidence, evidence_view, money
from .policy_config import apply_policy
from .validate import validate
from .validate_config_v2 import VERSION as CONFIG_V2, validate_v2
from .validate_policies import (
    allocation_within_cap,
    validate_billing_policies,
)
from .validate_readings import (
    KIND_CLI_WINDOW,
    KIND_GATEWAY_SNAPSHOT,
    KIND_PROVIDER_BILLING,
    validate_readings,
)


# Explicit busy timeout (seconds) for every store connection: a hot journal
# left by a crashed writer, or a lock held by a concurrent writer, is waited
# out for a bounded, documented duration instead of the driver's implicit
# default, and then surfaces as STORE_ERROR rather than a raw driver fault.
BUSY_TIMEOUT_S = 30.0


def _connect(path, must_exist):
    """Open the store with foreign_keys=ON. Never raises sqlite3.Error."""
    if must_exist and not os.path.isfile(path):
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_NOT_FOUND)
    try:
        conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_S)
    except sqlite3.Error as exc:
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from exc
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except sqlite3.Error as exc:
        conn.close()
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from exc


def _reject_duplicate_keys(pairs):
    """object_pairs_hook: "no additional keys at any level" includes a
    key that appears twice in the same object."""
    doc = {}
    for key, value in pairs:
        if key in doc:
            raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
        doc[key] = value
    return doc


def init_db(path):
    """Create or upgrade the store to the current schema version.

    The migration itself is transactional and ordered (see
    migrate.migrate): a version-0 store runs steps 1, 2 and 3, a card-1
    (version-1) store runs steps 2 and 3, a card-2 (version-2) store runs
    step 3, a store at the current version is a no-op, and a newer, unknown
    version is rejected before anything is written or switched.
    """
    if sqlite3.sqlite_version_info < (3, 37, 0):
        # STRICT tables require SQLite >= 3.37; fail before touching the file.
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED)
    conn = _connect(path, must_exist=False)
    try:
        migrate(conn)
    except sqlite3.Error as exc:
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from exc
    finally:
        conn.close()


def import_config(path, file):
    """Validate a version-1 document and reconcile the inventory atomically.

    Validation happens before any write, so a rejected document leaves the
    store untouched. The reconciliation runs under the write lock and is an
    UPSERT, not a delete-and-reinsert: retained aliases keep their stored
    UUID and every card-2 state column (coverage_complete, the gateway
    report); new aliases get new UUIDs; and a removed alias is deleted only
    when nothing references it (a reading, window, or card-3 policy-table
    row still pointing at a removed account/machine is INVALID_CONFIG). A
    delete-and-reinsert would
    fail on the foreign keys once any reading exists and would silently reset
    the operator-owned state. Future cards: an active-lease check will gate
    this reconciliation.
    """
    try:
        with open(file, "r", encoding="utf-8-sig") as handle:
            doc = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, ValueError, RecursionError):
        # Missing or unreadable file, malformed JSON, undecodable text, or
        # pathologically deep nesting: a bad document, not a store fault.
        raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)

    # Version 2 adds the policy tables; version 1 (and an absent version)
    # keeps validate()'s contract and an unknown version is its
    # SCHEMA_UNSUPPORTED. A version-1 import never touches the policy tables.
    policy_doc = None
    if (isinstance(doc, dict) and type(doc.get("version")) is int
            and doc["version"] == CONFIG_V2):
        accounts, machines, gateways, policy_doc = validate_v2(doc)
    else:
        accounts, machines, gateways = validate(doc)

    conn = _connect(path, must_exist=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            require_supported(conn)
            conflicts_before = _conflicting_cap_scopes(conn)
            _upsert(conn, accounts, machines, gateways, policy_doc)
            _refuse_new_cap_basis_conflicts(conn, conflicts_before)
            conn.execute("COMMIT")
        except BaseException:
            rollback_quietly(conn)
            raise
    except sqlite3.Error as exc:
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from exc
    finally:
        conn.close()


def _upsert(conn, accounts, machines, gateways, policy_doc=None):
    """Reconcile the inventory to the document. Inside the import's BEGIN
    IMMEDIATE. Expects the normalized tuples from validate.validate().

    Retained aliases are updated in place (config-owned columns only, so the
    card-2 state columns survive); new aliases are inserted; removed aliases
    are deleted only when unreferenced (children before parents). Before any
    of that, an account with a billing policy must stay ingest-able under
    the document (metered kind, B01 allocation) - otherwise the import is
    refused and nothing is written.
    """
    # Read the old maps under the write lock: a concurrent import cannot
    # commit new UUIDs between this read and the updates/deletes below.
    old_accounts = dict(conn.execute("SELECT alias, id FROM accounts"))
    old_machines = dict(conn.execute("SELECT alias, id FROM machines"))
    old_gateways = dict(conn.execute("SELECT alias, id FROM gateways"))

    # The id each alias maps to: the stored UUID when retained, a new UUID
    # otherwise. Gateways reference these ids, not the aliases.
    account_ids = {}
    machine_ids = {}
    for alias, _vendor, _kind, _budget in accounts:
        account_ids[alias] = old_accounts.get(alias) or str(uuid.uuid4())
    for alias in machines:
        machine_ids[alias] = old_machines.get(alias) or str(uuid.uuid4())

    # Billing-policy guard: an account that carries a policy must stay
    # ingest-able, because every billing reading for it re-checks the
    # metered kind and the B01 allocation (shares + non-AI allowance
    # <= cap, on the cap's tax basis) against the CURRENT config. An
    # import that dropped the alias (the removal guards below refuse it
    # the same way) or that flipped its kind, or that broke the
    # allocation, is refused - static error, nothing written - instead
    # of stranding the account: every later billing reading would be
    # refused, and a mixed batch would lose its gateway snapshot too.
    # The check mirrors the ingest re-check: same kind rule, same
    # allocation, against the DOCUMENT's new kind, cap and shares - the
    # config a later reading would re-check if the import were allowed.
    # The shares come from the document (the DB still holds the old
    # gateways at this point), not the store.
    new_accounts = {alias: (kind, budget)
                    for alias, _vendor, kind, budget in accounts}
    new_shares = {}
    for _galias, account, _machine, share, _period in gateways:
        new_shares[account] = new_shares.get(account, 0) + share
    for (alias, kind, allowance, cap_basis, ledger_basis,
         vat_bps) in conn.execute(
            "SELECT a.alias, a.kind, "
            "p.non_ai_allowance_micro_eur, p.cap_basis, p.ledger_basis, "
            "p.vat_bps FROM billing_policies p "
            "JOIN accounts a ON a.id = p.account_id"):
        new = new_accounts.get(alias)
        if new is None or new[0] != kind:
            raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
        allocation_within_cap(
            new[1], new_shares.get(alias, 0), allowance, cap_basis,
            ledger_basis, vat_bps)

    # Accounts: update config-owned columns for retained aliases (the card-2
    # state columns - window_keys, activity_seq, coverage_complete,
    # required_reporters, outage_local_concurrency_cap - are left untouched);
    # insert new ones.
    for alias, vendor, kind, budget in accounts:
        if alias in old_accounts:
            conn.execute(
                "UPDATE accounts SET vendor=?, kind=?, budget_micro_eur=? "
                "WHERE id=?",
                (vendor, kind, budget, account_ids[alias]),
            )
        else:
            conn.execute(
                "INSERT INTO accounts(id, alias, vendor, kind, budget_micro_eur)"
                " VALUES(?,?,?,?,?)",
                (account_ids[alias], alias, vendor, kind, budget),
            )

    # Machines: only id + alias in this card (the alias is the identity), so a
    # retained machine needs no update; insert new ones.
    for alias in machines:
        if alias not in old_machines:
            conn.execute(
                "INSERT INTO machines(id, alias) VALUES(?,?)",
                (machine_ids[alias], alias),
            )

    # Gateways: update config-owned columns for retained aliases (the report
    # columns - ledger_epoch, last_seq, period_*, policy_hashes, last_report_at,
    # state, sized_ready - are left untouched); insert new ones. Exception:
    # a retained gateway re-pointed to a different machine or account has
    # its report columns reset and is marked reconciliation_required. The
    # new owner's ledger is a new ledger (its reports typically restart at
    # seq 1); carrying the old owner's epoch/seq/hashes over would drop
    # those reports as smaller seqs and then accept them as a continuation
    # without reconciliation.
    for alias, account, machine, share, period in gateways:
        gateway_id = old_gateways.get(alias) or str(uuid.uuid4())
        if alias in old_gateways:
            old = conn.execute(
                "SELECT account_id, machine_id FROM gateways WHERE id=?",
                (gateway_id,),
            ).fetchone()
            owner_changed = (old[0] != account_ids[account]
                             or old[1] != machine_ids[machine])
            if owner_changed:
                conn.execute(
                    "UPDATE gateways SET account_id=?, machine_id=?, "
                    "share_micro_eur=?, period=?, ledger_epoch=NULL, "
                    "last_seq=NULL, period_start=NULL, period_end=NULL, "
                    "policy_hashes=NULL, last_report_at=NULL, state=?, "
                    "sized_ready=0 WHERE id=?",
                    (account_ids[account], machine_ids[machine], share,
                     period, GATEWAY_STATE_RECONCILE, gateway_id),
                )
                _forget_evidence(conn, gateway_id)
            else:
                conn.execute(
                    "UPDATE gateways SET account_id=?, machine_id=?, "
                    "share_micro_eur=?, period=? WHERE id=?",
                    (account_ids[account], machine_ids[machine], share,
                     period, gateway_id),
                )
        else:
            conn.execute(
                "INSERT INTO gateways(id, alias, account_id, machine_id,"
                " share_micro_eur, period) VALUES(?,?,?,?,?,?)",
                (gateway_id, alias, account_ids[account],
                 machine_ids[machine], share, period),
            )

    # Version 2: reconcile the seven policy tables NOW, before the
    # inventory deletions below, so rows of a removed account/machine are
    # removed (or refuse the import) first and the guards below then see the
    # post-import policy state. A version-1 import skips this and leaves the
    # policy tables as they are.
    if policy_doc is not None:
        apply_policy(conn, policy_doc, account_ids, machine_ids)

    # Removed aliases, children before parents. validate() guarantees no
    # retained/new gateway references a removed account or machine. Every
    # other reference is checked against REMOVAL_GUARDS, so a referenced
    # removal is always the static INVALID_CONFIG (the foreign keys stay as
    # the backstop, never the first line: their failure would read as a
    # store fault).
    for alias in set(old_gateways) - {g[0] for g in gateways}:
        _refuse_if_referenced(conn, "gateways", old_gateways[alias])
        conn.execute("DELETE FROM gateways WHERE id=?", (old_gateways[alias],))
    for alias in set(old_machines) - set(machines):
        _refuse_if_referenced(conn, "machines", old_machines[alias])
        conn.execute("DELETE FROM machines WHERE id=?", (old_machines[alias],))
    for alias in set(old_accounts) - {a[0] for a in accounts}:
        _refuse_if_referenced(conn, "accounts", old_accounts[alias])
        conn.execute("DELETE FROM accounts WHERE id=?", (old_accounts[alias],))


# Every (table, column) that points at an inventory row, declared by a
# foreign key. A test compares this list with the live schema's foreign
# keys, so a new reference cannot be forgotten.
REMOVAL_GUARDS = {
    "accounts": (
        ("readings", "account_id"), ("windows", "account_id"),
        ("gateways", "account_id"), ("models", "account_id"),
        ("project_accounts", "account_id"),
        ("billing_policies", "account_id"), ("billing_totals", "account_id"),
        ("blocks", "account_id"), ("leases", "account_id"),
        ("adjustments", "account_id"), ("limit_events", "account_id")),
    "machines": (
        ("readings", "machine_id"), ("gateways", "machine_id"),
        ("seats", "machine_id"), ("leases", "machine_id"),
        ("billing_policies", "reporter_machine_id")),
    "gateways": (
        ("leases", "gateway_id"), ("receipts", "gateway_id"),
        ("adjustments", "gateway_id"),
        ("gateway_epochs", "gateway_id"), ("gateway_totals", "gateway_id"),
        ("gateway_approvals", "gateway_id"), ("gateway_adoptions", "gateway_id"),
        ("gateway_readers", "gateway_id"),
        ("gateway_transitions", "gateway_id"), ("gateway_chain_state", "gateway_id")),
}


def _forget_evidence(conn, gateway_id):
    """A gateway moved to another machine or account (card R1-3a, DESIGN-R1-3.md section 7): its approvals are deleted, its
    chain state no longer points at an adoption (the adoption rows stay, as history), and one `owner_changed` transition is
    written (no event id). It must be approved and adopted again. A row of chain state is never created here."""
    conn.execute("DELETE FROM gateway_approvals WHERE gateway_id=?", (gateway_id,))
    conn.execute("UPDATE gateway_chain_state SET current_adoption_id=NULL WHERE gateway_id=?", (gateway_id,))
    conn.execute("INSERT INTO gateway_transitions(gateway_id, kind, event_id, adoption_id, detected_at, resolved_by_adoption) "
                 "VALUES(?, 'owner_changed', NULL, NULL, ?, NULL)", (gateway_id, int(time.time() * 1000)))


def _conflicting_cap_scopes(conn):
    """The money-limit scopes whose accounts declare more than one known cap basis, as a set of (scope, scope id) (card R1-M follow-up).

    A team, project, seat or platform cap has no tax basis of its own; each account declares the basis its caps are counted in, and
    spending counted with VAT cannot be added to spending counted without it. The accounts of a scope are the accounts of its project
    (a team or a seat belongs to a project; the platform spans every account). Only accounts with a billing policy whose cap basis is
    known (`ex_vat` or `invoice_total`) take part; an account with no policy or an `unknown` basis does not. Read on the state the
    connection sees, so it can be asked before an import's changes and again on the state the import would leave."""
    known = {account_id: basis for account_id, basis in conn.execute(
        "SELECT account_id, cap_basis FROM billing_policies WHERE cap_basis IN (?, ?)", (BASIS_EX_VAT, BASIS_INVOICE_TOTAL))}
    if len(set(known.values())) < 2:
        return set()                                        # nothing can conflict
    accounts_of = {
        "platform": "SELECT id FROM accounts",
        "project": "SELECT account_id FROM project_accounts WHERE project_id=?",
        "team": "SELECT pa.account_id FROM project_accounts pa JOIN teams t ON t.project_id=pa.project_id WHERE t.id=?",
        "seat": "SELECT pa.account_id FROM project_accounts pa JOIN teams t ON t.project_id=pa.project_id "
                "JOIN seats s ON s.team_id=t.id WHERE s.id=?",
    }
    conflicting = set()
    for scope, scope_id in conn.execute(
            "SELECT DISTINCT scope, scope_id FROM limits WHERE metric='money_micro_eur'").fetchall():
        query = accounts_of[scope]
        found = conn.execute(query, () if scope == "platform" else (scope_id,)).fetchall()
        if len({known[row[0]] for row in found if row[0] in known}) > 1:
            conflicting.add((scope, scope_id))
    return conflicting


def _refuse_new_cap_basis_conflicts(conn, before):
    """Refuse an import that ADDS a conflicting money-limit scope (card R1-M follow-up, fix round 1, D-188).

    `before` is `_conflicting_cap_scopes` taken before the import's changes. It is compared with the same set on the state the
    import would leave, INSIDE the import's transaction, so a refusal rolls everything back and writes nothing. Only a scope that was
    not conflicting before and is now is a new conflict: an unrelated import leaves the set the same and is accepted, and a repairing
    import shrinks it and is accepted. So a store that already holds a mismatch (made before this check, and possibly not
    repairable by import once billing history freezes a cap basis) stays importable. The lease check keeps refusing leases in a
    conflicting scope. The message is fixed and names no account."""
    if _conflicting_cap_scopes(conn) - before:
        raise QuotaError(CODE_CAP_BASIS_CONFLICT, MSG_CAP_BASIS_CONFLICT)


def _refuse_if_referenced(conn, target, entity_id):
    for table, column in REMOVAL_GUARDS[target]:
        if _references(conn, table, column, entity_id):
            raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)


def _references(conn, table, column, entity_id):
    """True when any row in `table` points at `entity_id` via `column`."""
    row = conn.execute(
        "SELECT 1 FROM " + table + " WHERE " + column + " = ? LIMIT 1",
        (entity_id,),
    ).fetchone()
    return row is not None


def _allocated(conn, account_id):
    """The account's gateway shares, added exactly (card R1-M, S5 to S7). The database's own SUM stops with an error when the
    shares together pass the largest number it keeps; the shares are added by `money.exact_sum` instead, so a total that is
    merely huge is an ordinary number (and the allocation check then refuses it as too large), never a store fault."""
    try:
        return money.exact_sum(conn, "gateways WHERE account_id = ?", (account_id,), "share_micro_eur")
    except money.MoneyError:
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from None


def billing_policy_import(path, file):
    """Validate a billing-policy document and apply it atomically.

    Validation (the closed shape, the closed enums, the B01 allocation
    check) happens before or under the write lock, and the import is an
    UPSERT - one policy per metered account. Once the account has a
    provider billing reading, the protected fields (the reporter
    machine, the scope alias/revision, and the three tax bases) are
    frozen: a change to any of them is refused, while the operator-
    tunable fields (allowance, rate, cadence, tolerances, actions)
    still update.
    """
    try:
        with open(file, "r", encoding="utf-8-sig") as handle:
            doc = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, ValueError, RecursionError):
        # Missing or unreadable file, malformed JSON, undecodable text, or
        # pathologically deep nesting: a bad document, not a store fault.
        raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)

    policies = validate_billing_policies(doc)

    conn = _connect(path, must_exist=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            require_supported(conn)
            conflicts_before = _conflicting_cap_scopes(conn)
            _apply_billing_policies(conn, policies)
            _refuse_new_cap_basis_conflicts(conn, conflicts_before)
            conn.execute("COMMIT")
        except BaseException:
            rollback_quietly(conn)
            raise
    except sqlite3.Error as exc:
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from exc
    finally:
        conn.close()


def _apply_billing_policies(conn, policies):
    """Apply a validated billing-policy document, one upsert per policy.

    Inside the import's BEGIN IMMEDIATE. The store-level rules: the
    account exists and is METERED (a provider bill is attributed money;
    a subscription account has no cap to bound an allowance), the
    reporter machine exists, and the B01 allocation (shares + allowance
    <= cap, on the cap's tax basis) fits. The protected fields are
    frozen once a provider billing reading for the account exists.
    """
    accounts = {}
    for (account_id, alias, kind, budget) in conn.execute(
            "SELECT id, alias, kind, budget_micro_eur FROM accounts"):
        accounts[alias] = (account_id, kind, budget)
    machine_ids = dict(conn.execute("SELECT alias, id FROM machines"))
    shares = {}
    for (account_id,) in conn.execute("SELECT DISTINCT account_id FROM gateways").fetchall():
        shares[account_id] = _allocated(conn, account_id)

    for policy in policies:
        account = accounts.get(policy["account"])
        if account is None or account[1] != "metered":
            raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
        account_id, _kind, budget = account
        reporter_id = machine_ids.get(policy["reporter_machine"])
        if reporter_id is None:
            raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
        allocation_within_cap(
            budget, shares.get(account_id, 0),
            policy["non_ai_allowance_micro_eur"],
            policy["cap_basis"], policy["ledger_basis"],
            policy["vat_bps"])

        existing = conn.execute(
            "SELECT reporter_machine_id, scope_alias, scope_revision, "
            "cap_basis, provider_basis, ledger_basis "
            "FROM billing_policies WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        has_history = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM readings WHERE kind = "
            "'provider_billing' AND account_id = ?)",
            (account_id,),
        ).fetchone()[0]
        if existing is not None and has_history:
            protected = (reporter_id, policy["scope_alias"],
                         policy["scope_revision"], policy["cap_basis"],
                         policy["provider_basis"], policy["ledger_basis"])
            if existing != protected:
                # Owner/scope/basis changes are refused once a billing
                # reading exists (section 11): the later audited
                # resolution integration is the only path past them.
                raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)

        conn.execute(
            "INSERT INTO billing_policies(account_id, reporter_machine_id, "
            "scope_alias, scope_revision, dedicated_ai, "
            "non_ai_allowance_micro_eur, cap_basis, provider_basis, "
            "ledger_basis, vat_bps, poll_seconds, max_age_seconds, "
            "lag_seconds, abs_tolerance_micro_eur, relative_tolerance_bps, "
            "mode, stale_action, divergence_action) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id) DO UPDATE SET "
            "reporter_machine_id=excluded.reporter_machine_id, "
            "scope_alias=excluded.scope_alias, "
            "scope_revision=excluded.scope_revision, "
            "dedicated_ai=excluded.dedicated_ai, "
            "non_ai_allowance_micro_eur=excluded.non_ai_allowance_micro_eur, "
            "cap_basis=excluded.cap_basis, "
            "provider_basis=excluded.provider_basis, "
            "ledger_basis=excluded.ledger_basis, "
            "vat_bps=excluded.vat_bps, "
            "poll_seconds=excluded.poll_seconds, "
            "max_age_seconds=excluded.max_age_seconds, "
            "lag_seconds=excluded.lag_seconds, "
            "abs_tolerance_micro_eur=excluded.abs_tolerance_micro_eur, "
            "relative_tolerance_bps=excluded.relative_tolerance_bps, "
            "mode=excluded.mode, "
            "stale_action=excluded.stale_action, "
            "divergence_action=excluded.divergence_action",
            (account_id, reporter_id, policy["scope_alias"],
             policy["scope_revision"], 1,
             policy["non_ai_allowance_micro_eur"], policy["cap_basis"],
             policy["provider_basis"], policy["ledger_basis"],
             policy["vat_bps"], policy["poll_seconds"],
             policy["max_age_seconds"], policy["lag_seconds"],
             policy["abs_tolerance_micro_eur"],
             policy["relative_tolerance_bps"], policy["mode"],
             policy["stale_action"], policy["divergence_action"]),
        )


def _check_http_identity(conn, wire_identity):
    """Fix round 3 finding F1: see lease._check_http_identity - the same
    HTTP wire-identity check, duplicated rather than imported (store.py
    and lease.py deliberately do not import each other)."""
    if wire_identity is None:
        return
    route, key, wire_hash = wire_identity
    row = conn.execute(
        "SELECT wire_hash FROM http_request_identity WHERE route=? "
        "AND idempotency_key=?", (route, key)).fetchone()
    if row is not None and row[0] != wire_hash:
        raise QuotaError(CODE_IDEMPOTENCY_CONFLICT, MSG_IDEMPOTENCY_CONFLICT)


def _record_http_identity(conn, wire_identity, now_ms):
    """See lease._record_http_identity - a monkeypatch point for the
    interrupted-commit regressions."""
    if wire_identity is None:
        return
    route, key, wire_hash = wire_identity
    conn.execute(
        "INSERT OR IGNORE INTO http_request_identity(route, "
        "idempotency_key, wire_hash, created_at) VALUES(?,?,?,?)",
        (route, key, wire_hash, now_ms))


def reading_import(path, file, wire_identity=None):
    """Validate a card-2 reading document and apply it atomically.

    Validation (shape/types, no future observations) happens before any
    write. The import runs under the write lock (BEGIN IMMEDIATE): each
    reading is idempotent by event_id (a replay - same id, same content - is
    a no-op; a collision - same id, different content - aborts the whole
    batch with IDEMPOTENCY_CONFLICT), references are resolved, and the
    reading is recorded and reduced into windows / the gateway report. All
    of it commits as one transaction, so a partial import never commits.

    `wire_identity` (fix round 3 finding F1) is the HTTP layer's own
    `(route, key, wire_hash)` full-wire-body identity for the single-
    reading HTTP path, checked and recorded atomically with this same
    transaction. None for the CLI batch-import path (no HTTP identity to
    guard).
    """
    now_ms = int(time.time() * 1000)
    try:
        with open(file, "r", encoding="utf-8-sig") as handle:
            doc = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, ValueError, RecursionError):
        # Missing or unreadable file, malformed JSON, undecodable text, or
        # pathologically deep nesting: a bad document, not a store fault.
        raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)

    readings = validate_readings(doc, now_ms)

    conn = _connect(path, must_exist=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("SAVEPOINT batch")
            try:
                _check_http_identity(conn, wire_identity)
                require_supported(conn)
                machine_ids = dict(conn.execute("SELECT alias, id FROM machines"))
                account_ids = dict(conn.execute("SELECT alias, id FROM accounts"))
                gateway_ids = dict(conn.execute("SELECT alias, id FROM gateways"))
                for reading in readings:
                    _apply_reading(conn, reading, machine_ids, account_ids,
                                   gateway_ids, now_ms)
                _record_http_identity(conn, wire_identity, now_ms)
            except QuotaError as exc:
                if exc.code == CODE_IDEMPOTENCY_CONFLICT:
                    # Card R1-3b (DESIGN-R1-3.md 3, "the durable collision record"): the refusal leaves a permanent record, written
                    # INSIDE this write transaction (a follow-on transaction after the rollback could be lost to a stop in between).
                    # Roll back to the start so no rejected reading data is kept, write the record, commit, then return the conflict.
                    conn.execute("ROLLBACK TO batch")
                    _record_collisions(conn, readings, now_ms)
                    _after_collision_record(conn)
                    conn.execute("COMMIT")
                raise
            conn.execute("COMMIT")
        except BaseException:
            rollback_quietly(conn)
            raise
    except sqlite3.Error as exc:
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from exc
    finally:
        conn.close()


def _apply_reading(conn, reading, machine_ids, account_ids, gateway_ids, now_ms):
    """Apply one normalized reading inside the import's write transaction.

    Resolves references (a dangling machine/account/gateway alias is
    INVALID_CONFIG), enforces idempotency by event_id (replay = no-op,
    collision = IDEMPOTENCY_CONFLICT), records the reading, and reduces it
    into windows / the gateway report.
    """
    machine_id = machine_ids.get(reading["machine"])
    if machine_id is None:
        raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)

    # An account of null marks an ambiguous source: recorded, but reduced
    # into no account (account isolation).
    account_alias = reading["account"]
    account_id = (account_ids.get(account_alias)
                  if account_alias is not None else None)
    if account_alias is not None and account_id is None:
        raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)

    kind = reading["kind"]
    event_id = reading["event_id"]
    payload = json.dumps(reading["payload"], sort_keys=True)

    # Idempotency by event_id, checked against the store under the lock.
    existing = conn.execute(
        "SELECT kind, machine_id, account_id, observed_at, payload "
        "FROM readings WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if existing is not None:
        signature = (kind, machine_id, account_id, reading["observed_at"], payload)
        if existing != signature:
            raise QuotaError(CODE_IDEMPOTENCY_CONFLICT, MSG_IDEMPOTENCY_CONFLICT)
        # Identical replay: no change.
        return

    conn.execute(
        "INSERT INTO readings(event_id, machine_id, account_id, seat_id, kind, "
        "observed_at, received_at, payload) VALUES(?,?,?,?,?,?,?,?)",
        (event_id, machine_id, account_id, None, kind,
         reading["observed_at"], now_ms, payload),
    )

    if kind == KIND_CLI_WINDOW:
        _reduce_cli_window(conn, reading, account_id)
    elif kind == KIND_GATEWAY_SNAPSHOT:
        _reduce_gateway_snapshot(conn, reading, gateway_ids, now_ms)
    else:  # KIND_PROVIDER_BILLING
        _reduce_provider_billing(conn, reading, account_id, machine_id)


def _reduce_provider_billing(conn, reading, account_id, machine_id):
    """Reduce a provider_billing reading into billing_totals.

    A provider bill is attributed money, so the account must be metered
    (a subscription account has no spend to attribute). The account must
    have an operator billing policy: with no policy the owner is unknown,
    so a reading from any machine, scope alias or revision is rejected
    and nothing is reduced into billing_totals. Where the policy exists,
    the reading must match it - reporter machine, scope alias and scope
    revision (section 11): a foreign machine, an inactive revision, or a
    scope alias the policy does not designate is rejected,
    and a replica copy never adds totals.

    The metered kind and the B01 allocation invariant are re-checked
    against the current config on every reading: a config-import that
    broke either refuses the reading (exit 2) until the operator repairs
    it.

    The row is keyed by (account_id, scope_revision, period_start); the
    reduction is by SOURCE time (the pure reduce_billing_total): newer
    observations set latest (a newer total below the high-water latches
    a conflict), an equal-time unequal total latches a conflict, and an
    older total above the latest latches one too (raising the
    high-water where it beats it) without refreshing the latest time.
    The conflict is sticky: only a later audited resolution clears it.
    The currency and the money basis live in the policy, not the row.
    """
    account = conn.execute(
        "SELECT kind, budget_micro_eur FROM accounts WHERE id = ?",
        (account_id,)).fetchone()
    if account[0] != "metered":
        raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)

    policy = conn.execute(
        "SELECT reporter_machine_id, scope_alias, scope_revision, "
        "non_ai_allowance_micro_eur, cap_basis, ledger_basis, vat_bps "
        "FROM billing_policies WHERE account_id = ?", (account_id,)
    ).fetchone()
    if policy is None:
        # No policy, no owner: nothing is accepted until the operator
        # designates the reporter, scope and revision.
        raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
    if (machine_id != policy[0]
            or reading["scope_alias"] != policy[1]
            or reading["scope_revision"] != policy[2]):
        raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
    # B01 is enforced at ingest as well as at policy import: config-
    # import is a full replacement, so the shares, the cap or the
    # account kind may have moved since the policy was checked. The
    # reading is accepted only while the CURRENT config still holds
    # shares + non-AI allowance <= cap on the cap basis; an unknown tax
    # basis defers the check (the reading is recorded, never approved).
    shares = _allocated(conn, account_id)
    allocation_within_cap(
        account[1], shares, policy[3], policy[4], policy[5], policy[6])

    period_start = reading["period_start"]
    period_end = reading["period_end"]
    scope_revision = reading["scope_revision"]
    row = conn.execute(
        "SELECT period_end, latest_micro_eur, observed_at, "
        "latest_event_id, high_water_micro_eur, high_water_event_id, "
        "conflict "
        "FROM billing_totals WHERE account_id = ? AND scope_revision = ? "
        "AND period_start = ?",
        (account_id, scope_revision, period_start),
    ).fetchone()
    if row is not None:
        if row[0] != period_end:
            # Row invariant: one (account, scope_revision, period_start)
            # row carries one period_end. A different end on the same key
            # is a scope mismatch, not a second period (the import path
            # cannot reach this now that a period is exactly one UTC
            # month; the guard pins the invariant against other writers).
            raise QuotaError(CODE_BILLING_SCOPE_MISMATCH,
                              MSG_BILLING_SCOPE_MISMATCH)
        existing = row[1:]
    else:
        existing = None
    updated = reduce_billing_total(existing, reading["total_micro_eur"],
                                   reading["observed_at"],
                                   reading["event_id"])
    if updated is None:
        # The reading is recorded but changes nothing (a replay or an
        # older total at or below the latest).
        return
    (latest, observed_at, latest_event_id, high_water, high_water_event_id,
     conflict) = updated
    if existing is None:
        conn.execute(
            "INSERT INTO billing_totals(account_id, scope_revision, "
            "period_start, period_end, latest_micro_eur, observed_at, "
            "latest_event_id, high_water_micro_eur, high_water_event_id, "
            "conflict) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (account_id, scope_revision, period_start, period_end, latest,
             observed_at, latest_event_id, high_water, high_water_event_id,
             conflict),
        )
    else:
        conn.execute(
            "UPDATE billing_totals SET latest_micro_eur=?, observed_at=?, "
            "latest_event_id=?, high_water_micro_eur=?, "
            "high_water_event_id=?, conflict=? "
            "WHERE account_id=? AND scope_revision=? AND period_start=?",
            (latest, observed_at, latest_event_id, high_water,
             high_water_event_id, conflict,
             account_id, scope_revision, period_start),
        )


def _reduce_cli_window(conn, reading, account_id):
    """Reduce a cli_window reading into the account's windows.

    An ambiguous source (account_id None) is recorded but reduced into no
    account: no window row is created or updated.
    """
    if account_id is None:
        return
    for key, used_bps, resets_at in reading["windows"]:
        row = conn.execute(
            "SELECT used_bps, resets_at, observed_at, confidence, source_stale, "
            "covered_seq, source_event_id FROM windows "
            "WHERE account_id = ? AND window_key = ?",
            (account_id, key),
        ).fetchone()
        reduced = reduce_window(
            row, used_bps, resets_at, reading["observed_at"],
            reading["confidence"], reading["source_stale"],
            reading["covered_seq"], reading["event_id"],
        )
        if reduced is None:
            # Out of order (an older reset epoch): no change to the window.
            continue
        (new_used, new_resets, new_obs, new_conf, new_stale, new_cov, new_ev) = reduced
        if row is None:
            conn.execute(
                "INSERT INTO windows(account_id, window_key, used_bps, resets_at, "
                "observed_at, confidence, source_stale, covered_seq, "
                "source_event_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (account_id, key, new_used, new_resets, new_obs, new_conf,
                 1 if new_stale else 0, new_cov, new_ev),
            )
        else:
            conn.execute(
                "UPDATE windows SET used_bps=?, resets_at=?, observed_at=?, "
                "confidence=?, source_stale=?, covered_seq=?, source_event_id=? "
                "WHERE account_id=? AND window_key=?",
                (new_used, new_resets, new_obs, new_conf,
                 1 if new_stale else 0, new_cov, new_ev, account_id, key),
            )


def _after_collision_record(conn):
    """A fault-injection point (a test replaces it): after the collision record is written and before it is committed."""


def _note_transition(conn, gateway_id, kind, event_id, now_ms, conflict=(None, None, None), target=None):
    """Write one transition row (card R1-3b), once: the unique key (gateway, kind, event) makes a repeat a no-op. Nothing here
    resolves or overwrites a transition. `conflict` is the incarnation, epoch and seq that collided (a conflict kind only);
    `target` is the reader a `reader_changed` is about (one row per reader, by its own unique key)."""
    chain = conn.execute("SELECT current_adoption_id FROM gateway_chain_state WHERE gateway_id=?", (gateway_id,)).fetchone()
    conn.execute(
        "INSERT OR IGNORE INTO gateway_transitions(gateway_id, kind, event_id, adoption_id, target_incarnation, "
        "conflict_incarnation, conflict_epoch, conflict_seq, detected_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (gateway_id, kind, event_id, None if chain is None else chain[0], target, conflict[0], conflict[1], conflict[2], now_ms))


def _reader_state(conn, gateway_id, incarnation):
    """The state of a reader in `gateway_readers` (`pending`, `current`, `retired`), or None if it has no row. The ONE place a writer
    asks whether a reader is retired: a report of a retired reader is kept and counted and creates no transition of any kind."""
    row = conn.execute("SELECT state FROM gateway_readers WHERE gateway_id=? AND reader_incarnation=?",
                       (gateway_id, incarnation)).fetchone()
    return None if row is None else row[0]


def _count_retired_report(conn, gateway_id):
    conn.execute("INSERT OR IGNORE INTO gateway_chain_state(gateway_id) VALUES(?)", (gateway_id,))
    conn.execute("UPDATE gateway_chain_state SET retired_reader_reports = retired_reader_reports + 1 WHERE gateway_id=?", (gateway_id,))


# One fixed statement per counter: the names are never built into SQL text.
_COUNTER_SQL = {
    "late_reports": "UPDATE gateway_chain_state SET late_reports = late_reports + 1 WHERE gateway_id=?",
    "old_chain_reports": "UPDATE gateway_chain_state SET old_chain_reports = old_chain_reports + 1 WHERE gateway_id=?",
    "duplicate_reports": "UPDATE gateway_chain_state SET duplicate_reports = duplicate_reports + 1 WHERE gateway_id=?",
}


def _promote_reader(conn, gateway_id, incarnation, adoption_id, now_ms):
    """Make one reader `current` (card R1-3c; adoption in R1-3d uses the same step). The old `current` reader and every `pending` one
    is RETIRED FIRST, because the unique index on `current` is immediate; only then is the chosen reader promoted. A retired reader
    never becomes current again, so a promotion of one is refused here as a fault."""
    if _reader_state(conn, gateway_id, incarnation) == "retired":
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED)
    conn.execute(
        "UPDATE gateway_readers SET state='retired', retired_at=?, decided_by_adoption=? "
        "WHERE gateway_id=? AND reader_incarnation<>? AND state IN ('current','pending')",
        (now_ms, adoption_id, gateway_id, incarnation))
    conn.execute("UPDATE gateway_readers SET state='current', decided_by_adoption=?, retired_at=NULL "
                 "WHERE gateway_id=? AND reader_incarnation=?", (adoption_id, gateway_id, incarnation))


def _evidence_state(conn, gateway_id, report):
    """Read, before the report is stored, the facts `evidence.assess` needs (card R1-3c)."""
    inc, epoch = report.incarnation, report.epoch
    chain = conn.execute("SELECT current_adoption_id, last_quiet_unattributed FROM gateway_chain_state WHERE gateway_id=?",
                         (gateway_id,)).fetchone()
    adoption_id = None if chain is None else chain[0]
    adoption = None
    if adoption_id is not None:
        row = conn.execute(
            "SELECT reader_incarnation, ledger_epoch, generation, period_start, seq, settled_micro_eur, unattributed_micro_eur, "
            "price_hash, child_cap_hash FROM gateway_adoptions WHERE id=?", (adoption_id,)).fetchone()
        adoption = evidence.Adoption(*row)
    generation = conn.execute("SELECT generation FROM gateway_epochs WHERE gateway_id=? AND reader_incarnation=? AND ledger_epoch=?",
                              (gateway_id, inc, epoch)).fetchone()
    same_seq = tuple(
        (r[0], r[1], r[2], r[3], r[4], r[5], r[6], bool(r[7]), r[8], r[9], r[10], bool(r[11])) for r in conn.execute(
            "SELECT period_start, period_end, settled_micro_eur, reserved_micro_eur, uncertain_micro_eur, unattributed_micro_eur, "
            "receipts_seq, quiet, held_over_reservation, price_hash, child_cap_hash, gateway_flag FROM gateway_totals "
            "WHERE gateway_id=? AND reader_incarnation=? AND ledger_epoch=? AND seq=?", (gateway_id, inc, epoch, report.seq)))
    max_seq = conn.execute("SELECT MAX(seq) FROM gateway_totals WHERE gateway_id=? AND reader_incarnation=? AND ledger_epoch=?",
                           (gateway_id, inc, epoch)).fetchone()[0]
    # The highest `settled` already seen in this report's own month (same reader, epoch, generation and month): `settled` is cumulative
    # within every month, also before that month's adoption, so a fall is caught before any roll adopts the lower figure. In the adopted
    # chain the adopted report itself is the floor; in any other month the first report is compared with nothing (the month reset).
    floor, adopted = 0, None
    if adoption is not None and (inc, epoch, report.generation, report.period_start) == (
            adoption.incarnation, adoption.epoch, adoption.generation, adoption.period_start):
        floor, adopted = adoption.seq, adoption.settled
    row = conn.execute(
        "SELECT MAX(settled_micro_eur) FROM gateway_totals WHERE gateway_id=? AND reader_incarnation=? AND ledger_epoch=? "
        "AND generation=? AND period_start=? AND seq>=? AND seq<?",
        (gateway_id, inc, epoch, report.generation, report.period_start, floor, report.seq)).fetchone()[0]
    previous = max((v for v in (adopted, row) if v is not None), default=None)
    open_kinds = frozenset(r[0] for r in conn.execute(
        "SELECT DISTINCT kind FROM gateway_transitions WHERE gateway_id=? AND resolved_by_adoption IS NULL", (gateway_id,)))
    approved = conn.execute("SELECT 1 FROM gateway_approvals WHERE gateway_id=? AND price_hash=? AND child_cap_hash=?",
                            (gateway_id, report.price_hash, report.child_cap_hash)).fetchone() is not None
    return evidence.State(
        _reader_state(conn, gateway_id, inc), adoption, None if generation is None else generation[0], same_seq, max_seq,
        previous, None if chain is None else chain[1], open_kinds, approved)


def _apply_assessment(conn, gateway_id, report, assessment, now_ms):
    """Write what `evidence.assess` decided, once, at ingest (card R1-3c). Nothing is recomputed from history and nothing is resolved."""
    if assessment.standing == evidence.STANDING_RETIRED:
        _count_retired_report(conn, gateway_id)                       # kept as history and counted; no transition of any kind
        return
    if assessment.create_reader:
        conn.execute("INSERT INTO gateway_readers(gateway_id, reader_incarnation, first_event_id, first_seen_at, state) "
                     "VALUES(?,?,?,?,'pending')", (gateway_id, report.incarnation, report.event_id, now_ms))
    for kind, conflict in assessment.transitions:
        _note_transition(conn, gateway_id, kind, report.event_id, now_ms, conflict or (None, None, None),
                         report.incarnation if kind == evidence.KIND_READER_CHANGED else None)
    if assessment.counter is not None or assessment.remember_quiet or assessment.roll:
        conn.execute("INSERT OR IGNORE INTO gateway_chain_state(gateway_id) VALUES(?)", (gateway_id,))
    if assessment.counter is not None:
        conn.execute(_COUNTER_SQL[assessment.counter], (gateway_id,))
    if assessment.remember_quiet:
        conn.execute("UPDATE gateway_chain_state SET last_quiet_event_id=?, last_quiet_unattributed=? WHERE gateway_id=?",
                     (report.event_id, report.unattributed, gateway_id))
    if assessment.roll:
        _roll_period(conn, gateway_id, report, now_ms)


def _roll_period(conn, gateway_id, report, now_ms):
    """The automatic month roll (DESIGN-R1-3.md 4.4): a `period_roll` adoption whose baseline is the report, no operator. The same
    reader stays current (the old current is retired before the promotion, as in every writer of `current`)."""
    previous = conn.execute("SELECT current_adoption_id FROM gateway_chain_state WHERE gateway_id=?", (gateway_id,)).fetchone()[0]
    adoption_id = conn.execute(
        "INSERT INTO gateway_adoptions(gateway_id, event_id, reader_incarnation, ledger_epoch, generation, period_start, seq, "
        "settled_micro_eur, reserved_micro_eur, uncertain_micro_eur, unattributed_micro_eur, price_hash, child_cap_hash, "
        "adopted_at, operator, kind, prev_adoption_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,'period_roll',?)",
        (gateway_id, report.event_id, report.incarnation, report.epoch, report.generation, report.period_start, report.seq,
         report.settled, report.reserved, report.uncertain, report.unattributed, report.price_hash, report.child_cap_hash,
         now_ms, previous)).lastrowid
    _promote_reader(conn, gateway_id, report.incarnation, adoption_id, now_ms)
    conn.execute("UPDATE gateway_chain_state SET current_adoption_id=?, last_quiet_event_id=?, last_quiet_unattributed=? "
                 "WHERE gateway_id=?", (adoption_id, report.event_id, report.unattributed, gateway_id))
    conn.execute("INSERT INTO audit(actor_alias, action, object_id, at, redacted_details) VALUES(?,?,?,?,?)",
                 ("coordinator", "gateway_period_roll", report.event_id, now_ms, json.dumps({"adoption_id": adoption_id})))


def _record_report_v4(conn, reading, gateway_id, now_ms):
    """Keep a version-4 gateway report and decide what it means (cards R1-3b and R1-3c): record its (reader incarnation, epoch) with
    its generation, copy it into `gateway_totals`, and write the transitions `evidence.assess` returns. The same (gateway,
    incarnation, epoch) with another generation is the identity conflict of the design (`epoch_generation_mismatch`): the first
    mapping stays. A report of a retired reader is kept and counted and creates no transition. Only `status` will read the result."""
    event_id = reading["event_id"]
    incarnation, epoch, generation = reading["reader_incarnation"], reading["ledger_epoch"], reading["generation"]
    hashes = reading["policy_hashes"]
    report = evidence.Report(
        event_id, incarnation, epoch, generation, reading["seq"], reading["period_start"], reading["period_end"],
        reading["settled_micro_eur"], reading["reserved_micro_eur"], reading["uncertain_micro_eur"], reading["unattributed_micro_eur"],
        reading["receipts_seq"], bool(reading["quiet"]), reading["held_over_reservation"], hashes["price"], hashes["child_cap"],
        bool(reading["reconciliation_required"]))
    state = _evidence_state(conn, gateway_id, report)
    assessment = evidence.assess(report, state)
    if state.generation is None:
        conn.execute("INSERT INTO gateway_epochs(gateway_id, reader_incarnation, ledger_epoch, generation, first_event_id) "
                     "VALUES(?,?,?,?,?)", (gateway_id, incarnation, epoch, generation, event_id))
    conn.execute(
        "INSERT INTO gateway_totals(event_id, gateway_id, reader_incarnation, ledger_epoch, generation, seq, period_start, "
        "period_end, settled_micro_eur, reserved_micro_eur, uncertain_micro_eur, unattributed_micro_eur, receipts_seq, quiet, "
        "held_over_reservation, price_hash, child_cap_hash, gateway_flag, observed_at, received_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, gateway_id, incarnation, epoch, generation, reading["seq"], reading["period_start"], reading["period_end"],
         reading["settled_micro_eur"], reading["reserved_micro_eur"], reading["uncertain_micro_eur"],
         reading["unattributed_micro_eur"], reading["receipts_seq"], 1 if reading["quiet"] else 0,
         reading["held_over_reservation"], hashes["price"], hashes["child_cap"],
         1 if reading["reconciliation_required"] else 0, reading["observed_at"], now_ms))
    _apply_assessment(conn, gateway_id, report, assessment, now_ms)


def _record_collisions(conn, readings, now_ms):
    """After a refused collision (the same event id with different content), leave a durable `sequence_conflict` (card R1-3b).

    Run INSIDE the write transaction, after the rollback to its start, so the state read here is the store's own and no rejected
    reading data exists. For each version-4 gateway report of the refused batch whose event id is already stored with DIFFERENT
    content: the authenticated sender (the reading's machine and account) must be the gateway's CURRENT owner, re-checked here
    under the lock (the payload's gateway name alone, and any authorization from before the rollback, are never trusted); and the
    stored report must belong to the same gateway, incarnation and epoch (provenance). A sender that is not the owner, or a
    collision without that provenance, writes nothing. A collision from an incarnation that `gateway_readers` marks retired is only
    counted (`retired_reader_reports`), never a transition. A repeat of the same collision is a no-op."""
    for reading in readings:
        if reading["kind"] != KIND_GATEWAY_SNAPSHOT or "reader_incarnation" not in reading:
            continue
        stored = conn.execute("SELECT kind, machine_id, account_id, observed_at, payload FROM readings WHERE event_id=?",
                              (reading["event_id"],)).fetchone()
        gateway = conn.execute(
            "SELECT g.id, m.alias, a.alias, g.machine_id, g.account_id FROM gateways g JOIN machines m ON m.id=g.machine_id "
            "JOIN accounts a ON a.id=g.account_id WHERE g.alias=?", (reading["gateway"],)).fetchone()
        if stored is None or gateway is None:
            continue
        gateway_id, owner_machine, owner_account = gateway[0], gateway[1], gateway[2]
        if owner_machine != reading["machine"] or owner_account != reading["account"]:
            continue                                                   # not the gateway's owner: nothing is written
        signature = (reading["kind"], gateway[3], gateway[4], reading["observed_at"],
                     json.dumps(reading["payload"], sort_keys=True))
        if tuple(stored) == signature:
            continue                                                   # the same content: not a collision of content
        report = conn.execute("SELECT gateway_id, reader_incarnation, ledger_epoch, seq FROM gateway_totals WHERE event_id=?",
                              (reading["event_id"],)).fetchone()
        if (report is None or report[0] != gateway_id or report[1] != reading["reader_incarnation"]
                or report[2] != reading["ledger_epoch"]):
            continue                                                   # no provenance: the stored report is not this reader's
        if _reader_state(conn, gateway_id, reading["reader_incarnation"]) == "retired":
            _count_retired_report(conn, gateway_id)
            continue
        _note_transition(conn, gateway_id, "sequence_conflict", reading["event_id"], now_ms,
                         (report[1], report[2], report[3]))


def _reduce_gateway_snapshot(conn, reading, gateway_ids, now_ms=0):
    """Reduce a gateway_snapshot reading into the gateway's report columns.

    Section 5: both sources bind seat/machine/account explicitly. A snapshot
    must bind to its gateway's owner - the gateway's own machine and
    account. A different machine, or an ambiguous source (account null), is
    a mismatch, not a different gateway: INVALID_CONFIG.
    """
    gateway_id = gateway_ids.get(reading["gateway"])
    if gateway_id is None:
        raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
    owner = conn.execute(
        "SELECT m.alias, a.alias FROM gateways g "
        "JOIN machines m ON m.id = g.machine_id "
        "JOIN accounts a ON a.id = g.account_id "
        "WHERE g.id = ?",
        (gateway_id,),
    ).fetchone()
    if owner[0] != reading["machine"] or owner[1] != reading["account"]:
        raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
    if "reader_incarnation" in reading:
        _record_report_v4(conn, reading, gateway_id, now_ms)           # card R1-3b: a version-4 report is also kept as a typed row
    row = conn.execute(
        "SELECT ledger_epoch, last_seq, period_start, period_end, policy_hashes, "
        "sized_ready, state FROM gateways WHERE id = ?",
        (gateway_id,),
    ).fetchone()
    reduced = reduce_gateway_report(
        row, reading["ledger_epoch"], reading["seq"], reading["period_start"],
        reading["period_end"], reading["policy_hashes"], reading["sized_ready"],
        reading["reconciliation_required"],
    )
    if reduced is None:
        # Out of order (a non-increasing ledger sequence): no change.
        return
    (new_epoch, new_seq, new_start, new_end, new_hashes, new_sized, new_state) = reduced
    # last_report_at is the report's source time (the METER_STALE age base).
    conn.execute(
        "UPDATE gateways SET ledger_epoch=?, last_seq=?, period_start=?, "
        "period_end=?, policy_hashes=?, sized_ready=?, state=?, last_report_at=? "
        "WHERE id=?",
        (new_epoch, new_seq, new_start, new_end, new_hashes,
         1 if new_sized else 0, new_state, reading["observed_at"], gateway_id),
    )


def set_coverage(path, account, complete):
    """Set the operator-only coverage_complete flag for an account.

    This is the ONLY path that may change coverage_complete; telemetry
    (reading-import) never touches it. `complete` is a boolean.
    """
    conn = _connect(path, must_exist=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            require_supported(conn)
            row = conn.execute(
                "SELECT id FROM accounts WHERE alias = ?", (account,)
            ).fetchone()
            if row is None:
                raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
            conn.execute(
                "UPDATE accounts SET coverage_complete=? WHERE id=?",
                (1 if complete else 0, row[0]),
            )
            conn.execute("COMMIT")
        except BaseException:
            rollback_quietly(conn)
            raise
    except sqlite3.Error as exc:
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from exc
    finally:
        conn.close()


def set_machine_identity(path, machine, identity_ref, enabled):
    """Enroll or update a machine's HTTP identity allowlist entry
    (operator-only; card 5's identity allowlist, docs/DESIGN.md section
    6: "optional private authenticated TLS tailnet ingress with machine
    allowlist"). There is no remote/HTTP path to this: enrollment is a
    local, operator-driven action, same as set_coverage/reconcile.

    `identity_ref` is the opaque value the host ingress presents per
    request (unique among enrolled machines; None un-enrolls). `enabled`
    is a boolean; a disabled machine is refused like an unenrolled one.
    """
    conn = _connect(path, must_exist=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            require_supported(conn)
            row = conn.execute(
                "SELECT id FROM machines WHERE alias = ?", (machine,)
            ).fetchone()
            if row is None:
                raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
            if identity_ref is not None and conn.execute(
                    "SELECT 1 FROM machines WHERE identity_ref = ? "
                    "AND id != ?", (identity_ref, row[0])).fetchone():
                raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
            conn.execute(
                "UPDATE machines SET identity_ref=?, enabled=? WHERE id=?",
                (identity_ref, 1 if enabled else 0, row[0]),
            )
            conn.execute("COMMIT")
        except BaseException:
            rollback_quietly(conn)
            raise
    except sqlite3.Error as exc:
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from exc
    finally:
        conn.close()


def reconcile(path, gateway):
    """Refused (card R1-3d, DESIGN-R1-3.md 8.1 and F10): clearing a gateway's problems without naming any evidence is gone.

    The old command set `gateways.state` back to `ok` for any alias, with no report named and nothing resolved. It is kept only so that
    an old script gets the fixed sentence that points to `gateway-adopt --evidence`. It does not open the store, so it changes nothing,
    whatever the alias is."""
    raise QuotaError(CODE_EVIDENCE_REQUIRED, MSG_EVIDENCE_REQUIRED)


_HASH64 = re.compile(r"[0-9a-f]{64}")


def gateway_approve(path, gateway, price_hash, child_cap_hash, now_ms=None):
    """Record that the operator has seen a gateway's PAIR of policy fingerprints and accepts it (DESIGN-R1-3.md 2.1), audited.

    An unknown gateway, or a hash that is not 64 lowercase hex characters (a missing child-cap fingerprint included: the feature is not
    installed, so there is nothing to approve), is the existing INVALID_CONFIG sentence and writes nothing. Approving a pair that is
    already approved changes nothing and writes no second audit row. An approval is for the pair: a change of either hash is a new,
    unapproved pair."""
    if not all(type(h) is str and _HASH64.fullmatch(h) for h in (price_hash, child_cap_hash)):
        raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    conn = _connect(path, must_exist=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            require_supported(conn)
            row = conn.execute("SELECT id FROM gateways WHERE alias = ?", (gateway,)).fetchone()
            if row is None:
                raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
            added = conn.execute("INSERT OR IGNORE INTO gateway_approvals(gateway_id, price_hash, child_cap_hash, approved_at, operator) "
                                 "VALUES(?,?,?,?,?)", (row[0], price_hash, child_cap_hash, now_ms, "operator")).rowcount
            if added:
                conn.execute("INSERT INTO audit(actor_alias, action, object_id, at, redacted_details) VALUES(?,?,?,?,?)",
                             ("operator", "gateway_approved", row[0], now_ms, json.dumps({})))
            conn.execute("COMMIT")
        except BaseException:
            rollback_quietly(conn)
            raise
    except sqlite3.Error as exc:
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from exc
    finally:
        conn.close()


def _after_adopt_step(conn, step):
    """The fault-injection point of `gateway_adopt`: called after each step of the transaction (DESIGN-R1-3.md 2.3), so a test can stop the
    adoption between any two steps and see that nothing stayed."""


def gateway_adopt(path, gateway, event_id, resolve, now_ms=None):
    """Adopt a quiet report as the gateway's trusted baseline, choose its reader and clear the open items the operator names
    (DESIGN-R1-3.md 2.3). Returns the new adoption id.

    BEGIN IMMEDIATE, then every item of the predicate is read again inside the transaction (`evidence_view.adopt_blockers`); one
    failure is ADOPTION_REFUSED with its fixed sentence, the reason is only in `status`, and nothing was written. Then, in order and all
    or nothing: record the report's (reader, epoch, generation) if it is new; the adoption row; the reader made `current` and every other
    one retired (the retired reader's `reader_changed` is resolved as retired, not as approved: its reader row says which); the named
    transitions resolved; the chain state and `gateways.state`; one audit row with the incarnations and ids and no amount; COMMIT.
    `resolve` is the list of transition ids as the operator typed them (strings or whole numbers)."""
    ids = set()
    for item in resolve:
        if type(item) is bool or not (isinstance(item, int) or (isinstance(item, str) and item.isascii() and item.isdigit())):
            raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
        ids.add(int(item))
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    conn = _connect(path, must_exist=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            require_supported(conn)
            row = conn.execute("SELECT id FROM gateways WHERE alias = ?", (gateway,)).fetchone()
            if row is None:
                raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
            gateway_id = row[0]
            if evidence_view.adopt_blockers(conn, gateway_id, event_id, ids, now_ms):             # step 1
                raise QuotaError(CODE_ADOPTION_REFUSED, MSG_ADOPTION_REFUSED)
            report = evidence_view.stored_report(conn, gateway_id, event_id)
            inc = report["reader_incarnation"]
            conn.execute("INSERT OR IGNORE INTO gateway_epochs(gateway_id, reader_incarnation, ledger_epoch, generation, first_event_id) "
                         "VALUES(?,?,?,?,?)", (gateway_id, inc, report["ledger_epoch"], report["generation"], event_id))
            _after_adopt_step(conn, 2)
            previous = conn.execute("SELECT current_adoption_id FROM gateway_chain_state WHERE gateway_id=?", (gateway_id,)).fetchone()
            adoption_id = conn.execute(
                "INSERT INTO gateway_adoptions(gateway_id, event_id, reader_incarnation, ledger_epoch, generation, period_start, seq, "
                "settled_micro_eur, reserved_micro_eur, uncertain_micro_eur, unattributed_micro_eur, price_hash, child_cap_hash, "
                "adopted_at, operator, kind, prev_adoption_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'operator',?)",
                (gateway_id, event_id, inc, report["ledger_epoch"], report["generation"], report["period_start"], report["seq"],
                 report["settled_micro_eur"], report["reserved_micro_eur"], report["uncertain_micro_eur"],
                 report["unattributed_micro_eur"], report["price_hash"], report["child_cap_hash"], now_ms, "operator",
                 None if previous is None else previous[0])).lastrowid
            _after_adopt_step(conn, 3)
            conn.execute("INSERT OR IGNORE INTO gateway_readers(gateway_id, reader_incarnation, first_event_id, first_seen_at, state) "
                         "VALUES(?,?,?,?,'pending')", (gateway_id, inc, event_id, now_ms))
            retired = [r[0] for r in conn.execute(
                "SELECT reader_incarnation FROM gateway_readers WHERE gateway_id=? AND reader_incarnation<>? AND state IN "
                "('current','pending') ORDER BY reader_incarnation", (gateway_id, inc))]
            _promote_reader(conn, gateway_id, inc, adoption_id, now_ms)
            _after_adopt_step(conn, 4)
            conn.executemany("UPDATE gateway_transitions SET resolved_by_adoption=? WHERE id=? AND gateway_id=? "
                             "AND resolved_by_adoption IS NULL", [(adoption_id, i, gateway_id) for i in sorted(ids)])
            _after_adopt_step(conn, 5)
            conn.execute("INSERT OR IGNORE INTO gateway_chain_state(gateway_id) VALUES(?)", (gateway_id,))
            conn.execute("UPDATE gateway_chain_state SET current_adoption_id=?, last_quiet_event_id=?, last_quiet_unattributed=? "
                         "WHERE gateway_id=?", (adoption_id, event_id, report["unattributed_micro_eur"], gateway_id))
            left = conn.execute("SELECT COUNT(*) FROM gateway_transitions WHERE gateway_id=? AND resolved_by_adoption IS NULL",
                                (gateway_id,)).fetchone()[0]
            conn.execute("UPDATE gateways SET state=? WHERE id=?",
                         (GATEWAY_STATE_RECONCILE if left else GATEWAY_STATE_OK, gateway_id))
            _after_adopt_step(conn, 6)
            conn.execute("INSERT INTO audit(actor_alias, action, object_id, at, redacted_details) VALUES(?,?,?,?,?)",
                         ("operator", "gateway_adopted", event_id, now_ms,
                          json.dumps({"adoption_id": adoption_id, "chosen": inc, "retired": retired, "resolved": sorted(ids)})))
            _after_adopt_step(conn, 7)
            conn.execute("COMMIT")
            return adoption_id
        except BaseException:
            rollback_quietly(conn)
            raise
    except sqlite3.Error as exc:
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from exc
    finally:
        conn.close()


def evidence_inputs(path, account, now_ms=None):
    """The evaluator's gateway input for one account, built from the usable chain of each of its gateways (card R1-3d, DESIGN-R1-3.md
    2.4): `{"gateways", "readings", "unresolved_event_ids", "states"}`. One read transaction; nothing is written. Nothing in the grant
    path calls it before card R1-5."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    conn = _connect(path, must_exist=True)
    try:
        conn.execute("BEGIN")
        try:
            require_supported(conn)
            result = evidence_view.inputs(conn, account, now_ms)
            conn.execute("COMMIT")
            return result
        except BaseException:
            rollback_quietly(conn)
            raise
    except sqlite3.Error as exc:
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from exc
    finally:
        conn.close()


def _project_windows(conn, account_id, now_ms):
    """Project an account's windows, evaluating freshness at the wall clock."""
    windows = []
    for key, used_bps, resets_at, observed_at, confidence, source_stale in conn.execute(
        "SELECT window_key, used_bps, resets_at, observed_at, confidence, "
        "source_stale FROM windows WHERE account_id = ? ORDER BY window_key",
        (account_id,),
    ):
        current, reason = window_freshness(observed_at, now_ms)
        windows.append({
            "window_key": key,
            "used_bps": used_bps,
            "resets_at": resets_at,
            "confidence": confidence,
            "source_stale": bool(source_stale),
            "observed_at": observed_at,
            "age_ms": now_ms - observed_at,
            "current": current,
            "freshness_reason": reason,
        })
    return windows


def _project_billing(conn, now_ms):
    """Project the card-2b-1 billing state, one entry per account that has a
    billing policy or reduced totals, sorted by account alias.

    Only local aliases and numbers appear (privacy). Freshness is evaluated
    at read time against the policy's max_age; an account with totals but no
    policy projects an unknown freshness (never assumed fresh).
    """
    machine_alias = dict(conn.execute("SELECT id, alias FROM machines"))
    account_alias = dict(conn.execute("SELECT id, alias FROM accounts"))

    policies = {}
    for (account_id, reporter_id, scope_alias, scope_rev, dedicated,
         allowance, cap_basis, provider_basis, ledger_basis, vat_bps,
         poll_s, max_age_s, lag_s, abs_tol, rel_tol, mode, stale_action,
         divergence_action) in conn.execute(
            "SELECT account_id, reporter_machine_id, scope_alias, "
            "scope_revision, dedicated_ai, non_ai_allowance_micro_eur, "
            "cap_basis, provider_basis, ledger_basis, vat_bps, poll_seconds, "
            "max_age_seconds, lag_seconds, abs_tolerance_micro_eur, "
            "relative_tolerance_bps, mode, stale_action, divergence_action "
            "FROM billing_policies"):
        policies[account_id] = {
            "reporter_machine": machine_alias.get(reporter_id),
            "scope_alias": scope_alias,
            "scope_revision": scope_rev,
            "dedicated_ai": bool(dedicated),
            "non_ai_allowance_micro_eur": allowance,
            "cap_basis": cap_basis,
            "provider_basis": provider_basis,
            "ledger_basis": ledger_basis,
            "vat_bps": vat_bps,
            "poll_seconds": poll_s,
            "max_age_seconds": max_age_s,
            "lag_seconds": lag_s,
            "abs_tolerance_micro_eur": abs_tol,
            "relative_tolerance_bps": rel_tol,
            "mode": mode,
            "stale_action": stale_action,
            "divergence_action": divergence_action,
        }

    totals_by_account = {}
    for (account_id, scope_rev, p_start, p_end, latest, obs, _latest_ev,
         hw, _hw_ev, conflict) in conn.execute(
            "SELECT account_id, scope_revision, period_start, period_end, "
            "latest_micro_eur, observed_at, latest_event_id, "
            "high_water_micro_eur, high_water_event_id, conflict "
            "FROM billing_totals"):
        totals_by_account.setdefault(account_id, []).append(
            (scope_rev, p_start, p_end, latest, obs, hw, conflict))

    entries = []
    account_ids = sorted(set(policies) | set(totals_by_account),
                         key=lambda aid: account_alias.get(aid, ""))
    for account_id in account_ids:
        policy = policies.get(account_id)
        max_age_ms = (policy["max_age_seconds"] * 1000) if policy else None
        totals = []
        for (scope_rev, p_start, p_end, latest, obs, hw, conflict) in sorted(
                totals_by_account.get(account_id, []), key=lambda t: (t[0], t[1])):
            fresh, reason = billing_freshness(obs, max_age_ms, now_ms)
            totals.append({
                "scope_revision": scope_rev,
                "period_start": p_start,
                "period_end": p_end,
                "latest_micro_eur": latest,
                "observed_at": obs,
                "high_water_micro_eur": hw,
                "conflict": bool(conflict),
                "age_ms": (now_ms - obs) if obs is not None else None,
                "fresh": fresh,
                "freshness_reason": reason,
            })
        entries.append({
            "account": account_alias.get(account_id),
            "policy": policy,
            "totals": totals,
        })
    return entries


def policy_ids(path):
    """The ids a lease request names, read-only (card L1c): every seat with
    its machine, team and project aliases, every model with its account
    alias, and the profile task classes - sorted, aliases and local UUIDs
    only. Never an identity reference, a vendor, a budget, a rate, a path or
    any other stored value. One read transaction; nothing is written."""
    conn = _connect(path, must_exist=True)
    try:
        conn.execute("BEGIN")
        try:
            require_supported(conn)
            seats = [
                {"machine": machine, "team": team, "project": project,
                 "seat": seat, "id": seat_id}
                for machine, team, project, seat, seat_id in conn.execute(
                    "SELECT m.alias, t.alias, p.alias, s.alias, s.id "
                    "FROM seats s JOIN machines m ON m.id = s.machine_id "
                    "JOIN teams t ON t.id = s.team_id "
                    "JOIN projects p ON p.id = t.project_id "
                    "ORDER BY p.alias, t.alias, m.alias, s.alias")]
            models = [
                {"account": account, "model": model, "id": model_id}
                for account, model, model_id in conn.execute(
                    "SELECT a.alias, mo.alias, mo.id FROM models mo "
                    "JOIN accounts a ON a.id = mo.account_id "
                    "ORDER BY a.alias, mo.alias")]
            task_classes = [row[0] for row in conn.execute(
                "SELECT task_class FROM profiles ORDER BY task_class")]
            # Card 7c-2: a limit event names its account by id, so the account ids are listed too
            # (the alias and the local id, nothing else).
            accounts = [
                {"alias": alias, "id": account_id}
                for alias, account_id in conn.execute(
                    "SELECT alias, id FROM accounts ORDER BY alias")]
            # Card L6a-1: a gateway receipt names its gateway by id, so the gateways are listed too
            # (the alias and the local id, nothing else: never a share, an account or a machine).
            gateways = [
                {"alias": alias, "id": gateway_id}
                for alias, gateway_id in conn.execute(
                    "SELECT alias, id FROM gateways ORDER BY alias")]
            conn.execute("COMMIT")
        except BaseException:
            rollback_quietly(conn)
            raise
    except sqlite3.Error as exc:
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from exc
    finally:
        conn.close()
    return {"seats": seats, "models": models, "task_classes": task_classes,
            "accounts": accounts, "gateways": gateways}


def status(path):
    """Project the inventory plus card-2 windows/reports and the card-2b-1
    billing state, sorted by alias.

    One read transaction: the version check and all projections share a
    single snapshot. Freshness (window age/current, the gateway report
    state, and the billing total's age against its policy's max_age) is
    evaluated against the wall clock at read time.
    """
    now_ms = int(time.time() * 1000)
    conn = _connect(path, must_exist=True)
    try:
        conn.execute("BEGIN")
        try:
            require_supported(conn)
            accounts = []
            for account_id, alias, kind, budget, coverage in conn.execute(
                "SELECT a.id, a.alias, a.kind, a.budget_micro_eur, a.coverage_complete "
                "FROM accounts a ORDER BY a.alias"
            ).fetchall():
                allocated = _allocated(conn, account_id)
                entry = {
                    "alias": alias,
                    "kind": kind,
                    "budget_micro_eur": budget,
                    "allocated_micro_eur": allocated,
                    "coverage_complete": bool(coverage),
                    "windows": _project_windows(conn, account_id, now_ms),
                }
                if allocated > money.INT64_MAX:
                    # The shares together are above the largest number the store keeps: the figure is exact, and this says so.
                    # (Absent otherwise, so the inventory of every ordinary account is unchanged.)
                    entry["allocated_above_int64"] = True
                accounts.append(entry)
            machines = [
                row[0]
                for row in conn.execute("SELECT alias FROM machines ORDER BY alias")
            ]
            gateways = []
            for (gateway_id, alias, account, machine, share, last_report_at, ledger_epoch,
                 last_seq, period_start, period_end, policy_hashes, sized_ready,
                 state) in conn.execute(
                    "SELECT g.id, g.alias, a.alias, m.alias, g.share_micro_eur, "
                    "g.last_report_at, g.ledger_epoch, g.last_seq, g.period_start, "
                    "g.period_end, g.policy_hashes, g.sized_ready, g.state "
                    "FROM gateways g "
                    "JOIN accounts a ON a.id = g.account_id "
                    "JOIN machines m ON m.id = g.machine_id "
                    "ORDER BY g.alias"
                ):
                gateways.append({
                    "alias": alias,
                    "account": account,
                    "machine": machine,
                    "share_micro_eur": share,
                    "report": {
                        "state": gateway_report_state(last_report_at, state, now_ms),
                        "last_report_at": last_report_at,
                        "ledger_epoch": ledger_epoch,
                        "last_seq": last_seq,
                        "period_start": period_start,
                        "period_end": period_end,
                        "policy_hashes": (json.loads(policy_hashes)
                                          if policy_hashes is not None else None),
                        "sized_ready": bool(sized_ready),
                    },
                })
                # Card R1-3d: the evidence block, from the stored version-4 reports and the adoption chain (never from the legacy
                # columns above). Absent for a gateway with no version-4 report, like `allocated_above_int64`.
                evidence_block = evidence_view.project(conn, gateway_id, now_ms)
                if evidence_block is not None:
                    gateways[-1]["evidence"] = evidence_block
            result = {
                "schema_version": SCHEMA_VERSION,
                "accounts": accounts,
                "machines": machines,
                "gateways": gateways,
                "billing": _project_billing(conn, now_ms),
            }
            conn.execute("COMMIT")
            return result
        except BaseException:
            rollback_quietly(conn)
            raise
    except sqlite3.Error as exc:
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from exc
    finally:
        conn.close()
