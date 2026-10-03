"""The wire format of `reporter admit` (card L6a-2; the neutral spec 4.2 and 4.3): one JSON request in, one JSON answer
out, strict on both sides.

* **Compact, sorted keys, ASCII only** when written. When read, anything that is not exactly one JSON object is refused:
  a boolean or a float where a whole number belongs, a repeated key, NaN or an infinity, anything after the object, a
  key that is missing or extra, a value of the wrong type or outside its bound, another operation's shape.
* **Size caps** (measured as compact JSON at the bounds of 4.2): 512 bytes for the request and for the answer, enforced
  while reading, so a legal message is never cut and a larger one is a failure.
* **Closed failure words**, shared with the caller: `not_json`, `bad_shape`, `out_of_bounds`, `too_large`. A failure
  carries no text from the input.
* **Closed `held` reasons**: only the six words of `HELD_REASONS`.

The four operations: `admit` (the only one that can answer `held`), `recall`, `pending` and `closed`. Every operation
reads one JSON object with exactly its own keys and writes one answer of its own shape; another operation's shape is a
failure.
"""

import json
import re

VERSION = 1
OP_ADMIT, OP_RECALL, OP_PENDING, OP_CLOSED = "admit", "recall", "pending", "closed"
OPERATIONS = (OP_ADMIT, OP_RECALL, OP_PENDING, OP_CLOSED)
MAX_REQUEST_BYTES = 512
MAX_ANSWER_BYTES = 512
MAX_PENDING_ANSWER_BYTES = 9216          # 35 bytes of frame, 64 entries of at most 128 bytes, 63 commas (8,290)
MAX_LIMIT = 64

HELD_REASONS = ("coordinator_unreachable", "refused", "parked", "context_unprovable", "store_busy", "not_configured")

NOT_JSON = "not_json"
BAD_SHAPE = "bad_shape"
OUT_OF_BOUNDS = "out_of_bounds"
TOO_LARGE = "too_large"

# The neutral spec's bounds (4.2).
AGENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
RESERVED_AGENTS = frozenset({"operator"})
MESSAGE_ID = re.compile(r"[A-Za-z0-9._:-]{1,32}")
PROFILE = re.compile(r"[a-z0-9-]{1,32}")
REFERENCE = re.compile(r"[A-Za-z0-9._:-]{1,64}")
MAX_DISPATCH_BYTES = 10 ** 12 - 1
MAX_CALLS = 100_000
MAX_MICRO_EUR = 10 ** 12 - 1
MIN_TTL, MAX_TTL = 1, 86_400
CONTEXT_MODES = ("fresh", "resume")

REQUEST_KEYS = frozenset({"v", "op", "agent", "message_id", "parent_request_id", "profile", "context"})
KEYED_KEYS = frozenset({"v", "op", "agent", "message_id"})            # recall and closed
PENDING_KEYS = frozenset({"v", "op", "agent", "limit"})
ENTRY_KEYS = frozenset({"message_id", "reference"})
CONTEXT_KEYS = frozenset({"mode", "dispatch_bytes"})
ADMITTED_KEYS = frozenset({"max_calls", "max_micro_eur", "reference", "status", "ttl_seconds", "v"})
HELD_KEYS = frozenset({"reason", "status", "v"})


class WireFailure(Exception):
    """A request or answer that is not allowed. `word` is one of the four closed words; nothing else is kept."""

    def __init__(self, word):
        super().__init__(word)
        self.word = word


def _no_repeats(pairs):
    keys = [key for key, _value in pairs]
    if len(set(keys)) != len(keys):
        raise WireFailure(BAD_SHAPE)
    return dict(pairs)


def _no_constant(_name):
    raise WireFailure(BAD_SHAPE)             # NaN and the infinities


def _no_float(_text):
    raise WireFailure(BAD_SHAPE)             # a float where a whole number belongs, however it is written


def loads(raw, cap):
    """The bytes of one message -> its one JSON object, or WireFailure. `raw` is at most `cap` + 1 bytes long (the
    caller reads no more); one byte over the cap is `too_large`."""
    if len(raw) > cap:
        raise WireFailure(TOO_LARGE)
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise WireFailure(NOT_JSON) from None
    try:
        doc = json.loads(text, object_pairs_hook=_no_repeats, parse_constant=_no_constant, parse_float=_no_float)
    except WireFailure:
        raise
    except (ValueError, RecursionError):
        raise WireFailure(NOT_JSON) from None
    if type(doc) is not dict:
        raise WireFailure(BAD_SHAPE)
    return doc


