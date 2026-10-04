"""What the stored gateway evidence says right now (card R1-3d, DESIGN-R1-3.md 2.2, 2.4 and 8.3).

Everything here READS: it takes an open connection inside a transaction the caller owns and never writes. Three things are answered
from the tables the ingest wrote (`gateway_totals`, `gateway_adoptions`, `gateway_readers`, `gateway_transitions`,
`gateway_chain_state`, `gateway_approvals`):

* `adopt_blockers`: which of the ten items of the adoption predicate (2.2) fail for one named report. The adopting command runs it
  inside its write transaction, and `status` runs it for the newest report, so the two can never disagree.
* `usable`: whether the evidence of a gateway may be leaned on now (2.4), and the chain it would be read from.
* `project`: the `evidence` object of `status` (8.3), and `inputs`: the evaluator's gateway input built from the usable chain.

The evidence is read ONLY from the adopted chain. The legacy report columns of `gateways` (last_seq, ledger_epoch, state, ...) are
never read here: they still run for older readings, and a report that is not in the adopted chain must not become usable through them.
No amount, hash, name or path ever appears in a word; the closed words below are all a caller gets about a failure.
"""

import datetime
import json
import re

from . import money, receipt_coverage

FRESH_MS = 120_000                  # a report is fresh for 120 s (the item 4 of the predicate; also the evaluator's bracket)

# The ten items of the predicate, each with the closed word `status` shows when it fails. The order is the design's.
NOT_BOUND = "not_bound"
INVALID = "invalid"
NOT_CURRENT_PERIOD = "not_current_period"
NOT_FRESH = "not_fresh"
NOT_APPROVED = "not_approved"
IDENTITY_CONFLICT = "identity_conflict"
READER_RETIRED = "reader_retired"
NOT_NEWEST = "not_newest"
CONTESTED = "contested"
GATEWAY_NOT_READY = "gateway_not_ready"
OPEN_TRANSITIONS_UNNAMED = "open_transitions_unnamed"
NOT_QUIET = "not_quiet"
BLOCKERS = (NOT_BOUND, INVALID, NOT_CURRENT_PERIOD, NOT_FRESH, NOT_APPROVED, IDENTITY_CONFLICT, READER_RETIRED, NOT_NEWEST,
            CONTESTED, GATEWAY_NOT_READY, OPEN_TRANSITIONS_UNNAMED, NOT_QUIET)

# The closed states of `status`. `none` is the state of a gateway with no version-4 report; `status` leaves the whole block out then
# (like `allocated_above_int64`), so it is listed here and never printed.
STATE_NONE = "none"
STATE_UNAPPROVED = "unapproved"
STATE_UNADOPTED = "unadopted"
STATE_USABLE = "usable"
STATE_STALE = "stale"
STATE_PERIOD_MISMATCH = "period_mismatch"
STATE_TRANSITION_OPEN = "transition_open"
STATES = (STATE_NONE, STATE_UNAPPROVED, STATE_UNADOPTED, STATE_USABLE, STATE_STALE, STATE_PERIOD_MISMATCH, STATE_TRANSITION_OPEN)

# Section 6, fixed text: no amount, hash or id in it.
ACTION_SENTENCE = ("The gateway has a change that needs your decision. Run `status` to list the open items, "
                   "then adopt a current report with `gateway-adopt`.")

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")



def current_period(now_ms):
    """(start, end) of the UTC month `now_ms` is in, in milliseconds."""
    now = datetime.datetime.fromtimestamp(now_ms // 1000, datetime.timezone.utc)
    start = datetime.datetime(now.year, now.month, 1, tzinfo=datetime.timezone.utc)
    end = datetime.datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=datetime.timezone.utc)
    return int(start.timestamp()) * 1000, int(end.timestamp()) * 1000


def _rows(cursor):
    """The rows of a `SELECT * FROM gateway_totals` cursor as dicts keyed by column name."""
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, values, strict=True)) for values in cursor.fetchall()]


def stored_report(conn, gateway_id, event_id):
    """One stored version-4 report of this gateway as a dict, or None."""
    found = _rows(conn.execute("SELECT * FROM gateway_totals WHERE gateway_id=? AND event_id=?", (gateway_id, event_id)))
    return found[0] if found else None


def newest_report(conn, gateway_id):
    """The newest stored version-4 report of the gateway (the last one that arrived, whoever sent it), or None."""
    found = _rows(conn.execute("SELECT * FROM gateway_totals WHERE gateway_id=? ORDER BY rowid DESC LIMIT 1", (gateway_id,)))
    return found[0] if found else None


