"""The card-2 reading reducer: pure functions that fold a validated reading
into the windows / gateway-report state.

No I/O and no implicit clock: the wall clock (for freshness) is passed in by
the caller, so these functions are deterministic and unit-testable. The store
layer (store.py) is responsible for the idempotency/dedup decision (replay
no-op vs. collision) around these reducers, and for the BEGIN IMMEDIATE
transaction that makes each import atomic.

Freshness rules (docs/DESIGN.md sections 5 and 7):
  * The high-water utilization per reset epoch never decreases; a new, later
    epoch is restarted by the first post-reset observation.
  * An out-of-order reading (an older reset epoch, or a non-increasing ledger
    sequence) must not regress the stored state.
  * confidence is explicit (observed / unknown); source_stale is a separate
    boolean that preserves a legacy "stale" reading's value and source time.
  * Incomplete coverage always uses the 600-second wall cap; a gateway report
    older than 120 seconds fails closed as stale (METER_STALE).
"""

import calendar
import datetime
import json

from .errors import (
    CODE_BILLING_BASIS_UNKNOWN,
    CODE_INVALID_CONFIG,
    MSG_BILLING_BASIS_UNKNOWN,
    MSG_INVALID_CONFIG,
    QuotaError,
)

# 600-second wall cap for window freshness (section 5).
WALL_CAP_MS = 600_000
# Fail-closed age for a gateway report: older than this is METER_STALE (section 7).
GATEWAY_STALE_MS = 120_000
# Future observations beyond 30 seconds are rejected at ingest (section 7).
FUTURE_SLOP_MS = 30_000

# The explicit confidence vocabulary (section 7).
CONFIDENCE_OBSERVED = "observed"
CONFIDENCE_UNKNOWN = "unknown"

# Gateway report states. "none" and "stale" are resolved at status time;
# "ok" and "reconciliation_required" are stored at ingest time.
GATEWAY_STATE_NONE = "none"
GATEWAY_STATE_STALE = "stale"
GATEWAY_STATE_OK = "ok"
GATEWAY_STATE_RECONCILE = "reconciliation_required"

# Window freshness reasons (section 5).
FRESH_FRESH = "fresh"
FRESH_WALL_CAP = "wall_cap"


def _high_water(a, b):
    """max(a, b) with None treated as 'no value' (an unknown reading cannot
    push the high-water down)."""
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


def _hashes_json(policy_hashes):
    """Canonical JSON for a policy_hashes dict, or None when absent. Sorting
    the keys makes an equal policy compare equal regardless of key order."""
    if policy_hashes is None:
        return None
    return json.dumps(policy_hashes, sort_keys=True)


def reduce_window(existing, used_bps, resets_at, observed_at, confidence,
                  source_stale, covered_seq, event_id):
    """Fold one cli_window entry into a window's state.

    Returns the new window row tuple
      (used_bps, resets_at, observed_at, confidence, source_stale, covered_seq,
       source_event_id)
    or None when the reading is out of order (an older reset epoch) and must
    not change the window. An unknown reading over a window that already has
    a known value returns the row unchanged (the window keeps its value, its
    observed_at, and its confidence).

    `existing` is None, or the current window row tuple of the same shape.
    `observed_at` is the reading's *source* observation time (preserved; the
    ingest time never refreshes it).
    """
    if existing is None:
        return (used_bps, resets_at, observed_at, confidence, source_stale,
                covered_seq, event_id)

    e_used, e_resets, e_obs, e_conf, e_stale, e_cov, e_ev = existing

    # An unknown reading carries no value of its own. It must not move
    # observed_at or relabel the confidence of a window that already has a
    # known value: that would make a stale value look fresh (resetting the
    # 600-second wall-cap age) and demote observed evidence to unknown.
    # used_bps is null here, so nothing else can change either - the reading
    # itself is still recorded in the readings table.
    if confidence == CONFIDENCE_UNKNOWN and e_used is not None:
        return existing

    # Reset-epoch ordering, when both epochs are known:
    #   * an older (lower) epoch is out of order -> no change;
    #   * a later (higher) epoch is the post-reset observation ONLY when the
    #     sample was taken at or after the stored reset time -> restart the
    #     high-water from this reading;
    #   * a later (higher) epoch from a sample taken BEFORE the stored reset
    #     time is just reset-time jitter, not a new epoch -> fall through to
    #     the same-epoch high-water so an older sample cannot lower the mark.
    if e_resets is not None and resets_at is not None:
        if resets_at < e_resets:
            return None
        if resets_at > e_resets and observed_at >= e_resets:
            return (used_bps, resets_at, observed_at, confidence,
                    source_stale, covered_seq, event_id)

    # Same epoch (or an unknown epoch): the high-water mark never decreases,
    # and the window's observed_at tracks the latest observation.
    new_used = _high_water(e_used, used_bps)
    new_obs = max(e_obs, observed_at)
    new_resets = e_resets if e_resets is not None else resets_at
    if observed_at >= e_obs:
        # This observation is the latest: its metadata wins.
        return (new_used, new_resets, new_obs, confidence, source_stale,
                covered_seq, event_id)
    # An older observation in the same epoch: keep the latest observation's
    # metadata, but still apply the high-water on used_bps.
    return (new_used, new_resets, new_obs, e_conf, e_stale, e_cov, e_ev)


