# Step record - Card L6a-1: gateway receipts settle quota leases (revised)

Work item `card-l6a-1`. Branch `card-l6a-1`, built in two steps on main `363c87e` (card R1-0 merged).
Contract: `C:\lw\records\DESIGN-6A.md` (sha256 `626d9534...`), sections 5.4, 8.3, 8.4, 8.5 (status only), 8.6
(the `admission` table and its read-only reader only), 8.8 and 19. Interpreter: CPython 3.13 (`C:\lw\venv`),
standard library only. Audience: the lead and the reviewer. Status: **built, fixture-only.**

## In plain words

A gateway keeps one final receipt for each quota lease it served: calls made, text in and out, cost, and how it
ended. This card teaches the reporter to read those receipts and send each one to Leasewise as the final
"settle" of its lease.

The first build did that, but it only kept a receipt if it already knew the lease, and it could not say what
to do when the gateway rebuilt its list. This revised card replaces it with a **chain of custody**:

1. **Keep.** Every receipt is written down exactly as received, with the number of the last receipt taken, in
   one step, even when the reporter cannot yet place it.
2. **Release.** A kept receipt is released for sending only when a record in the admission store names its lease
   and the very gateway the receipt came from. The settle text is made once, at that moment, and never changes.
   A receipt with no record waits (`unknown_reference`); one whose record names another gateway is set aside for
   good (`wrong_gateway`). Both are looked at again each cycle, and nothing is sent for them.
3. **Send.** Only the stored text is sent, byte for byte, again and again if needed.

Step 1 built the chain. Step 2 made it visible (the status), proved that a stop at any hand-off loses nothing and
sends nothing twice, ran one end-to-end settle against the real in-process handler, and wrote the README.

## Honest note on how the tests were written

The step 1 tests were written together with the code, not strictly red first. In step 2 the status tests were
written first and failed (the new words did not exist); the crash-point tests were written before the three new
fault points in `run.py` and then checked by two deliberate breakages (a hand-off moved; the release no longer
sets `pending`), which made all 14 of them fail before the code was restored.

## What the card does, link by link

- **The page** (`reporter/receipts.py`): one versioned envelope (`envelope_version` 1, the ledger `generation`
  even when empty, `after_seq`, `next_seq`, `has_more`, receipts with nullable token counts and an ISO close
  time). A wrong version, a key that is not allowed, a wrong type or bound, a repeated key, a gap or repeat in
  `seq`, or an `after_seq` that is not the one asked for refuses the whole page: nothing is stored, a counter goes
  up. A missing file, a command that fails or is too slow, or text that is not JSON is "read again later".
- **The admission store** (`reporter/admission_store.py`): the one module that holds the `admission` layout
  (reused later by the admission command) and the reader `read_bindings`, opened `mode=ro` with `query_only`.
  Missing, busy or unreadable means "read again later", never an error and never proof that a lease is unknown.
- **State version 3** (`reporter/state.py`; migrates from 1 and 2 only): `receipt_source`, `receipt_binding`,
  `receipt_capture` and `receipt_delivery` are immutable; `receipt_cursor` is updated as pages are taken (and is never
  deleted); `receipt_state` is the table whose rows change status (`captured`, `quarantined`, `pending`, `sent`,
  `failed_permanent`, with a closed reason). Stage 1
  (`capture_page`) and stage 2 (`release_waiting`) are each one `BEGIN IMMEDIATE`.
- **Source ownership** (requirement 6): a source is a name plus the gateway it was bound to, for good. A capture
  freezes that gateway; release compares the binding with the frozen gateway, never with today's configuration.
  A name mapped to another gateway is a new source with its own cursors.
- **Generations**: a page from another generation switches the current generation (a new one starts at 0; one seen
  before takes its kept cursor back, so A, B, A continues A). The page is used only if it starts at that cursor.
- **Delivery**: the pass order is unchanged (limit, receipt, lease). Only `receipt_delivery.body` is sent. The
  answer table of 8.4 is in `decisions.py`: `200` done; `LEASE_CLOSED` is a permanent failure of that row (a settle
  never answers it); `RECEIPT_CONFLICT` and the other refusals fail that row for good; `401`, `429`, `5xx` and no
  answer retry the same bytes. A failure never erases custody and never stops other rows or passes.
- **Status** (`reporter/status.py`, `status_version` 3, new in this unmerged card so no older-version handling
  beyond the existing one is needed): `gateway_receipts` carries `rows`, `failed`, `quarantine` with
  `quarantine_action` (one fixed sentence per reason), `generation_changes` (per source: how many, when the latest
  appeared), `awaiting_receipt` (count and age of the oldest) and `overdue` (awaiting and past the lease's own
  expiry), `refused`, `read_later`. A snapshot without the new words still renders as text.
- **Config**: `admission_store` (an absolute path) in `[gateway_receipts]`; fixed-sentence refusals as before.
- **Fault points** (`run.py`, a test seam like the existing `answered` and `applied`): `read`, `captured`,
  `released`.

