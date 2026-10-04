"""The money reader's cycle (card R1-READER step 3b, DESIGN-R1-READER.md 8 and 9): the loop that ties the parts together.

One pass, in the money reader's own lane (`Reporter._send_report`):

1. If the reader is halted: do nothing (counted `reader_halted`, or `state_unavailable` for a damaged state).
2. Check the state (the start-up check; the first ever start makes the reader's identity).
3. Send the waiting report, if there is one (`gwsend.send_pending`). Only when it was accepted does the pass go on; in every other case it
   stops, and the gateway is not read while a report is pending.
4. Run the gateway's `report` command through the contained runner.
5. Read the snapshot strictly (`gwreport.parse_snapshot`).
6. Read the receipt pages after the saved position, up to exactly the snapshot's own `N`.
7. Compute the figures (`gwreport.compute`) for the UTC month of the reader's own clock.
8. Build the version-4 bytes (`gwwire.build_report`) and, in one transaction, number and save them (`gwstate.mint`).
9. Send the new report.

A new report is made at most once per `interval_seconds` (lead decision D-217, `Pacer`); inside the interval only a waiting report is sent.
Any failure of steps 4 to 8 means NO report, never a partial one, and ONE closed word, counted. A word never carries a value from the report.
Nothing here replaces, rebuilds or drops an unaccepted report: a pending report is always sent first, as the same bytes.
"""

from . import gwreport, gwsend, gwstate, gwwire, receipts, runner

STATUS_FAILED = "status_failed"                  # the runner's word: it could not run, contain, finish, exit cleanly or be read
STATE_UNAVAILABLE = gwstate.STATE_UNAVAILABLE

# The closed words this pass can add to the money reader's counts (the others belong to the send side, `gwsend`).
CYCLE_WORDS = (STATUS_FAILED,) + gwreport.WORDS


class Pacer:
    """At most one new report per `interval` seconds (lead decision D-217), on a monotonic clock kept in memory only: a new process reports at once.
    It is told when a report was made, not when one was tried, so a failed read is tried again at the next cycle."""

    def __init__(self, interval, monotonic):
        self.interval, self.monotonic, self.last = interval, monotonic, None

    def due(self):
        return self.last is None or self.monotonic() - self.last >= self.interval

    def made(self):
        self.last = self.monotonic()


def count(state, word):
    """One more cycle that ended with `word`. A count is a number under the reader's own agent word; it keeps nothing else."""
    with state.txn():
        state.bump(gwstate.READER_AGENT, word)


def _snapshot(report, stop):
    """Step 4 and 5 -> a `gwreport.Snapshot`, or Refused. The command is run in the report mode: nothing is appended to it."""
    done = runner.run(list(report.command), timeout_seconds=report.timeout_seconds, max_bytes=report.max_bytes, env=report.env,
                      stop=stop, mode=runner.MODE_REPORT)
    if done.word != runner.OK or not done.cleanup_confirmed or done.data is None:
        raise gwreport.Refused(STATUS_FAILED)
    return gwreport.parse_snapshot(done.data)


def _totals(state, source, snapshot, stop):
    """Step 6 -> the receipts of the snapshot's generation read up to exactly its `N` (a `gwreport.Totals`), or Refused. Nothing is saved here:
    the new position is written only by the mint, so a failed read changes nothing and is simply repeated."""
    totals = gwstate.saved_totals(state, snapshot.generation)
    if totals.read_through > snapshot.through_seq:
        raise gwreport.Refused(gwreport.RECEIPTS_ROLLED_BACK)       # saved sums cannot be un-added
    while totals.read_through < snapshot.through_seq:
        read = receipts.read_page(source, totals.read_through, stop, month_rows=True)
        if read.status != receipts.OK or not read.cleanup_confirmed:
            raise gwreport.Refused(gwreport.RECEIPTS_FAILED)
        folded = gwreport.fold_page(totals, read.page, snapshot)    # another generation, a gap, a repeat... stop the read
        if folded.read_through <= totals.read_through:
            raise gwreport.Refused(gwreport.RECEIPTS_FAILED)        # the gateway promised receipts up to N and gave none
        totals = folded
    return totals


def make_report(state, report, source, clock, stop):
    """Steps 4 to 8 -> None when a new report was numbered and saved (pending), else the one closed word that says why not."""
    try:
        snapshot = _snapshot(report, stop)
        observed_at = clock()                        # the moment the reader has the snapshot: its UTC month is the report's month
        totals = _totals(state, source, snapshot, stop)
        figures = gwreport.compute(snapshot, totals, gwreport.month_of(observed_at))
    except gwreport.Refused as refused:
        return refused.word

    def build(incarnation, epoch, seq):
        return gwwire.build_report(figures, report.gateway_id, report.account_id, incarnation, epoch, seq, observed_at)
    try:
        gwstate.mint(state, snapshot.generation, build, totals, clock())
    except gwstate.Refused:
        return STATE_UNAVAILABLE                      # the state is not what a mint needs: the operator's `reset` is the way out
    return None


def run_lane(state, client, report, source, clock, stop, pacer):
    """One pass of the money reader -> `gwsend.Outcome` (the same value the send side gives, so the lane ranks it the same way)."""
    first = gwsend.send_pending(state, client, clock())
    if first.word not in (None, gwsend.ACCEPTED) or stop.is_set():
        return first                                  # steps 1 and 3: halted, or the waiting report was not accepted: stop here
    if not pacer.due():
        return first                                  # not yet time for a new report: nothing is written and no word is counted
    started = gwstate.start(state, clock())
    if started.halt is not None:
        word = STATE_UNAVAILABLE if started.halt == STATE_UNAVAILABLE else gwsend.READER_HALTED
        count(state, word)
        return gwsend.Outcome(word, first.sent, False)
    word = make_report(state, report, source, clock, stop)
    if word is not None:
        count(state, word)
        return gwsend.Outcome(word, first.sent, False)
    pacer.made()
    second = gwsend.send_pending(state, client, clock())             # step 9: the saved bytes, as the send side always sends them
    return gwsend.Outcome(second.word, first.sent + second.sent, second.unauthorized)
