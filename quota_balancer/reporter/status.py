"""`reporter status`: what the reporter knows, per agent (DESIGN-7A.md 4.9).

Built only from the reporter's own state, so it holds no secret: no URL, no
identity, no message text. While `run` owns the state file nobody else can
read it, so `run` also leaves a copy of this report next to it
(`snapshot_path`); `status` shows that copy, marked with its source.
"""

import datetime
import json
import os

from . import rounds

STATUS_VERSION = 1

# Stored finals, reports and acquisitions are counted by their state.
_NOTE_401 = "last_401"
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
    return "last_ok:" + agent


def last_401_note():
    return _NOTE_401


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
    failed_codes = _counts(
        state, "SELECT COALESCE(last_error_code, 'UNKNOWN'), COUNT(*) FROM "
        "acquisition WHERE agent=? AND state='failed_permanent' GROUP BY 1",
        agent)
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
            None if stopped is None else {"code": stopped[0]},
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


def build_status(state, configured, now_ms, silent_after_seconds):
    """The whole report as plain data. `configured` is the set of agent
    names that have a table in the configuration file."""
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
        "code": refused[0], "at": iso(refused[1])}
    report["silent_configured_seats"] = sorted(
        n for n, a in report["agents"].items() if a["silent"])
    report["operator_actions"] = _actions(report)
    return report


def _actions(report):
    actions = []
    if report["journal_refused"]:
        actions.append("the journal is refused: see journal_refused")
    if report["last_401"]:
        actions.append("Leasewise refused the identity: check the identity "
                       "value in the configuration")
    agents = report["agents"].values()
    if any(a["messages"]["open"] or a["messages"]["incomplete"]
           or a["messages"]["outcome_unknown"]
           or a["acquisitions"]["pending"] for a in agents):
        actions.append("open or incomplete messages: after an outage run "
                       "`expire-sweep` on the Leasewise store (explicit, "
                       "never a timer)")
    if any(a["stopped_by_configuration_fault"] for a in agents):
        actions.append("an agent is stopped by a configuration fault: fix "
                       "it, then start the reporter again")
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
        out.append("!! Leasewise refused our identity (%s) at %s"
                   % (report["last_401"]["code"], report["last_401"]["at"]))
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
    for line in report["operator_actions"]:
        out.append("-> " + line)
    return "\n".join(out)
