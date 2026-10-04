# Step record - Card R1-READER, step 3a: the money reader's send side, the refusal budget, and the operator's `resume` and `reset`

Work item `card-r1-reader-3a`. Branch `card-r1-reader-3a` stacked on `card-r1-reader-2` at `e378c01a2577182b5c0c0cecbd116210e08b0857` (it merges after step 2;
if step 2's head changes it is rebased onto it). Design: `C:\lw\records\DESIGN-R1-READER.md` (revision 4, SHA-256 `1de762ac...db5d`): D-195 and D-203, 3.1 rules 1 to 6,
8 steps 1, 3 and 9, 9, 12, 13, 14. Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only. Synthetic values and a fake transport only.

## In plain words

The money reader keeps at most one report waiting, saved as exact bytes. This step sends it and reads Leasewise's answer, and gives the operator two local,
audited commands to recover it. **Nothing reads a gateway yet:** step 3b builds the configuration, the whole cycle and the report bytes. Here the sender simply
sends whatever is pending, and with nothing pending it does one cheap read and nothing else.

## What was built

1. **The closed answer reading** (`gwsend.classify`; the posting is `client.post_reading`, which keeps no text and never raises). Exactly one of four classes:
   accepted (HTTP 202, a JSON object, `accepted` exactly `true`, the sent event id; then step 2's `accept`), conflict (409 and the closed code `IDEMPOTENCY_CONFLICT`:
   halt `continuity_conflict`), definitive refusal (400 and `INVALID_CONFIG` or `SCHEMA_UNSUPPORTED`) and retry (everything else, including a 200 or 201, a 202 that is not
   the exact acceptance, 401, 429, 5xx, no answer, a timeout, an unreadable body and an unreadable success): the same bytes are kept, the word is `send_failed`.
2. **The refusal budget.** `gwstate.REFUSAL_THRESHOLD = 5` consecutive definitive refusals (a small positive named constant). The count is on the pending row
   (`gwr_pending.refusals`), so a restart keeps it; reaching the threshold halts `send_refused`; a retry does not reset it; an acceptance clears it with the row.
3. **`resume`** (`gwstate.resume`; `reporter resume --confirm`): only after `send_refused` (otherwise `NOT_RESUMABLE`, nothing changed; never after
   `continuity_conflict` or `state_unavailable`). One transaction: the halt is cleared, the consecutive count goes back to 0, an audit row is written. The cumulative
   counts (`refused_answers`, the others) are kept. The next send resends the same saved bytes.
4. **`reset`** (`gwstate.reset`; `reporter reset --confirm`): step 2's transaction plus an audit row and the quarantine form. If the old value cannot be archived
   under the archive's normal constraints (Codex's case: a pending `event_id` of `broken`; also a damaged incarnation), the archive keeps the WHOLE old state as raw
   evidence in a bounded `quarantine` column (every column of every row, bytes as hex) and discards nothing. **The bound is 256 KiB** (`QUARANTINE_MAX_BYTES`). A state
   too large for it is refused as `reset_too_large` (command code `RESET_TOO_LARGE`) with nothing changed. A fresh identity starts without importing an unfinished sequence.
   **File-level recovery** for a state too large to quarantine: stop the reporter, move the reporter's state file aside and keep it (it is the evidence), start the
   reporter again; a complete absence of state is a first start (D-203) with a fresh identity, which Leasewise then fences until the operator adopts a reader.
5. **The report's own lane** (`Reporter._send_report`, run first in `_send`, its own pass): a report that needs a retry never stops the limit, receipt or lease
   passes, and their retries never stop it (each pass is separate); only one report can be pending (the state allows no second row), and nothing mints while one is.
   A retry of the report counts toward the cycle's `blocked` rank (a 401 as credential, the rest as unreachable) so the back-off still works.
6. **Status** (`money_reader` block; status version 3 to 4): `pending`, `consecutive_refusals`, `refusal_threshold`, `halt`, and `counts` of `send_failed`,
   `send_refused`, `continuity_conflict`, `reader_halted`, `state_unavailable`, `refused_answers`, `resumed`, `resets`. A halt adds one fixed operator sentence.
   Numbers and closed words only; no value from a report, an answer or the identity.

## Choices made (each tested)

- **Counts are counters.** The five closed words are counted in the reporter's existing `counter` table under the reserved agent word `_money_reader` (it starts
  with an underscore so it can never be an agent's name). The cumulative total of refusals survives `resume` there.
