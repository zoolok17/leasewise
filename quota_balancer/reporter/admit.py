"""`reporter admit`: the turn admission program (card L6a-2, step 1; DESIGN-6A 8.6 and 8.7; the neutral spec 4.2).

Before a paid turn starts, the wrapper asks one question: may this turn run, and under what limits? This program asks
Leasewise for a lease, keeps the answer in the admission store, and gives the SAME answer every time it is asked about
the same message, however often it is run, killed or run at the same time as itself.

How it stays safe (the order of the steps is the point):

1. A request whose message already has a saved grant is answered from the store, without a lock and without Leasewise.
2. Otherwise the message's key lock is taken (one of 64 files; at most 10 s of waiting), so one helper at a time works
   on one message. The whole run has ONE deadline of 20 s, below the hook's 25 s.
3. The request is saved BEFORE it is sent, and every body is saved before it is sent, so a lost answer can be asked
   again with the very same bytes (Leasewise then replays its stored lease).
4. A stale estimate is rebuilt (at most 3 times) with a fresh time, again saved first.
5. If a rebuilt body is told "same request, different body", an earlier body was granted after all (a slow answer that
   landed late). The saved bodies are replayed, newest first (at most 4), to find that grant. Which bodies were already
   tried is kept in the store, so a run that ran out of time continues next time instead of starting again.
6. The grant is saved before anything is printed. A grant already saved by someone else is returned, never replaced.
7. Any other refusal or outage is `held` with one closed word and a back-off kept in the store, so a wrapper that asks
   again every few seconds does not hit Leasewise every time.

The other three questions the wrapper asks are answered here too, on the same store:

* `recall`: the stored admission, or "unknown". Read-only: it takes no lock and can never create anything.
* `pending`: the admissions not yet acknowledged as closed, least recently returned first by a durable return number
  (never by the clock); the same short transaction gives every returned entry the next number, so an entry that cannot
  be closed yet goes to the back of the line.
* `closed`: records the acknowledgement once, waiting for the message's key lock for at most the bounded wait.

Only `admit` can answer `held`. When another operation cannot complete (the store is busy or unusable, the lock is not
free in time) it exits non-zero with NO answer, and the caller keeps its work and asks again.

Nothing from Leasewise's answer or from a fault is ever printed: only the closed words of `admit_wire`.
"""

import json
import os
import re
import time
import uuid

from . import admission_lock, mapper
from . import admission_store as store
from . import admit_wire as wire

TOTAL_SECONDS = 20.0            # the whole run: lock, requests, rebuilds, replays, saving, answering
LOCK_WAIT_SECONDS = 10.0
POST_TIMEOUT_SECONDS = 5.0
MIN_POST_SECONDS = 0.5          # less than this is left: not worth starting another request
MIN_OPEN_SECONDS = 0.2          # less than this is left: not worth starting an attempt to open the store
MAX_REBUILDS = 3
MAX_REPLAYS = 4
BACKOFF_MS = {"coordinator_unreachable": 10_000, "parked": 30_000, "refused": 60_000}

EXIT_OK, EXIT_BAD_REQUEST, EXIT_FAILED = 0, 2, 3

ERR_UNREACHABLE = "UNREACHABLE"          # the closed word kept when Leasewise gave no usable answer
ERR_STALE = "ESTIMATE_STALE"
ERR_CONFLICT = "IDEMPOTENCY_CONFLICT"
ERR_PARKED = "ACCOUNT_PARKED"
ERR_DENIED = "POLICY_DENIED"
ERR_BODY_CAP = "BODY_CAP"                # the message used up its request bodies (admission_store.MAX_BODIES)
PAID = ("metered_advisory", "metered")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class Environment:
    """What a run needs from outside, so a test can replace each part: the resolved `admission` block, the resolved
    agents, an HTTP client, the clocks, and a `fault(stage)` seam that can stop the run at a hand-off."""

    def __init__(self, admission, agents, client, now_ms=None, monotonic=time.monotonic, sleep=time.sleep, fault=None):
        self.admission = admission
        self.agents = agents
        self.client = client
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.monotonic = monotonic
        self.sleep = sleep
        self.fault = fault or (lambda _stage: None)


class Incomplete(Exception):
    """An operation other than `admit` could not complete now: no answer is printed and the exit is non-zero."""


def run(raw, env):
    """The request bytes -> (exit code, bytes to print). Exit 0 prints an answer; exit 2 is a request that is not
    allowed (the closed word is returned as text, nothing is printed on standard output); exit 3 is an operation
    other than `admit` that could not complete (no answer, the caller keeps its work)."""
    deadline = env.monotonic() + TOTAL_SECONDS
    try:
        request = wire.parse_request(raw)
    except wire.WireFailure as exc:
        return EXIT_BAD_REQUEST, exc.word.encode("ascii")
    if request.op != wire.OP_ADMIT:
        try:
            return EXIT_OK, _other(request, env, deadline)
        except (Incomplete, store.StoreBusy, store.StoreUnusable, wire.WireFailure):
            return EXIT_FAILED, b""
    try:
        return EXIT_OK, _admit(request, env, deadline)
    except wire.WireFailure:
        return EXIT_OK, wire.held("refused")            # an answer that cannot be written within its bounds