## Fix round 1 (after the merge-gate read)

Codex read the card at `a41b8e60` and found that the receipt intake keeps everything safely and survives crashes, but
could stall, could misroute, was too lenient in two page checks and hid trouble with the admission store. This round
fixes each finding; the numbers are Codex's.

| Finding | What was wrong, in plain words | What it does now | Tests |
| --- | --- | --- | --- |
| F1 (major) fair release | the reporter looked at the oldest 500 unreleased receipts every cycle, so 500 that could never be released kept every newer one waiting for ever | new receipts are labelled in one statement; the ones with a record are released first; `wrong_gateway` and `duplicate_reference` are permanent and leave the selection (still counted in `status`); `unknown_reference` ones are re-checked through a rotating window after a cursor kept in the state, so every one is looked at again over time | `test_card7l6a1_state.FairRelease` (1,200 unknown ones then a late record; 650 wrong-gateway ones; duplicates; a window of 20 over 100 rows across restarts) |
| F2 (major) source deadline | a slow source command, or one whose child kept its output open, could freeze every upload | ONE deadline covers reading, exit and cleanup; a reader thread; the command runs in its own process group (a job object on Windows, `reporter/proctree.py`) and on a timeout, on too much output and after every run the whole tree is ended by exact process id | `test_card7l6a1_adapter.Command` (a parent that exits while its child holds the output; a program that never stops; a writer that sends too much; a good run): each returns within the deadline plus 2 s and leaves no process |
| F3 (major) relations | a damaged store could send one lease's receipt to another | before anything is copied, the reference must equal the lease id and the hash must be the SHA-256 of it; any mismatch refuses the whole read | `test_card7l6a1_admission_store` (the mismatching row), `test_card7l6a1_status.StoreTrouble` (through the whole reporter: nothing is copied or sent) |
| F4 zero calls | `calls` = 0 with unknown counts was accepted | with no call, both counts and the money must be the number 0; the whole page is refused, nothing is captured, the cursor stays | `test_card7l6a1_adapter.Pages`, `test_card7l6a1_status.Report` |
| F5 page limit | any page up to 1000 was accepted whatever was asked | a validated `page_limit` (1 to 1000, default 100); the reporter adds `--limit` itself (a command that names it is refused); a longer page is refused before anything is stored | adapter `Pages` and `Fixture`, `test_card7l6a1_config` |
| F6 store trouble hidden | status said nothing about the admission store | `status` shows its current state (`ok`, `missing`, `busy`, `unreadable`, `history_limit`) and counts of past failures, in the JSON and the text, with a sentence for the ones that need you | `test_card7l6a1_status.StoreTrouble` |
| F7 history limit | more than 100,000 granted rows meant "unreadable" | its own state `history_limit` with its own sentence. **The limit is exactly 100,000 granted rows: above it no new binding can be copied and later receipts stay unknown.** Reading only what is new is a prerequisite for live use and comes with card L6a-2 (a durable grant order value). Admissions are never to be deleted | `test_card7l6a1_admission_store`, `StoreTrouble` |
| F8 exactly 200 | `201` counted as done | a settle succeeds only on `200`; any other 2xx is retried with the same bytes and shows `MALFORMED_RESPONSE` | `test_card7l6a1_delivery` |
| F9 words | README and status text were wrong in six places | `gateway` not `ovh-gateway`; the 7c-2 state paragraph is marked historical; `receipt_cursor` is not immutable; an omitted `admission_store` setting versus a missing file; `RECEIPT_CONFLICT` has its own action ("look at the parked account"); "keeps its hold" says an advisory lease holds nothing | `StoreTrouble.test_a_receipt_conflict_...` |

Honest note: F1 and F2 were designed against Codex's description; the new F2 process tests hang on the old reader (the
orphan test was started against it and did not return within 100 seconds) and pass on the new one. The F1 design changed
once while writing its tests: the first version labelled only 500 new captures a cycle, which made a late record for a
capture not yet labelled wait several cycles; labelling is now one statement.

## Requirements 6 and 7 (DESIGN-6A 19.1), and the tests that prove them

Requirement 6, source ownership frozen at capture:

| Part | Test |
| --- | --- |
| A source is its name and gateway for good | `test_card7l6a1_state`: `test_a_source_is_its_name_and_its_gateway_for_good`, `test_a_source_cannot_be_rebound_or_deleted` |
| Capture an unknown reference, restart with a changed mapping, the binding arrives: no release | `test_card7l6a1_state`: `test_a_changed_source_mapping_never_releases_an_old_capture_under_the_new_mapping`; whole reporter: `test_card7l6a1_crash.Mapping` |
| A binding for the frozen gateway still releases | `test_a_binding_for_the_frozen_gateway_still_releases_after_the_mapping_changed` |
| A new source has its own cursors | `test_the_new_source_starts_its_own_cursors_and_captures_alongside_the_old` |
| A, B, A reuses A's kept cursor | `test_a_generation_seen_again_takes_its_kept_cursor_back_a_b_a`, `test_a_new_generation_starts_at_zero_and_the_old_cursor_is_kept` |

