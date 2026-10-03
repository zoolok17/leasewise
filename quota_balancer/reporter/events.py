"""The journal reader: schema mirror, discovery and the snapshot read.

The closed schema below mirrors DESIGN-7A.md Part 3.3 as changed by Part 12
(D1 dispositions, D3 `message_at` and `stream_closed`, D4 unnumbered stream
records). It is the ONLY place that knows the journal's shape. A line with
any other schema version is refused (`UnsupportedVersion`); a damaged line
is counted and skipped, never guessed at.

Files (Part 3.8): `<root>/<agent>/streams.jsonl` and
`<root>/<agent>/<generation>-<n>.jsonl`; a stream is `<agent>.<generation>`.
"""

import datetime
import json
import os
import re
import uuid

SCHEMA_VERSION = 1
MAX_LINE_BYTES = 4096
INT64_MAX = 2 ** 63 - 1

KIND_STREAM_STARTED = "stream_started"
KIND_STREAM_CLOSED = "stream_closed"
KIND_DISPATCH_STARTED = "dispatch_started"
KIND_DISPATCH_ENDED = "dispatch_ended"
KIND_MESSAGE_DISPOSED = "message_disposed"
EVENT_KINDS = (KIND_DISPATCH_STARTED, KIND_DISPATCH_ENDED,
               KIND_MESSAGE_DISPOSED)
STREAM_KINDS = (KIND_STREAM_STARTED, KIND_STREAM_CLOSED)

DISPOSITIONS = ("completed", "dead_lettered", "delivery_failed",
                "outcome_unknown")
OUTCOMES = ("success", "failed", "not_launched")
EXITS = ("normal", "spawn_error", "exception")
CLIS = ("claude", "codex")
SESSIONS = ("fresh", "resume")
FAILURE_CLASSES = ("poison_eligible", "known_global_infra",
                   "ambiguous_or_unknown", "config_blocked",
                   "infra_retry_exhausted", "gateway_held", "other")
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens",
              "cache_write_tokens")

_COMMON = frozenset({"v", "kind", "event_id", "stream", "at", "agent",
                     "dropped_total"})
_SHAPES = {
    KIND_STREAM_STARTED: (frozenset({
        "agent_version", "previous_stream", "mode", "unmanaged", "segment",
        "after_seq"}), frozenset()),
    KIND_STREAM_CLOSED: (frozenset({"last_seq"}), frozenset()),
    KIND_DISPATCH_STARTED: (frozenset({
        "seq", "message_id", "turn_id", "cli", "cli_session", "message_at"}),
        frozenset({"attempts_recorded"})),
    KIND_DISPATCH_ENDED: (frozenset({
        "seq", "message_id", "turn_id", "launched", "outcome", "exit",
        "duration_ms", "usage"}), frozenset({"failure_class"})),
    KIND_MESSAGE_DISPOSED: (frozenset({
        "seq", "message_id", "disposition", "message_at"}),
        frozenset({"attempts_recorded"})),
}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class JournalError(Exception):
    """A damaged or unreadable journal item (static message, no content)."""


class UnsupportedVersion(JournalError):
    """A line of another schema version: the whole round is refused."""


def _bad():
    raise JournalError("the journal record is invalid")


def _is_int(value, low=0):
    return type(value) is int and low <= value <= INT64_MAX


def parse_time(text):
    """ISO-8601 with a time zone -> epoch milliseconds, or None."""
    if not isinstance(text, str) or not text or len(text) > 40:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    delta = parsed - datetime.datetime(1970, 1, 1,
                                       tzinfo=datetime.timezone.utc)
    return (delta.days * 86_400 + delta.seconds) * 1000 \
        + delta.microseconds // 1000


def _check_usage(usage):
    if usage is None:
        return
    if not isinstance(usage, dict) or set(usage) != set(USAGE_KEYS):
        _bad()
    for value in usage.values():
        if value is not None and not _is_int(value):
            _bad()


