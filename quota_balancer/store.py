"""SQLite store for the Card-1 inventory (accounts, machines, gateways).

Every connection runs with foreign_keys=ON; the database uses WAL (set by
the migration and re-asserted on open; WAL mode is persistent).
"""

import json
import os
import sqlite3
import uuid

from .errors import (
    CODE_INVALID_CONFIG,
    CODE_SCHEMA_UNSUPPORTED,
    CODE_STORE_ERROR,
    MSG_INVALID_CONFIG,
    MSG_SCHEMA_UNSUPPORTED,
    MSG_STORE_FAILED,
    MSG_STORE_NOT_FOUND,
    QuotaError,
)
from .migrate import SCHEMA_VERSION, migrate
from .validate import validate


def _open(path, must_exist):
    if must_exist and not os.path.isfile(path):
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_NOT_FOUND)
    try:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    except sqlite3.Error as exc:
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from exc


def _require_version(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise QuotaError(CODE_SCHEMA_UNSUPPORTED, MSG_SCHEMA_UNSUPPORTED)


def init_db(path):
    """Create the store at schema version 1; idempotent for a version-1 store."""
    conn = _open(path, must_exist=False)
    try:
        migrate(conn)
    except sqlite3.Error as exc:
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from exc
    finally:
        conn.close()


def import_config(path, file):
    """Validate a version-1 document and replace the inventory atomically.

    Validation happens before any write, so a rejected document leaves the
    store untouched. Within a single transaction the replacement reuses the
    stored UUID of every alias that survives; new aliases get new UUIDs.
    Future cards: an active-lease check will gate this replacement.
    """
    try:
        with open(file, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError, RecursionError):
        # Missing/unreadable file, malformed JSON, undecodable text, or
        # pathologically deep nesting: all are a bad document, not a store fault.
        raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)

    accounts, machines, gateways = validate(doc)

    conn = _open(path, must_exist=True)
    try:
        _require_version(conn)
        _replace(conn, accounts, machines, gateways)
    except sqlite3.Error as exc:
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from exc
    finally:
        conn.close()


def _replace(conn, accounts, machines, gateways):
    with conn:
        old_accounts = dict(conn.execute("SELECT alias, id FROM accounts"))
        old_machines = dict(conn.execute("SELECT alias, id FROM machines"))
        old_gateways = dict(conn.execute("SELECT alias, id FROM gateways"))

        conn.execute("DELETE FROM gateways")
        conn.execute("DELETE FROM machines")
        conn.execute("DELETE FROM accounts")

        account_ids = {}
        for alias, vendor, kind, budget in accounts:
            account_id = old_accounts.get(alias) or str(uuid.uuid4())
            account_ids[alias] = account_id
            conn.execute(
                "INSERT INTO accounts(id, alias, vendor, kind, budget_micro_eur) "
                "VALUES (?,?,?,?,?)",
                (account_id, alias, vendor, kind, budget),
            )
        machine_ids = {}
        for alias in machines:
            machine_id = old_machines.get(alias) or str(uuid.uuid4())
            machine_ids[alias] = machine_id
            conn.execute("INSERT INTO machines(id, alias) VALUES (?,?)", (machine_id, alias))
        for alias, account, machine, share, period in gateways:
            gateway_id = old_gateways.get(alias) or str(uuid.uuid4())
            conn.execute(
                "INSERT INTO gateways(id, alias, account_id, machine_id, share_micro_eur, period) "
                "VALUES (?,?,?,?,?,?)",
                (gateway_id, alias, account_ids[account], machine_ids[machine], share, period),
            )


def status(path):
    """Project the inventory to aliases only, sorted by alias."""
    conn = _open(path, must_exist=True)
    try:
        _require_version(conn)
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
        machines = [row[0] for row in conn.execute("SELECT alias FROM machines ORDER BY alias")]
        gateways = [
            {"alias": alias, "account": account, "machine": machine, "share_micro_eur": share}
            for alias, account, machine, share in conn.execute(
                "SELECT g.alias, a.alias, m.alias, g.share_micro_eur "
                "FROM gateways g "
                "JOIN accounts a ON a.id = g.account_id "
                "JOIN machines m ON m.id = g.machine_id "
                "ORDER BY g.alias"
            )
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "accounts": accounts,
            "machines": machines,
            "gateways": gateways,
        }
    except sqlite3.Error as exc:
        raise QuotaError(CODE_STORE_ERROR, MSG_STORE_FAILED) from exc
    finally:
        conn.close()