def _other(request, env, deadline):
    """`recall`, `pending` and `closed`. None of them creates a store file: with no file there is nothing to find,
    nothing pending and nothing to close."""
    path = env.admission.store
    if request.op == wire.OP_RECALL:
        status, row = store.read_admission(path, request.agent, request.message_id)
        if status == store.LATER and row == store.STORE_MISSING:
            return wire.unknown()                       # no file: nothing was ever admitted
        if status != store.OK:
            raise Incomplete()
        if row is None or row["state"] != "granted":
            return wire.unknown()
        # a grant whose time is used up is still the admission of that message: its reference is what the close needs
        return wire.admitted(row["reference"], row["max_calls"], row["max_micro_eur"], max(row["ttl_seconds"], wire.MIN_TTL))
    if not os.path.isfile(path):
        return wire.pending_answer([], request.limit) if request.op == wire.OP_PENDING else wire.closed_ok()
    writer = _open(env, deadline)
    try:
        if request.op == wire.OP_PENDING:
            return wire.pending_answer(writer.pending(request.agent, request.limit, env.now_ms()), request.limit)
        lock = admission_lock.KeyLock(path, request.agent, request.message_id, env.monotonic, env.sleep)
        if not lock.acquire(min(deadline, env.monotonic() + LOCK_WAIT_SECONDS)):
            raise Incomplete()
        try:
            writer.close_message(request.agent, request.message_id, env.now_ms())
        finally:
            lock.release()
        return wire.closed_ok()
    finally:
        writer.close()


# --- the answer ---------------------------------------------------------------------------------------------------

def _reason(code):
    if code == ERR_UNREACHABLE:
        return "coordinator_unreachable"
    return "parked" if code == ERR_PARKED else "refused"


def _answer(row):
    """The stored grant as the answer. A grant whose time is already used up is `held`, never a zero."""
    if row["ttl_seconds"] < wire.MIN_TTL:
        return wire.held("refused")
    return wire.admitted(row["reference"], row["max_calls"], row["max_micro_eur"], row["ttl_seconds"])


def _hold(env, writer, row, code, restart_recovery=True):
    """Remember why (the closed word and when to try again) and answer `held`."""
    reason = _reason(code)
    writer.note_error(row["id"], code, env.now_ms() + BACKOFF_MS[reason], restart_recovery)
    return wire.held(reason)


# --- what is sent -------------------------------------------------------------------------------------------------

def _unprovable(request, agent, admission):
    """True when the token bound of this turn cannot be proven from what the operator wrote. Nothing is invented: a
    missing bound, a sum above the model's ceiling, or `resume` with no resume bound."""
    initial = admission.fresh_overhead_bound_tokens if request.mode == "fresh" \
        else admission.resume_history_bound_tokens
    growth = admission.growth_bound_tokens
    if initial is None or growth is None:
        return True
    ceiling = admission.context_ceiling_tokens
    if ceiling is not None and initial + request.dispatch_bytes + growth * (agent.expected_calls - 1) > ceiling:
        return True
    return False


def _ids(agent_name, message_id):
    turn = str(uuid.uuid5(mapper.NAMESPACE, "admit-turn|%s|%s" % (agent_name, message_id)))
    return str(uuid.uuid5(mapper.NAMESPACE, "admit|%s|%s" % (agent_name, message_id))), turn


def _body_text(request, agent, admission, now_ms):
    """The request body (DESIGN-6A 8.7), as the compact, sorted text that is stored and sent byte for byte."""
    request_id, turn = _ids(request.agent, request.message_id)
    initial = admission.fresh_overhead_bound_tokens if request.mode == "fresh" \
        else admission.resume_history_bound_tokens
    body = {"request_id": request_id, "turn_id": turn, "seat_id": agent.seat_id, "task_class": agent.task_class,
            "model_id": agent.model_id, "effort": agent.effort, "gateway_id": admission.gateway_id,
            "parent_lease_id": None,
            "context": {"mode": request.mode.upper(), "initial_tokens": initial,
                        "dispatch_tokens": request.dispatch_bytes, "source": "conservative_bound",
                        "observed_at": now_ms, "growth_tokens_per_call": admission.growth_bound_tokens},
            "expected_calls": agent.expected_calls, "max_calls": agent.max_calls, "output_cap": agent.output_cap,
            "per_call_output_cap": agent.per_call_output_cap, "ttl_ms": agent.ttl_ms}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _rebuilt_text(body_text, now_ms):
    return json.dumps(mapper.rebuild_body(json.loads(body_text), now_ms), sort_keys=True, separators=(",", ":"))


