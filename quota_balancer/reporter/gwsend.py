"""The money reader's send side (card R1-READER step 3a, DESIGN-R1-READER.md 3.1, 8 steps 1 and 3, 9, 12; D-195).

The reader has at most one pending report, saved as exact bytes. This module sends it and reads Leasewise's answer into exactly one of four classes:

* **accepted**   HTTP 202, a JSON object whose `accepted` is exactly `true` and whose `event_id` is the one sent. Only this clears the pending report
                 (`gwstate.accept`, one transaction).
* **conflict**   HTTP 409 with the closed code `IDEMPOTENCY_CONFLICT`: the reader halts (`continuity_conflict`) until the operator resets it.
* **refused**    HTTP 400 with the closed code `INVALID_CONFIG` or `SCHEMA_UNSUPPORTED` (a definitive refusal): counted on the pending row; a run of
                 `gwstate.REFUSAL_THRESHOLD` in a row halts the reader (`send_refused`).
* **retry**      everything else (401, 429, 5xx, no answer, a timeout, an unreadable body, an unreadable success, a 200 or 201, a 202 that is not
                 the exact acceptance): the same bytes are kept and tried again later. Never rebuilt, never renumbered.

Nothing of the report or of the answer is kept in any word; the counters are numbers. The report travels in its own lane (`Reporter._send_report`):
its retry never stops another upload, and another upload's retry never stops it.
"""

from collections import namedtuple

from .. import errors
from . import gwstate

ACCEPTED = "accepted"
CONFLICT = "conflict"
REFUSED = "refused"
RETRY = "retry"
CLASSES = (ACCEPTED, CONFLICT, REFUSED, RETRY)

# The closed words of the outcome of one send pass (shown as counts in the reporter's status).
SEND_FAILED = "send_failed"
READER_HALTED = "reader_halted"
STATE_UNAVAILABLE = gwstate.STATE_UNAVAILABLE

DEFINITIVE_CODES = (errors.CODE_INVALID_CONFIG, errors.CODE_SCHEMA_UNSUPPORTED)

# `word`: None when nothing was pending; else one of the words above or an answer class. `sent`: 1 when the report was accepted. `unauthorized`:
# True when the retry was a 401 (the cycle ranks a credential problem above "unreachable").
Outcome = namedtuple("Outcome", "word sent unauthorized")


def classify(status, doc, code, event_id):
    """One answer -> exactly one of `CLASSES`. `doc` is the parsed JSON object or None; `code` the closed error word or None."""
    if (status == 202 and type(status) is int and type(doc) is dict and doc.get("accepted") is True
            and type(doc.get("event_id")) is str and doc.get("event_id") == event_id):
        return ACCEPTED
    if status == 409 and code == errors.CODE_IDEMPOTENCY_CONFLICT:
        return CONFLICT
    if status == 400 and code in DEFINITIVE_CODES:
        return REFUSED
    return RETRY


def send_pending(state, client, now_ms):
    """Send the pending report, if any, and apply the answer -> `Outcome`. Reads the start-up check first: a halted or inconsistent state sends
    nothing (the count `reader_halted` or `state_unavailable` goes up). Never raises on an answer."""
    held = gwstate.pending(state)
    if held is None:
        return Outcome(None, 0, False)                                   # the common case: no money reader, or nothing waiting
    body, event_id, key = held[2], held[3], held[4]
    started = gwstate.start(state, now_ms)
    if started.halt is not None:
        word = STATE_UNAVAILABLE if started.halt == STATE_UNAVAILABLE else READER_HALTED
        with state.txn():
            state.bump(gwstate.READER_AGENT, word)
        return Outcome(word, 0, False)
    status, doc, code = client.post_reading(key, body)
    answer = classify(status, doc, code, event_id)
    if answer == ACCEPTED:
        if gwstate.accept(state, status, doc):
            return Outcome(ACCEPTED, 1, False)
        gwstate.start(state, now_ms)                                     # the exact answer, but the state would not take it: it is damaged
        with state.txn():
            state.bump(gwstate.READER_AGENT, STATE_UNAVAILABLE)
        return Outcome(STATE_UNAVAILABLE, 0, False)
    if answer == CONFLICT:
        gwstate.note_conflict(state)
        return Outcome(CONFLICT, 0, False)
    if answer == REFUSED:
        gwstate.note_refusal(state)
        return Outcome(REFUSED, 0, False)
    with state.txn():
        state.bump(gwstate.READER_AGENT, SEND_FAILED)
    return Outcome(SEND_FAILED, 0, status == 401)