def _int(value, low, high):
    if type(value) is not int:
        raise WireFailure(BAD_SHAPE)             # a boolean is not a whole number here
    if not low <= value <= high:
        raise WireFailure(OUT_OF_BOUNDS)
    return value


def _text(value, pattern):
    if type(value) is not str:
        raise WireFailure(BAD_SHAPE)
    if pattern.fullmatch(value) is None:
        raise WireFailure(OUT_OF_BOUNDS)
    return value


def _closed(doc, keys):
    if frozenset(doc) != keys:
        raise WireFailure(BAD_SHAPE)


class Request:
    """One checked request. Only the fields of its operation are set."""

    __slots__ = ("op", "agent", "message_id", "parent_request_id", "profile", "mode", "dispatch_bytes", "limit")

    def __repr__(self):
        return "Request(<hidden>)"

    __str__ = __repr__


AdmitRequest = Request          # the name step 1 used


def _agent(value):
    agent = _text(value, AGENT)
    if agent.casefold() in RESERVED_AGENTS:
        raise WireFailure(OUT_OF_BOUNDS)
    return agent


def parse_request(raw):
    """The bytes read from standard input -> Request, or WireFailure. The operation is named by `op`; the keys are
    closed and must be exactly that operation's."""
    doc = loads(raw, MAX_REQUEST_BYTES)
    op = doc.get("op")
    if type(op) is not str or op not in OPERATIONS:
        raise WireFailure(BAD_SHAPE)
    request = Request()
    request.op = op
    if op == OP_PENDING:
        _closed(doc, PENDING_KEYS)
        _int(doc["v"], VERSION, VERSION)
        request.agent = _agent(doc["agent"])
        request.limit = _int(doc["limit"], 1, MAX_LIMIT)
        return request
    if op in (OP_RECALL, OP_CLOSED):
        _closed(doc, KEYED_KEYS)
        _int(doc["v"], VERSION, VERSION)
        request.agent = _agent(doc["agent"])
        request.message_id = _text(doc["message_id"], MESSAGE_ID)
        return request
    _closed(doc, REQUEST_KEYS)
    _int(doc["v"], VERSION, VERSION)
    request.agent = _agent(doc["agent"])
    request.message_id = _text(doc["message_id"], MESSAGE_ID)
    parent = doc["parent_request_id"]
    if type(parent) is not str:
        raise WireFailure(BAD_SHAPE)
    request.parent_request_id = parent if parent == "" else _text(parent, MESSAGE_ID)   # empty: no parent, never invented
    request.profile = _text(doc["profile"], PROFILE)
    context = doc["context"]
    if type(context) is not dict:
        raise WireFailure(BAD_SHAPE)
    _closed(context, CONTEXT_KEYS)
    mode = context["mode"]
    if type(mode) is not str:
        raise WireFailure(BAD_SHAPE)
    if mode not in CONTEXT_MODES:
        raise WireFailure(OUT_OF_BOUNDS)
    request.mode = mode
    request.dispatch_bytes = _int(context["dispatch_bytes"], 0, MAX_DISPATCH_BYTES)
    return request


def encode(doc, cap=MAX_ANSWER_BYTES):
    """One answer as compact, sorted, ASCII JSON. Over the cap is a failure, never a cut message."""
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(raw) > cap:
        raise WireFailure(TOO_LARGE)
    return raw


def encode_op(op, **fields):
    """A request of any operation exactly as the caller writes it (used by the tests and by the other side)."""
    return json.dumps(dict({"v": VERSION, "op": op}, **fields), sort_keys=True, separators=(",", ":")).encode("ascii")


def encode_request(agent, message_id, parent_request_id, profile, mode, dispatch_bytes):
    """A request exactly as the caller writes it (used by the tests and by anyone building the other side)."""
    return json.dumps({"v": VERSION, "op": OP_ADMIT, "agent": agent, "message_id": message_id,
                       "parent_request_id": parent_request_id, "profile": profile,
                       "context": {"mode": mode, "dispatch_bytes": dispatch_bytes}},
                      sort_keys=True, separators=(",", ":")).encode("ascii")


def held(reason):
    if reason not in HELD_REASONS:
        raise WireFailure(BAD_SHAPE)
    return encode({"v": VERSION, "status": "held", "reason": reason})


