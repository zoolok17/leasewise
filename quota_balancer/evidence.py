"""The rules that decide what a gateway report means for trust (card R1-3c, DESIGN-R1-3.md sections 3 and 4).

Pure functions: no database, no clock, no file. The store reads the facts a report needs, calls `assess`, and writes what comes back
(`store._apply_assessment`), once, at ingest. Nothing here is ever recomputed from history, and nothing here resolves or re-opens a
transition: an operator's adoption resolves them (card R1-3d), and a row that was resolved is never written again because the store
writes a row only for the report that shows it.

The order of the rules is the design:

1. A report from a RETIRED reader is only counted. It creates no transition of any kind.
2. IDENTITY BEFORE ORDER: the same (gateway, incarnation, epoch, number) with different content is a `sequence_conflict`, whatever
   arrived first; the same triple with another generation is an `epoch_generation_mismatch`.
3. Then order, inside one reader: a duplicate, an older epoch or period of the current reader, a late (lower) number.
4. Then, for a report that is in order in the current chain: the changes it shows (`reported_by_gateway`, `epoch_changed`, the two
   fingerprints, `figure_fell`, `unattributed_rose`), and the automatic month roll, which looks at the report only after every one of
   those was decided, so a report that says "reconciliation required" can never roll the month.

`unattributed` is compared on QUIET reports only (4.1, 4.3): a leased turn in flight raises it and its receipt lowers it again.
"""

from collections import namedtuple

from . import money

KIND_READER_CHANGED = "reader_changed"
KIND_EPOCH_CHANGED = "epoch_changed"
KIND_EPOCH_GENERATION_MISMATCH = "epoch_generation_mismatch"
KIND_PRICE_HASH_CHANGED = "price_hash_changed"
KIND_CHILD_CAP_HASH_CHANGED = "child_cap_hash_changed"
KIND_FIGURE_FELL = "figure_fell"
KIND_UNATTRIBUTED_ROSE = "unattributed_rose"
KIND_SEQUENCE_CONFLICT = "sequence_conflict"
KIND_REPORTED_BY_GATEWAY = "reported_by_gateway"

# The kinds that describe a STATE (a changed fingerprint, a new epoch, a gateway that says it is on hold, a rise that was never taken in).
# One is open at a time: while one is open, the next report that shows the same state adds nothing. The others describe an EVENT
# (a fall, a collision, a new reader) and are written for each report that shows them.
LATCHED_KINDS = frozenset({KIND_EPOCH_CHANGED, KIND_PRICE_HASH_CHANGED, KIND_CHILD_CAP_HASH_CHANGED,
                           KIND_UNATTRIBUTED_ROSE, KIND_REPORTED_BY_GATEWAY})

# The standing of a report (what the order rules decided).
STANDING_RETIRED = "retired"          # from a retired reader: counted only
STANDING_CONFLICT = "conflict"        # collides with a stored report of the same identity
STANDING_DUPLICATE = "duplicate"      # the same identity and content under another event id
STANDING_OLD_CHAIN = "old_chain"      # an older epoch of the current reader
STANDING_OLD_PERIOD = "old_period"    # an older month than the chain's
STANDING_LATE = "late"                # a lower number than one already stored in its own epoch
STANDING_UNCHAINED = "unchained"      # from a reader that is not the current one: kept, and judged by no chain
STANDING_IN_ORDER = "in_order"

# The counters of `gateway_chain_state` that a standing adds to.
COUNTERS = {
    STANDING_RETIRED: "retired_reader_reports",
    STANDING_DUPLICATE: "duplicate_reports",
    STANDING_OLD_CHAIN: "old_chain_reports",
    STANDING_OLD_PERIOD: "old_chain_reports",
    STANDING_LATE: "late_reports",
}

READER_PENDING = "pending"
READER_CURRENT = "current"
READER_RETIRED = "retired"

# A version-4 report as the rules see it. `content` is everything that is the report's own figure (not its event id, its generation,
# or when it was seen), so two reports with the same identity can be told apart by what they say.
Report = namedtuple("Report", "event_id incarnation epoch generation seq period_start period_end settled reserved uncertain "
                              "unattributed receipts_seq quiet held_over_reservation price_hash child_cap_hash flag")
# The adoption the chain starts at: the baseline report's own figures.
Adoption = namedtuple("Adoption", "incarnation epoch generation period_start seq settled unattributed price_hash child_cap_hash")
# What the store knows when the report arrives, before the report is stored.
#   reader        the state of the report's incarnation in `gateway_readers`, or None if it has no row
#   adoption      the current adoption, or None
#   generation    the generation recorded for the report's (incarnation, epoch), or None if that pair is new
#   same_seq      the contents of the stored reports with the report's own (incarnation, epoch, number)
#   max_seq       the highest number stored for the report's (incarnation, epoch), or None
#   previous      the settled figure of the previous in-order report of the chain (the adoption's own baseline if none), or None
#   last_quiet    `unattributed` of the previous quiet report of the chain, or None
#   open_kinds    the kinds of every transition of the gateway that is still open
#   approved      whether the report's pair of fingerprints is approved
State = namedtuple("State", "reader adoption generation same_seq max_seq previous last_quiet open_kinds approved")
# What to write. `transitions` is a tuple of (kind, conflict) with conflict None or (incarnation, epoch, number);
# a `reader_changed` is about the report's own incarnation.
Assessment = namedtuple("Assessment", "standing transitions counter create_reader remember_quiet roll")