def reduce_gateway_report(existing, ledger_epoch, seq, period_start, period_end,
                          policy_hashes, sized_ready, reconciliation_required):
    """Fold a gateway_snapshot into the gateway's stored report state.

    Returns the new stored report tuple
      (ledger_epoch, last_seq, period_start, period_end, policy_hashes_json,
       sized_ready, state)
    or None when the report is out of order (a same-ledger non-increasing
    sequence) and must not regress the report.

    `existing` is None, or the current stored report tuple of the same shape.
    A gateway row carries NULL report columns until its first report, so a
    stored row with a NULL sequence is treated as 'no report yet'. A change of
    ledger epoch or policy hashes requires reconciliation, which is STICKY:
    once set it is not cleared by the next report, only by the operator's
    explicit reconcile verb (store.reconcile).
    """
    new_hashes = _hashes_json(policy_hashes)
    e_seq = existing[1] if existing is not None else None
    if existing is None or e_seq is None:
        # A stored row can hold a NULL sequence while carrying a sticky
        # reconciliation mark: store.import_config resets a re-pointed
        # gateway's report columns (machine or account changed) and marks it
        # reconciliation_required. The new owner's first report is a new
        # ledger's first report, not a continuation, so it is accepted here
        # - and the stored mark is preserved: this is not a normal first
        # report for a brand-new gateway.
        e_state = existing[6] if existing is not None else None
        state = (GATEWAY_STATE_RECONCILE
                 if (reconciliation_required
                     or e_state == GATEWAY_STATE_RECONCILE)
                 else GATEWAY_STATE_OK)
        return (ledger_epoch, seq, period_start, period_end, new_hashes,
                sized_ready, state)

    e_epoch, e_seq, _e_start, _e_end, e_hashes, _e_sized, e_state = existing
    # m1: compare the ledger epoch BEFORE the sequence. A changed epoch is a
    # new ledger with its own sequence counter, so it is accepted even when
    # its first sequence is lower than the old ledger's; only a same-ledger
    # non-increasing sequence is an out-of-order no-op.
    epoch_changed = (e_epoch is not None and ledger_epoch is not None
                     and ledger_epoch != e_epoch)
    if not epoch_changed and seq <= e_seq:
        # Same ledger, non-increasing sequence: replay is a no-op.
        return None

    # Sticky (M2): once reconciliation is required it stays until the
    # operator's explicit reconcile verb clears it. From a non-reconciled
    # state, an epoch change, a hash change, or an explicit flag raises it.
    if e_state == GATEWAY_STATE_RECONCILE:
        state = GATEWAY_STATE_RECONCILE
    else:
        hashes_changed = new_hashes != e_hashes
        state = (GATEWAY_STATE_RECONCILE
                 if (reconciliation_required or epoch_changed or hashes_changed)
                 else GATEWAY_STATE_OK)

    new_epoch = (ledger_epoch if (e_epoch is None or epoch_changed) else e_epoch)
    return (new_epoch, seq, period_start, period_end, new_hashes,
            sized_ready, state)


