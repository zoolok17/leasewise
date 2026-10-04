"""`reporter status`: what the reporter knows, per agent (DESIGN-7A.md 4.9).

Built only from the reporter's own state, so it holds no secret: no URL, no
identity, no message text. While `run` owns the state file nobody else can
read it, so `run` also leaves a copy of this report next to it
(`snapshot_path`); `status` shows that copy, marked with its source.
"""

import datetime
import json
import os

from . import capture, decisions, gwcycle, gwstate, receipts, rounds
from . import state as state_mod

STATUS_VERSION = 4          # 2: card 7c-2 added `limit_events`; 3: card L6a-1 added `gateway_receipts`; 4: card R1-READER step 3a added `money_reader`

# Said by `status` and the README, word for word (claude-agenttalk-lead approved it; DESIGN-7B 13.12):
# capture of limit events is best effort, and this is what closes the gap.
LIMIT_GAP = ("A park that starts and ends while the reporter is not running, or between two checks, is "
             "never reported. The seat still parks on its own, and the next refusal after the reporter is "
             "back is reported. This closes when agenttalk keeps a permanent \"parked\" record in its turn "
             "journal (a later agenttalk change, after the turn-journal pull request #300 is merged, "
             "together with Codex limit events). Until then, limit events are best effort.")

# Said in the text view when the report is the snapshot of a reporter older than status version 2
# (still running during an upgrade): it never claims that limit capture is off.
OLDER_REPORTER = ("A reporter from before this version is still running. Its status has no limit-event "
                  "section, so nothing is said here about limit events.")

# Said by `status` and the README (decision O2): the gateway records no cached input, so a receipt
# settles with 0 there. Zero is a gap in what is recorded, not a measurement.
CACHED_INPUT_NOTE = ("Cached input is reported as 0: the gateway records none, so 0 is not proof that "
                     "none was used.")

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
    # Journal rows of a message that was set aside because it is bound to a paid lease (`superseded`) are not counted.
    acq = _counts(state, "SELECT state, COUNT(*) FROM acquisition WHERE agent=? AND NOT EXISTS (SELECT 1 FROM"
                  " superseded x WHERE x.agent=acquisition.agent AND x.message_id=acquisition.message_id)"
                  " GROUP BY state", agent)
    failed_codes = {}
    for code, count in _counts(
            state, "SELECT COALESCE(last_error_code, 'UNKNOWN_CODE'), COUNT(*)"
            " FROM acquisition WHERE agent=? AND state='failed_permanent' AND NOT EXISTS (SELECT 1 FROM"
            " superseded x WHERE x.agent=acquisition.agent AND x.message_id=acquisition.message_id)"
            " GROUP BY 1", agent).items():
        word = decisions.closed_code(code)
        failed_codes[word] = failed_codes.get(word, 0) + count
    failures = _failures(state, agent)
    reports = _counts(state, "SELECT state, COUNT(*) FROM report WHERE agent=? AND NOT EXISTS (SELECT 1 FROM"
                      " superseded x WHERE x.agent=report.agent AND x.message_id=report.message_id)"
                      " GROUP BY state", agent)
    finals = _counts(state, "SELECT state, COUNT(*) FROM final WHERE agent=? AND NOT EXISTS (SELECT 1 FROM"
                     " superseded x WHERE x.agent=final.agent AND x.message_id=final.message_id)"
                     " GROUP BY state", agent)
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


QUARANTINE_ACTIONS = {
    "unknown_reference": "No admission record names this receipt's lease yet: check that the admission store is the "
                         "one this machine uses. It is sent by itself once its record appears.",
    "wrong_gateway": "The lease's record expects another gateway than the one this receipt came from: check which "
                     "gateway the receipt source is configured for. It is never sent.",
    "duplicate_reference": "A second receipt arrived for a lease and ledger that already has one: look at the "
                           "gateway. It is never sent.",
}


