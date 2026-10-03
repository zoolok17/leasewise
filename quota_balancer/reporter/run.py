"""The reporter's loop: read the journal, then send what is due (card 7a-3).

A cycle is one snapshot round (`rounds.run_round`) followed by sending the
outbox, one request at a time, oldest message first. Everything is stored
before it is sent and again after the answer, in separate transactions, so a
stop or a crash at any point is safe: the next start sends the same bytes
with the same Idempotency-Key and Leasewise answers with what it already did.

`drain` is one cycle; `run` repeats it until told to stop.
"""

import random
import threading
import time

from . import decisions, events, rounds, status

# What a cycle ended with.
OK = "ok"
UNREACHABLE = "unreachable"         # no answer, 5xx or 429: try again later
CREDENTIAL = "credential"           # a 401: the identity must be fixed

MAX_SLEEP_SECONDS = 900


def _now_ms():
    return int(time.time() * 1000)


class CycleResult:
    __slots__ = ("new_events", "sent", "blocked", "journal_stopped")

    def __init__(self):
        self.new_events = 0
        self.sent = 0
        self.blocked = None             # None, UNREACHABLE or CREDENTIAL
        self.journal_stopped = None     # None or a closed reason word

    def as_dict(self):
        return {"new_events": self.new_events, "sent": self.sent,
                "blocked": self.blocked,
                "journal": "ok" if self.journal_stopped is None
                else "stopped: " + self.journal_stopped}


class Reporter:
    def __init__(self, config, agents, state, client, *, clock=_now_ms,
                 rng=random.random, stop=None, fault=None):
        self.config = config
        self.agents = agents            # {agent: mapper.AgentConfig}
        self.state = state
        self.client = client
        self.clock = clock
        self.rng = rng
        self.stop = stop or threading.Event()
        self.fault = fault              # test seam: fault(stage)
        self.failures = 0               # consecutive blocked cycles
        # An operator who fixed a configuration fault only has to restart.
        with state.txn():
            state.clear_agent_stops()

    # --- one cycle -----------------------------------------------------------

    def cycle(self):
        result = CycleResult()
        self._read(result)
        if not self.stop.is_set():
            self._send(result)
        self.failures = 0 if result.blocked is None else self.failures + 1
        status.write_snapshot(status.snapshot_path(self.state.path),
                              self.report())
        return result

    def report(self):
        return status.build_status(self.state, set(self.config.agents),
                                   self.clock(),
                                   self.config.silent_after_seconds)

    def _read(self, result):
        try:
            outcome = rounds.run_round(self.state, self.config.journal_root,
                                       self.agents, self.clock(),
                                       fault=self.fault)
        except events.UnsupportedVersion:
            reason = "schema_version"
        except events.JournalError:
            reason = "segment_shorter_than_cursor"
        else:
            result.new_events = outcome.new_events
            with self.state.txn():
                self.state.clear_note(status.journal_stopped_note())
            return
        # The round wrote nothing. Say so, loudly and durably.
        result.journal_stopped = reason
        with self.state.txn():
            self.state.set_note(status.journal_stopped_note(), reason,
                                self.clock())

    def _send(self, result):
        while not self.stop.is_set():
            now = self.clock()
            item = self.state.next_item(now)
            if item is None:
                return
            answer = self.client.send(item)
            if self.fault:
                self.fault("answered")
            now = self.clock()          # a rebuild is stamped after the wait
            decision = self.state.apply(item, answer, now, self.rng())
            with self.state.txn():
                if decision.action == decisions.DONE:
                    self.state.set_note(status.last_ok_note(item.agent), None,
                                        now)
                elif answer.status == 401:
                    self.state.set_note(status.last_401_note(), decision.code,
                                        now)
            if decision.action == decisions.DONE:
                result.sent += 1
            elif decision.action == decisions.RETRY:
                # Leasewise is not answering, or refuses the identity: every
                # other request would meet the same wall. Stop this cycle.
                result.blocked = CREDENTIAL if answer.status == 401 \
                    else UNREACHABLE
                return

    # --- the commands ----------------------------------------------------------

    def drain(self):
        """One pass: read, send what is due, return. -> CycleResult."""
        return self.cycle()

    def run(self, on_cycle=None):
        """Cycles until `stop` is set. Waits `poll_seconds`, or longer while
        Leasewise does not answer (the same 5 s .. 15 min back-off with
        jitter as every item)."""
        while not self.stop.is_set():
            result = self.cycle()
            if on_cycle:
                on_cycle(result)
            wait = self.config.poll_seconds
            if result.blocked is not None:
                wait = max(wait, decisions.backoff_ms(
                    self.failures, self.rng()) / 1000.0)
            self.stop.wait(min(wait, MAX_SLEEP_SECONDS))