def admitted(reference, max_calls, max_micro_eur, ttl_seconds):
    """The `admitted` answer, every field checked against its bound before it is written."""
    _text(reference, REFERENCE)
    for value, high in ((max_calls, MAX_CALLS), (max_micro_eur, MAX_MICRO_EUR)):
        if value is not None:
            _int(value, 1, high)
    _int(ttl_seconds, MIN_TTL, MAX_TTL)
    return encode({"v": VERSION, "status": "admitted", "reference": reference, "max_calls": max_calls,
                   "max_micro_eur": max_micro_eur, "ttl_seconds": ttl_seconds})


def parse_answer(raw):
    """The other side's reading of an `admit` answer: strict, so a program that answers wrongly is caught by the caller
    as a failure. -> the answer's dict (an `admitted` one or a `held` one)."""
    doc = loads(raw, MAX_ANSWER_BYTES)
    status = doc.get("status")
    if status == "admitted":
        _closed(doc, ADMITTED_KEYS)
        _int(doc["v"], VERSION, VERSION)
        _text(doc["reference"], REFERENCE)
        for key, high in (("max_calls", MAX_CALLS), ("max_micro_eur", MAX_MICRO_EUR)):
            if doc[key] is not None:
                _int(doc[key], 1, high)
        _int(doc["ttl_seconds"], MIN_TTL, MAX_TTL)
        return doc
    if status == "held":
        _closed(doc, HELD_KEYS)
        _int(doc["v"], VERSION, VERSION)
        if type(doc["reason"]) is not str:
            raise WireFailure(BAD_SHAPE)
        if doc["reason"] not in HELD_REASONS:
            raise WireFailure(OUT_OF_BOUNDS)
        return doc
    raise WireFailure(BAD_SHAPE)


def unknown():
    return encode({"v": VERSION, "status": "unknown"})


def closed_ok():
    return encode({"v": VERSION, "status": "ok"})


def pending_answer(entries, limit):
    """The `pending` answer: 0 to `limit` entries of (message_id, reference), each within its bounds, at most 9,216
    bytes. More entries than the limit is a failure of the program, never a long answer."""
    if len(entries) > limit:
        raise WireFailure(OUT_OF_BOUNDS)
    messages = []
    for message_id, reference in entries:
        messages.append({"message_id": _text(message_id, MESSAGE_ID), "reference": _text(reference, REFERENCE)})
    return encode({"v": VERSION, "status": "ok", "messages": messages}, MAX_PENDING_ANSWER_BYTES)


def parse_op_answer(op, raw, limit=None):
    """The other side's strict reading of an answer to operation `op`: `admit` (admitted or held), `recall`
    (admitted or unknown), `pending` (ok with its entries) and `closed` (ok). Another operation's shape, an extra or
    missing key, a boolean or a float for a whole number, or a `held` anywhere but `admit` is a failure."""
    if op == OP_PENDING:
        doc = loads(raw, MAX_PENDING_ANSWER_BYTES)
        _closed(doc, frozenset({"v", "status", "messages"}))
        _int(doc["v"], VERSION, VERSION)
        if doc["status"] != "ok" or type(doc["messages"]) is not list:
            raise WireFailure(BAD_SHAPE)
        if len(doc["messages"]) > (MAX_LIMIT if limit is None else limit):
            raise WireFailure(OUT_OF_BOUNDS)
        for entry in doc["messages"]:
            if type(entry) is not dict:
                raise WireFailure(BAD_SHAPE)
            _closed(entry, ENTRY_KEYS)
            _text(entry["message_id"], MESSAGE_ID)
            _text(entry["reference"], REFERENCE)
        return doc
    if op == OP_CLOSED:
        doc = loads(raw, MAX_ANSWER_BYTES)
        _closed(doc, frozenset({"v", "status"}))
        _int(doc["v"], VERSION, VERSION)
        if doc["status"] != "ok":
            raise WireFailure(BAD_SHAPE)
        return doc
    if op == OP_RECALL:
        doc = loads(raw, MAX_ANSWER_BYTES)
        if doc.get("status") == "unknown":
            _closed(doc, frozenset({"v", "status"}))
            _int(doc["v"], VERSION, VERSION)
            return doc
        if doc.get("status") != "admitted":
            raise WireFailure(BAD_SHAPE)             # `held` belongs to `admit` alone
        return parse_answer(raw)
    if op == OP_ADMIT:
        return parse_answer(raw)
    raise WireFailure(BAD_SHAPE)