def parse_event(raw):
    """One complete journal line (bytes, no newline) -> a checked dict with
    `at_ms` added. Raises UnsupportedVersion or JournalError."""
    if len(raw) > MAX_LINE_BYTES:
        _bad()
    try:
        obj = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        _bad()
    if not isinstance(obj, dict):
        _bad()
    if obj.get("v") != SCHEMA_VERSION or type(obj.get("v")) is not int:
        if "v" in obj:
            raise UnsupportedVersion("the journal schema version is not supported")
        _bad()
    kind = obj.get("kind")
    if kind not in _SHAPES:
        _bad()
    required, optional = _SHAPES[kind]
    keys = set(obj)
    if not (_COMMON | required) <= keys or keys - _COMMON - required - optional:
        _bad()
    for name in ("event_id",):
        try:
            uuid.UUID(obj[name])
        except (ValueError, AttributeError, TypeError):
            _bad()
    for name in ("stream", "agent"):
        if not isinstance(obj[name], str) or not _NAME.match(obj[name]):
            _bad()
    if not obj["stream"].startswith(obj["agent"] + "."):
        _bad()
    if not _is_int(obj["dropped_total"]):
        _bad()
    obj["at_ms"] = parse_time(obj["at"])
    if obj["at_ms"] is None:
        _bad()
    if "seq" in required and not _is_int(obj["seq"], 1):
        _bad()
    if kind == KIND_STREAM_STARTED:
        if not _is_int(obj["segment"], 1) or not _is_int(obj["after_seq"]):
            _bad()
        if obj["previous_stream"] is not None and (
                not isinstance(obj["previous_stream"], str)
                or not _NAME.match(obj["previous_stream"])):
            _bad()
        if obj["mode"] != "loop" or not isinstance(obj["unmanaged"], list) \
                or any(item != "cadence" for item in obj["unmanaged"]) \
                or not isinstance(obj["agent_version"], str) \
                or len(obj["agent_version"]) > 40:
            _bad()
    elif kind == KIND_STREAM_CLOSED:
        if not _is_int(obj["last_seq"]):
            _bad()
    else:
        if not isinstance(obj["message_id"], str) \
                or not _NAME.match(obj["message_id"]):
            _bad()
        if "attempts_recorded" in obj and not _is_int(obj["attempts_recorded"]):
            _bad()
        if kind in (KIND_DISPATCH_STARTED, KIND_MESSAGE_DISPOSED):
            raw_time = obj["message_at"]
            if raw_time is not None and (not isinstance(raw_time, str)
                                         or len(raw_time) > 40):
                _bad()
        if kind == KIND_DISPATCH_STARTED:
            if not (isinstance(obj["turn_id"], str)
                    and _NAME.match(obj["turn_id"])
                    and obj["cli"] in CLIS and obj["cli_session"] in SESSIONS):
                _bad()
        elif kind == KIND_DISPATCH_ENDED:
            if not (isinstance(obj["turn_id"], str)
                    and _NAME.match(obj["turn_id"])
                    and type(obj["launched"]) is bool
                    and obj["outcome"] in OUTCOMES
                    and obj["exit"] in EXITS
                    and _is_int(obj["duration_ms"])):
                _bad()
            if obj.get("failure_class") not in (None,) + FAILURE_CLASSES:
                _bad()
            if (obj["outcome"] == "not_launched") == obj["launched"] \
                    or (obj["outcome"] == "success" and not obj["launched"]):
                _bad()
            _check_usage(obj["usage"])
        else:
            if obj["disposition"] not in DISPOSITIONS:
                _bad()
    return obj