- **`state_unavailable` is counted by the sender** each time a pending report meets a damaged state, and a halted reader counts `reader_halted` each cycle.
- **The audit** is a new immutable table `gwr_audit` (act `resume` or `reset`, time, whether the reset was quarantined, the cumulative refusals at the time).
- **Reporter schema version 6 (a step of its own).** The archive's `quarantine` column and `gwr_audit` are NOT part of version 5: step 2 is merged on main
  (`c163535`) with version 5 and neither of them, and a version number is a promise that every file at that version has the same tables. So version 5's definitions
  are exactly main's, and a step 5 to 6 adds the column (bounded to 256 KiB) and the table with its two immutability triggers, in one transaction (a fresh file
  runs the version-5 definitions and then the same step, so a fresh file equals an upgraded one). A reporter that knows only version 5 refuses a version-6 file. My
  earlier note that these were added to version 5 "because step 2 is not merged" was wrong.
- **An exact acceptance the state will not take** (damage that appears while the request is out) keeps the report, runs the start-up check (which halts) and
  counts `state_unavailable`.
- **A reporter with no money reader is unchanged:** with nothing pending the sender reads one row and creates nothing (a test shows the identity table stays empty).

## Tests (`tests/test_cardr1reader3a_send.py`, 36)

`Classes` (9): the exact acceptance; ten answers that are not (`"true"`, 1, false, another id, no id, null id, no `accepted`, 200, 201, 203); the collision and the
halt after it; a 409 with another code; each 400 code and another 400; **twelve retries** (401, 403, 429, 500, 503, no answer, a timeout, unreadable JSON, an unreadable
success, empty, a list, the identity echoed) with the same bytes each time; nothing pending; a damaged state; an acceptance the state will not take. `Budget` (4): the
threshold is a small positive constant; n-1 keeps it and the n-th halts; a retry in between does not reset; the count survives a restart and an acceptance clears it.
`Interruption` (1): a failure at each real SQL write of the acceptance, before and after it runs, leaves the old state (no third) and the same answer then applies.
`Resume` (4), `Reset` (6: normal; Codex's damaged `event_id` through the quarantine, raw evidence kept; a damaged incarnation; too large; large but healthy archives
normally; a failure at each hook point of a quarantined reset), `Commands` (5, through `reporter_main`): no `--confirm` changes nothing; resume; resume refused after a
collision or a damaged state; reset prints the new identity; a state owned by a running reporter is refused. `Lane` (7, a whole reporter cycle): the report's retry
beside a successful upload, and the reverse; a credential refusal ranks above unreachable; a halted report does not stop the others; no mint while pending; the status;
a reporter with no money reader.

**Red first.** The sender, the client method and the commands are new, so on the previous head the whole file fails at import (36 tests).

## Existing tests that changed (the version number or a closed key list only)

The status version is 4 now: `test_card7c2_status`, `test_card7l6a1_status` (the number), and `test_card7a3_commands` (the top-level key list gains `money_reader`).

## Accepted notes (the lead accepted all three)

- **The report's retry counts toward the cycle's back-off rank** (a 401 as credential, the rest as unreachable). It still stops nothing else.
- **`state_unavailable` and `reader_halted` count cycles, not events:** the count goes up once per cycle in which a pending report meets a damaged or halted state.
- **The text view of `status` does not print the new `money_reader` block.** The JSON does, and so do the operator-action sentences.

## Schema step tests (`tests/test_cardr1reader3a_schema6.py`, 7)

A version-5 file made by the version-5 definitions, with rows in the reader's tables, upgrades to 6 with every old row unchanged (the old archive row gains a NULL
quarantine); a fresh file at 6 equals an upgraded one (the schema compared); a fault inside the step, and a fault after its first statement, leave version 5 whole
(no new table or column); a version-5 reader refuses a version-6 file and changes nothing; files of versions 3 and 4 reach 6 in one step and equal a fresh file; the
version-5 definitions contain neither addition. Red first: all 7 fail on the previous head. The "newer unknown version" tests of `test_card7c2_state` and
`test_card7l6a1_state` now use 7, and the version expectations of `test_card7c2_state`, `test_card7l6a1_state`, `test_card7l6a2_fix1b` and
`test_cardr1reader2_state` say 6.

## Limits and notes

- Nothing reads a gateway, builds a report or sends one that was not minted by a test: step 3b.
- The threshold of 5 is a judgment: small enough to stop a defect quickly, large enough that a short clock slip does not. It is one named constant.
- Raw quarantine evidence may hold whatever a damaged file held. It is kept only in the archive, never shown by `status`, and a state this large is refused rather than cut.
