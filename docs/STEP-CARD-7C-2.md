# Step record - Card 7c-2: usage-limit events from the park marker

Work item `card-7c-2`. Branch `card-7c-2`, from main `da91e1f`. Contract:
`C:\lw\records\DESIGN-7C2.md` (the lead's decisions in its section 15 win). This record grows one
step at a time; all four steps and the fix rounds are below. **Card 7c-2 is built.**
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
- Written at step 1: steps 2 and 3 (below) built the reporter's outbox and client, the marker reader
  and the account map. Step 4 (the wait cap, `status`, the README gap statement and the end-to-end
  test) is not built yet.

## Step 2: the reporter's outbox and client for limit events

Built on step 1 (`437acb6`), in four small commits so a change from review lands cleanly:
decisions, state, client, scheduling.

### In plain words

Once the reporter knows of a limit, the event must reach Leasewise even across restarts and
outages, and nothing else in the reporter may hold it back. This step builds the safe queue (the
outbox) for limit events and their sending. It does not read park files; that is step 3 (below). The
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
| `test_card7c2_delivery.py` (15) | a parked account refusing every lease never delays a limit row (same cycle, limit path first); a denied acquisition that stops its agent does not either; oldest first; a refused row fails alone; the passes stay separate (a limit row with no answer does not stop the lease pass, fix round 1); a back-off row is skipped; `401`; every try resends the same bytes across a restart; a sent row is never resent; no body value or exception text in errors, notes, counters or status |

Each file was run red against the code before its commit (decisions: 9 tests, 24 errors before;
state: 22 tests, 1 failure and 44 errors before; client: 11 tests, 14 failures before; delivery: 13
tests, 12 failures before).

### Limits (stated, not hidden)

- `status` does not yet show limit events (step 4). The park files are read by step 3 (below).
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

Tests, `tests/test_card7c2_replay_domain.py` (8). **This file needs Git and the historical commit
`da91e1f` in the repository, so it cannot run from a source archive or a shallow clone.** Each event is
recorded **the old way** by running the limit-event code from the repository's own history
(`da91e1f`), at `INT64_MAX`,
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

## Step 3: the account map, the strict reader and the event

Built on fix round 1 (`6440fca`), in small commits: config, policy-ids accounts, reader, event and
capture, wiring. Sections of `DESIGN-7C2.md` 1 to 5, 8, 10 and 11, the lead's decisions D1, D7 to D12
(section 15) and section 16.2 to 16.5.

### In plain words

This step makes the reporter actually notice a park. The operator's settings say which account each
agenttalk folder's Claude seats use. The reporter reads the park files in those folders strictly:
anything unexpected gives no event, and a file that may just be half written is looked at again on
the next scan. For each fresh, valid file it builds the one frozen event for that account and limit
and puts it in the step 2 queue. The wait cap, the status output, the README gap statement and the
end-to-end test are step 4.

### What changed

- **Config (`config.py`).** A new optional `limit_events` block of sources: `bus_root`, `os_user`,
  `provider`, `account`. Closed keys, every field required, `provider` only `claude` in v1 (the rule
  is on a list, so a later `codex` source on the same folder needs no new rule), at most 8 sources.
  **Uniqueness** is on the RESOLVED folder (`realpath` plus `normcase`) with the provider, never on
  the text in the file. **Consistency:** one (`os_user`, `provider`) must name one account. The bus
  folder may not contain the state file or the configuration file. A configuration without the block
  is exactly as before. Errors are fixed sentences that repeat no value.
- **`policy-ids` accounts (`store.py`, `config.py`).** `policy-ids` lists `accounts: [{alias, id}]`,
  sorted, aliases and local ids only. `load_snapshot` accepts the list as optional (an older snapshot
  has none). `resolve_limit_sources` gives each source its account id and refuses to start
  (`CONFIG_UNRESOLVED`) when the account is missing, ambiguous (never picks the first) or has an id
  that is not a UUID. The `reporter` command resolves it at start.
- **The strict reader (`parkreader.py`).** Bounded listing (256 entries, `.json` names whose stem is an
  agent name, links and reparse points skipped), bounded files (4,096 bytes), one flat JSON object
  with exactly the eleven documented keys, no duplicate keys, no `NaN` or `Infinity`, whole numbers
  only (a boolean is not a number), every value checked. Every file ends as OK, READ_LATER (missing,
  cannot be opened, torn, or a documented key is missing) or REFUSED (an undocumented key, a wrong
  type, out of range, another version or provider). A fractional `parked_at` is converted to
  milliseconds counted down, never rounded up (D11). Only five values are kept; the agent name,
  message id and generation are checked and dropped.
- **Event and capture (`mapper.py`, `capture.py`, `state.py`).** The key, the ids and the body are as
  in the spec (D8, D12): `event_id` and `turn_id` are uuid5 under the reporter's namespace with the
  prefixes `limit|` and `limit-turn|`; `observed_at` is `parked_at` in milliseconds; `resets_at` is
  RAW; `exhausted_windows` is `[window]`; no `model_id`. Fresh only (300 seconds, judged from the
  marker's own time on the reporter's clock); a window or a park time of `null`, a stale marker and a
  reset already passed give no event; a provider with no source in that folder is `unmapped`.
  `State.record_capture` writes new rows, the cumulative `captured` count and the LAST scan's counts in
  one transaction; a row that already exists is never touched.
- **Wiring (`run.py`, `cli.py`).** `cycle` (so `run` and `drain`) is read, capture, send. A fault in
  capture is caught whole, counted as `capture_fault`, keeps no text, and never stops lease sending.
  The 60-second wait cap waits for step 4.

### Tests (`tests/test_card7c2_*.py`)

| File | What it proves |
| --- | --- |
| `test_card7c2_config.py` (19) | the block, closed keys, values, 8 sources, uniqueness on the resolved folder (trailing separator, `..`, forward slashes, case on Windows), room for a later codex source, consistency, the state and configuration file kept out of a bus folder, no value repeated in an error |
| `test_card7c2_accounts.py` (11) | `policy-ids` accounts; the optional list; malformed lists; resolution; a missing, ambiguous or non-UUID account refuses start; no value in the message |
| `test_card7c2_reader.py` (32) | valid markers (both real samples, each with its own fitting clock; one clock cannot fit both and the 9-day bound is not relaxed); torn files, a missing key, a sharing violation, a vanishing file; version 2, `true` as the version, an unknown provider, an extra key, wrong types and ranges, nesting, duplicate keys, `NaN`, oversize; listing rules, a symbolic link, 256 entries; privacy |
| `test_card7c2_capture.py` (30) | the identity matrix (two seats one account; two messages one seat; a later reset; fresh to stale; a remapped account; two unknown resets; a re-park after another failure with an unknown reset is new and with a known reset is not); the frozen body; reset passed; null window or park time; unmapped; torn and refused files counted; one transaction |
| `test_card7c2_wiring.py` (13) | capture and delivery in one cycle, `drain`, a parked account refusing every lease, a fault in capture, no capture without sources, the command refusing to start on a missing or ambiguous account |

The existing tests that changed: `tests/test_cardl1c.py` `test_the_shape_is_closed_and_sorted` and
`test_an_empty_store_lists_nothing` (the `policy-ids` listing gained `accounts`).

The reader, the config and the capture tests were checked with deliberate faults (a mutant that
refuses a missing key, accepts a boolean version, allows duplicate keys, rounds the park time up,
drops the entry cap, the size bound, the stale check, the reset-passed check, the machine in the key,
adds the margin in the reporter, and so on): every one made tests fail. The reader and the capture
code were written before their tests this time; the tests were then shown to have teeth that way.

### Limits (stated, not hidden)

- **The map is trusted (R-011).** The files prove nothing about the account. If someone signs a
  different account into the same OS user, the map is wrong until the operator edits it.
- **One outbox, one memory (spec 16.3).** The frozen body holds only while the same state file is
  kept. If the outbox is recreated, or two reporters run with the same `machine` alias, the same
  `event_id` can be rebuilt with a different first-seen `observed_at`: Leasewise answers `409` and that
  row fails for good, shown in `status` (step 4). There is no deduplication across outboxes. This is
  in the README "Limits".
- **Best effort (R-012).** A park that starts and ends while the reporter is not running, or between
  two scans, is never reported. The statement in `status` and the README section are step 4.

## Fix round 2 (Codex's read of step 2 and fix round 1, tk-5fb0bb40fd91; the last for steps 1 and 2)

### In plain words

Three small things were left. None of them loses an event.

1. **The queue's privacy promise was only a spelling check.** It was meant to accept only known window
   words, but accepted any short lowercase word, so a private phrase could have been stored and sent.
   It now accepts only `five_hour` and `seven_day`, the producer's own words, from ONE list that the
   park-file reader shares, so the two can never differ.
2. **A malformed evidence value crashed instead of being refused.** A list or an object raised a raw
   error. The type is now checked before the value, so it gets the queue's fixed refusal.
3. **The cycle summary could hide a login problem.** When the limit pass got "not authorised" (401)
   and the lease pass then found the server down (503), the summary said only "unreachable"; the
   other way round it said "credential". The two outcomes are now ranked: a credential refusal from
   either pass shows as `credential`, above `unreachable`, above nothing. The rule is simple and does
   not depend on order. Both passes still run; each stops only on its own wall.

### Tests (`tests/test_card7c2_fix2.py`, 10 tests; red first: 6 errors and 5 failures before)

- A closed window vocabulary: both producer words accepted (one, both, none); `synthetic_private_prose`
  and other short lowercase words refused by the body builder AND through `add_limit_event`; entries
  that are not text refused; the reader and the queue share one tuple (`is`).
- Type before membership: a list, an object, `None`, a number, a boolean, bytes or a tuple as
  `evidence_kind` give the fixed sentence "a limit event body is not valid", with no row inserted.
- The summary: 401 then 503, 503 then 401 and 401 then 401 all give `credential`, both passes ran,
  both rows are still pending with one try, the credential note is kept; 503 then 503 gives
  `unreachable` with no credential note; the failure streak counts one blocked cycle.

Codex's probes (`test_7c2_delta_probes.py`, copied into scratch, never edited, pointed at this
worktree): 7 of 7 pass, including the four that failed.