Requirement 7, the parts for this card:

| Part | Test |
| --- | --- |
| Exact types, identifier bounds, the page size cap | `test_card7l6a1_adapter`: `test_the_envelope_values_have_exact_types`, `test_the_bounds_are_enforced`, `test_too_many_receipts_are_refused`, `test_text_that_is_not_json_is_read_later_and_too_large_is_refused` |
| A missing or busy admission store | `test_card7l6a1_admission_store`: `test_a_missing_store_is_read_later`, `test_a_busy_store_is_read_later_and_never_an_error`, `test_a_writer_in_the_middle_of_a_transaction_shows_only_what_was_committed`; whole reporter: `test_card7l6a1_status.Custody.test_a_missing_or_busy_admission_store_keeps_the_receipts_and_is_read_again_later` |
| The real handler: advisory acquisition and null-count settlement | `test_card7l6a1_e2e`: `test_an_advisory_lease_settles_from_a_receipt_with_unknown_input_and_is_marked_usage_unknown` |
| Exact-byte resend after a lost reply | `test_card7l6a1_crash`: `test_5_a_stop_after_the_send...`, `test_a_lost_200_resends_the_same_bytes...`, `test_a_resend_after_a_retry_and_a_restart_is_byte_for_byte_the_first`; real handler: `test_card7l6a1_e2e.test_a_lost_200_against_the_real_handler_is_replayed_to_the_same_settle` |
| Privacy | `test_card7l6a1_privacy` (6 tests: a loud string appears nowhere; the stored rows hold only allow-listed fields; the command prints nothing private) and `test_card7l6a1_status.Custody.test_none_of_it_names_a_lease_a_reference_a_gateway_id_or_a_path` |

## The crash table (8.3) and its tests

| Stop | What is left | Test (`test_card7l6a1_crash`) |
| --- | --- | --- |
| after the read, before stage 1 | nothing stored; the page is read again | `test_1_...` |
| after stage 1 | evidence and cursor; released next cycle | `test_2_...` |
| during stage 2 (between the delivery insert and the state update) | both rolled back; released again, same bytes | `test_3_...`; `test_card7l6a1_state`: `test_a_fault_between_the_delivery_insert_and_the_state_update_rolls_both_back`, `test_release_after_the_fault_builds_the_identical_body_event_id_and_bytes` |
| after stage 2, before the send | a pending delivery, sent from its stored bytes | `test_4_...` |
| after the send, before the answer is applied | the same bytes again; the replay is a `200` | `test_5_...`, `test_a_stop_after_the_answer_is_applied_sends_nothing_again` |

Each is repeated with three receipts (`test_with_three_receipts_...`): none lost, none sent twice beyond the one
idempotent replay.

## Tests

The L6a-1 files, after fix round 1: adapter 47, admission store 17, state 40, delivery 22, config 24, status 36,
end to end 15, privacy 6, crash 15 (222 in all). Step 2 added 11 status, 3 end to end and 15 crash tests; fix round 1
added 31 tests.

## Gates

Memory free at the start of fix round 1: 8.5 GB. The full suite, in chunks each under 8 minutes, proven to cover every
test file once (16 + 1 + 7 + 2 + 1 + 2 + 36 = 65 files, equal to the 65 `test_*.py` files):

| Chunk | Tests |
|---|---|
| `test_card[1-4]*.py` | 454 |
| `test_card5*.py` | 122 |
| `test_cardl1*.py` | 215 |
| `test_cardl6*.py` | 40 |
| `test_cardr1*.py` | 16 |
| `test_billing*.py` | 46 |
| `test_card7*.py` | 749 (2 skipped, as before) |
| **Total** | **1642** (equals the `discover` collection) |

`ruff --select F` is clean on the changed files.

## Limits (DESIGN-6A 19.2, the ones that apply here)

- Fixture-only until agenttalk's receipt command ships; the page format is our reading of the design and lives in
  `receipts.py`. Live gateways need the X3 upgrade.
- Receipts and captures are kept without compaction. A replaced ledger cannot rebuild old missing receipts from
  the binding alone; awaiting work stays visible (`awaiting_receipt`, `overdue`).
- A missing receipt keeps its liability and never becomes a guessed zero; nothing else settles that lease.
- Cached input is reported as 0: a convention, not a measurement.
- Journal suppression and `gateway_agents` belong to card L6a-2; the observe-only acquisition before an agent is
  declared is a transition limit there, not a second authority.
- Everything stays advisory until the enforcement gates (6a-3, R1, 6b).
- A refused source stays stuck until it is fixed; `status` asks for attention. The state file cannot be downgraded.
