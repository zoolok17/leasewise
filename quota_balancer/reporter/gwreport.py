"""The gateway reader's pure half (card R1-READER step 1): read one `gateway report` snapshot, add up the receipts, and compute the
figures Leasewise keeps for a month (DESIGN-R1-READER.md sections 5 and 6, and the lead's worked accounting example).

Nothing here touches a database, a clock, a file or a process, and nothing here sends anything. Step 1 builds only the parts that read and
compute; the state, the cycle and the sending come in steps 2 and 3. The command's key names come from agenttalk's merged head
`921c82d4` and sit in one table (`gwcontract.py`). Live use still waits for the later steps and for Codex's read.

Three rules run through it:

* **No guessing.** A missing, mistyped, out of range or unknown-version field, or damage in the figures, is a refusal with one closed word
  (`Refused.word`). Never a clamp, never a 0 for a figure that cannot be computed.
* **No names.** The snapshot may carry text the reader does not use. The reader keeps only numbers, states and hex digests, and a
  `Snapshot` shows nothing of itself when printed.
* **One snapshot, one boundary.** Receipts are used only up to the snapshot's own `child_receipts_through_seq` (`N`), in the snapshot's own
  generation, even when a page returns more.
"""

import datetime
import json
import re
from collections import namedtuple

from .. import money
from . import gwcontract as c

# The closed words a refusal can carry (shown in the reporter's status as counts, never with a value).
STATUS_SHAPE = "status_shape"                    # the snapshot is not what the contract allows (including an unknown version)
CHILD_CAP_NOT_READY = "child_cap_not_ready"
RECEIPTS_FAILED = "receipts_failed"              # the receipts were not read up to N
RECEIPTS_ROLLED_BACK = "receipts_rolled_back"    # the same generation now reports an N below the saved position
GENERATION_CHANGED = "generation_changed"        # a receipt page names another generation
PERIOD_MISMATCH = "period_mismatch"              # a period later than this month: the clocks disagree
RECEIPTS_EXCEED_SETTLED = "receipts_exceed_settled"
UNATTRIBUTED_UNAVAILABLE = "unattributed_unavailable"
AMOUNT_INVALID = "amount_invalid"                # a sum that is not an amount (an overflow): never clamped
WORDS = (STATUS_SHAPE, CHILD_CAP_NOT_READY, RECEIPTS_FAILED, RECEIPTS_ROLLED_BACK, GENERATION_CHANGED, PERIOD_MISMATCH,
         RECEIPTS_EXCEED_SETTLED, UNATTRIBUTED_UNAVAILABLE, AMOUNT_INVALID)

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_PERIOD = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")          # ASCII digits only: `\d` would also take other scripts' digits


class Refused(Exception):
    """A snapshot or a computation that must not become a report. Carries one closed word and nothing else."""

    def __init__(self, word):
        super().__init__(word)
        self.word = word


class Snapshot:
    """One checked `gateway report`: numbers, states and hex digests only. It prints nothing of itself."""

    __slots__ = ("version", "generation", "price_hash", "child_cap_hash", "periods", "unresolved", "through_seq", "unreceipted", "hold")

    def __repr__(self):
        return "Snapshot(<hidden>)"

    __str__ = __repr__


def _refuse():
    raise Refused(STATUS_SHAPE)


def _no_repeats(pairs):
    keys = [key for key, _value in pairs]
    if len(set(keys)) != len(keys):
        _refuse()
    return dict(pairs)


def _no_constant(_name):
    _refuse()                                    # NaN and the infinities are not numbers a report may carry


MAX_AMOUNT = 10 ** 12                            # agenttalk's bound for any stored amount (`RECEIPT_MAX_AMOUNT`)
MAX_SEQ = 2 ** 63 - 1                            # agenttalk's bound for a receipt number


def _whole(value, high):
    if type(value) is not int or not 0 <= value <= high:
        _refuse()
    return value


def _amount(value):
    """A whole number 0..10^12 (a bool, a float, a negative or a text is refused)."""
    return _whole(value, MAX_AMOUNT)


def _period(value):
    if type(value) is not str or _PERIOD.fullmatch(value) is None:
        _refuse()
    return value


def _need(row, *keys):
    """Every key the reader uses is PRESENT in the row before any value is read. A missing key is damage; only an explicit null is "no value"
    (a lookup with a default would read a missing `actual_micro_eur` as "no cost recorded yet" and change the figures)."""
    if any(key not in row for key in keys):
        _refuse()


def _rows(doc, key):
    rows = doc.get(key)
    if type(rows) is not list:
        _refuse()
    for row in rows:
        if type(row) is not dict:
            _refuse()
    return rows


def _months(doc, key, amount_key):
    """A list of {period, amount} rows -> {month: amount}. Both keys present, one row per month, in month order (agenttalk's own rule)."""
    found = {}
    for row in _rows(doc, key):
        _need(row, c.KEY_PERIOD, amount_key)
        month = _period(row[c.KEY_PERIOD])
        if month in found or (found and month < max(found)):
            _refuse()
        found[month] = _amount(row[amount_key])
    return found


