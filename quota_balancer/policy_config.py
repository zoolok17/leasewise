"""Reconcile the seven policy tables to a validated version-2 document.

Runs inside config-import's BEGIN IMMEDIATE (store._upsert), between the
inventory upserts and the inventory deletions, so one failure rolls the
whole document back. Card 2/3 upsert discipline: a retained alias keeps its
UUID and only its config-owned columns are updated (never seats.last_seen_at
or any other operational column); a removed alias is deleted only when
nothing references it, otherwise INVALID_CONFIG and the caller rolls back.
Children are deleted before parents.
"""

import json
import uuid

from .errors import CODE_INVALID_CONFIG, MSG_INVALID_CONFIG, QuotaError
from .validate_config_v2 import PLATFORM_ID


def _refuse():
    raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)


def _new_id():
    return str(uuid.uuid4())


def _referenced(conn, sql, params):
    return conn.execute(sql, params).fetchone() is not None


def apply_policy(conn, pol, account_ids, machine_ids):
    """account_ids / machine_ids: alias -> id for the document's accounts
    and machines (stored UUIDs when retained, fresh ones when new; the
    inventory rows exist by now)."""
    project_ids = _upsert_projects(conn, pol["projects"])
    team_ids = _upsert_teams(conn, pol["teams"], project_ids)
    _upsert_project_accounts(conn, pol["project_accounts"], project_ids,
                             account_ids)
    seat_ids = _upsert_seats(conn, pol["seats"], team_ids, machine_ids)
    model_ids = _upsert_models(conn, pol["models"], account_ids)
    _upsert_profiles(conn, pol["profiles"])
    scope_ids = {"project": project_ids, "team": team_ids, "seat": seat_ids}
    _upsert_limits(conn, pol["limits"], scope_ids)

    # Removals, children before parents: limits, seats, teams,
    # project_accounts, profiles, models, projects.
    _delete_limits(conn, pol["limits"], scope_ids)
    _delete_seats(conn, seat_ids)
    _delete_teams(conn, team_ids)
    _delete_project_accounts(conn, pol["project_accounts"], project_ids,
                             account_ids)
    _delete_profiles(conn, pol["profiles"])
    _delete_models(conn, model_ids)
    _delete_projects(conn, project_ids)


# --- upserts ----------------------------------------------------------------

def _upsert_projects(conn, projects):
    old = dict(conn.execute("SELECT alias, id FROM projects"))
    ids = {}
    for alias in projects:
        ids[alias] = old.get(alias) or _new_id()
        if alias not in old:
            conn.execute("INSERT INTO projects(id, alias) VALUES(?,?)",
                         (ids[alias], alias))
    return ids


def _upsert_teams(conn, teams, project_ids):
    old = {(p, a): i for i, p, a in conn.execute(
        "SELECT id, project_id, alias FROM teams")}
    ids = {}
    for project, alias in teams:
        key = (project_ids[project], alias)
        ids[(project, alias)] = old.get(key) or _new_id()
        if key not in old:
            conn.execute(
                "INSERT INTO teams(id, project_id, alias) VALUES(?,?,?)",
                (ids[(project, alias)], key[0], alias))
    return ids


def _upsert_project_accounts(conn, pairs, project_ids, account_ids):
    old = set(conn.execute(
        "SELECT project_id, account_id FROM project_accounts"))
    for project, account in pairs:
        key = (project_ids[project], account_ids[account])
        if key not in old:
            conn.execute("INSERT INTO project_accounts(project_id, "
                         "account_id) VALUES(?,?)", key)


