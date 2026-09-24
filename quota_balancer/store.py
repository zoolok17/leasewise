"""SQLite store for the Card-1 inventory (accounts, machines, gateways).

Connections are autocommit (isolation_level=None); every transaction is
explicit. The import replacement takes the write lock (BEGIN IMMEDIATE)
before it reads the old alias->UUID maps, so a concurrent import cannot
slip between the read and the write. The status projection is a single
read transaction: the version check and the three SELECTs share one
snapshot. Read paths never switch journal mode and never write, so a
store whose version is rejected is left byte-identical.
"""

import json
import os
import sqlite3
import uuid

from .errors import (
    CODE_INVALID_CONFIG,
    CODE_STORE_ERROR,
    MSG_INVALID_CONFIG,
    MSG_STORE_FAILED,
    MSG_STORE_NOT_FOUND,
    QuotaError,
)
from .migrate import SCHEMA_VERSION, migrate, require_supported, rollback_quietly
from .validate import validate


def _connect(path, must_exist):
    """Open the store with foreign_keys=ON. Never raises sqlite3.Error."""
    if must_exist and not os.path.isfile(path):
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_NOT_FOUND)
    try:
        conn = sqlite3.connect(path, isolation_level=None)
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
    """Create the store at schema version 1; idempotent for a version-1 store.

    The migration itself is transactional (see migrate.migrate). A store
    whose user_version is not 0 or 1 is rejected before anything is
    written or switched.
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
    """Validate a version-1 document and replace the inventory atomically.

    Validation happens before any write, so a rejected document leaves the
    store untouched. The replacement runs under the write lock: the old
    alias->UUID maps are read inside the same transaction, every retained
    alias reuses its stored UUID, and new aliases get new UUIDs. Future
    cards: an active-lease check will gate this replacement.
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
            _replace(conn, accounts, machines, gateways)
            conn.execute("COMMIT")
        except BaseException:
            rollback_quietly(conn)
            raise
    except sqlite3.Error as exc:
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from exc
    finally:
        conn.close()


def _replace(conn, accounts, machines, gateways):
    """Replace all rows. Called inside the import's BEGIN IMMEDIATE.

    Expects the normalized tuples from validate.validate().
    """
    # Read the old maps under the write lock: a concurrent import cannot
    # commit new UUIDs between this read and the DELETEs below.
    old_accounts = dict(conn.execute("SELECT alias, id FROM accounts"))
    old_machines = dict(conn.execute("SELECT alias, id FROM machines"))
    old_gateways = dict(conn.execute("SELECT alias, id FROM gateways"))

    conn.execute("DELETE FROM gateways")
    conn.execute("DELETE FROM machines")
    conn.execute("DELETE FROM accounts")

    # New id per alias: the stored UUID when the alias is retained, a new
    # UUID otherwise. Gateways reference the ids, not the aliases.
    account_ids = {}
    for alias, vendor, kind, budget in accounts:
        account_id = old_accounts.get(alias) or str(uuid.uuid4())
        account_ids[alias] = account_id
        conn.execute(
            "INSERT INTO accounts(id, alias, vendor, kind, budget_micro_eur)"
            " VALUES(?,?,?,?,?)",
            (account_id, alias, vendor, kind, budget),
        )

    machine_ids = {}
    for alias in machines:
        machine_id = old_machines.get(alias) or str(uuid.uuid4())
        machine_ids[alias] = machine_id
        conn.execute(
            "INSERT INTO machines(id, alias) VALUES(?,?)",
            (machine_id, alias),
        )

    for alias, account, machine, share, period in gateways:
        conn.execute(
            "INSERT INTO gateways(id, alias, account_id, machine_id,"
            " share_micro_eur, period) VALUES(?,?,?,?,?,?)",
            (
                old_gateways.get(alias) or str(uuid.uuid4()),
                alias,
                account_ids[account],
                machine_ids[machine],
                share,
                period,
            ),
        )


def status(path):
    """Project the inventory to aliases only, sorted by alias.

    One read transaction: the version check and the three projections
    share a single snapshot, so the output never mixes two inventories.
    """
    conn = _connect(path, must_exist=True)
    try:
        conn.execute("BEGIN")
        try:
            require_supported(conn)
            accounts = [
                {
                    "alias": alias,
                    "kind": kind,
                    "budget_micro_eur": budget,
                    "allocated_micro_eur": allocated,
                }
                for alias, kind, budget, allocated in conn.execute(
                    "SELECT a.alias, a.kind, a.budget_micro_eur, "
                    "COALESCE((SELECT SUM(g.share_micro_eur) FROM gateways g "
                    "WHERE g.account_id = a.id), 0) "
                    "FROM accounts a ORDER BY a.alias"
                )
            ]
            machines = [
                row[0]
                for row in conn.execute("SELECT alias FROM machines ORDER BY alias")
            ]
            gateways = [
                {
                    "alias": alias,
                    "account": account,
                    "machine": machine,
                    "share_micro_eur": share,
                }
                for alias, account, machine, share in conn.execute(
                    "SELECT g.alias, a.alias, m.alias, g.share_micro_eur "
                    "FROM gateways g "
                    "JOIN accounts a ON a.id = g.account_id "
                    "JOIN machines m ON m.id = g.machine_id "
                    "ORDER BY g.alias"
                )
            ]
            result = {
                "schema_version": SCHEMA_VERSION,
                "accounts": accounts,
                "machines": machines,
                "gateways": gateways,
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