def open_transition_ids(conn, gateway_id):
    return [r[0] for r in conn.execute("SELECT id FROM gateway_transitions WHERE gateway_id=? AND resolved_by_adoption IS NULL "
                                       "ORDER BY id", (gateway_id,))]


def _approved(conn, gateway_id, report):
    return conn.execute("SELECT 1 FROM gateway_approvals WHERE gateway_id=? AND price_hash=? AND child_cap_hash=?",
                        (gateway_id, report["price_hash"], report["child_cap_hash"])).fetchone() is not None


def _valid(report):
    """Item 2: every amount whole and in range, `unattributed` inside `settled`, both hashes 64 hex, both identities 32 hex. Nothing is
    read as 0 when it is missing."""
    try:
        amounts = [money.check(report[key]) for key in ("settled_micro_eur", "reserved_micro_eur", "uncertain_micro_eur",
                                                          "unattributed_micro_eur")]
    except money.MoneyError:
        return False
    return (amounts[3] <= amounts[0]
            and all(type(report[key]) is str and _HEX64.fullmatch(report[key]) for key in ("price_hash", "child_cap_hash"))
            and all(type(report[key]) is str and _HEX32.fullmatch(report[key]) for key in ("generation", "reader_incarnation")))


def adopt_blockers(conn, gateway_id, event_id, named, now_ms):
    """The items of the adoption predicate (2.2) that FAIL for the report `event_id`, as closed words in the design's order; empty means
    adoptable. `named` is the set of transition ids the operator names (None: nothing named, which is what `status` asks). Reads only."""
    report = stored_report(conn, gateway_id, event_id)
    owner = conn.execute("SELECT g.machine_id, g.account_id FROM gateways g WHERE g.id=?", (gateway_id,)).fetchone()
    sender = conn.execute("SELECT machine_id, account_id, kind FROM readings WHERE event_id=?", (event_id,)).fetchone()
    if report is None or owner is None or sender is None or sender[2] != "gateway_snapshot" or tuple(sender[:2]) != tuple(owner):
        return [NOT_BOUND]                                           # nothing else can be judged about a report that is not this gateway's
    failed = set()
    if not _valid(report):
        failed.add(INVALID)
    if (report["period_start"], report["period_end"]) != current_period(now_ms):
        failed.add(NOT_CURRENT_PERIOD)
    if not 0 <= now_ms - report["observed_at"] <= FRESH_MS:
        failed.add(NOT_FRESH)
    if not _approved(conn, gateway_id, report):
        failed.add(NOT_APPROVED)
    recorded = conn.execute("SELECT generation FROM gateway_epochs WHERE gateway_id=? AND reader_incarnation=? AND ledger_epoch=?",
                            (gateway_id, report["reader_incarnation"], report["ledger_epoch"])).fetchone()
    if recorded is not None and recorded[0] != report["generation"]:
        failed.add(IDENTITY_CONFLICT)
    state = conn.execute("SELECT state FROM gateway_readers WHERE gateway_id=? AND reader_incarnation=?",
                         (gateway_id, report["reader_incarnation"])).fetchone()
    if state is not None and state[0] == "retired":
        failed.add(READER_RETIRED)
    highest = conn.execute("SELECT MAX(seq) FROM gateway_totals WHERE gateway_id=? AND reader_incarnation=? AND ledger_epoch=?",
                           (gateway_id, report["reader_incarnation"], report["ledger_epoch"])).fetchone()[0]
    if highest is not None and highest > report["seq"]:
        failed.add(NOT_NEWEST)
    elif conn.execute("SELECT 1 FROM gateway_adoptions WHERE event_id=?", (event_id,)).fetchone() is not None:
        failed.add(NOT_NEWEST)                                            # already adopted (by another operator): nothing newer to adopt
    contested = conn.execute(
        "SELECT MAX(conflict_seq) FROM gateway_transitions WHERE gateway_id=? AND resolved_by_adoption IS NULL "
        "AND kind IN ('sequence_conflict','epoch_generation_mismatch') AND conflict_incarnation=? AND conflict_epoch=?",
        (gateway_id, report["reader_incarnation"], report["ledger_epoch"])).fetchone()[0]
    if contested is not None and report["seq"] <= contested:
        failed.add(CONTESTED)
    if report["gateway_flag"]:
        failed.add(GATEWAY_NOT_READY)
    if set(open_transition_ids(conn, gateway_id)) != set(named or ()):
        failed.add(OPEN_TRANSITIONS_UNNAMED)
    if not report["quiet"]:
        failed.add(NOT_QUIET)
    return [word for word in BLOCKERS if word in failed]