def _upsert_seats(conn, seats, team_ids, machine_ids):
    old = {(m, a): i for i, m, a in conn.execute(
        "SELECT id, machine_id, alias FROM seats")}
    ids = {}
    for seat in seats:
        key = (machine_ids[seat["machine"]], seat["alias"])
        team_id = team_ids[(seat["project"], seat["team"])]
        ids[(seat["machine"], seat["alias"])] = old.get(key) or _new_id()
        seat_id = ids[(seat["machine"], seat["alias"])]
        if key in old:
            # Config-owned columns only: last_seen_at is operational.
            conn.execute(
                "UPDATE seats SET team_id=?, role=?, enforcement=? "
                "WHERE id=?",
                (team_id, seat["role"], seat["enforcement"], seat_id))
        else:
            conn.execute(
                "INSERT INTO seats(id, team_id, machine_id, alias, role, "
                "enforcement) VALUES(?,?,?,?,?,?)",
                (seat_id, team_id, key[0], seat["alias"], seat["role"],
                 seat["enforcement"]))
    return ids


_MODEL_COLUMNS = ("context_ceiling", "output_ceiling", "tariff_input_rate",
                  "tariff_output_rate", "reserve_input_rate",
                  "reserve_output_rate", "reservation_mode", "policy_hash",
                  "effort_aliases")


def _upsert_models(conn, models, account_ids):
    old = {(a, al): i for i, a, al in conn.execute(
        "SELECT id, account_id, alias FROM models")}
    ids = {}
    for model in models:
        key = (account_ids[model["account"]], model["alias"])
        ids[key] = old.get(key) or _new_id()
        values = [model[c] for c in _MODEL_COLUMNS]
        if key in old:
            conn.execute(
                "UPDATE models SET " + ", ".join(
                    c + "=?" for c in _MODEL_COLUMNS) + " WHERE id=?",
                values + [ids[key]])
        else:
            conn.execute(
                "INSERT INTO models(id, account_id, alias, " + ", ".join(
                    _MODEL_COLUMNS) + ") VALUES(?,?,?" + ",?" * len(
                        _MODEL_COLUMNS) + ")",
                [ids[key], key[0], key[1]] + values)
    return ids


_PROFILE_COLUMNS = ("expected_calls", "growth_tokens", "margin_bps",
                    "max_calls", "output_cap", "per_call_output_cap",
                    "ttl_ms", "policy_revision")


def _upsert_profiles(conn, profiles):
    old = dict(conn.execute("SELECT task_class, id FROM profiles"))
    for profile in profiles:
        candidates = json.dumps(profile["candidates"])
        values = [profile[c] for c in _PROFILE_COLUMNS]
        if profile["task_class"] in old:
            conn.execute(
                "UPDATE profiles SET candidates=?, context_rules='{}', "
                "calibration='{}', " + ", ".join(
                    c + "=?" for c in _PROFILE_COLUMNS) + " WHERE id=?",
                [candidates] + values + [old[profile["task_class"]]])
        else:
            conn.execute(
                "INSERT INTO profiles(id, task_class, candidates, "
                "context_rules, calibration, " + ", ".join(
                    _PROFILE_COLUMNS) + ") VALUES(?,?,?,'{}','{}'" + ",?" * len(
                        _PROFILE_COLUMNS) + ")",
                [_new_id(), profile["task_class"], candidates] + values)


def _scope_id(lim, scope_ids):
    """The stored scope id of a limit's alias path."""
    path = lim["path"]
    if lim["scope"] == "platform":
        return PLATFORM_ID
    if lim["scope"] == "project":
        return scope_ids["project"][path["project"]]
    if lim["scope"] == "team":
        return scope_ids["team"][(path["project"], path["team"])]
    return scope_ids["seat"][(path["machine"], path["seat"])]


def _store_key(lim, scope_ids):
    return (lim["scope"], _scope_id(lim, scope_ids), lim["metric"],
            lim["period"], lim["window_key"] or "")


