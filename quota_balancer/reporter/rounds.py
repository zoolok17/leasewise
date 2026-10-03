"""Snapshot rounds, message assembly and completeness.

A round lists every segment that exists at one instant, reads each up to
its length then (complete lines only), stores what it found, moves the
cursors, and only then decides finals. Cross-stream order is never assumed:
a message is whatever events carry its id, wherever they were found
(DESIGN-7A.md 4.3 and Part 12 D3-D5).

"Complete" means "no loss the journal can see". It is not a guarantee: a
process whose journal never started, or a clock set back, can still hide
a loss (Part 12, the honest limit).
"""

import json

from . import events as ev
from . import mapper

INF = float("inf")
# D3c: `message_at` is the SENDING machine's clock, which can disagree with
# the machine that writes the journal. A message whose time is earlier than
# the successor's start plus this allowance may have reached the dead stream.
CLOCK_SKEW_ALLOWANCE_SECONDS = 600
MAX_REPORTS_PER_MESSAGE = 50
_MARGIN_MS = CLOCK_SKEW_ALLOWANCE_SECONDS * 1000

REASON_OK = "ok"
# In the order the primary reason is chosen.
REASONS = ("unknown_arrival", "clock_inconsistent", "missing_stream",
           "unclean_predecessor", "torn_tail", "seq_gap", "dropped",
           "unmatched_dispatch")


class RoundResult:
    def __init__(self, round_no):
        self.round_no = round_no
        self.new_events = 0
        self.finals = []          # (agent, message_id) created this round


# --- reading and storing --------------------------------------------------

