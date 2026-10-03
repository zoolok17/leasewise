# Step record - Card 7c-2: usage-limit events from the park marker

Work item `card-7c-2`. Branch `card-7c-2`, from main `da91e1f`. Contract:
`C:\lw\records\DESIGN-7C2.md` (the lead's decisions in its section 15 win). This record grows one
step at a time; step 1 of 4 is below.
Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only. Audience: the lead and the reviewers.

## In plain words

When a seat reports "this account is out until a given time", Leasewise parks the account. It must
hold the account until **30 seconds after** that time, not exactly at it, so the provider has really
reopened. Until now Leasewise stored the time as it was sent and added nothing. Step 1 adds the 30
seconds, once, inside Leasewise. The time the reporter sent is still kept as it was sent, as
evidence, so a repeated delivery of an old event behaves exactly as before.

What did not change: how an account is un-parked. Leasewise still never recovers a block by itself;
that is card 7c-3 and it is still missing. So the margin only moves the time that a block *says* it
waits for; an operator still clears the block.

## Step 1: the margin (this record)

### What changed

- `quota_balancer/lease.py`
  - New constant `LIMIT_RETRY_MARGIN_MS = 30_000`, used in one place, `record_limit_event`.
  - A **new block** gets `retry_at = resets_at + 30 000`. A reset that is not known stays not known.
  - A **join** (a different event on an account that already has a live block) merges the NEW value
    with the margin already added: `merge(old, resets_at + 30 000)`. The old value is never touched
    again, so the margin is never added twice. The merge rule itself is unchanged: the latest known
    value wins, and an unknown value from either side makes the result unknown.
  - The reset as sent stays **raw** in `limit_events.resets_at`, and stays part of the replay
    comparison. A replay of a known event is still a pure read: it returns the block as it is and
    writes nothing.
  - `_validate_limit_event`: `resets_at` may be at most `INT64_MAX - 30 000`, so the sum always fits
    the store. One more is refused as `INVALID_REQUEST`.
  - A comment where `turn_id` is validated: for a limit event it is only a checked id kept for the
    replay comparison, not linked to a lease (decision D8).
- `quota_balancer/http_api.py`: no change. The `202` answer carries the block's `retry_at`, so it is
  now the margined value.

### Decision D2: no backfill

Blocks that already exist keep the `retry_at` they have. Nothing is rewritten. Why: a backfill would
silently change values an operator may have seen, nothing in the store depends on the margin yet, and
there is no deployment. A **new** event joining such a block gets `max(old stored value, its own
reset + 30 000)`.

The same holds for legacy rows (migration v8): their content is never compared or merged, and a
replay returns the block as it is.

### Tests

New: `tests/test_card7c2_margin.py` (15 tests).

| Behaviour | Test |
| --- | --- |
| the constant is 30 000 | `test_the_margin_is_thirty_seconds` |
| insert: block and 202 answer margined, raw kept in `limit_events` | `test_a_new_block_holds_until_the_reset_plus_the_margin` |
| unknown stays unknown | `test_an_unknown_reset_stays_unknown` |
| join with a later, an earlier and an unknown reset | the three `test_a_join_with_...` tests, and `test_a_known_reset_joining_an_unknown_one_leaves_it_unknown` |
| added exactly once across insert, join and replay | `test_the_margin_is_added_exactly_once_across_insert_join_and_replay` |
| ceiling: `INT64_MAX - 30 000` accepted, one more refused | `test_the_input_ceiling_keeps_the_sum_inside_the_store` |
| an event stored before the change, replayed after: no conflict, no margin, nothing written | `test_a_duplicate_stored_before_the_change_is_a_pure_read_with_no_margin` |
| a changed reset under an old id is still a conflict | `test_a_changed_reset_under_an_old_event_id_is_still_a_conflict` |
| a new event joining an old block: margin on its own value only | `test_a_new_event_joining_an_old_block_...`, `test_an_old_raw_value_later_than_...` |
| an operator-cleared block is never re-parked by a replay | `test_an_operator_cleared_block_is_never_re_parked_by_a_replay` |
| a legacy row is untouched | `test_a_legacy_row_is_left_untouched` |

Changed (expected values go from the raw reset to the reset plus 30 000), all in
`tests/test_card5.py`, class `FixRound2LimitEventEvidenceMerge`:

- `test_later_known_reset_wins_over_an_earlier_one`: `NOW + 3_600_000` becomes `NOW + 3_600_000 + 30_000`.
- `test_a_smaller_reset_from_a_later_joining_event_does_not_regress_the_retry_at`: the same change.
- `test_unknown_reset_from_a_joining_event_poisons_the_merged_retry_at`: still unknown after the join;
  the first event's answer now asserts `NOW + 60_000 + 30_000`.

No other existing test reads a `retry_at` that a limit event produced: the others insert blocks
directly with SQL.

### Limits (stated, not hidden)

- **Recovery is still missing (card 7c-3).** A parked block stays parked, whatever its `retry_at`
  says, until an operator clears it. The margin does not change that.
- A reset that is already in the past when the event arrives still parks the account, with a
  `retry_at` in the past. Nothing un-parks it by itself.
- Steps 2 to 4 (the reporter's outbox and client, the marker reader and account map, the wiring,
  status and README) are not built yet.
