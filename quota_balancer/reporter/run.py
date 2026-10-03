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

from . import admission_store, capture, decisions, events, receipts, rounds, status
from . import state as state_mod

# What a cycle ended with.
OK = "ok"
UNREACHABLE = "unreachable"         # no answer, 5xx or 429: try again later
CREDENTIAL = "credential"           # a 401: the identity must be fixed

MAX_SLEEP_SECONDS = 900
# While limit capture is configured the wait between cycles never exceeds this, even while Leasewise
# is not answering: the wrapper refreshes a park file about once a minute and a file counts as fresh
# for 300 seconds, so a longer wait could let a park start and end between two looks (decision D5).
LIMIT_CAPTURE_MAX_WAIT_SECONDS = 60
MAX_PAGES_PER_CYCLE = 6          # card L6a-1: pages (and generation switches) taken from a receipt source per cycle


def _worse(current, new):
    """The higher-ranked of two cycle outcomes: credential, then unreachable, then None. A login
    problem an operator must fix is never hidden by a later "the server is down"."""
    if CREDENTIAL in (current, new):
        return CREDENTIAL
    return UNREACHABLE if UNREACHABLE in (current, new) else None


def _now_ms():
    return int(time.time() * 1000)


class CycleResult:
    __slots__ = ("new_events", "sent", "blocked", "journal_stopped", "new_limit_events", "new_receipts")

    def __init__(self):
        self.new_limit_events = 0       # card 7c-2: limit events captured this cycle
        self.new_receipts = 0           # card L6a-1: gateway receipts stored this cycle
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
                 rng=random.random, stop=None, fault=None, limit_sources=(), gateway_receipts=None):
        self.config = config
        self.limit_sources = tuple(limit_sources)   # card 7c-2: resolved account map (may be empty)
        self.gateway_receipts = gateway_receipts    # card L6a-1: the resolved receipt source, or None
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
            self._capture(result)
        if not self.stop.is_set():
            self._receive(result)
        if not self.stop.is_set():
            self._send(result)
        # The back-off grows over consecutive blocked cycles. A cycle that
        # found nothing due (an item still waiting out its own back-off) is
        # neither: only a request that got through ends the streak.
        if result.blocked is not None:
            self.failures += 1
        elif result.sent:
            self.failures = 0
        status.write_snapshot(status.snapshot_path(self.state.path),
                              self.report())
        return result

    def report(self):
        return status.build_status(self.state, set(self.config.agents),
                                   self.clock(),
                                   self.config.silent_after_seconds,
                                   self.limit_sources, self.gateway_receipts)

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

    def _capture(self, result):
        """Notice the park markers of the configured folders and queue their limit events (card 7c-2).
        A fault anywhere in here is caught whole: counted, no text kept, and it never stops lease
        sending."""
        if not self.limit_sources:
            return
        try:
            result.new_limit_events = capture.run(self.state, self.limit_sources,
                                                  self.config.machine, self.clock())
        except Exception:               # noqa: BLE001 - nothing from the fault is kept
            try:
                self.state.note_capture_fault(self.clock())
            except Exception:           # noqa: BLE001 - even the count may fail; sending goes on
                pass

    def _receive(self, result):
        """The receipt custody chain's intake (card L6a-1, DESIGN-6A 8.3), once per cycle: copy the new bindings
        from the admission store (read-only; a missing or busy store means "read again later"), take pages from
        the source into immutable evidence (stage 1, even when no binding is known), then release every capture
        whose binding is now known (stage 2). A page that cannot be read or is refused stores nothing and moves
        nothing; only a closed word and a count are kept. A fault anywhere in here is caught whole: counted, no
        text kept, and it never stops sending."""
        source = self.gateway_receipts
        if source is None:
            return
        try:
            now = self.clock()
            self._copy_bindings(source, now)
            source_id = self.state.ensure_source(source.gateway, source.gateway_id, now)
            result.new_receipts = self._take_pages(source, source_id)
            self.state.release_waiting(self.clock())
            if self.fault:
                self.fault("released")          # stage 2 is durable: the delivery bodies and `pending`
        except Exception:               # noqa: BLE001 - nothing from the fault is kept
            try:
                with self.state.txn():
                    self.state.bump(state_mod.RECEIPT_AGENT, "read_fault")
            except Exception:           # noqa: BLE001 - even the count may fail; sending goes on
                pass

    def _copy_bindings(self, source, now):
        status, found = admission_store.read_bindings(source.admission_store)
        if status == admission_store.OK:
            self.state.copy_bindings(found, now)
        else:
            with self.state.txn():
                self.state.bump(state_mod.RECEIPT_AGENT, "store:" + found)
                self.state.set_note("receipt_store_read", found, now)

    def _take_pages(self, source, source_id):
        """Pages of this source, up to a few per cycle. A page from a generation other than the current one was
        asked for with another ledger's cursor: the generation is switched (a new one starts at 0, one seen
        before takes its kept cursor back) and the page is used only if it happens to start there, else read again."""
        added = 0
        for _ in range(MAX_PAGES_PER_CYCLE):
            generation, after = self.state.current_cursor(source_id)
            read = receipts.read_page(source, after)
            now = self.clock()
            if read.status != receipts.OK:
                self.state.note_receipt_read(read.status, read.reason, now)
                break
            page = read.page
            if self.fault:
                self.fault("read")              # the page is read and nothing of it is stored yet
            if generation is not None and page.generation != generation:
                after = self.state.switch_generation(source_id, page.generation, now)
                if page.after_seq != after:
                    continue                      # asked with the wrong cursor: nothing of it is taken
            added += self.state.capture_page(source_id, source.gateway_id, page, now)
            if self.fault:
                self.fault("captured")          # stage 1 is durable: the evidence and the cursor
            if not page.has_more:
                break
        return added

    def _send(self, result):
        """Three passes, in this order and SEPARATE: limit events (card 7c-2), then gateway receipts
        (card L6a-1), then leases. Limit events and receipts belong to no seat and no message, so
        nothing a lease answers (a retry, a 429 from a parked account, a refusal) may delay one; and
        each route can fail on its own while the others work, so a retry in one pass does not stop
        the next. Each pass stops on its own wall. `result.blocked` combines the outcomes by RANK, so
        the summary never depends on which pass ran last: a credential refusal (401) from any pass
        shows as CREDENTIAL, above UNREACHABLE (no answer, 5xx, 429), above nothing."""
        self._send_pass(result, self.state.next_limit_item)
        self._send_pass(result, self.state.next_receipt_item)
        self._send_pass(result, self.state.next_item)

    def _send_pass(self, result, pick):
        """Send what `pick` offers, one request at a time. True if the pass ran out of due items;
        False if it was stopped (told to stop, or blocked: result.blocked says which)."""
        while not self.stop.is_set():
            now = self.clock()
            item = pick(now)
            if item is None:
                return True
            answer = self.client.send(item)
            if self.fault:
                self.fault("answered")
            now = self.clock()          # a rebuild is stamped after the wait
            decision = self.state.apply(item, answer, now, self.rng())
            if self.fault:
                self.fault("applied")   # the answer and its notes are durable
            if decision.action == decisions.DONE:
                result.sent += 1
            elif decision.action == decisions.RETRY:
                # Leasewise is not answering, or refuses the identity: every other request of
                # THIS pass would meet the same wall, so this pass stops (the other pass still
                # runs). The summary keeps the worse of the two outcomes: credential above
                # unreachable.
                result.blocked = _worse(result.blocked, CREDENTIAL if answer.status == 401
                                        else UNREACHABLE)
                return False
        return False

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
            longest = MAX_SLEEP_SECONDS
            if self.limit_sources:
                longest = min(longest, LIMIT_CAPTURE_MAX_WAIT_SECONDS)
            self.stop.wait(min(wait, longest))