# --- what comes back ----------------------------------------------------------------------------------------------

def _grant_of(doc, body_text, now_ms):
    """Leasewise's `200` or `201` answer to the saved body `body_text` -> the grant to save, or None when it is not a
    paid grant at all (a lease of another kind, or an answer that is missing a field).

    * **The owner comes from the saved body**, never from today's configuration: the binding names the gateway that
      the request which won was written for, even if the alias was mapped elsewhere after a crash.
    * **Custody is separate from permission to run.** A replay can show a lease that has since expired (`uncertain`)
      or settled. It is still the lease of this message, so it is still saved (a close, a receipt and `recall` need
      it); its time is recorded as none, so `admit` answers `held` and the turn does not run."""
    lease_id, expires = doc.get("lease_id"), doc.get("expires_at")
    if type(lease_id) is not str or not _UUID.fullmatch(lease_id) or type(expires) is not int \
            or isinstance(expires, bool) or doc.get("enforcement") not in PAID \
            or doc.get("state") not in ("active", "uncertain", "settled"):
        return None
    try:
        gateway_id = json.loads(body_text)["gateway_id"]
    except (ValueError, KeyError, TypeError):
        return None
    if type(gateway_id) is not str or not _UUID.fullmatch(gateway_id):
        return None
    max_calls = max_micro_eur = None                  # metered-advisory: the gateway uses its own ledger ceiling
    if doc["enforcement"] == "metered":
        caps, hold = doc.get("caps"), doc.get("money_hold")
        if type(caps) is not dict or type(caps.get("max_calls")) is not int or type(hold) is not int:
            return None
        max_calls, max_micro_eur = caps["max_calls"], hold
    ttl = min((expires - now_ms) // 1000, wire.MAX_TTL) if doc["state"] == "active" else 0
    return {"lease_id": lease_id, "gateway_id": gateway_id, "ref_sha256": mapper.lease_ref_hash(lease_id),
            "max_calls": max_calls, "max_micro_eur": max_micro_eur, "ttl_seconds": max(ttl, 0), "expires_ms": expires}


def _post(env, row, body_text, deadline):
    """One POST of the stored bytes, with whatever time is left. -> (status, doc, code), or None when there is not
    enough time to start one."""
    left = deadline - env.monotonic()
    if left < MIN_POST_SECONDS:
        return None
    return env.client.post_acquisition(row["request_id"], body_text.encode("utf-8"), min(POST_TIMEOUT_SECONDS, left))


def _saved(env, writer, row, doc, body_text):
    """A paid grant is saved (once) and answered from what is stored. -> bytes, or None if it is not a paid grant."""
    grant = _grant_of(doc, body_text, env.now_ms())
    if grant is None:
        return None
    env.fault("granted")                                # Leasewise has granted; nothing is saved yet
    stored = writer.save_grant(row["id"], grant)
    env.fault("saved")                                  # saved; nothing is printed yet
    return _answer(stored)


def _granted(status, doc):
    return status in (200, 201) and doc is not None


# --- the flow -----------------------------------------------------------------------------------------------------

def _open(env, deadline):
    """Open the store for writing. Two helpers that create the file at the same moment can meet a lock that the
    database's own wait does not cover (switching a new file to WAL), so a busy open is tried again, inside the
    lock-wait bound and the run's deadline, before it is given up as `store_busy`. An attempt is only started when
    there is time to finish it, its wait is what is left, and every later wait follows the same deadline."""
    until = min(deadline, env.monotonic() + LOCK_WAIT_SECONDS)
    while True:
        left = until - env.monotonic()
        if left < MIN_OPEN_SECONDS:
            raise store.StoreBusy()
        try:
            writer = store.Writer(env.admission.store, left * 1000)
        except store.StoreBusy:
            env.sleep(min(admission_lock.POLL_SECONDS, max(0.0, until - env.monotonic())))
            continue
        writer.set_deadline(lambda: deadline - env.monotonic())
        return writer


def _admit(request, env, deadline):
    admission = env.admission
    agent = env.agents.get(request.agent)
    writer = None
    try:
        row = None
        if os.path.isfile(admission.store):
            writer = _open(env, deadline)
            row = writer.lookup(request.agent, request.message_id)
            if row is not None and row["state"] == "granted":
                return _answer(row)                     # grants never change: no lock, no Leasewise
        if row is None:
            # Only a NEW message is judged by today's settings. A request that is already saved is recovered from what
            # was saved, whatever the settings say now (configuration changes apply to new messages only).
            if agent is None or request.agent not in admission.gateway_agents:
                return wire.held("not_configured")
            if _unprovable(request, agent, admission):
                return wire.held("context_unprovable")
        elif agent is None:
            return wire.held("not_configured")           # nothing to rebuild a stale estimate from
        if writer is None:
            writer = _open(env, deadline)
        lock = admission_lock.KeyLock(admission.store, request.agent, request.message_id, env.monotonic, env.sleep)
        if not lock.acquire(min(deadline, env.monotonic() + LOCK_WAIT_SECONDS)):
            return wire.held("store_busy")
        try:
            return _locked(request, agent, env, writer, deadline)
        finally:
            lock.release()
    except (store.StoreBusy, store.StoreUnusable):
        return wire.held("store_busy")
    finally:
        if writer is not None:
            writer.close()


def _locked(request, agent, env, writer, deadline):
    row = writer.lookup(request.agent, request.message_id)
    if row is not None and row["state"] == "granted":
        return _answer(row)                             # another helper saved it while this one waited
    now_ms = env.now_ms()
    if row is None:
        request_id, _turn = _ids(request.agent, request.message_id)
        row = writer.begin_request(request.agent, request.message_id, request_id,
                                   _body_text(request, agent, env.admission, now_ms), now_ms)
        env.fault("request_saved")                      # the request is durable; nothing has been sent
    elif row["not_before_ms"] > now_ms and row["last_error_code"]:
        return wire.held(_reason(row["last_error_code"]))     # still backing off: do not ask Leasewise again yet
    if row["replay_tried"] >= 1:
        return _search(request, env, writer, row, deadline)   # an earlier run already learned the newest body conflicts
    while True:
        body_text = row["body_current"]
        sent = _post(env, row, body_text, deadline)
        if sent is None:
            return wire.held("store_busy")              # out of time: the progress is in the store
        status, doc, code = sent
        if status is None and deadline - env.monotonic() <= 0:
            return wire.held("store_busy")              # the deadline cut the exchange short: the progress is in the store
        env.fault("posted")                             # the answer is here; nothing is saved yet
        if _granted(status, doc):
            answer = _saved(env, writer, row, doc, body_text)
            return answer if answer is not None else _hold(env, writer, row, ERR_DENIED)
        if code == ERR_STALE and status is not None:
            if row["rebuilds"] >= MAX_REBUILDS:
                return _hold(env, writer, row, ERR_STALE)
            if writer.attempt_count(row["id"]) >= store.MAX_BODIES:
                # Over a long outage the saved bodies must not grow without bound. At the cap the message is held as
                # refused, shown in the status, and nothing is deleted.
                return _hold(env, writer, row, ERR_BODY_CAP, restart_recovery=False)
            writer.rebuild(row["id"], _rebuilt_text(body_text, env.now_ms()), env.now_ms())
            row = writer.lookup(request.agent, request.message_id)    # the new body is saved before it is sent
            continue
        if code == ERR_CONFLICT:
            return _search(request, env, writer, row, deadline)
        return _hold(env, writer, row, _failure_word(status, code))


def _failure_word(status, code):
    """The word kept for an answer that is not a grant: no usable answer (no status, a server fault, a refused
    identity, a rate limit with no word) is `UNREACHABLE`; a parked account is its own word; the rest are refusals."""
    if code == ERR_PARKED:
        return ERR_PARKED
    if status is None or status == 401 or status >= 500 or (status == 429 and code is None) or status < 400:
        return ERR_UNREACHABLE
    return code or "UNKNOWN_CODE"


def _search(request, env, writer, row, deadline):
    """The current body was told "same request, another body": an earlier body was granted. Replay the saved bodies,
    newest first (at most 4); the one Leasewise answers with a lease is the winning grant. Each body that Leasewise
    says no to is counted in the store, so a run that runs out of time continues where it stopped."""
    bodies = writer.attempts(row["id"], MAX_REPLAYS)
    tried = max(row["replay_tried"], 1)                 # the newest body is the one that just conflicted
    writer.mark_replayed(row["id"], tried)
    for index in range(tried, len(bodies)):
        sent = _post(env, row, bodies[index], deadline)
        if sent is None:
            return wire.held("store_busy")
        status, doc, code = sent
        if status is None and deadline - env.monotonic() <= 0:
            return wire.held("store_busy")
        env.fault("posted")
        if _granted(status, doc):
            answer = _saved(env, writer, row, doc, bodies[index])
            return answer if answer is not None else _hold(env, writer, row, ERR_DENIED)
        if code == ERR_CONFLICT:
            writer.mark_replayed(row["id"], index + 1)
            continue
        return _hold(env, writer, row, _failure_word(status, code), restart_recovery=False)
    return _hold(env, writer, row, ERR_CONFLICT, restart_recovery=False)       # none replays: nothing is guessed
