"""The reporter's loop: read the journal, then send what is due (card 7a-3).

A cycle is one snapshot round (`rounds.run_round`) followed by sending the
outbox, one request at a time, oldest message first. Everything is stored
before it is sent and again after the answer, in separate transactions, so a
stop or a crash at any point is safe: the next start sends the same bytes
with the same Idempotency-Key and Leasewise answers with what it already did.

`drain` is one cycle; `run` repeats it until told to stop.
"""

import contextlib
import random
import threading
import time

from . import admission_store, capture, decisions, events, gwcycle, gwsend, gwstate, receipts, rounds, status
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
RECEIPT_BUDGET_FLOOR = 30       # card L6a-1b: seconds of command reading per cycle, at least (checked before a read starts)
MAX_GRANT_BATCHES = 20          # card L6a-2: batches of admission grants (500 each) copied per cycle
UNKNOWN_CAP = "unknown"             # the note when the summary could not be read (never a fresh zero)


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
                 rng=random.random, stop=None, fault=None, limit_sources=(), gateway_receipts=None, gateway_report=None):
        self.config = config
        self.limit_sources = tuple(limit_sources)   # card 7c-2: resolved account map (may be empty)
        self.gateway_receipts = gateway_receipts    # card L6a-1: the resolved receipt source, or None
        # Card R1-READER step 3b: the money reader's report command. It works only with a command receipt source of the same gateway
        # (the configuration refuses anything else); without the table the reader does nothing and the lane only sends a waiting report.
        self.gateway_report = gateway_report if gateway_receipts is not None else None
        # Lead decision D-217: a new report at most once per `interval_seconds`, counted on the monotonic clock in memory (a restart reports at once).
        self.pacer = None if self.gateway_report is None else gwcycle.Pacer(self.gateway_report.interval_seconds, lambda: self.monotonic())
        # Card L6a-2 (DESIGN-6A 8.5): the agents whose turns the gateway admits. The journal pipeline never acquires
        # or reports for them (their journal observations are still kept).
        admission = getattr(config, "admission", None)
        self.gateway_agents = frozenset(admission.gateway_agents) if admission is not None else frozenset()
        self.agents = agents            # {agent: mapper.AgentConfig}
        self.state = state
        self.client = client
        self.clock = clock
        self.rng = rng
        self.stop = stop or threading.Event()
        self.fault = fault              # test seam: fault(stage)
        self.failures = 0               # consecutive blocked cycles
        self.monotonic = time.monotonic     # the cycle's receipt budget (a test replaces it with a virtual clock)
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
            self._summarize()
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
                                   self.limit_sources, self.gateway_receipts, self.gateway_agents)

    def _read(self, result):
        try:
            journal_agents = {name: one for name, one in self.agents.items() if name not in self.gateway_agents}
            outcome = rounds.run_round(self.state, self.config.journal_root,
                                       journal_agents, self.clock(),
                                       fault=self.fault, suppressed=self.gateway_agents)
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

    def _summarize(self):
        """The admission summary (the store's state and how many messages are held at the body cap) is refreshed
        whenever `[admission]` is configured, with or without receipt intake. When receipts are configured too, the
        one checked store path in `_copy_bindings` already did it this cycle. A fault here is caught whole."""
        admission = getattr(self.config, "admission", None)
        if admission is None or self.gateway_receipts is not None:
            return
        with contextlib.suppress(Exception):    # nothing from the fault is kept
            self._refresh_summary(admission.store, self.clock())

    def _refresh_summary(self, store_path, now):
        """Read the summary and note it. A read that fails notes the store's closed state and marks the count
        `unknown`: a failed read never shows as a fresh zero."""
        status, found = admission_store.read_summary(store_path)
        with self.state.txn():
            if status == admission_store.OK:
                self.state.set_note("receipt_store_read", admission_store.OK, now)
                self.state.set_note(state_mod.BODY_CAP_NOTE, str(found["at_body_cap"]), now)
            else:
                self.state.bump(state_mod.RECEIPT_AGENT, "store:" + found)
                self.state.set_note("receipt_store_read", found, now)
                self.state.set_note(state_mod.BODY_CAP_NOTE, UNKNOWN_CAP, now)

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
        """Copy new grants in bounded batches, after the number of the last one taken (kept in the state, saved
        with each batch), so no history size can stop the reader and nothing is read twice. A batch with a damaged
        row or a row whose reference or hash does not belong to its lease copies nothing and the store shows as
        unreadable. Then journal rows of newly bound messages are set aside, and the number of messages held at the
        body cap is noted (a count only)."""
        for _ in range(MAX_GRANT_BATCHES):
            status, found = admission_store.read_bindings(source.admission_store, self.state.grant_cursor(),
                                                          admission_store.BATCH)
            if status != admission_store.OK:
                with self.state.txn():
                    self.state.bump(state_mod.RECEIPT_AGENT, "store:" + found)
                    self.state.set_note("receipt_store_read", found, now)
                    self.state.set_note(state_mod.BODY_CAP_NOTE, UNKNOWN_CAP, now)     # not read: never a fresh zero
                return
            with self.state.txn():
                self.state.set_note("receipt_store_read", admission_store.OK, now)      # trouble is over: say so
            if found:
                self.state.copy_bindings(found, now, cursor=found[-1].grant_seq)
            if len(found) < admission_store.BATCH:
                break
        self.state.supersede_bound(now)
        self._refresh_summary(source.admission_store, now)

    def _take_pages(self, source, source_id):
        """Pages of this source, up to a few per cycle. A page from a generation other than the current one was
        asked for with another ledger's cursor: the generation is switched (a new one starts at 0, one seen
        before takes its kept cursor back) and the page is used only if it happens to start there, else read again.

        A command source has a time budget for the whole cycle, `max(timeout_seconds, 30)` seconds, checked only BEFORE a
        read starts: once it is spent the cycle takes no more pages (the next cycle goes on from the cursor), and the send
        passes run. A read that starts just before the budget ends runs for its own deadline, so receipt reading can take
        about `max(t, 30) + t + 2` seconds (52 at the default) with normally returning Windows calls."""
        added = 0
        budget_end = None
        if source.command is not None:
            budget_end = self.monotonic() + max(source.timeout_seconds, RECEIPT_BUDGET_FLOOR)
        for _ in range(MAX_PAGES_PER_CYCLE):
            if budget_end is not None and self.monotonic() >= budget_end:
                break
            generation, after = self.state.current_cursor(source_id)
            # Card R1-RC: a gateway at agenttalk 921c82d4 or later prints the month on every row, an older one does not; this path reads
            # both and keeps neither the month nor any other new field (`state.capture_page` stores the same fields as before).
            read = receipts.read_page(source, after, self.stop, month_rows=receipts.EITHER)
            now = self.clock()
            if source.command is not None:
                self.state.note_receipt_cleanup(read.cleanup_confirmed, now)
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
        self._send_report(result)
        self._send_pass(result, self.state.next_limit_item)
        self._send_pass(result, self.state.next_receipt_item)
        self._send_pass(result, lambda now: self.state.next_item(now, self.gateway_agents))

    def _send_report(self, result):
        """The money reader's pending report (card R1-READER step 3a), in its OWN lane: it is sent here, first, and an answer that needs a retry
        stops nothing else; the other passes run whatever happened here, and a retry in any of them never stops this one. At most one report is
        pending (the state allows no second row), and a new one is minted only after this one is accepted. With nothing pending this is one
        cheap read and nothing else."""
        if self.stop.is_set():
            return
        try:
            if self.gateway_report is None:
                outcome = gwsend.send_pending(self.state, self.client, self.clock())
            else:
                outcome = gwcycle.run_lane(self.state, self.client, self.gateway_report, self.gateway_receipts, self.clock, self.stop, self.pacer)
        except Exception:    # noqa: BLE001 - the money reader's lane boundary: nothing in this lane may stop the reporter or the other lanes
            self._count_lane_failure()                  # only a closed word is kept: no message, no value, no trace
            return
        result.sent += outcome.sent
        if outcome.word == gwsend.SEND_FAILED:
            result.blocked = _worse(result.blocked, CREDENTIAL if outcome.unauthorized else UNREACHABLE)

    def _count_lane_failure(self):
        """Count one `state_unavailable` for the money reader's lane. If even that cannot be written, it is dropped: the lane never ends the cycle."""
        try:
            with self.state.txn():
                self.state.bump(gwstate.READER_AGENT, gwstate.STATE_UNAVAILABLE)
        except Exception:    # noqa: BLE001 - same boundary
            return

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