def gateway_report_state(last_report_at, stored_state, now_ms):
    """Resolve the gateway report state for status.

    "none" when there is no report; "stale" (METER_STALE) when the last report
    is older than the fail-closed age; otherwise the stored state.
    """
    if last_report_at is None:
        return GATEWAY_STATE_NONE
    if now_ms - last_report_at > GATEWAY_STALE_MS:
        return GATEWAY_STATE_STALE
    return stored_state if stored_state is not None else GATEWAY_STATE_OK


def window_freshness(observed_at, now_ms):
    """Return (current, reason) for a window at status time.

    The 600-second wall cap applies to every window in this card: the only
    path to exceed it (idle proof under complete coverage) needs the turn
    tracking introduced by a later card, and a missing proof uses the wall cap.
    """
    age_ms = now_ms - observed_at
    if age_ms <= WALL_CAP_MS:
        return True, FRESH_FRESH
    return False, FRESH_WALL_CAP


# Billing freshness (section 11): fresh iff age <= max_age, no idle
# exemption. `max_age_ms` is None when no policy supplies a max_age (the
# freshness is then "unknown", not assumed fresh or stale).
FRESH_BILLING_FRESH = "fresh"
FRESH_BILLING_STALE = "stale"
FRESH_BILLING_UNKNOWN = "unknown"


def billing_freshness(observed_at, max_age_ms, now_ms):
    """Return (fresh, reason) for a billing total at status time.

    `observed_at` is the source time (lastUpdate), not the poll receipt time;
    it is never refreshed by an unchanged re-import (a failed or unchanged
    poll produces no reading, and a replay is a no-op). There is no idle
    exemption: the age is the source age, full stop.
    """
    if observed_at is None or max_age_ms is None:
        return None, FRESH_BILLING_UNKNOWN
    if now_ms - observed_at <= max_age_ms:
        return True, FRESH_BILLING_FRESH
    return False, FRESH_BILLING_STALE


# Tax bases (section 11): operator-declared, closed set. Unknown is a
# permitted configuration, not permission to compare.
BASIS_UNKNOWN = "unknown"
BASIS_EX_VAT = "ex_vat"
BASIS_INVOICE_TOTAL = "invoice_total"
KNOWN_BASES = (BASIS_EX_VAT, BASIS_INVOICE_TOTAL)
CLOSED_BASES = (BASIS_UNKNOWN,) + KNOWN_BASES

# Checked signed-64 arithmetic (section 7): every amount is bounded before
# and after conversion.
INT64_MAX = 2 ** 63 - 1
VAT_BPS_MAX = 10_000  # basis points; 10000 == 100%


def convert_basis(amount, source_basis, target_basis, vat_bps):
    """Pure tax-basis conversion, shared by the later 2b cards.

    Convert a non-negative integer micro-EUR amount between two
    operator-declared bases. Both directions round UP (a half-micro
    fraction is never dropped), in checked integer arithmetic:
      ex_vat -> invoice_total: ceil(x * (10000 + vat_bps) / 10000)
      invoice_total -> ex_vat: ceil(x * 10000 / (10000 + vat_bps))
      equal bases: unchanged (no rate is needed, so vat_bps may be None)
    Bases are never guessed: an unknown (closed) basis, or a missing/
    out-of-range required rate, yields BILLING_BASIS_UNKNOWN - deferral,
    not approval. A non-closed basis, a non-integer/negative amount, or an
    overflowing result is INVALID_CONFIG.
    """
    if type(amount) is not int or amount < 0 or amount > INT64_MAX:
        raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
    if source_basis not in CLOSED_BASES or target_basis not in CLOSED_BASES:
        raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
    if source_basis == target_basis:
        return amount
    if source_basis == BASIS_UNKNOWN or target_basis == BASIS_UNKNOWN:
        raise QuotaError(CODE_BILLING_BASIS_UNKNOWN, MSG_BILLING_BASIS_UNKNOWN)
    if type(vat_bps) is not int or not 0 <= vat_bps <= VAT_BPS_MAX:
        # A missing or unusable required rate: no implicit VAT default is
        # permitted.
        raise QuotaError(CODE_BILLING_BASIS_UNKNOWN, MSG_BILLING_BASIS_UNKNOWN)
    if source_basis == BASIS_EX_VAT:
        numerator, denominator = amount * (10_000 + vat_bps), 10_000
    else:
        numerator, denominator = amount * 10_000, 10_000 + vat_bps
    result = (numerator + denominator - 1) // denominator  # ceil
    if result > INT64_MAX:
        raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)
    return result


