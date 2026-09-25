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
import sqlite3
import time
import uuid

from .errors import (
    CODE_IDEMPOTENCY_CONFLICT,
    CODE_INVALID_CONFIG,
    CODE_STORE_ERROR,
    MSG_IDEMPOTENCY_CONFLICT,
    MSG_INVALID_CONFIG,
    MSG_STORE_FAILED,
    MSG_STORE_NOT_FOUND,
    QuotaError,
)
from .migrate import SCHEMA_VERSION, migrate, require_supported, rollback_quietly
from .reduce import (
    GATEWAY_STATE_OK,
    GATEWAY_STATE_RECONCILE,
    billing_freshness,
    gateway_report_state,
    reduce_billing_total,
    reduce_gateway_report,
    reduce_window,
    window_freshness,
)
from .validate import validate
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

    accounts, machines, gateways = validate(doc)

    conn = _connect(path, must_exist=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            require_supported(conn)
            _upsert(conn, accounts, machines, gateways)
            conn.execute("COMMIT")
        except BaseException:
            rollback_quietly(conn)
            raise
    except sqlite3.Error as exc:
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from exc
    finally:
        conn.close()


def _upsert(conn, accounts, machines, gateways):
    """Reconcile the inventory to the document. Inside the import's BEGIN
    IMMEDIATE. Expects the normalized tuples from validate.validate().

    Retained aliases are updated in place (config-owned columns only, so the
    card-2 state columns survive); new aliases are inserted; removed aliases
    are deleted only when unreferenced (children before parents).
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

    # Removed aliases, children before parents. validate() guarantees no
    # retained/new gateway references a removed account or machine, so the
    # only live references to a removed alias are readings, windows, and
    # the card-3 policy tables (seats.machine_id, models.account_id,
    # project_accounts.account_id - empty in this card, guarded from the
    # moment the tables exist).
    for alias in set(old_gateways) - {g[0] for g in gateways}:
        conn.execute("DELETE FROM gateways WHERE id=?", (old_gateways[alias],))
    for alias in set(old_machines) - set(machines):
        if (_references(conn, "readings", "machine_id", old_machines[alias])
                or _references(conn, "seats", "machine_id",
                               old_machines[alias])):
            raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
        conn.execute("DELETE FROM machines WHERE id=?", (old_machines[alias],))
    for alias in set(old_accounts) - {a[0] for a in accounts}:
        if (_references(conn, "readings", "account_id", old_accounts[alias])
                or _references(conn, "windows", "account_id",
                               old_accounts[alias])
                or _references(conn, "models", "account_id",
                               old_accounts[alias])
                or _references(conn, "project_accounts", "account_id",
                               old_accounts[alias])):
            raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
        conn.execute("DELETE FROM accounts WHERE id=?", (old_accounts[alias],))


def _references(conn, table, column, entity_id):
    """True when any row in `table` points at `entity_id` via `column`."""
    row = conn.execute(
        "SELECT 1 FROM " + table + " WHERE " + column + " = ? LIMIT 1",
        (entity_id,),
    ).fetchone()
    return row is not None


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
            _apply_billing_policies(conn, policies)
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
    for (account_id, total) in conn.execute(
            "SELECT account_id, SUM(share_micro_eur) FROM gateways "
            "GROUP BY account_id"):
        shares[account_id] = total

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
            policy["cap_basis"], policy["provider_basis"],
            policy["ledger_basis"], policy["vat_bps"])

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


def reading_import(path, file):
    """Validate a card-2 reading document and apply it atomically.

    Validation (shape/types, no future observations) happens before any
    write. The import runs under the write lock (BEGIN IMMEDIATE): each
    reading is idempotent by event_id (a replay - same id, same content - is
    a no-op; a collision - same id, different content - aborts the whole
    batch with IDEMPOTENCY_CONFLICT), references are resolved, and the
    reading is recorded and reduced into windows / the gateway report. All
    of it commits as one transaction, so a partial import never commits.
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
            require_supported(conn)
            machine_ids = dict(conn.execute("SELECT alias, id FROM machines"))
            account_ids = dict(conn.execute("SELECT alias, id FROM accounts"))
            gateway_ids = dict(conn.execute("SELECT alias, id FROM gateways"))
            for reading in readings:
                _apply_reading(conn, reading, machine_ids, account_ids,
                               gateway_ids, now_ms)
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
        _reduce_gateway_snapshot(conn, reading, gateway_ids)
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

    The row is keyed by (account_id, scope_revision, period_start); the
    reduction is by SOURCE time (the pure reduce_billing_total): newer
    observations set latest, an equal-time unequal total latches a
    conflict, and an older higher total raises the high-water without
    refreshing the latest time. The currency and the money basis live in
    the policy, not the row.
    """
    account_kind = conn.execute(
        "SELECT kind FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()[0]
    if account_kind != "metered":
        raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)

    policy = conn.execute(
        "SELECT reporter_machine_id, scope_alias, scope_revision "
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

    period_start = reading["period_start"]
    period_end = reading["period_end"]
    scope_revision = reading["scope_revision"]
    existing = conn.execute(
        "SELECT latest_micro_eur, observed_at, latest_event_id, "
        "high_water_micro_eur, high_water_event_id, conflict "
        "FROM billing_totals WHERE account_id = ? AND scope_revision = ? "
        "AND period_start = ?",
        (account_id, scope_revision, period_start),
    ).fetchone()
    updated = reduce_billing_total(existing, reading["total_micro_eur"],
                                   reading["observed_at"],
                                   reading["event_id"])
    if updated is None:
        # The reading is recorded but changes nothing (a replay or an
        # older total that does not beat the high-water).
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


def _reduce_gateway_snapshot(conn, reading, gateway_ids):
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


def reconcile(path, gateway):
    """Clear a gateway's reconciliation_required state (operator-only).

    A ledger-epoch or policy-hash change sets reconciliation_required, which
    is sticky: it is NOT cleared by the next matching report. Only this
    explicit operator verb clears it (back to ok), adopting the current
    report as the trusted baseline. Unknown gateway -> INVALID_CONFIG.
    """
    conn = _connect(path, must_exist=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            require_supported(conn)
            row = conn.execute(
                "SELECT id FROM gateways WHERE alias = ?", (gateway,)
            ).fetchone()
            if row is None:
                raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
            conn.execute(
                "UPDATE gateways SET state=? WHERE id=?",
                (GATEWAY_STATE_OK, row[0]),
            )
            conn.execute("COMMIT")
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
            for account_id, alias, kind, budget, allocated, coverage in conn.execute(
                "SELECT a.id, a.alias, a.kind, a.budget_micro_eur, "
                "COALESCE((SELECT SUM(g.share_micro_eur) FROM gateways g "
                "WHERE g.account_id = a.id), 0), a.coverage_complete "
                "FROM accounts a ORDER BY a.alias"
            ):
                accounts.append({
                    "alias": alias,
                    "kind": kind,
                    "budget_micro_eur": budget,
                    "allocated_micro_eur": allocated,
                    "coverage_complete": bool(coverage),
                    "windows": _project_windows(conn, account_id, now_ms),
                })
            machines = [
                row[0]
                for row in conn.execute("SELECT alias FROM machines ORDER BY alias")
            ]
            gateways = []
            for (alias, account, machine, share, last_report_at, ledger_epoch,
                 last_seq, period_start, period_end, policy_hashes, sized_ready,
                 state) in conn.execute(
                    "SELECT g.alias, a.alias, m.alias, g.share_micro_eur, "
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