def parse_stream_record(raw):
    """One line of `streams.jsonl`: {stream, previous_stream, started_at}."""
    if len(raw) > MAX_LINE_BYTES:
        _bad()
    try:
        obj = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        _bad()
    if not isinstance(obj, dict) or set(obj) != {
            "stream", "previous_stream", "started_at"}:
        _bad()
    if not isinstance(obj["stream"], str) or not _NAME.match(obj["stream"]) \
            or "." not in obj["stream"]:
        _bad()
    if obj["previous_stream"] is not None and (
            not isinstance(obj["previous_stream"], str)
            or not _NAME.match(obj["previous_stream"])):
        _bad()
    started = parse_time(obj["started_at"])
    if started is None:
        _bad()
    return {"stream": obj["stream"], "previous_stream": obj["previous_stream"],
            "started_ms": started}


def stream_generation(stream, agent):
    return stream[len(agent) + 1:]


class SegmentRef:
    __slots__ = ("agent", "stream", "segment", "path", "size")

    def __init__(self, agent, stream, segment, path, size):
        self.agent = agent
        self.stream = stream
        self.segment = segment
        self.path = path
        self.size = size


class AgentSnapshot:
    """What exists for one agent at the instant a round starts."""

    def __init__(self, agent, records, damaged_records, segments):
        self.agent = agent
        self.records = records              # list of stream-record dicts
        self.damaged_records = damaged_records
        self.segments = segments            # list of SegmentRef


class SegmentRead:
    def __init__(self):
        self.events = []        # parsed event/stream dicts, in file order
        self.end_offset = 0     # after the last complete line
        self.torn = False       # bytes after the last newline, in the snapshot
        self.damaged = 0        # complete lines that could not be read
        self.missing = False    # the file vanished


def _complete_lines(data):
    """(lines, consumed_bytes) for the newline-terminated part of `data`."""
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], 0
    return data[:cut].split(b"\n"), cut + 1


def _read_stream_records(path):
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        return [], 0
    lines, _used = _complete_lines(data)
    records, damaged, seen = [], 0, set()
    for line in lines:
        try:
            record = parse_stream_record(line)
        except JournalError:
            damaged += 1
            continue
        if record["stream"] not in seen:
            seen.add(record["stream"])
            records.append(record)
    return records, damaged


def take_snapshot(root, agents=None):
    """List every agent's `streams` record and segments, with each
    segment's length RIGHT NOW. Segment bytes are read later, up to exactly
    that length, so a round is one consistent view."""
    snapshot = {}
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        names = []
    for agent in names:
        if agents is not None and agent not in agents:
            continue
        directory = os.path.join(root, agent)
        if not os.path.isdir(directory) or not _NAME.match(agent):
            continue
        records, damaged = _read_stream_records(
            os.path.join(directory, "streams.jsonl"))
        segments = []
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".jsonl") or name == "streams.jsonl":
                continue
            base = name[:-len(".jsonl")]
            generation, _dash, number = base.rpartition("-")
            if not generation or not number.isdigit() or int(number) < 1:
                continue
            path = os.path.join(directory, name)
            try:
                size = os.stat(path).st_size
            except FileNotFoundError:
                continue
            segments.append(SegmentRef(agent, agent + "." + generation,
                                       int(number), path, size))
        snapshot[agent] = AgentSnapshot(agent, records, damaged, segments)
    return snapshot


def read_segment(ref, offset):
    """Read `ref` from `offset` to its snapshot length: complete lines only.
    An unknown schema version raises UnsupportedVersion."""
    result = SegmentRead()
    result.end_offset = offset
    if offset > ref.size:
        raise JournalError("the journal segment is shorter than its cursor")
    if offset == ref.size:
        return result
    try:
        with open(ref.path, "rb") as handle:
            handle.seek(offset)
            data = handle.read(ref.size - offset)
    except FileNotFoundError:
        result.missing = True
        return result
    lines, used = _complete_lines(data)
    result.end_offset = offset + used
    result.torn = len(data) > used
    for line in lines:
        try:
            event = parse_event(line)
        except UnsupportedVersion:
            raise
        except JournalError:
            result.damaged += 1
            continue
        if event["stream"] != ref.stream:
            result.damaged += 1
            continue
        result.events.append(event)
    return result