def parse_snapshot(raw):
    """The bytes `gateway report` printed -> a Snapshot, or Refused. A strict, allowlisted projection: strict JSON (no repeated key, no
    constant such as NaN), the one accepted version, an exact check of every field it uses, and every other key ignored. A snapshot with
    `child_cap_ready` false, or a null `child_receipt_report_version` or `child_receipts_through_seq` (no lease binding), is
    `child_cap_not_ready`; every other fault is `status_shape`."""
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_repeats, parse_constant=_no_constant)
    except Refused:
        raise
    except (ValueError, RecursionError):                       # UnicodeError is a ValueError
        raise Refused(STATUS_SHAPE) from None
    if type(doc) is not dict:
        _refuse()
    version = doc.get(c.KEY_VERSION)
    if type(version) is not int or version != c.ACCEPTED_VERSION:
        _refuse()                                              # a new version is a refusal until the design is revised
    snap = Snapshot()
    snap.version = version
    # The readiness flag comes first: when it is false, the hash and every receipt figure are null by contract (nothing to check).
    ready = doc.get(c.KEY_CAP_READY)
    if type(ready) is not bool:
        _refuse()
    if not ready:
        raise Refused(CHILD_CAP_NOT_READY)
    # Receipt version and receipt number are null when lease binding is not installed: no lease can own money yet.
    if c.KEY_RECEIPT_VERSION not in doc or c.KEY_THROUGH not in doc:
        _refuse()                                              # absent is damage; only an explicit null means "not installed"
    receipt_version, through = doc[c.KEY_RECEIPT_VERSION], doc[c.KEY_THROUGH]
    if receipt_version is None or through is None:
        raise Refused(CHILD_CAP_NOT_READY)
    if type(receipt_version) is not int or receipt_version != c.ACCEPTED_RECEIPT_VERSION:
        _refuse()                                              # rows would not carry the month: the reader cannot use them
    snap.through_seq = _whole(through, MAX_SEQ)
    generation = doc.get(c.KEY_GENERATION)
    if type(generation) is not str or _HEX32.fullmatch(generation) is None:
        _refuse()
    snap.generation = generation
    for key in (c.KEY_PRICE_HASH, c.KEY_CAP_HASH):
        value = doc.get(key)
        if type(value) is not str or _HEX64.fullmatch(value) is None:
            _refuse()                                          # with the caps ready, neither hash may be null
    snap.price_hash, snap.child_cap_hash = doc[c.KEY_PRICE_HASH], doc[c.KEY_CAP_HASH]
    snap.periods = _months(doc, c.KEY_PERIODS, c.KEY_COMMITTED)
    unresolved = []
    for row in _rows(doc, c.KEY_UNRESOLVED):
        _need(row, c.KEY_STATE, c.KEY_RESERVED, c.KEY_ACTUAL)        # the cost may be null (not recorded yet) but never absent
        state, actual = row[c.KEY_STATE], row[c.KEY_ACTUAL]
        if type(state) is not str or state not in c.STATES:
            _refuse()
        unresolved.append((state, _amount(row[c.KEY_RESERVED]), None if actual is None else _amount(actual)))
    snap.unresolved = tuple(unresolved)
    snap.unreceipted = _months(doc, c.KEY_UNRECEIPTED, c.KEY_UNRECEIPTED_AMOUNT)
    hold = doc.get(c.KEY_HOLD)
    if type(hold) is not bool:
        _refuse()
    if c.KEY_HOLD_REASON not in doc:
        _refuse()
    reason = doc[c.KEY_HOLD_REASON]
    if hold != (reason is not None) or (reason is not None and (type(reason) is not str or reason not in c.HOLD_REASONS)):
        _refuse()                    # a hold has one closed word and no hold has none; checked, then dropped: never kept, never sent
    snap.hold = hold
    return snap


# --- the receipts ---------------------------------------------------------------------------------------------------------------

# What the reader keeps of the receipts it has read, per generation: the highest receipt number read (never above a snapshot's N), the
# number and the total of the receipts of each month (a receipt whose charges lie wholly in that month), and how many receipts belong to
# no month (the charges span months, or there was no charge): those are never part of any month's coverage.
Totals = namedtuple("Totals", "generation read_through by_period no_month")


def empty_totals(generation):
    return Totals(generation, 0, (), 0)


def _month_sums(totals):
    return {month: (count, total) for month, count, total in totals.by_period}


def check_watermark(totals, snapshot):
    """The saved position against the snapshot's N. Receipts already added cannot be un-added, so an N below the position (in the same
    generation) is `receipts_rolled_back`; a position below N means the receipts were not all read (`receipts_failed`)."""
    if totals.generation != snapshot.generation:
        raise Refused(GENERATION_CHANGED)
    if totals.read_through > snapshot.through_seq:
        raise Refused(RECEIPTS_ROLLED_BACK)
    if totals.read_through < snapshot.through_seq:
        raise Refused(RECEIPTS_FAILED)