def usable_chain(conn, gateway_id):
    """The adoption's chain as stored reports, oldest first: the adopted report itself and every later report of the same reader,
    epoch, generation and month that INGEST judged in order when it arrived (`gateway_totals.in_order`, written in the same transaction
    as the report and never recomputed: arrival order cannot be rebuilt from a rowid or a clock reading), by number. Empty when the
    gateway has no adoption or the adopted reader is not the current one."""
    chain = conn.execute("SELECT g.current_adoption_id FROM gateway_chain_state g WHERE g.gateway_id=?", (gateway_id,)).fetchone()
    if chain is None or chain[0] is None:
        return []
    adoption = conn.execute("SELECT event_id, reader_incarnation, ledger_epoch, generation, period_start, seq "
                            "FROM gateway_adoptions WHERE id=? AND gateway_id=?", (chain[0], gateway_id)).fetchone()
    if adoption is None:
        return []
    reader = conn.execute("SELECT state FROM gateway_readers WHERE gateway_id=? AND reader_incarnation=?",
                          (gateway_id, adoption[1])).fetchone()
    base = stored_report(conn, gateway_id, adoption[0])
    if reader is None or reader[0] != "current" or base is None:
        return []
    later = _rows(conn.execute(
        "SELECT * FROM gateway_totals WHERE gateway_id=? AND reader_incarnation=? AND ledger_epoch=? AND generation=? "
        "AND period_start=? AND seq>? AND in_order=1 ORDER BY seq", (gateway_id,) + tuple(adoption[1:5]) + (adoption[5],)))
    return [base] + later


def usable(conn, gateway_id, now_ms):
    """(state, chain): whether the gateway's evidence may be leaned on now (2.4), as one closed word, and the usable chain. An adoption
    exists and its reader is the current one, the pair of the newest in-order report is still approved, nothing is open, that report is
    no older than 120 s, and its month is the current month. Anything else is not usable and the word says which first."""
    chain = usable_chain(conn, gateway_id)
    if not chain:
        return STATE_UNADOPTED, []
    newest = chain[-1]
    if not _approved(conn, gateway_id, newest):
        return STATE_UNAPPROVED, chain
    if open_transition_ids(conn, gateway_id):
        return STATE_TRANSITION_OPEN, chain
    if not 0 <= now_ms - newest["observed_at"] <= FRESH_MS:
        return STATE_STALE, chain
    if (newest["period_start"], newest["period_end"]) != current_period(now_ms):
        return STATE_PERIOD_MISMATCH, chain
    return STATE_USABLE, chain


# Whether Leasewise's `receipts` table records a ledger generation. It does not (a later card adds the column), so the answer of the comparison is
# fixed before any receipt is read: mode C. A test fails the day the column exists, which forces this line to be revisited.
RECEIPTS_CARRY_GENERATION = False


def coverage(conn, gateway_id, latest):
    """The C3 comparison (card R1-3e) for the report `status` describes: the report against this gateway's FINAL receipts. Information only.
    Leasewise's receipts carry no ledger generation and no month, so today every receipt is mode C: the answer is (None, False). Because that is
    known before reading anything, the receipt history is NOT read while it holds (it only grows); the comparison is given no receipts and the
    flag that says the source cannot carry a generation, and returns the same two values."""
    receipts = []
    if RECEIPTS_CARRY_GENERATION:
        receipts = [receipt_coverage.Receipt(gateway_id, None, seq, None, amount) for seq, amount in conn.execute(
            "SELECT seq, actual_micro_eur FROM receipts WHERE gateway_id=? AND final=1 ORDER BY seq", (gateway_id,))]
    result = receipt_coverage.compare(
        receipt_coverage.Report(gateway_id, latest["generation"], latest["receipts_seq"], latest["period_start"], latest["settled_micro_eur"]),
        receipts, receipts_carry_generation=RECEIPTS_CARRY_GENERATION)
    return result.unreceipted_micro_eur, result.receipts_exceed_settled