def reduce_billing_total(existing, total, observed_at, event_id):
    """Fold one provider_billing reading into a billing_totals row.

    Returns the new stored tuple
      (latest_micro_eur, observed_at, latest_event_id,
       high_water_micro_eur, high_water_event_id, conflict)
    or None when the reading changes nothing (an older total that does not
    beat the high-water).

    Ordering is by SOURCE time (observed_at), never arrival order
    (section 11):
      * a newer observation sets latest - a newer downward correction
        retains the high-water and any latched conflict;
      * an equal-time unequal total latches conflict; the latest event is
        the UUID-sort winner so the display is deterministic, and its
        amount is the latest amount;
      * an older higher total raises the high-water AND latches conflict,
        without refreshing the stored (latest) time;
      * the high-water is the maximum total ever observed; each amount
        retains its own source time and event.
    The conflict is sticky: only a later audited resolution integration
    clears it, never a poll.
    """
    if existing is None:
        return (total, observed_at, event_id, total, event_id, 0)
    e_latest, e_obs, e_ev, e_hw, e_hw_ev, e_conf = existing
    if observed_at > e_obs:
        new_hw, new_hw_ev = ((total, event_id) if total > e_hw
                             else (e_hw, e_hw_ev))
        return (total, observed_at, event_id, new_hw, new_hw_ev, e_conf)
    if observed_at < e_obs:
        if total > e_hw:
            return (e_latest, e_obs, e_ev, total, event_id, 1)
        return None
    # Equal source time: unequal totals are a conflict.
    if total == e_latest:
        return None
    winner_event, winner_total = ((event_id, total) if event_id > e_ev
                                  else (e_ev, e_latest))
    new_hw, new_hw_ev = ((total, event_id) if total > e_hw
                         else (e_hw, e_hw_ev))
    return (winner_total, e_obs, winner_event, new_hw, new_hw_ev, 1)


def _add_calendar_months_ms(ms, months):
    """Add calendar months to a UTC epoch-ms timestamp (day clamped)."""
    dt = datetime.datetime.fromtimestamp(ms // 1000, datetime.timezone.utc)
    year, month = divmod(dt.year * 12 + dt.month - 1 + months, 12)
    month += 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return int(datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc)
               .timestamp() * 1000)


def billing_retention_eligible(period_end_ms, unresolved, now_ms):
    """Pure retention-eligibility predicate (section 11); 2b does no
    deletion.

    A record is eligible for the later pruning owner's deletion only when
    it is resolved and its period ended at least 13 calendar months before
    `now_ms`. Unresolved (conflicted) evidence is retained indefinitely,
    and financial history is never eligible before the 13-month mark.
    """
    if unresolved or period_end_ms > now_ms:
        # A period that has not ended yet is never eligible.
        return False
    try:
        return now_ms >= _add_calendar_months_ms(period_end_ms, 13)
    except (OverflowError, OSError, ValueError):
        # A period end so far in the past that the proleptic calendar
        # cannot represent it: it is well past the 13-month mark.
        return True