### Where the text changed

The comment in `run.py` that said "Stop this cycle" now says this pass stops and the other still runs,
and the delivery-test row above no longer says the cycle stops before the lease pass. The
`replay_domain` test needs Git history (see above).

## Fix round 1 for step 3 (Codex's read, tk-6deb0398d743)

### In plain words

Two limits in the park-file reader were not real limits yet, and one README sentence was out of date.

1. **The folder walk is now bounded before anything is collected.** The reader opened at most 256 files,
   but it still walked, checked and remembered every entry in the folder and sorted them all first. A
   folder with many entries could have delayed lease sending and used memory without bound. Now the
   walk stops after 256 directory entries visited (ignored names count too); only then are the names
   kept sorted and read. One more entry is looked at only to learn that there is more, and the scan says
   so with a flag (`too_many_entries`, counted as 0 or 1 per folder), never a count of the folder.
   **Consequence, stated:** in a folder with more than 256 entries, some markers may not be seen in
   that scan (the first 256 in the folder's own order are looked at). In practice a park folder holds
   one small file per parked seat. The README "Limits" says so.
2. **The wake time has a near limit now.** `reset_epoch` may be at most 9 days ahead, but `wake_epoch`
   was only checked against the year 2100. It is now checked against the same range plus the
   wrapper's offset, named `WAKE_AFTER_RESET_SECONDS` = 30 (the wake is the reset plus 30 seconds).
   Out of range is refused. It is still never sent.
3. **Stale sentences.** The README said "Nothing reads the park files yet (step 3)" and "comes in the
   next steps"; this step record said steps 2 to 4 were not built. Updated; step 4 is the one that is
   still to come.

### Tests (red first: the reader file had 3 failures and 4 errors, the capture file 1 error)

- `Bounded` (5, in `test_card7c2_reader.py`): with a fake `scandir` that counts how far it was
  advanced, 1,000 entries give at most 257 advances (256 and the one look-ahead) and at most 256 names
  read; 1,000 ignored names also stop at 257 advances and read nothing; a mixed folder keeps only what
  it visited; a normal folder is unchanged; the names kept are still read in sorted order.
- Exactly 256 files give no flag; 261 give the flag and 256 markers.
- `WakeBound` (3): the reset's range plus 30 seconds is accepted and one second more refused; `0`,
  the year-2100 value `4102444800` and `MAX_EPOCH` refused; `null` and a wake already in the past
  accepted; a reset at its own edge still has room for its wake; the wake is never kept.
- Capture: the count `over_cap` is replaced by the flag `too_many_entries`.

Codex's probes (`test_7c2_step3_review_probes.py`, copied into scratch, never edited): 18 of 18 pass,
including the two that failed.

## Step 4: the wait cap, status, README, privacy and the end-to-end test

Built on step 3 fix round 1 (`c1e1d93`), in small commits: wait cap, status, README, privacy and end to
end, this record.

### In plain words

After this step an operator can see in `reporter status` that limit events are on and what they are
doing, and the status says plainly, in the words claude-agenttalk-lead approved, that capture is best
effort. The README explains the feature and its limits in one section. While limit capture is on, the
reporter never waits more than 60 seconds between looks. One end-to-end test proves the whole path: a
park file, the reporter, the real Leasewise handler, and the account parked until the reset plus 30
seconds.

### What changed

- **The wait cap (D5, `run.py`).** While limit capture is configured the wait between cycles is at
  most 60 seconds (`LIMIT_CAPTURE_MAX_WAIT_SECONDS`), even while Leasewise is not answering (it could
  be 900) and even if `poll_seconds` is set higher. Without limit sources the wait is exactly as before.
- **Status version 2 (D3, `status.py`).** A top-level `limit_events` object: `configured`; the account
  map in use as the operator wrote it (an OS-user label, the provider, an account alias, so the map can
  be checked: the only defence against R-011); the last scan's counts, including `too_many_entries`;
  `captured` (cumulative); the numbers of pending, sent and failed rows; failed rows by closed code;
  `capture_fault` as a count and a time; the time of the last good delivery; and the best-effort
  sentence. `render_text` prints one line, `limit events (best effort): <sentence>`, when limit events
  are configured and nothing when not. A failed limit event and a capture fault each add an operator
  action. Closed words and counts only. The existing test that pins the status keys gained
  `limit_events`.
- **The gap sentence (R-012).** `status.LIMIT_GAP`, word for word as approved, is byte-identical in the
  status and the README (a test checks it). It names what closes the gap: a permanent "parked" record in
  agenttalk's turn journal, a later agenttalk change after pull request #300 merges, together with Codex
  limit events.
- **The README.** The step 1 to 3 notes are merged into one "Card 7c-2 - limit events" section: what it
  does in plain words, how to set it up, what is read, the limits, and a short technical detail.
- **The test rig.** `tests/reporter_env.py` `Rig.open` resolves the account map the way the command
  does.

### Tests (`tests/test_card7c2_*.py`; 35 new in this step)

| File | What it proves |
| --- | --- |
| `test_card7c2_wait.py` (5) | the 60-second cap with limit sources, even in a long outage and with a slow poll setting; without sources the wait is as before |
| `test_card7c2_status.py` (12) | version 2; the closed shape; not configured says so and carries no gap; the map in use; the sentence verbatim and exactly one text line; counts after a capture and a delivery; the scan counts describe the last scan only; a refused row by closed word with an operator action; a capture fault as a count and a time; pending while Leasewise is down; the command; a running reporter's snapshot |
| `test_card7c2_readme.py` (7) | the sentence byte-identical in the README and the status; one section with every limit; plain words before technical detail; no stale sentences; the TOML example is a valid block |
| `test_card7c2_privacy.py` (5), **privacy_7c2** | loud markers in every string field of a marker, a loud folder, torn, refused and denied files and a loud server answer: none appears in the wire, the stored rows, the status JSON and text, the notes, the counters, the logs or anything the command prints; the wire body has exactly the allow-listed keys; a canary shows the search would notice a leak |
| `test_card7c2_e2e.py` (6) | against the real in-process Leasewise handler: the account is parked with `retry_at` = reset + 30 000 ms and the raw reset kept; a second cycle sends nothing; a second seat adds no event; a later reset moves the hold; a parked account refuses leases while the limit event still got through; a restart loses and repeats nothing |

Red first: the wait, status and README tests were run before their code (wait 5 tests, 1 failure and 1
error; status 12 tests, 3 failures and 9 errors; README 7 tests, 3 failures and 2 errors). The privacy
and end-to-end tests describe code that already existed; to show they have teeth I ran deliberate
faults against them and the sentence, version and wait tests (the margin set to zero, capture not wired,
the seat in the event key, the sentence changed, the status version left at 1, the cap at 600): every
one made tests fail.

### What exists now

- **Leasewise:** a limit event parks the account until the reset plus 30 seconds; the raw reset is kept;
  replays are pure reads and keep the old domain; `policy-ids` lists accounts.
- **The reporter:** the account map and its checks; the strict park-file reader; one frozen event per
  account and limit; a durable queue with its own pass, sent first; capture in every cycle with a
  60-second cap; status version 2 with the gap sentence.
- **Evidence:** the full suite at the end of step 4 is 1,353 tests in three chunks (454 + 383 + 516,
  all passing, 2 skipped), equal to the `discover` collection and covering all 52 test files once; the
  end-to-end test; the privacy test; and the fix rounds' reviews by Codex.

### Residual risks (stated, not hidden)

- **R-011, the map is trusted.** If someone signs a different account into the same OS user, the map is
  wrong until the operator edits it, and the reporter cannot see the switch.
- **R-012, best effort.** A park that starts and ends while the reporter is not running, or between two
  looks, is never reported. It closes with agenttalk's permanent "parked" record in its turn journal.
- **One outbox, one memory.** A recreated state file, or two reporters with one `machine` alias, can
  give a `409` that fails that event for good.
- **256 entries.** In a folder with more than 256 entries some markers may not be seen in that scan.
- **No recovery yet (card 7c-3).** Leasewise never un-parks an account by itself; an operator clears the
  block. A reset already in the past still parks, with a `retry_at` in the past.
- **Advisory until R1.** A missed event costs a missing warning, not a wrong refusal.
- **Claude only.** Codex limit events wait for agenttalk's Codex producer (agenttalk 7c-1c).

### What is next

Card 7c-3 (recovery: a coordinated probe and the way a parked block ends) and card 7b (readings, after
agenttalk issue #301). The agenttalk journal record that closes R-012 is a later agenttalk change.