def project(conn, gateway_id, now_ms):
    """The `evidence` object of `status` for one gateway (8.3), or None when it has no version-4 report. Only numbers, closed words,
    local aliases' ids and random identities: no amount other than the quiet figure and the newest report's settled total (which the operator
    compares with the gateway's own), no hash, no path."""
    latest = newest_report(conn, gateway_id)
    if latest is None:
        return None
    state, _chain = usable(conn, gateway_id, now_ms)
    adopted = None
    chain = conn.execute("SELECT current_adoption_id, last_quiet_event_id, last_quiet_unattributed, late_reports, old_chain_reports, "
                         "duplicate_reports, retired_reader_reports FROM gateway_chain_state WHERE gateway_id=?",
                         (gateway_id,)).fetchone()
    if chain is not None and chain[0] is not None:
        row = conn.execute("SELECT event_id, reader_incarnation, seq, ledger_epoch, period_start, adopted_at, kind "
                           "FROM gateway_adoptions WHERE id=?", (chain[0],)).fetchone()
        adopted = dict(zip(("event_id", "reader_incarnation", "seq", "epoch", "period_start", "adopted_at", "kind"), row, strict=True))
    open_rows = conn.execute("SELECT id, kind, event_id FROM gateway_transitions WHERE gateway_id=? AND resolved_by_adoption IS NULL "
                             "ORDER BY id", (gateway_id,)).fetchall()
    counters = (0, 0, 0, 0) if chain is None else tuple(chain[3:7])
    unreceipted, exceeds = coverage(conn, gateway_id, latest)
    return {
        "state": state,
        "approved": _approved(conn, gateway_id, latest),
        "adopted": adopted,
        "latest": {"event_id": latest["event_id"], "seq": latest["seq"], "observed_at": latest["observed_at"],
                   "age_ms": now_ms - latest["observed_at"], "quiet": bool(latest["quiet"]),
                   "held_over_reservation": latest["held_over_reservation"], "settled_micro_eur": latest["settled_micro_eur"]},
        "readers": [{"reader_incarnation": r[0], "state": r[1], "first_seen_at": r[2]} for r in conn.execute(
            "SELECT reader_incarnation, state, first_seen_at FROM gateway_readers WHERE gateway_id=? "
            "ORDER BY first_seen_at, reader_incarnation", (gateway_id,))],
        "last_quiet": None if chain is None or chain[1] is None else {"event_id": chain[1], "unattributed": chain[2]},
        "open_transitions": [{"id": r[0], "kind": r[1], "event_id": r[2]} for r in open_rows],
        "adopt_blockers": adopt_blockers(conn, gateway_id, latest["event_id"], None, now_ms),
        "late_reports": counters[0],
        "old_chain_reports": counters[1],
        "duplicate_reports": counters[2],
        "retired_reader_reports": counters[3],
        "unreceipted_micro_eur": unreceipted,                    # null when coverage cannot be proven (mode C: today always); never 0 for unknown
        "receipts_exceed_settled": exceeds,
        "action": ACTION_SENTENCE if open_rows or latest["gateway_flag"] else None,
    }


def _envelope(conn, event_id):
    """One stored reading as the evaluator's closed reading envelope, or None."""
    row = conn.execute("SELECT r.event_id, m.alias, a.alias, r.kind, r.observed_at, r.payload, r.received_at FROM readings r "
                       "JOIN machines m ON m.id=r.machine_id LEFT JOIN accounts a ON a.id=r.account_id WHERE r.event_id=?",
                       (event_id,)).fetchone()
    if row is None:
        return None
    return {"event_id": row[0], "machine": row[1], "account": row[2], "kind": row[3], "observed_at": row[4],
            "payload": json.loads(row[5]), "received_at": row[6]}


def inputs(conn, account_alias, now_ms):
    """The evaluator's gateway input for one account (card R1-3d, 2.4), built from the usable chain only: `gateways` (each with the event
    ids of its usable chain, EMPTY when the gateway is not usable, which the evaluator reads as unusable and so as an unknown exposure),
    `readings` (the stored envelopes of those chains and of the open transitions' reports), `unresolved_event_ids`, and `states` (the
    closed word per gateway alias, to say why). Read-only; nothing in the grant path calls it before card R1-5."""
    gateways, readings, unresolved, states = [], {}, [], {}
    for gateway_id, alias, share in conn.execute(
            "SELECT g.id, g.alias, g.share_micro_eur FROM gateways g JOIN accounts a ON a.id=g.account_id WHERE a.alias=? "
            "ORDER BY g.alias", (account_alias,)).fetchall():
        state, chain = usable(conn, gateway_id, now_ms)
        states[alias] = state
        ids = [report["event_id"] for report in chain] if state == STATE_USABLE else []
        for event_id in ids + [r[0] for r in conn.execute(
                "SELECT event_id FROM gateway_transitions WHERE gateway_id=? AND resolved_by_adoption IS NULL "
                "AND event_id IS NOT NULL ORDER BY id", (gateway_id,))]:
            envelope = _envelope(conn, event_id)
            if envelope is not None and envelope["account"] == account_alias:
                readings[event_id] = envelope
                if event_id not in ids and event_id not in unresolved:
                    unresolved.append(event_id)
        gateways.append({"gateway": alias, "share_micro_eur": share, "current_chain_event_ids": ids})
    return {"gateways": gateways, "readings": list(readings.values()), "unresolved_event_ids": unresolved, "states": states}
