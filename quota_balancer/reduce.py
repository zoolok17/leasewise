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

import json

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
    not change the window.

    `existing` is None, or the current window row tuple of the same shape.
    `observed_at` is the reading's *source* observation time (preserved; the
    ingest time never refreshes it).
    """
    if existing is None:
        return (used_bps, resets_at, observed_at, confidence, source_stale,
                covered_seq, event_id)

    e_used, e_resets, e_obs, e_conf, e_stale, e_cov, e_ev = existing

    # Reset-epoch ordering, when both epochs are known:
    #   * an older (lower) epoch is out of order -> no change;
    #   * a later (higher) epoch is the post-reset observation -> restart the
    #     high-water from this reading.
    if e_resets is not None and resets_at is not None:
        if resets_at < e_resets:
            return None
        if resets_at > e_resets:
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
    or None when the report is out of order (a non-increasing ledger sequence)
    and must not regress the report.

    `existing` is None, or the current stored report tuple of the same shape.
    A gateway row carries NULL report columns until its first report, so a
    stored row with a NULL sequence is treated as 'no report yet'. A change of
    ledger epoch or policy hashes between reports requires reconciliation.
    """
    new_hashes = _hashes_json(policy_hashes)
    e_seq = existing[1] if existing is not None else None
    if existing is None or e_seq is None:
        state = (GATEWAY_STATE_RECONCILE if reconciliation_required
                 else GATEWAY_STATE_OK)
        return (ledger_epoch, seq, period_start, period_end, new_hashes,
                sized_ready, state)

    e_epoch, e_seq, e_start, e_end, e_hashes, e_sized, _e_state = existing
    if seq <= e_seq:
        # Duplicate or out-of-order sequence: replay is a no-op.
        return None
    epoch_changed = ledger_epoch != e_epoch
    hashes_changed = new_hashes != e_hashes
    state = (GATEWAY_STATE_RECONCILE
             if (reconciliation_required or epoch_changed or hashes_changed)
             else GATEWAY_STATE_OK)
    return (ledger_epoch, seq, period_start, period_end, new_hashes,
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
