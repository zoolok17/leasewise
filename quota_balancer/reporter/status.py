"""`reporter status`: what the reporter knows, per agent (DESIGN-7A.md 4.9).

Built only from the reporter's own state, so it holds no secret: no URL, no
identity, no message text. While `run` owns the state file nobody else can
read it, so `run` also leaves a copy of this report next to it
(`snapshot_path`); `status` shows that copy, marked with its source.
"""

import datetime
import json
import os

from . import capture, decisions, rounds
from . import state as state_mod

STATUS_VERSION = 2          # 2: card 7c-2 added the top-level `limit_events` object

# Said by `status` and the README, word for word (claude-agenttalk-lead approved it; DESIGN-7B 13.12):
# capture of limit events is best effort, and this is what closes the gap.
LIMIT_GAP = ("A park that starts and ends while the reporter is not running, or between two checks, is "
             "never reported. The seat still parks on its own, and the next refusal after the reporter is "
             "back is reported. This closes when agenttalk keeps a permanent \"parked\" record in its turn "
             "journal (a later agenttalk change, after the turn-journal pull request #300 is merged, "
             "together with Codex limit events). Until then, limit events are best effort.")

# Stored finals, reports and acquisitions are counted by their state.
_NOTE_401 = state_mod.NOTE_LAST_401
_NOTE_CREDENTIAL = state_mod.NOTE_CREDENTIAL_ACTIVE
MAX_LISTED_FAILURES = 50
_NOTE_JOURNAL = "journal_stopped"


def snapshot_path(state_path):
    return state_path + ".status.json"