def run_round(state, root, configs, now_ms, fault=None):
    """One snapshot round. `configs` maps agent -> mapper.AgentConfig (an
    agent without one is read and counted, never sent). `fault(stage)` is
    a test seam called right after the data, the cursors and the finals
    are stored. An unknown schema version raises before anything is
    written."""
    # A crash between storing a round's data and deciding its finals leaves
    # that round unfinished. Finish it from what it stored BEFORE any new
    # input is read, so a final never depends on whether the reporter crashed.
    result = RoundResult(state.round_number() + 1)
    _finish_unfinished_round(state, configs, result)
    snapshot = ev.take_snapshot(root)
    reads = []
    for agent in sorted(snapshot):
        for ref in sorted(snapshot[agent].segments,
                          key=lambda r: (r.stream, r.segment)):
            reads.append((ref, ev.read_segment(
                ref, state.cursor(ref.stream, ref.segment))))
    round_no = state.round_number() + 1

    with state.txn() as conn:
        for agent in sorted(snapshot):
            for record in snapshot[agent].records:
                _store_record(conn, agent, record)
            conn.execute(
                "INSERT INTO counter(agent, name, value) VALUES(?,?,?) "
                "ON CONFLICT(agent, name) DO UPDATE SET value=excluded.value",
                (agent, "damaged_stream_records",
                 snapshot[agent].damaged_records))
        for ref, read in reads:
            _store_read(state, ref, read, round_no, configs, now_ms, result)
        torn = _confirm_torn(state, snapshot, reads)
        conn.execute("INSERT INTO meta(key, value) VALUES('round', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (round_no,))
    if fault:
        fault("data")

    with state.txn() as conn:
        for ref, read in reads:
            offset = ref.size if (ref.stream, ref.segment) in torn \
                else read.end_offset
            conn.execute(
                "INSERT INTO cursor(stream, segment, offset) VALUES(?,?,?) "
                "ON CONFLICT(stream, segment) DO UPDATE SET "
                "offset=excluded.offset", (ref.stream, ref.segment, offset))
    if fault:
        fault("cursor")

    with state.txn():
        _evaluate(state, sorted(snapshot), configs, round_no, result)
        _mark_evaluated(state, round_no)
    if fault:
        fault("final")
    return result


def _mark_evaluated(state, round_no):
    state.conn.execute(
        "INSERT INTO meta(key, value) VALUES('evaluated', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (round_no,))


def _finish_unfinished_round(state, configs, result):
    stored = state.round_number()
    done = state.one("SELECT value FROM meta WHERE key='evaluated'")
    if stored == 0 or (done is not None and done[0] >= stored):
        return
    agents = [row[0] for row in state.all(
        "SELECT DISTINCT agent FROM stream ORDER BY agent")]
    with state.txn():
        _evaluate(state, agents, configs, stored, result)
        _mark_evaluated(state, stored)


def _store_record(conn, agent, record):
    conn.execute(
        "INSERT INTO stream(stream, agent, previous_stream, started_ms,"
        " in_record) VALUES(?,?,?,?,1) ON CONFLICT(stream) DO UPDATE SET"
        " previous_stream=excluded.previous_stream,"
        " started_ms=excluded.started_ms, in_record=1",
        (record["stream"], agent, record["previous_stream"],
         record["started_ms"]))


def _stored_stream(conn, ref, header_ms, previous):
    conn.execute(
        "INSERT OR IGNORE INTO stream(stream, agent, previous_stream,"
        " started_ms, in_record) VALUES(?,?,?,?,0)",
        (ref.stream, ref.agent, previous, header_ms))


def _final_exists(conn, agent, message_id):
    return conn.execute("SELECT 1 FROM final WHERE agent=? AND message_id=?",
                        (agent, message_id)).fetchone() is not None


def _late(state, agent, message_id):
    state.conn.execute(
        "INSERT INTO late(agent, message_id, count) VALUES(?,?,1) "
        "ON CONFLICT(agent, message_id) DO UPDATE SET count=count+1",
        (agent, message_id))


def _ensure_acquisition(state, config, message_id, now_ms):
    if config is None:
        return
    turn = mapper.turn_uuid(config.seat_id, message_id)
    body = json.dumps(mapper.acquisition_body(config, message_id, now_ms),
                      sort_keys=True)
    state.conn.execute(
        "INSERT OR IGNORE INTO acquisition(agent, message_id, turn_uuid,"
        " request_id, body_original, body_current) VALUES(?,?,?,?,?,?)",
        (config.agent, message_id, turn, mapper.request_id(turn), body, body))


def _store_read(state, ref, read, round_no, configs, now_ms, result):
    conn = state.conn
    if read.damaged:
        state.bump(ref.agent, "damaged_lines", read.damaged)
    for item in read.events:
        kind = item["kind"]
        if kind == ev.KIND_STREAM_STARTED:
            if item["segment"] != ref.segment:
                state.bump(ref.agent, "damaged_lines")
                continue
            _stored_stream(conn, ref, item["at_ms"], item["previous_stream"])
            conn.execute(
                "INSERT OR IGNORE INTO segment(stream, segment, after_seq,"
                " header_ms) VALUES(?,?,?,?)",
                (ref.stream, ref.segment, item["after_seq"], item["at_ms"]))
            continue
        if kind == ev.KIND_STREAM_CLOSED:
            _stored_stream(conn, ref, item["at_ms"], None)
            conn.execute(
                "UPDATE stream SET closed=1, closed_ms=?, last_seq=?,"
                " closed_dropped=? WHERE stream=?",
                (item["at_ms"], item["last_seq"], item["dropped_total"],
                 ref.stream))
            continue
        _stored_stream(conn, ref, item["at_ms"], None)
        inserted = conn.execute(
            "INSERT OR IGNORE INTO event(stream, seq, agent, kind, at_ms,"
            " dropped_total) VALUES(?,?,?,?,?,?)",
            (ref.stream, item["seq"], ref.agent, kind, item["at_ms"],
             item["dropped_total"])).rowcount
        if not inserted:
            continue
        result.new_events += 1
        _store_event(state, ref, item, round_no, configs.get(ref.agent),
                     now_ms)


def _store_event(state, ref, item, round_no, config, now_ms):
    conn = state.conn
    agent, message_id = ref.agent, item["message_id"]
    kind = item["kind"]
    finalized = _final_exists(conn, agent, message_id)
    if finalized:
        _late(state, agent, message_id)
    if kind == ev.KIND_MESSAGE_DISPOSED:
        conn.execute(
            "INSERT OR IGNORE INTO disposal(agent, message_id, stream, seq,"
            " at_ms, disposition, message_at, round_no) VALUES(?,?,?,?,?,?,?,?)",
            (agent, message_id, ref.stream, item["seq"], item["at_ms"],
             item["disposition"], item["message_at"], round_no))
        return
    if kind == ev.KIND_DISPATCH_STARTED:
        conn.execute(
            "INSERT INTO dispatch(agent, message_id, turn_id, stream, started,"
            " start_ms, message_at, has_message_at) VALUES(?,?,?,?,1,?,?,1) "
            "ON CONFLICT(agent, message_id, turn_id) DO UPDATE SET started=1,"
            " start_ms=excluded.start_ms, message_at=excluded.message_at,"
            " has_message_at=1",
            (agent, message_id, item["turn_id"], ref.stream, item["at_ms"],
             item["message_at"]))
    else:
        usage = None if item["usage"] is None else json.dumps(
            item["usage"], sort_keys=True)
        conn.execute(
            "INSERT INTO dispatch(agent, message_id, turn_id, stream, ended,"
            " end_ms, launched, outcome, usage_json) VALUES(?,?,?,?,1,?,?,?,?) "
            "ON CONFLICT(agent, message_id, turn_id) DO UPDATE SET ended=1,"
            " end_ms=excluded.end_ms, launched=excluded.launched,"
            " outcome=excluded.outcome, usage_json=excluded.usage_json",
            (agent, message_id, item["turn_id"], ref.stream, item["at_ms"],
             1 if item["launched"] else 0, item["outcome"], usage))
    if finalized or config is None:
        return
    _ensure_acquisition(state, config, message_id, now_ms)
    if kind == ev.KIND_DISPATCH_ENDED and item["launched"]:
        turn = mapper.turn_uuid(config.seat_id, message_id)
        count = conn.execute(
            "SELECT COUNT(*) FROM report WHERE agent=? AND message_id=?",
            (agent, message_id)).fetchone()[0]
        if count >= MAX_REPORTS_PER_MESSAGE:
            state.bump(agent, "reports_over_cap")
            return
        event_id = mapper.dispatch_event_id(turn, item["turn_id"])
        body = json.dumps(mapper.dispatch_report_body(
            event_id, item["outcome"], item["usage"]), sort_keys=True)
        # An agent stopped by a configuration fault sends no reports: a new
        # one is dropped and counted at once (finals are never dropped).
        stopped = conn.execute(
            "SELECT 1 FROM agent_state WHERE agent=? AND failed_code IS NOT "
            "NULL", (agent,)).fetchone() is not None
        added = conn.execute(
            "INSERT OR IGNORE INTO report(event_id, agent, message_id,"
            " turn_id, body, state) VALUES(?,?,?,?,?,?)",
            (event_id, agent, message_id, item["turn_id"], body,
             "dropped" if stopped else "pending")).rowcount
        if stopped and added:
            state.bump(agent, "reports_dropped")


def _confirm_torn(state, snapshot, reads):
    """A torn tail is only a fact once the writer cannot still be writing
    it: a later segment of the same stream exists, the stream ended
    (closed), or a later stream started. Returns {(stream, segment)}."""
    conn = state.conn
    confirmed = set()
    last_segment = {}
    for agent in snapshot:
        for ref in snapshot[agent].segments:
            last_segment[ref.stream] = max(last_segment.get(ref.stream, 0),
                                           ref.segment)
    for ref, read in reads:
        if not read.torn or read.missing:
            continue
        stream = conn.execute(
            "SELECT started_ms, closed FROM stream WHERE stream=?",
            (ref.stream,)).fetchone()
        if stream is None:
            continue                # not a stream we know: nothing to anchor
        later_stream = conn.execute(
            "SELECT 1 FROM stream WHERE agent=? AND started_ms>? LIMIT 1",
            (ref.agent, stream[0])).fetchone() is not None
        if ref.segment < last_segment[ref.stream] or later_stream \
                or stream[1]:
            following = conn.execute(
                "SELECT after_seq FROM segment WHERE stream=? AND segment>? "
                "ORDER BY segment LIMIT 1", (ref.stream, ref.segment)
            ).fetchone()
            # The last event written before the tear: those up to the next
            # segment's `after_seq`, or all of them when nothing follows.
            last = conn.execute(
                "SELECT MAX(at_ms) FROM event WHERE stream=? AND seq<=?",
                (ref.stream, following[0] if following and following[0]
                 is not None else 2 ** 62)).fetchone()[0]
            header = conn.execute(
                "SELECT header_ms FROM segment WHERE stream=? AND segment=?",
                (ref.stream, ref.segment)).fetchone()
            when = max(t for t in (last, header[0] if header else None,
                                   stream[0]) if t is not None)
            conn.execute(
                "INSERT OR IGNORE INTO segment(stream, segment) VALUES(?,?)",
                (ref.stream, ref.segment))
            conn.execute(
                "UPDATE segment SET torn=1, torn_ms=? WHERE stream=? AND "
                "segment=?", (when, ref.stream, ref.segment))
            confirmed.add((ref.stream, ref.segment))
    return confirmed


# --- facts and completeness -------------------------------------------------

class _Stream:
    def __init__(self, row):
        (self.name, self.previous, self.start, self.in_record, self.closed,
         self.closed_ms, self.last_seq, self.closed_dropped) = row
        self.segments = []
        self.events = []
        self.succ_start = INF


def _load_streams(state, agent):
    streams = {}
    for row in state.all(
            "SELECT stream, previous_stream, started_ms, in_record, closed,"
            " closed_ms, last_seq, closed_dropped FROM stream WHERE agent=?",
            (agent,)):
        streams[row[0]] = _Stream(row)
    for stream, segment, after_seq, header_ms, torn, torn_ms in state.all(
            "SELECT s.stream, s.segment, s.after_seq, s.header_ms, s.torn,"
            " s.torn_ms FROM segment s JOIN stream t ON t.stream=s.stream "
            "WHERE t.agent=? ORDER BY s.stream, s.segment", (agent,)):
        streams[stream].segments.append((segment, after_seq, header_ms, torn,
                                         torn_ms))
    for stream, seq, at_ms, dropped in state.all(
            "SELECT e.stream, e.seq, e.at_ms, e.dropped_total FROM event e "
            "JOIN stream t ON t.stream=e.stream WHERE t.agent=? "
            "ORDER BY e.stream, e.seq", (agent,)):
        streams[stream].events.append((seq, at_ms, dropped))
    ordered = sorted(streams.values(), key=lambda s: (s.start, s.name))
    for stream in ordered:
        chained = [t for t in ordered if t.previous == stream.name]
        later = [t for t in ordered
                 if (t.start, t.name) > (stream.start, stream.name)]
        if chained:
            stream.succ_start = min(t.start for t in chained)
        elif later:
            stream.succ_start = later[0].start
    return streams, ordered


def _gaps(stream):
    """[(lo, hi, reason, first_seq, last_seq)]: the places this stream is
    known to have lost something. `lo` and `hi` are the times of the read
    events around the loss (or the stream's start, a header or its closing
    record); `first_seq` and `last_seq` are the lost numbers, or None for a
    torn tail whose numbers are unknown.

    A segment header's `after_seq` is a WATERMARK: the writer had written up
    to that number before the segment began. It is never an observed event,
    so a number below it that was not read is a loss like any other."""
    gaps = []
    prev_seq, prev_at, prev_drop = 0, stream.start, 0
    for seq, at, drop in sorted(stream.events):
        if seq > prev_seq + 1:
            gaps.append((prev_at, at, "seq_gap", prev_seq + 1, seq - 1))
        elif drop > prev_drop:
            gaps.append((prev_at, at, "dropped", seq, seq))
        prev_seq, prev_at, prev_drop = seq, at, max(prev_drop, drop)
    watermark, mark_ms = 0, None
    for _seg, after_seq, header_ms, _torn, _tm in stream.segments:
        if after_seq is not None and after_seq > watermark:
            watermark, mark_ms = after_seq, header_ms
    if stream.closed and stream.last_seq > prev_seq:
        gaps.append((prev_at, stream.closed_ms, "seq_gap", prev_seq + 1,
                     stream.last_seq))
    elif watermark > prev_seq:
        gaps.append((prev_at, mark_ms, "seq_gap", prev_seq + 1, watermark))
    elif stream.closed and stream.closed_dropped > prev_drop:
        gaps.append((prev_at, stream.closed_ms, "dropped", prev_seq + 1,
                     prev_seq + 1))
    for index, (_seg, _a, _h, torn, torn_ms) in enumerate(stream.segments):
        if torn:
            following = [s[2] for s in stream.segments[index + 1:]
                         if s[2] is not None]
            gaps.append((torn_ms, following[0] if following
                         else stream.succ_start, "torn_tail", None, None))
    return gaps


def _clock_regions(ordered, streams):
    regions = []
    for stream in ordered:
        previous = streams.get(stream.previous)
        if previous is not None and stream.start < previous.start:
            regions.append((stream.start, previous.start))
        last = None
        for _seq, at, _drop in stream.events:
            if last is not None and at < last:
                regions.append((at, last))
            last = at if last is None else max(last, at)
    return regions


def _message_time(rows):
    """(arrival_ms | None, inconsistent) from every copy of `message_at`.
    Every copy is the same stored value (the message record's ts), so copies
    must agree exactly: the clock-skew margin is only for comparing that value
    with this machine's clock, never one copy with another."""
    parsed, missing = set(), False
    for raw in rows:
        value = ev.parse_time(raw) if raw is not None else None
        if value is None:
            missing = True
        else:
            parsed.add(value)
    if missing or not parsed:
        return None, False
    return min(parsed), len(parsed) > 1


def _assess(message, streams, ordered, clock_regions, gaps_by_stream):
    """-> list of closed reasons (empty = complete)."""
    reasons = set()
    arr, inconsistent = _message_time(message["message_at"])
    if arr is None:
        reasons.add("unknown_arrival")
    if inconsistent:
        reasons.add("clock_inconsistent")
    disposed = message["disposal_at"]
    if arr is not None and (
            (disposed is not None and arr > disposed + _MARGIN_MS)
            or any(arr > start + _MARGIN_MS for start in message["start_ms"])):
        reasons.add("clock_inconsistent")
    # An open message may still lose events: its window has no end yet.
    end = INF if disposed is None else disposed
    # `message_at` is the sender's clock: widen every overlap test with a
    # local time by the allowance (D3c), always toward "possibly overlapping".
    low = -INF if arr is None else arr - _MARGIN_MS
    if any(lo <= end and hi >= low for lo, hi in clock_regions):
        reasons.add("clock_inconsistent")
    if message["unmatched"]:
        reasons.add("unmatched_dispatch")
    for stream in ordered:
        overlaps = stream.start <= end and stream.succ_start > low
        if overlaps and not stream.segments and not stream.events:
            reasons.add("missing_stream")
        if stream.previous and stream.previous not in streams \
                and (low == -INF or low < stream.start):
            reasons.add("missing_stream")
        for lo, hi, why, first, _last in gaps_by_stream[stream.name]:
            if hi < low:
                continue
            if disposed is None:
                relevant = True
            elif stream.name == message["disposal_stream"] \
                    and first is not None:
                # Within one stream the order is the sequence number, not a
                # rounded timestamp: a loss before the disposition counts.
                relevant = first <= message["disposal_seq"]
            else:
                relevant = lo <= end      # only times order it: a tie overlaps
            if relevant:
                reasons.add(why)
    if disposed is not None:
        for stream in ordered:
            if stream.closed or stream.name == message["disposal_stream"] \
                    or stream.start >= disposed:
                continue
            if stream.succ_start != INF and low < stream.succ_start:
                reasons.add("unclean_predecessor")
    return [r for r in REASONS if r in reasons]


def _load_messages(state, agent):
    messages = {}

    def get(message_id):
        return messages.setdefault(message_id, {
            "message_at": [], "start_ms": [], "end_ms": 0, "unmatched": False,
            "disposal_at": None, "disposal_stream": None,
            "disposal_seq": None, "disposition": None,
            "launched": [], "dispatches": 0})

    for (message_id, started, ended, start_ms, end_ms, message_at,
         has_message_at, launched, usage) in state.all(
            "SELECT message_id, started, ended, start_ms, end_ms, message_at,"
            " has_message_at, launched, usage_json FROM dispatch WHERE agent=?",
            (agent,)):
        msg = get(message_id)
        msg["dispatches"] += 1
        if has_message_at:
            msg["message_at"].append(message_at)
        if started:
            msg["start_ms"].append(start_ms)
            msg["end_ms"] = max(msg["end_ms"], start_ms)
        if ended:
            msg["end_ms"] = max(msg["end_ms"], end_ms)
        if started != ended:
            msg["unmatched"] = True
        if ended and launched:
            msg["launched"].append(None if usage is None
                                   else json.loads(usage))
    ordering = {}
    for (message_id, stream, seq, at_ms, disposition, message_at,
         _round_no, _started_ms) in state.all(
            "SELECT d.message_id, d.stream, d.seq, d.at_ms, d.disposition,"
            " d.message_at, d.round_no, s.started_ms FROM disposal d JOIN"
            " stream s ON s.stream=d.stream WHERE d.agent=? "
            "ORDER BY s.started_ms, d.seq", (agent,)):
        msg = get(message_id)
        msg["message_at"].append(message_at)
        if message_id not in ordering:                 # the first one wins
            ordering[message_id] = True
            msg["disposal_at"] = at_ms
            msg["disposal_stream"] = stream
            msg["disposal_seq"] = seq
            msg["disposition"] = disposition
            msg["end_ms"] = max(msg["end_ms"], at_ms)
    return messages


def _evaluate(state, agents, configs, round_no, result):
    conn = state.conn
    for agent in agents:
        streams, ordered = _load_streams(state, agent)
        if not ordered:
            continue
        gaps_by_stream = {s.name: _gaps(s) for s in ordered}
        regions = _clock_regions(ordered, streams)
        config = configs.get(agent)
        for message_id, msg in sorted(_load_messages(state, agent).items()):
            if _final_exists(conn, agent, message_id):
                continue
            reasons = _assess(msg, streams, ordered, regions, gaps_by_stream)
            complete = not reasons
            primary = reasons[0] if reasons else REASON_OK
            conn.execute(
                "INSERT INTO message_status(agent, message_id, complete,"
                " reason, reasons, disposition) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(agent, message_id) DO UPDATE SET"
                " complete=excluded.complete, reason=excluded.reason,"
                " reasons=excluded.reasons, disposition=excluded.disposition",
                (agent, message_id, 1 if complete else 0, primary,
                 ",".join(reasons), msg["disposition"]))
            if config is None or msg["disposition"] in (None,
                                                        "outcome_unknown"):
                continue
            if conn.execute(
                    "SELECT 1 FROM acquisition WHERE agent=? AND message_id=?",
                    (agent, message_id)).fetchone() is None:
                continue                    # no observed dispatch: no lease
            turn = mapper.turn_uuid(config.seat_id, message_id)
            totals = mapper.message_totals(msg["launched"], complete)
            body = mapper.final_body(mapper.final_event_id(turn),
                                     msg["disposition"], totals)
            conn.execute(
                "INSERT INTO final(agent, message_id, event_id, body,"
                " totals_json, complete, reason) VALUES(?,?,?,?,?,?,?)",
                (agent, message_id, body["event_id"],
                 json.dumps(body, sort_keys=True),
                 json.dumps(totals, sort_keys=True), 1 if complete else 0,
                 primary))
            result.finals.append((agent, message_id))