def fold_page(totals, page, snapshot):
    """Add one checked receipt page (`receipts.Page`, read with month rows) to `totals` -> new Totals. Pure: `totals` is never changed.

    The page must name the snapshot's generation (`generation_changed` otherwise: the read stops and no report is sent). It must continue
    exactly where `totals` stops (a gap or a repeat is `receipts_failed`). Only receipts with a number up to the snapshot's N are added,
    even when the page returns more, and the position never passes N. A snapshot of the same generation with an N below the position is
    `receipts_rolled_back`. A receipt with no month adds to no month."""
    if page.generation != snapshot.generation or totals.generation != snapshot.generation:
        raise Refused(GENERATION_CHANGED)
    if totals.read_through > snapshot.through_seq:
        raise Refused(RECEIPTS_ROLLED_BACK)
    sums = _month_sums(totals)
    position, no_month = totals.read_through, totals.no_month
    for row in page.receipts:
        if not hasattr(row, "charge_period"):
            raise Refused(STATUS_SHAPE)                        # a row read in the existing shape has no month: never counted as "no month"
        if row.seq > snapshot.through_seq:
            break                                              # a receipt above N belongs to the next report
        if row.seq != position + 1:
            raise Refused(RECEIPTS_FAILED)
        position = row.seq
        month = row.charge_period
        if month is None:
            no_month += 1
        else:
            count, total = sums.get(month, (0, 0))
            sums[month] = (count + 1, money.add(total, row.actual_micro_eur))
    return Totals(totals.generation, position, tuple(sorted((m, n, t) for m, (n, t) in sums.items())), no_month)


def covered(totals, month):
    """The sum of the receipts read for `month` (0 when there are none). A receipt with no month is in no month."""
    return _month_sums(totals).get(month, (0, 0))[1]


# --- the figures ----------------------------------------------------------------------------------------------------------------

# The computed figures of one report, before the reader's own incarnation, epoch and number are added (steps 2 and 3).
Figures = namedtuple("Figures", "period_start period_end settled reserved uncertain unattributed held_over_reservation "
                                "reconciliation_required quiet receipts_seq generation price_hash child_cap_hash sized_ready")


def month_of(ms):
    """The UTC month ("YYYY-MM") of a time in milliseconds."""
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime("%Y-%m")


def month_bounds(month):
    """(start, end) of a month "YYYY-MM" in UTC milliseconds: the start of the month and the start of the next."""
    year, number = int(month[:4]), int(month[5:7])
    start = datetime.datetime(year, number, 1, tzinfo=datetime.timezone.utc)
    end = datetime.datetime(year + (number == 12), number % 12 + 1, 1, tzinfo=datetime.timezone.utc)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


QUIET_VERSIONS = (1,)                            # report versions that always carry `unreceipted_bound_actual`


def _checked(value):
    try:
        return money.check(value)
    except money.MoneyError:
        raise Refused(AMOUNT_INVALID) from None


def compute(snapshot, totals, month):
    """The figures of one report for `month` (the UTC month of the reader's own clock when the snapshot was read), exactly as the worked
    accounting example: `unattributed = committed(P) - covered(P) - U(P)`.

    * `settled` is `committed(P)`; with no row for P and every row earlier it is 0 (the snapshot is complete); a row for a LATER month
      is `period_mismatch` (the clocks disagree).
    * `reserved` and `uncertain` count every unresolved attempt, whatever its month; an `uncertain` attempt with a recorded cost is
      counted by `held_over_reservation` only (its cost is already inside `settled`).
    * `covered(P)` uses only receipts up to the snapshot's N, in its generation, of month P. A receipt with no month is in no month.
    * `unattributed` below 0 is `receipts_exceed_settled`, never clamped. A snapshot with no `unreceipted_bound_actual` figure cannot make
      the figure exact (`unattributed_unavailable`).
    * `quiet` is derived from the report version (version 1 always carries `unreceipted_bound_actual`), never hard-wired.
    """
    if _PERIOD.fullmatch(month) is None:
        raise Refused(STATUS_SHAPE)
    check_watermark(totals, snapshot)
    if snapshot.unreceipted is None:
        raise Refused(UNATTRIBUTED_UNAVAILABLE)
    if any(other > month for other in snapshot.periods):
        raise Refused(PERIOD_MISMATCH)
    settled = snapshot.periods.get(month, 0)
    reserved = _checked(money.add(*(r for s, r, _a in snapshot.unresolved if s == "reserved")))
    uncertain = _checked(money.add(*(r for s, r, a in snapshot.unresolved if s == "uncertain" and a is None)))
    held = sum(1 for s, _r, a in snapshot.unresolved if s == "uncertain" and a is not None)
    owed = covered(totals, month) + snapshot.unreceipted.get(month, 0)
    if owed > settled:
        raise Refused(RECEIPTS_EXCEED_SETTLED)
    start, end = month_bounds(month)
    return Figures(start, end, settled, reserved, uncertain, settled - owed, held, snapshot.hold,
                   snapshot.version in QUIET_VERSIONS, snapshot.through_seq, snapshot.generation, snapshot.price_hash,
                   snapshot.child_cap_hash, False)
