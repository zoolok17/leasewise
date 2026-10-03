"""Hand-made journal files for the reporter-core tests (schema version 1,
DESIGN-7A.md Part 3.3 as changed by Part 12). Not a model of agenttalk's
writer: it writes exactly the bytes a test needs, including damaged ones.
All data is synthetic."""

import datetime
import json
import os
import uuid

T0 = 1_790_000_000_000          # a fixed instant, milliseconds
SEAT = "11111111-1111-4111-8111-111111111111"
MODEL = "22222222-2222-4222-8222-222222222222"
USAGE = {"input_tokens": 100, "output_tokens": 10,
         "cache_read_tokens": 50, "cache_write_tokens": 20}


def iso(ms):
    moment = datetime.datetime.fromtimestamp(ms / 1000,
                                             tz=datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (ms % 1000)


def line(obj):
    return (json.dumps(obj, sort_keys=True) + "\n").encode("ascii")


class Journal:
    def __init__(self, root, agent="alpha"):
        self.root = root
        self.agent = agent
        self.dir = os.path.join(root, agent)
        os.makedirs(self.dir, exist_ok=True)
        self.streams_path = os.path.join(self.dir, "streams.jsonl")

    def add_stream(self, name, start_ms, previous=None, register=True,
                   header=True):
        stream = Stream(self, name, previous, start_ms)
        if register:
            with open(self.streams_path, "ab") as handle:
                handle.write(line({
                    "stream": stream.stream,
                    "previous_stream": (self.agent + "." + previous)
                    if previous else None,
                    "started_at": iso(start_ms)}))
        if header:
            stream.open_segment(start_ms)
        return stream


class Stream:
    def __init__(self, journal, name, previous, start_ms):
        self.journal = journal
        self.agent = journal.agent
        self.gen = name
        self.stream = journal.agent + "." + name
        self.previous = (journal.agent + "." + previous) if previous else None
        self.start = start_ms
        self.seq = 0
        self.written_seq = 0
        self.dropped = 0
        self.segment = 0

    def path(self, number=None):
        return os.path.join(self.journal.dir, "%s-%d.jsonl"
                            % (self.gen, number or self.segment))

    def _common(self, kind, at):
        return {"v": 1, "kind": kind, "event_id": str(uuid.uuid4()),
                "stream": self.stream, "at": iso(at), "agent": self.agent,
                "dropped_total": self.dropped}

    def _append(self, obj_or_bytes, number=None):
        data = obj_or_bytes if isinstance(obj_or_bytes, bytes) \
            else line(obj_or_bytes)
        with open(self.path(number), "ab") as handle:
            handle.write(data)

    def open_segment(self, at):
        self.segment += 1
        obj = self._common("stream_started", at)
        obj.update({"agent_version": "test", "previous_stream": self.previous,
                    "mode": "loop", "unmanaged": [], "segment": self.segment,
                    "after_seq": self.written_seq})
        self._append(obj)

    def rotate(self, at):
        self.open_segment(at)

    def _event(self, kind, at, **extra):
        self.seq += 1
        obj = self._common(kind, at)
        obj["seq"] = self.seq
        obj.update(extra)
        return obj

    def write(self, obj):
        self._append(obj)
        self.written_seq = obj["seq"]

    def dispatch(self, message_id, turn_id, at, end_at=None, *, launched=True,
                 outcome="success", usage=USAGE, message_at="auto",
                 ended=True, started=True, cli="claude"):
        arrived = (at - 1000) if message_at == "auto" else message_at
        if started:
            self.write(self._event(
                "dispatch_started", at, message_id=message_id,
                turn_id=turn_id, cli=cli, cli_session="fresh",
                message_at=None if arrived is None else iso(arrived)))
        if ended:
            extra = {"message_id": message_id, "turn_id": turn_id,
                     "launched": launched, "outcome": outcome,
                     "exit": "normal", "duration_ms": 10, "usage": usage}
            if outcome != "success":
                extra["failure_class"] = "other"
            self.write(self._event("dispatch_ended",
                                   end_at if end_at is not None else at + 100,
                                   **extra))

    def disposed(self, message_id, disposition, at, message_at="auto"):
        arrived = (at - 5000) if message_at == "auto" else message_at
        self.write(self._event(
            "message_disposed", at, message_id=message_id,
            disposition=disposition,
            message_at=None if arrived is None else iso(arrived)))

    def lose(self):
        """An event that was assigned a number and never reached the file."""
        self.seq += 1

    def drop(self):
        """A queue-full drop: numbered, counted, never written."""
        self.seq += 1
        self.dropped += 1

    def close(self, at):
        obj = self._common("stream_closed", at)
        obj["last_seq"] = self.seq
        self._append(obj)

    def raw(self, data, number=None):
        self._append(data, number)

    def tear(self, number=None):
        self._append(b'{"v":1,"kind":"dispatch_sta', number)