def iso(ms):
    if ms is None:
        return None
    moment = datetime.datetime.fromtimestamp(ms / 1000,
                                             tz=datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (ms % 1000)


def last_ok_note(agent):
    return state_mod.last_ok_note(agent)


def last_401_note():
    return _NOTE_401


def credential_active_note():
    return _NOTE_CREDENTIAL


def _failures(state, agent):
    """Every permanently failed item (acquisition, report, final) with its closed
    code. A final also says whether the observation behind it was complete: a
    complete observation that was never delivered is a different thing from an
    incomplete one."""
    found = []
    for message_id, code in state.all(
            "SELECT message_id, last_error_code FROM acquisition WHERE "
            "agent=? AND state='failed_permanent' ORDER BY id", (agent,)):
        found.append({"kind": "acquisition", "message_id": message_id,
                      "code": decisions.closed_code(code) or decisions.UNKNOWN_CODE})
    for message_id, code in state.all(
            "SELECT message_id, last_error_code FROM report WHERE agent=? "
            "AND state='failed_permanent' ORDER BY id", (agent,)):
        found.append({"kind": "report", "message_id": message_id,
                      "code": decisions.closed_code(code) or decisions.UNKNOWN_CODE})
    for message_id, code, complete in state.all(
            "SELECT message_id, last_error_code, complete FROM final WHERE "
            "agent=? AND state='failed_permanent' ORDER BY id", (agent,)):
        found.append({"kind": "final", "message_id": message_id,
                      "code": decisions.closed_code(code) or decisions.UNKNOWN_CODE,
                      "observation_complete": bool(complete),
                      "delivered": False})
    return found


def journal_stopped_note():
    return _NOTE_JOURNAL


def _counts(state, sql, agent):
    return {row[0]: row[1] for row in state.all(sql, (agent,))}


def _streams(state, agent):
    streams, ordered = rounds._load_streams(state, agent)
    missing, gapped, torn = set(), 0, 0
    for stream in ordered:
        if not stream.segments and not stream.events:
            missing.add(stream.name)
        if stream.previous and stream.previous not in streams:
            missing.add(stream.previous)
        if any(g[2] in ("seq_gap", "dropped") for g in rounds._gaps(stream)):
            gapped += 1
        torn += sum(1 for seg in stream.segments if seg[3])
    dropped = 0
    for stream in ordered:
        seen = [e[2] for e in stream.events]
        if stream.closed:
            seen.append(stream.closed_dropped or 0)
        dropped += max(seen, default=0)
    return {"seen": len(ordered), "missing": len(missing),
            "with_gaps": gapped, "with_torn_tails": torn}, dropped


def _agent(state, agent, configured, now_ms, silent_after_ms):
    one = state.one
    streams, dropped = _streams(state, agent)
    reasons = _counts(state,
                      "SELECT reason, COUNT(*) FROM message_status WHERE "
                      "agent=? AND complete=0 GROUP BY reason", agent)
    row = one("SELECT COUNT(*), SUM(complete=1), SUM(disposition IS NULL),"
              " SUM(disposition='outcome_unknown') FROM message_status "
              "WHERE agent=?", (agent,))
    acq = _counts(state, "SELECT state, COUNT(*) FROM acquisition WHERE "
                  "agent=? GROUP BY state", agent)
    failed_codes = {}
    for code, count in _counts(
            state, "SELECT COALESCE(last_error_code, 'UNKNOWN_CODE'), COUNT(*)"
            " FROM acquisition WHERE agent=? AND state='failed_permanent' "
            "GROUP BY 1", agent).items():
        word = decisions.closed_code(code)
        failed_codes[word] = failed_codes.get(word, 0) + count
    failures = _failures(state, agent)
    reports = _counts(state, "SELECT state, COUNT(*) FROM report WHERE "
                      "agent=? GROUP BY state", agent)
    finals = _counts(state, "SELECT state, COUNT(*) FROM final WHERE agent=? "
                     "GROUP BY state", agent)
    last = one("SELECT MAX(at_ms), COUNT(*) FROM event WHERE agent=?",
               (agent,))
    stopped = one("SELECT failed_code FROM agent_state WHERE agent=?",
                  (agent,))
    ok = state.note(last_ok_note(agent))
    silent = configured and (last[0] is None
                             or now_ms - last[0] > silent_after_ms)
    return {
        "configured": configured,
        "silent": bool(silent),
        "stopped_by_configuration_fault":
            None if stopped is None
            else {"code": decisions.closed_code(stopped[0])},
        "permanent_failures_total": len(failures),
        "permanent_failures": failures[:MAX_LISTED_FAILURES],
        "streams": streams,
        "events_read": last[1],
        "last_event_at": iso(last[0]),
        "dropped_total": dropped,
        "damaged_lines": state.counter(agent, "damaged_lines"),
        "damaged_stream_records": state.counter(agent,
                                                "damaged_stream_records"),
        "messages": {
            "known": row[0], "complete": row[1] or 0,
            "incomplete": (row[0] or 0) - (row[1] or 0),
            "incomplete_reasons": reasons, "open": row[2] or 0,
            "outcome_unknown": row[3] or 0},
        "finals": {"pending": finals.get("pending", 0),
                   "sent": finals.get("sent", 0),
                   "failed": finals.get("failed_permanent", 0)},
        "late_observations": one(
            "SELECT COALESCE(SUM(count), 0) FROM late WHERE agent=?",
            (agent,))[0],
        "acquisitions": {"pending": acq.get("pending", 0),
                         "acquired": acq.get("acquired", 0),
                         "failed": acq.get("failed_permanent", 0),
                         "failed_codes": failed_codes},
        "reports": {"pending": reports.get("pending", 0),
                    "sent": reports.get("sent", 0),
                    "dropped": reports.get("dropped", 0),
                    "failed": reports.get("failed_permanent", 0),
                    "over_cap": state.counter(agent, "reports_over_cap")},
        "last_successful_call": iso(ok[1]) if ok else None,
    }


def _limit_events(state, limit_sources):
    """The `limit_events` object: closed words and counts only. Never a path, a file name, an agent
    name, a message id, a parsed value or the text of a fault. `sources` lists what the OPERATOR wrote
    in the account map (an OS-user label, the provider, an account alias): the one thing that shows
    which map is in use."""
    agent = capture.LIMIT_AGENT
    rows = {"pending": 0, "sent": 0, "failed_permanent": 0}
    for name, count in state.all("SELECT state, COUNT(*) FROM limit_event GROUP BY state"):
        rows[name] = count
    failed = {}
    for code, count in state.all("SELECT last_error_code, COUNT(*) FROM limit_event "
                                 "WHERE state='failed_permanent' GROUP BY last_error_code"):
        word = decisions.closed_code(code) or decisions.UNKNOWN_CODE
        failed[word] = failed.get(word, 0) + count
    scan = state.note("limit_last_scan")
    fault = state.note("limit_last_fault")
    ok = state.note(state_mod.last_ok_note(state_mod.LIMIT_AGENT))
    configured = bool(limit_sources)
    return {
        "configured": configured,
        "sources": sorted(({"os_user": s.os_user, "provider": s.provider, "account": s.account}
                           for s in limit_sources),
                          key=lambda s: (s["os_user"], s["provider"], s["account"])),
        "last_scan": None if scan is None else iso(scan[1]),
        "scan": {name: state.counter(agent, "scan:" + name) for name in capture.SKIP_COUNTS},
        "captured": state.counter(agent, "captured"),
        "rows": rows,
        "failed": failed,
        "capture_fault": {"count": state.counter(agent, "capture_fault"),
                          "last": None if fault is None else iso(fault[1])},
        "last_ok": None if ok is None else iso(ok[1]),
        "gap": LIMIT_GAP if configured else None,
    }


def build_status(state, configured, now_ms, silent_after_seconds, limit_sources=()):
    """The whole report as plain data. `configured` is the set of agent
    names that have a table in the configuration file; `limit_sources` is the
    account map of card 7c-2 (empty: limit events are off)."""
    agents = {row[0] for row in state.all(
        "SELECT agent FROM stream UNION SELECT agent FROM acquisition "
        "UNION SELECT agent FROM event")} | set(configured)
    report = {
        "status_version": STATUS_VERSION,
        "generated_at": iso(now_ms),
        "source": "state",
        "rounds_completed": state.round_number(),
        "agents": {name: _agent(state, name, name in configured, now_ms,
                                silent_after_seconds * 1000)
                   for name in sorted(agents)},
    }
    stopped = state.note(_NOTE_JOURNAL)
    report["journal_refused"] = None if stopped is None else {
        "reason": stopped[0], "since": iso(stopped[1]),
        "meaning": "the reporter reads no new journal input until this is "
                   "fixed (upgrade the reporter, or remove the journal)"}
    refused = state.note(_NOTE_401)
    report["last_401"] = None if refused is None else {
        "code": decisions.closed_code(refused[0]), "at": iso(refused[1]),
        "active": state.note(_NOTE_CREDENTIAL) is not None}
    report["limit_events"] = _limit_events(state, limit_sources)
    report["silent_configured_seats"] = sorted(
        n for n, a in report["agents"].items() if a["silent"])
    report["operator_actions"] = _actions(report)
    return report


def _actions(report):
    actions = []
    if report["journal_refused"]:
        actions.append("the journal is refused: see journal_refused")
    if report["last_401"] and report["last_401"]["active"]:
        actions.append("Leasewise refused the identity: check the identity "
                       "value in the configuration")
    agents = report["agents"].values()
    failed = sum(a["permanent_failures_total"] for a in agents)
    if failed:
        actions.append("%d item(s) failed for good and will not be retried "
                       "(see permanent_failures: acquisitions, reports and "
                       "finals, each with its code)" % failed)
    if any(a["messages"]["open"] or a["messages"]["incomplete"]
           or a["messages"]["outcome_unknown"]
           or a["acquisitions"]["pending"] for a in agents):
        actions.append("open or incomplete messages: after an outage run "
                       "`expire-sweep` on the Leasewise store (explicit, "
                       "never a timer)")
    if any(a["stopped_by_configuration_fault"] for a in agents):
        actions.append("an agent is stopped by a configuration fault: fix "
                       "it, then start the reporter again")
    limit = report["limit_events"]
    if limit["rows"]["failed_permanent"]:
        actions.append("%d limit event(s) failed for good and will not be retried (see limit_events: "
                       "failed, with each closed code)" % limit["rows"]["failed_permanent"])
    if limit["capture_fault"]["count"]:
        actions.append("limit event capture failed %d time(s) (last at %s): lease sending was not "
                       "affected" % (limit["capture_fault"]["count"], limit["capture_fault"]["last"]))
    return actions


def write_snapshot(path, report):
    """Atomic: a reader sees the old file or the new one, never half."""
    report = dict(report, source="snapshot of the running reporter")
    temp = path + ".tmp"
    with open(temp, "w", encoding="ascii") as handle:
        json.dump(report, handle, sort_keys=True)
    os.replace(temp, path)


def read_snapshot(path):
    try:
        with open(path, "r", encoding="ascii") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def render_text(report):
    """A short plain-words view of the same facts."""
    out = ["reporter status (%s, %s)" % (report["generated_at"],
                                         report["source"])]
    if report["journal_refused"]:
        out.append("!! THE JOURNAL IS REFUSED (%s): no new input is read"
                   % report["journal_refused"]["reason"])
    if report["last_401"]:
        last = report["last_401"]
        out.append("%s Leasewise refused our identity (%s) at %s%s"
                   % ("!!" if last["active"] else "(history)", last["code"],
                      last["at"], "" if last["active"]
                      else "; accepted again since"))
    for name, agent in report["agents"].items():
        m = agent["messages"]
        out.append("%s%s: %d events, %d messages (%d open, %d complete, "
                   "%d incomplete), finals sent %d, last call %s"
                   % (name, "" if agent["configured"] else " [unconfigured]",
                      agent["events_read"], m["known"], m["open"],
                      m["complete"], m["incomplete"], agent["finals"]["sent"],
                      agent["last_successful_call"] or "never"))
        if agent["stopped_by_configuration_fault"]:
            out.append("  !! stopped by a configuration fault: %s"
                       % agent["stopped_by_configuration_fault"]["code"])
        if agent["silent"]:
            out.append("  silent: no recent journal events")
        for item in agent["permanent_failures"]:
            extra = ""
            if item["kind"] == "final":
                extra = " (observation %s, NOT delivered)" % (
                    "complete" if item["observation_complete"]
                    else "incomplete")
            out.append("  !! failed for good: %s of message %s, %s%s"
                       % (item["kind"], item["message_id"], item["code"],
                          extra))
    if report["limit_events"]["configured"]:
        out.append("limit events (best effort): " + report["limit_events"]["gap"])
    for line in report["operator_actions"]:
        out.append("-> " + line)
    return "\n".join(out)