def _upsert_limits(conn, limits, scope_ids):
    old = {(s, si, m, p, w): i for i, s, si, m, p, w in conn.execute(
        "SELECT id, scope, scope_id, metric, period, "
        "COALESCE(window_key,'') FROM limits")}
    for lim in limits:
        key = _store_key(lim, scope_ids)
        if key in old:
            conn.execute("UPDATE limits SET soft=?, hard=? WHERE id=?",
                         (lim["soft"], lim["hard"], old[key]))
        else:
            conn.execute(
                "INSERT INTO limits(id, scope, scope_id, metric, period, "
                "window_key, soft, hard) VALUES(?,?,?,?,?,?,?,?)",
                (_new_id(), key[0], key[1], key[2], key[3],
                 lim["window_key"], lim["soft"], lim["hard"]))


# --- removals ---------------------------------------------------------------

def _delete_limits(conn, limits, scope_ids):
    keep = {_store_key(lim, scope_ids) for lim in limits}
    for lid, s, si, m, p, w in conn.execute(
            "SELECT id, scope, scope_id, metric, period, "
            "COALESCE(window_key,'') FROM limits").fetchall():
        if (s, si, m, p, w) in keep:
            continue
        # A limit that holds still point at is history a settle needs.
        if _referenced(conn, "SELECT 1 FROM holds WHERE limit_id=? LIMIT 1",
                       (lid,)):
            _refuse()
        conn.execute("DELETE FROM limits WHERE id=?", (lid,))


def _delete_seats(conn, seat_ids):
    keep = set(seat_ids.values())
    for (sid,) in conn.execute("SELECT id FROM seats").fetchall():
        if sid in keep:
            continue
        if _referenced(conn, "SELECT 1 FROM leases WHERE seat_id=? LIMIT 1",
                       (sid,)):
            _refuse()
        conn.execute("DELETE FROM seats WHERE id=?", (sid,))


def _delete_teams(conn, team_ids):
    keep = set(team_ids.values())
    for (tid,) in conn.execute("SELECT id FROM teams").fetchall():
        if tid not in keep:
            if _referenced(conn, "SELECT 1 FROM seats WHERE team_id=? "
                           "LIMIT 1", (tid,)):
                _refuse()
            conn.execute("DELETE FROM teams WHERE id=?", (tid,))


def _delete_project_accounts(conn, pairs, project_ids, account_ids):
    keep = {(project_ids[p], account_ids[a]) for p, a in pairs}
    for key in conn.execute(
            "SELECT project_id, account_id FROM project_accounts").fetchall():
        if tuple(key) not in keep:
            conn.execute("DELETE FROM project_accounts WHERE project_id=? "
                         "AND account_id=?", key)


def _delete_profiles(conn, profiles):
    keep = {p["task_class"] for p in profiles}
    for (task_class,) in conn.execute(
            "SELECT task_class FROM profiles").fetchall():
        if task_class not in keep:
            conn.execute("DELETE FROM profiles WHERE task_class=?",
                         (task_class,))


def _delete_models(conn, model_ids):
    keep = set(model_ids.values())
    for (mid,) in conn.execute("SELECT id FROM models").fetchall():
        if mid in keep:
            continue
        if (_referenced(conn, "SELECT 1 FROM leases WHERE model_id=? "
                        "LIMIT 1", (mid,))
                or _referenced(conn, "SELECT 1 FROM blocks WHERE model_id=? "
                               "LIMIT 1", (mid,))
                or _referenced(conn, "SELECT 1 FROM limit_events WHERE "
                               "model_id=? LIMIT 1", (mid,))):
            _refuse()
        conn.execute("DELETE FROM models WHERE id=?", (mid,))


def _delete_projects(conn, project_ids):
    keep = set(project_ids.values())
    for (pid,) in conn.execute("SELECT id FROM projects").fetchall():
        if pid in keep:
            continue
        if (_referenced(conn, "SELECT 1 FROM teams WHERE project_id=? "
                        "LIMIT 1", (pid,))
                or _referenced(conn, "SELECT 1 FROM project_accounts WHERE "
                               "project_id=? LIMIT 1", (pid,))):
            _refuse()
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
