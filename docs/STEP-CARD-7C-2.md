# Step record - Card 7c-2: usage-limit events from the park marker

Work item `card-7c-2`. Branch `card-7c-2`, from main `da91e1f`. Contract:
`C:\lw\records\DESIGN-7C2.md` (the lead's decisions in its section 15 win). This record grows one
step at a time; steps 1 and 2 of 4 are below.
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
  - A reset a NEW event states may be at most `INT64_MAX - 30 000` (`LIMIT_RESETS_AT_MAX`), so the
    sum always fits the store; one more is refused as `INVALID_REQUEST`. **Fix round 1 moved this
    check:** it applies once an event is known to be new (an insert or a join), after the lookup. A
    replay keeps the old checks (shape, type, a reset up to `INT64_MAX`, and the authorization). See
    "Fix round 1" below.
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

## Step 2: the reporter's outbox and client for limit events

Built on step 1 (`437acb6`), in four small commits so a change from review lands cleanly:
decisions, state, client, scheduling.

### In plain words

Once the reporter knows of a limit, the event must reach Leasewise even across restarts and
outages, and nothing else in the reporter may hold it back. This step builds the safe queue (the
outbox) for limit events and their sending. It does not read park files yet; that is step 3. The
tests put events into the queue by hand.

### What changed

- **Answers (`decisions.py`, commit 1).** A new kind, `LIMIT`. `200`, `201` and `202` mean recorded.
  No answer, `5xx`, `429` and `401` behave as for every kind (retry; `401` keeps the credential
  notes). Any other `4xx` fails **that one row** for good: never an agent stop (a limit event has no
  agent), never a rebuild, never a drop. `202` stays *not* success for the lease kinds, whose routes
  never answer it.
- **State (`state.py`, `mapper.py`, commit 2).** State version 2. A version-1 file is upgraded in
  one transaction (table and triggers; lease rows untouched; a second open does nothing; a failed
  upgrade leaves version 1). New table `limit_event`: one row per limit fact, `event_id` unique, the
  frozen canonical body text, a delivery state, its tries and back-off, a closed error word.
  Identity and body cannot be updated and a row cannot be deleted (triggers, like `final`).
  `State.add_limit_event` stores a row in one transaction with `INSERT OR IGNORE`: the same
  `event_id` twice is one row and the first bytes stay. It accepts only the allow-listed body
  (exactly seven fields, checked types and bounds, a reset at most `INT64_MAX - 30 000`, the ceiling
  Leasewise now enforces) and otherwise raises one fixed sentence that carries no value. `apply`
  needed no new code: its generic table handling covers the new table.
- **Client (`client.py`, commit 3).** The kind and the path are checked as a pair: a limit row goes
  to exactly `/v1/limit-events` and to no other path; no lease kind can reach that path; the lease
  rules are unchanged. A limit answer is read for its status and its closed error word only, so no
  field of a `202` body is kept.
- **Scheduling (`run.py`, commit 4).** `_send` runs two passes, the limit pass first. A lease answer
  of retry or `429` (a parked account refuses every lease request) never delays a limit row, and a
  denied lease that stops its agent never holds one. **The two passes stay separate** (fix round 1,
  16.6): a limit retry (no answer, `5xx`, `429`, `401`) does not stop the lease pass either, because
  the limit route can fail on its own while leases work. Each pass stops on its own wall and
  `blocked` says if either met one. A row waiting out its own back-off is skipped, and the lease
  pass still runs.

### Decisions applied

- **D4:** limit rows have their own pass, run first (lesson kn-941796d9afb6).
- **D6:** state version 2 with a migration; no downgrade. A reporter built before this card refuses a
  version-2 file with its existing fail-closed error. This is tested by running the state opener
  with the old version number and the old table list, which is exactly the old code path: it raises
  "the state file version is not supported" and the file (bytes, version, schema) is unchanged. It
  is also stated in the README "Limits".
- **D8:** `turn_id` names the limit fact (the step 1 comment; step 3 derives it).

### Tests (`tests/test_card7c2_*.py`)

| File | What it proves |
| --- | --- |
| `test_card7c2_decisions.py` (9) | the answer table above, including `202` not success for lease kinds, 4xx never an agent stop, an echoed unknown word becomes `UNKNOWN_CODE` |
| `test_card7c2_state.py` (24) | fresh file is version 2; v1 migrates with lease rows untouched; second open is a no-op; a failed upgrade stays v1; the old code path refuses v2 and changes nothing; a newer unknown version is refused; one row per `event_id` with the first bytes; immutability; body allow-list with a fixed sentence; scheduling order and back-off; `apply` for every answer |
| `test_card7c2_client.py` (11) | the path pair (limit rows only to the limit path; no lease kind can reach it; 14 refused paths); the wire bytes equal the stored bytes on every try; answer reading; no exception text kept |
| `test_card7c2_delivery.py` (15) | a parked account refusing every lease never delays a limit row (same cycle, limit path first); a denied acquisition that stops its agent does not either; oldest first; a refused row fails alone; no answer stops the cycle before the lease pass; a back-off row is skipped; `401`; every try resends the same bytes across a restart; a sent row is never resent; no body value or exception text in errors, notes, counters or status |

Each file was run red against the code before its commit (decisions: 9 tests, 24 errors before;
state: 22 tests, 1 failure and 44 errors before; client: 11 tests, 14 failures before; delivery: 13
tests, 12 failures before).

### Limits (stated, not hidden)

- Nothing reads the park files yet and `status` does not yet show limit events: steps 3 and 4.
- A sent limit event is final in the outbox; if an operator later clears the block in Leasewise
  while the park file still says the same thing, nothing is sent again (the same limit fact is one
  event for ever). That is by design (DESIGN-7C2.md section 5).
- If Leasewise keeps answering `429` or `5xx` for a limit event, it is retried with the usual
  5-second to 15-minute back-off, for ever; nothing drops it.

## Fix round 1 (Codex's first read, tk-e7f12e878cee; spec section 16)

### In plain words

Codex found one upgrade edge that broke a promise. A limit event that Leasewise accepted *before* this
card, with an extreme reset time (within 30 seconds of the largest number the store can hold), was
refused when the reporter sent it again, because step 1 checked the new, smaller ceiling before it
looked the event up. A repeated delivery must behave exactly as before. It does now. Codex also asked
for delivery tests that belong to step 2.

### 16.1: a replay keeps the old reset domain (`lease.py`)

- `_validate_limit_event` checks the reset in the OLD domain only, up to `INT64_MAX`.
- `record_limit_event` checks the smaller ceiling, `LIMIT_RESETS_AT_MAX = INT64_MAX - 30 000`, only
  after the lookup says the event is new (an insert or a join). A replay (including a legacy record)
  returns the block as it is: no conflict, no margin, nothing written. The legacy branch is reachable
  for those values.
- A new event at those values is still refused with `400`, as a new block and as a join; the block is
  left unchanged.
- This does not change section 7.2's rule in effect: it is the same rule, now true at the edges. The
  step 1 text above is updated; the README needed no change.

Tests, `tests/test_card7c2_replay_domain.py` (8): each event is recorded **the old way** by running
the limit-event code from the repository's own history (`da91e1f`), at `INT64_MAX`,
`INT64_MAX - 1` and `INT64_MAX - 29 999`; the same body is then replayed through the new code and
over HTTP: the same answer, no conflict, no margin (raw `retry_at`), and every table unchanged. The
same for a legacy record. A changed reset under an old id is still a conflict; a replay above
`INT64_MAX` is still invalid; a new event at those values is refused as an insert and as a join; a
new event joining an old block at the edge gets `max(old raw, new + margin)`. Run against step 1
(`437acb6`) the new file gave 8 tests, 4 failures and 6 errors; against the fix, all pass. Codex's own
reproduction (`test_7c2_review_edges.py`, copied, never edited) passes: 2 of 2.

### 16.6: delivery (step 2)

- **The two passes stay separate even after a limit row gets a retry.** Not true before: a limit
  retry stopped the cycle before the lease pass. Fixed in `run.py` (see step 2 above). Tests:
  `test_the_two_passes_stay_separate_even_after_a_limit_row_gets_a_retry` (red before: 1 failure) and
  `test_a_row_waiting_out_its_own_back_off_is_not_resent_but_the_lease_pass_runs`, which now has a
  lease message arriving while a limit row backs off.
- **Credential notes are written in the same transaction as the delivery state.** Already true:
  `State.apply` does everything inside one `txn()`. Now proven: a temporary trigger makes the row
  update fail; the credential note is not cleared by a success and not started by a `401`, and no
  `last_ok` note or try is recorded (`Atomic` in `test_card7c2_state.py`, 2 tests, they passed
  already).
- **A 202 completion, then a lease 401, then recovery by a later successful limit request.** Already
  worked; now a test: `Credential` in `test_card7c2_delivery.py`.

### Not in this round

Sections 16.2 to 16.5 (sample clocks, the stated one-outbox limit, folder identity, the reader table)
are for step 3.