def _awaiting(state, source, now_ms):
    """Bindings of this source's gateway that no capture has answered yet: how many, the age of the oldest in
    seconds (since this reporter first copied it), and how many of them are past the lease's own expiry."""
    if source is None:
        return {"count": 0, "oldest_age_seconds": None}, 0
    count, oldest, overdue = state.one(
        "SELECT COUNT(*), MIN(copied_ms), COALESCE(SUM(expires_ms < ?), 0) FROM receipt_binding b"
        " WHERE b.gateway_id=? AND NOT EXISTS (SELECT 1 FROM receipt_capture c WHERE c.ref_sha256=b.ref_sha256)",
        (now_ms, source.gateway_id))
    age = None if oldest is None else max(0, (now_ms - oldest) // 1000)
    return {"count": count, "oldest_age_seconds": age}, overdue


def _generation_changes(state):
    """Per source, how many ledger generations were seen after the first, and when the latest appeared."""
    out = []
    for source_id, alias in state.all("SELECT id, alias FROM receipt_source ORDER BY id"):
        seen, latest = state.one("SELECT COUNT(*), (SELECT first_seen_ms FROM receipt_cursor WHERE source_id=?"
                                 " ORDER BY page_ord DESC LIMIT 1) FROM receipt_cursor WHERE source_id=?",
                                 (source_id, source_id))
        out.append({"gateway": alias, "count": max(0, seen - 1),
                    "latest_first_seen": iso(latest) if seen > 1 else None})
    return out


CONTAIN_WORDS = ("cannot_run", "cannot_contain")
NOT_FINISHING_WORDS = ("timeout", "leftover_process", "exit_code", "read_failed", "interrupted")
CONTAIN_ACTION = ("The receipt command could not be started inside its protective job: nothing was run. Check the command "
                  "path and the Windows rules for jobs.")
NOT_FINISHING_ACTION = "The receipt command is not finishing: it was stopped. Look at the command itself."
CLEANUP_ACTION = ("The last receipt command read ended with termination requested but not confirmed: its output was not "
                  "used. Check that nothing it started is still running.")
STORE_STATES = {"ok": "ok", "store_missing": "missing", "store_busy": "busy", "store_unreadable": "unreadable"}
STORE_ACTIONS = {
    "missing": "The admission store file is not there: check that the admission command writes to the file the "
               "configuration names. Receipts are kept and are read against it again each cycle.",
    "unreadable": "The admission store cannot be used (damaged, another version, or a row that does not match its "
                  "lease): nothing is copied from it. Check it before more receipts arrive.",
}
BODY_CAP_ACTION = ("Some messages used up their request bodies in the admission store (%d): `admit` holds them as "
                   "refused and nothing is deleted. Look at why their grants never arrived (see the admission "
                   "command's own state).")


def _admission_store(state):
    """The current state of the admission store as the reporter last saw it, and how often it failed before. Closed
    words and counts only: never a path and never an exception's text. `state` is None before the first read."""
    note = state.note("receipt_store_read")
    current = None if note is None else STORE_STATES.get(note[0], "unreadable")
    agent = state_mod.RECEIPT_AGENT
    failures = {word: state.counter(agent, "store:" + name) for name, word in STORE_STATES.items() if name != "ok"}
    cap = state.note(state_mod.BODY_CAP_NOTE)
    # None = unknown: never read, or the last read failed. A failed read is never shown as a fresh zero.
    return {"state": current, "failures": failures,
            "at_body_cap": int(cap[0]) if cap is not None and cap[0].isdigit() else None}


def _journal_suppression(state, gateway_agents):
    """What the one-authority rule did (DESIGN-6A 8.5), as counts: journal events skipped because the gateway admits
    the agent or the message is bound, messages whose queued journal rows were set aside, and those whose acquisition
    had already been sent."""
    def total(name):
        return state.one("SELECT COALESCE(SUM(value), 0) FROM counter WHERE name=?", (name,))[0]
    return {"gateway_agents": len(gateway_agents), "suppressed": total("suppressed"),
            "superseded": total("superseded"), "superseded_after_send": total("superseded_after_send")}


# The sentences `status` says for a halted money reader, one per halt word (fixed text: no value from a report or an answer).
MONEY_READER_ACTIONS = {
    gwstate.SEND_REFUSED: ("the money reader stopped sending after repeated definitive refusals: if the cause was on Leasewise's side and is "
                           "fixed, run `reporter resume --confirm` (the same saved report is sent again); if the report itself is wrong, "
                           "run `reporter reset --confirm`"),
    gwstate.CONTINUITY_CONFLICT: ("Leasewise holds different bytes for a report number this reader saved: the money reader has stopped for "
                                  "good until the operator runs `reporter reset --confirm`, and the new reader must then be adopted"),
    gwstate.STATE_UNAVAILABLE: ("the money reader's memory is damaged or inconsistent and it sends nothing: run `reporter reset --confirm` "
                                "(the old state is kept in the archive), then adopt the new reader"),
}
# Card R1-READER step 3b added the closed words of the cycle (`gwcycle.CYCLE_WORDS`): each counts the cycles that ended with it.
MONEY_READER_COUNTS = ("send_failed", "send_refused", "continuity_conflict", "reader_halted", "state_unavailable", "refused_answers",
                       "resumed", "resets") + gwcycle.CYCLE_WORDS


def _money_reader(state):
    """Numbers and closed words only (card R1-READER step 3a): whether a report is pending, how many definitive refusals in a row it has had, the
    halt word, and the counts of the closed outcomes. Nothing from a report or an answer. A stored halt that is not a closed word shows as
    `state_unavailable` (the damaged value itself is never shown). This part of `status` can never stop the rest of it: if reading the money
    reader's tables fails in any way, the block says `state_unavailable` with zeros and the other parts are built as usual."""
    try:
        pending = state.one("SELECT refusals FROM gwr_pending")
        identity = state.one("SELECT halt FROM gwr_identity WHERE id=1")
        return {"pending": 0 if pending is None else 1,
                "consecutive_refusals": pending[0] if pending is not None and type(pending[0]) is int else 0,
                "refusal_threshold": gwstate.REFUSAL_THRESHOLD,
                "halt": None if identity is None else gwstate.closed_halt(identity[0]),
                "counts": {name: state.counter(gwstate.READER_AGENT, name) for name in MONEY_READER_COUNTS}}
    except Exception:    # noqa: BLE001 - the lane boundary: this block only, and only a closed word comes out (no message, no value kept)
        return {"pending": 0, "consecutive_refusals": 0, "refusal_threshold": gwstate.REFUSAL_THRESHOLD,
                "halt": gwstate.STATE_UNAVAILABLE, "counts": {name: 0 for name in MONEY_READER_COUNTS}}


def _gateway_receipts(state, source, now_ms, gateway_agents=()):
    """The `gateway_receipts` object (card L6a-1): closed words and counts only. Never a path, a
    command, a lease id, a reference, a figure from a receipt or the text of a fault. `gateway` is the
    alias the OPERATOR wrote."""
    agent = state_mod.RECEIPT_AGENT
    rows = {"pending": 0, "sent": 0, "failed_permanent": 0, "quarantined": 0}
    for name, count in state.all("SELECT state, COUNT(*) FROM receipt_state GROUP BY state"):
        rows["pending" if name == "captured" else name] += count        # captured = waiting for its release
    failed = {}
    for code, count in state.all("SELECT last_error_code, COUNT(*) FROM receipt_state "
                                 "WHERE state='failed_permanent' GROUP BY last_error_code"):
        word = decisions.closed_code(code) or decisions.UNKNOWN_CODE
        failed[word] = failed.get(word, 0) + count
    quarantine = {name: 0 for name in state_mod.QUARANTINE_REASONS}
    for reason, count in state.all("SELECT reason, COUNT(*) FROM receipt_state WHERE state='quarantined'"
                                   " GROUP BY reason"):
        if reason in quarantine:
            quarantine[reason] = count
    cursor = state.one("SELECT c.last_seq FROM receipt_cursor c JOIN receipt_source s ON s.id=c.source_id"
                       " WHERE s.alias=? ORDER BY c.page_ord DESC LIMIT 1",
                       (source.gateway if source is not None else "",))
    read = state.note("receipt_last_read")
    reason = state.note("receipt_last_reason")
    cleanup = state.note("receipt_cleanup")
    ok = state.note(state_mod.last_ok_note(agent))
    configured = source is not None
    awaiting, overdue = _awaiting(state, source, now_ms)
    return {
        "configured": configured,
        "gateway": source.gateway if configured else None,
        "last_seq": cursor[0] if cursor else 0,
        "last_read": None if read is None else iso(read[1]),
        "last_read_result": None if read is None else read[0],
        "last_read_reason": None if reason is None else reason[0],
        "source_kind": None if source is None else ("command" if source.command is not None else "fixture"),
        "cleanup_incomplete": {"count": state.counter(agent, "cleanup_incomplete"),
                               "current": cleanup is not None and cleanup[0] == "unconfirmed"},
        "stored": state.counter(agent, "stored"),
        "rows": rows,
        "failed": failed,
        "quarantine": quarantine,
        "admission_store": _admission_store(state),
        "journal_suppression": _journal_suppression(state, gateway_agents),
        "quarantine_action": dict(QUARANTINE_ACTIONS),
        "generation_changes": _generation_changes(state),
        "awaiting_receipt": awaiting,
        "overdue": overdue,
        "read_later": {name: state.counter(agent, "read_later:" + name) for name in receipts.LATER_REASONS},
        "refused": {name: state.counter(agent, "refused:" + name) for name in receipts.REFUSED_REASONS},
        "read_fault": state.counter(agent, "read_fault"),
        "last_ok": None if ok is None else iso(ok[1]),
        "cached_input": CACHED_INPUT_NOTE if configured else None,
    }


def build_status(state, configured, now_ms, silent_after_seconds, limit_sources=(), gateway_receipts=None,
                 gateway_agents=()):
    """The whole report as plain data. `configured` is the set of agent
    names that have a table in the configuration file; `limit_sources` is the
    account map of card 7c-2 (empty: limit events are off); `gateway_receipts` is the receipt
    source of card L6a-1 (None: gateway receipts are off)."""
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
    report["gateway_receipts"] = _gateway_receipts(state, gateway_receipts, now_ms, gateway_agents)
    report["money_reader"] = _money_reader(state)
    report["silent_configured_seats"] = sorted(
        n for n, a in report["agents"].items() if a["silent"])
    report["operator_actions"] = _actions(report, gateway_agents)
    return report


def _actions(report, gateway_agents=()):
    actions = []
    halted = report["money_reader"]["halt"]
    if halted:
        actions.append(MONEY_READER_ACTIONS[halted])
    if report["journal_refused"]:
        actions.append("the journal is refused: see journal_refused")
    if report["last_401"] and report["last_401"]["active"]:
        actions.append("Leasewise refused the identity: check the identity "
                       "value in the configuration")
    agents = report["agents"].values()
    open_agents = [one for name, one in report["agents"].items() if name not in gateway_agents]
    failed = sum(a["permanent_failures_total"] for a in agents)
    if failed:
        actions.append("%d item(s) failed for good and will not be retried "
                       "(see permanent_failures: acquisitions, reports and "
                       "finals, each with its code)" % failed)
    if any(a["messages"]["open"] or a["messages"]["incomplete"] or a["messages"]["outcome_unknown"]
           or a["acquisitions"]["pending"] for a in open_agents):
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
    receipt = report["gateway_receipts"]
    if receipt["rows"]["failed_permanent"]:
        actions.append("%d gateway receipt(s) failed for good and will not be retried (see "
                       "gateway_receipts: failed, with each closed code)" % receipt["rows"]["failed_permanent"])
    for reason, count in receipt["quarantine"].items():
        if count:
            actions.append("%d gateway receipt(s) are quarantined and not sent (%s): %s"
                           % (count, reason, receipt["quarantine_action"][reason]))
    if receipt["overdue"]:
        actions.append("%d lease(s) are overdue: their expiry has passed and no gateway receipt has come (the lease "
                       "keeps what it holds, which is nothing for an advisory lease; see gateway_receipts: "
                       "awaiting_receipt)" % receipt["overdue"])
    if receipt["failed"].get("RECEIPT_CONFLICT"):
        actions.append("%d gateway receipt(s) were refused as a conflict: Leasewise has parked the account; look at "
                       "the parked account (its blocks) before anything else is sent"
                       % receipt["failed"]["RECEIPT_CONFLICT"])
    store = receipt["admission_store"]              # present whenever the store was read, with or without receipts
    sentence = STORE_ACTIONS.get(store["state"])
    if sentence:
        actions.append(sentence)
    if store["at_body_cap"]:
        actions.append(BODY_CAP_ACTION % store["at_body_cap"])
    word = receipt.get("last_read_reason") if receipt["last_read_result"] == receipts.READ_LATER else None
    if word in CONTAIN_WORDS:
        actions.append(CONTAIN_ACTION)
    elif word in NOT_FINISHING_WORDS:
        actions.append(NOT_FINISHING_ACTION)
    if receipt.get("cleanup_incomplete", {}).get("current"):
        actions.append(CLEANUP_ACTION)
    if receipt["last_read_result"] == receipts.REFUSED:
        actions.append("the gateway receipt source was read but refused: no receipt after the last "
                       "stored one is taken until it is fixed (see gateway_receipts: refused)")
    if receipt["read_fault"]:
        actions.append("reading gateway receipts failed %d time(s): sending was not affected"
                       % receipt["read_fault"])
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
    limit = report.get("limit_events")
    if limit is None:
        # A snapshot of a reporter from before status version 2: it has no limit-event section, and
        # that says nothing about whether capture is on. Say only that.
        out.append(OLDER_REPORTER)
    elif limit["configured"]:
        out.append("limit events (best effort): " + limit["gap"])
    receipt = report.get("gateway_receipts")       # a snapshot of a version 1 or 2 reporter has none
    if receipt is not None and receipt["configured"]:
        out.append("gateway receipts: last seq %d, pending %d, delivered %d, failed %d. %s"
                   % (receipt["last_seq"], receipt["rows"]["pending"], receipt["rows"]["sent"],
                      receipt["rows"]["failed_permanent"], receipt["cached_input"]))
    if receipt is not None and receipt["configured"]:
        # a snapshot of an older reporter has none of these keys: read them with defaults
        out.append("gateway receipt source: %s; last read: %s" % (receipt.get("source_kind") or "fixture",
                                                                  receipt.get("last_read_reason") or "not read yet"))
        cleanup = receipt.get("cleanup_incomplete") or {"count": 0}
        if cleanup["count"]:
            out.append("gateway receipt command: termination requested but not confirmed %d time(s)%s"
                       % (cleanup["count"], " (not confirmed now)" if cleanup.get("current") else ""))
    store = None if receipt is None else receipt.get("admission_store")      # a snapshot of the first build has none
    if store is not None and (receipt["configured"] or store["state"] is not None):
        out.append("admission store: %s%s" % (store["state"] or "not read yet",
                                              "" if store["at_body_cap"] is None
                                              else ", %d at the body cap" % store["at_body_cap"]))
    for line in report["operator_actions"]:
        out.append("-> " + line)
    return "\n".join(out)