def content(report):
    """The figures a report states, as one comparable value: what makes a second report with the same identity the same or different."""
    return (report.period_start, report.period_end, report.settled, report.reserved, report.uncertain, report.unattributed,
            report.receipts_seq, report.quiet, report.held_over_reservation, report.price_hash, report.child_cap_hash, report.flag)


def _amount(value):
    """A figure as money, or `money.UNKNOWN` when it is not a whole number in range. Never zero."""
    try:
        return money.check(value)
    except money.MoneyError:
        return money.UNKNOWN


def _fell(earlier, later):
    """True only when both figures are known and the later one is lower. An unknown figure is never read as zero and never proves a fall."""
    earlier, later = _amount(earlier), _amount(later)
    return money.is_known(earlier) and money.is_known(later) and later < earlier


def _rose(baseline, later):
    baseline, later = _amount(baseline), _amount(later)
    return money.is_known(baseline) and money.is_known(later) and later > baseline


def assess(report, state):
    """Decide what a report means. See the module text for the order. Pure: the same inputs always give the same answer."""
    if state.reader == READER_RETIRED:
        return Assessment(STANDING_RETIRED, (), COUNTERS[STANDING_RETIRED], False, False, False)

    adoption = state.adoption
    ours = adoption is not None and adoption.incarnation == report.incarnation       # the current adoption's reader
    transitions = []
    # A reader never seen before is kept as `pending`. It raises `reader_changed` only if an adoption already exists to differ from.
    create_reader = state.reader is None and not ours
    if create_reader and adoption is not None:
        transitions.append((KIND_READER_CHANGED, None))

    mine = (report.incarnation, report.epoch, report.seq)
    if state.generation is not None and state.generation != report.generation:
        transitions.append((KIND_EPOCH_GENERATION_MISMATCH, mine))

    # Identity before order: a collision is a conflict even when a newer report arrived first.
    mine_content = content(report)
    if any(other != mine_content for other in state.same_seq):
        transitions.append((KIND_SEQUENCE_CONFLICT, mine))
        return Assessment(STANDING_CONFLICT, tuple(transitions), None, create_reader, False, False)
    if state.same_seq:
        return Assessment(STANDING_DUPLICATE, tuple(transitions), COUNTERS[STANDING_DUPLICATE], create_reader, False, False)

    late = state.max_seq is not None and report.seq < state.max_seq
    if not ours:
        standing = STANDING_LATE if late else STANDING_UNCHAINED
        return Assessment(standing, tuple(transitions), COUNTERS.get(standing), create_reader, False, False)

    # From here on the report is from the current reader.
    if report.epoch < adoption.epoch:
        return Assessment(STANDING_OLD_CHAIN, tuple(transitions), COUNTERS[STANDING_OLD_CHAIN], create_reader, False, False)
    if report.period_start < adoption.period_start:
        return Assessment(STANDING_OLD_PERIOD, tuple(transitions), COUNTERS[STANDING_OLD_PERIOD], create_reader, False, False)
    if late:
        return Assessment(STANDING_LATE, tuple(transitions), COUNTERS[STANDING_LATE], create_reader, False, False)

    def raise_(kind, conflict=None):
        if kind in LATCHED_KINDS and kind in state.open_kinds:
            return                                                       # one open at a time
        if not any(found[0] == kind for found in transitions):
            transitions.append((kind, conflict))

    # What this in-order report shows. `reported_by_gateway` first: a gateway that says it is on hold is decided before any roll.
    if report.flag:
        raise_(KIND_REPORTED_BY_GATEWAY)
    if report.epoch > adoption.epoch:
        raise_(KIND_EPOCH_CHANGED)
    if report.price_hash != adoption.price_hash:
        raise_(KIND_PRICE_HASH_CHANGED)
    if report.child_cap_hash != adoption.child_cap_hash:
        raise_(KIND_CHILD_CAP_HASH_CHANGED)

    same_chain = (report.epoch == adoption.epoch and report.generation == adoption.generation
                  and report.period_start == adoption.period_start)
    remember_quiet = False
    if same_chain:
        if _fell(state.previous, report.settled):                        # `settled` only grows, so this is checked on every report
            raise_(KIND_FIGURE_FELL)
        if report.quiet:
            remember_quiet = True
            if _fell(state.last_quiet, report.unattributed):             # `unattributed` is compared on quiet reports only
                raise_(KIND_FIGURE_FELL)
            if _rose(adoption.unattributed, report.unattributed):        # the baseline is the adopted report itself
                raise_(KIND_UNATTRIBUTED_ROSE)

    # The automatic month roll: only the first quiet report of a new month, same reader, epoch and generation, the approved pair,
    # and nothing open (including whatever this very report just raised).
    roll = (report.quiet and report.period_start > adoption.period_start and report.epoch == adoption.epoch
            and report.generation == adoption.generation and report.price_hash == adoption.price_hash
            and report.child_cap_hash == adoption.child_cap_hash and state.approved
            and not state.open_kinds and not transitions
            and money.is_known(_amount(report.settled)) and money.is_known(_amount(report.unattributed)))
    return Assessment(STANDING_IN_ORDER, tuple(transitions), None, create_reader, remember_quiet, bool(roll))
