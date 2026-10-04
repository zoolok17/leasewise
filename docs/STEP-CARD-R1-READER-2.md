# Step record - Card R1-READER, step 2: the money reader's durable state, mint, acceptance, the start-up check and reset

Work item `card-r1-reader-2`. Branch `card-r1-reader-2` from main `4665fd4afa794a7775a2cf0744d3dd419cdd478e`. Design: `C:\lw\records\DESIGN-R1-READER.md`
(revision 4; the "Lead decision D-195" and "D-203" blocks win over any body sentence). Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only.
Synthetic values only; no gateway, key or provider is touched.

## In plain words

The money reader must never skip or replace a report number. It moves past number n only after Leasewise has accepted exactly the bytes it saved for n.
This step builds the memory that makes that true: a random identity, the ledger epochs, two progress numbers per generation (`minted_seq`, the highest number
saved, and `acked_seq`, the highest accepted), at most one pending report (the exact bytes waiting to be sent), the receipts already read, and an archive that
`reset` fills and never erases. It also builds the start-up check, which accepts only two consistent states and halts on anything else.
**Nothing is sent and no gateway is read in this step.** Step 3 builds the cycle, the sending, the client's answers, the operator commands and the status words.

## What was built

1. **The tables** (`gwstate.GWR_DDL`, reporter schema version 5; older files are migrated, nothing deleted, and a reporter that knows less refuses a newer file):
   `gwr_identity` (one row; the incarnation cannot be changed by an update), `gwr_epoch`, `gwr_seq` (the schema itself only accepts the two consistent pairs),
   `gwr_pending` (one row only, JSON bytes up to 1 MiB, event id and idempotency key in the id format), `gwr_receipts` and `gwr_receipt_months` (the section 7
   table is split in two so that the per-month totals are rows: one per generation and month), `gwr_archive` (immutable by triggers). Every text column is
   a hex string, an id, a month or one of the three halt words, each with a CHECK; the only free-form column is the report's own bytes.
2. **Mint** (`gwstate.mint`, one transaction): allowed only when the state is consistent, no halt is set and no pending row exists. It takes a new epoch when
   the generation differs from the newest epoch's (including a return to an earlier generation), derives the event id with `derive_event_id(incarnation,
   generation, n+1)`, asks the caller's pure `build(incarnation, epoch, seq)` for the exact bytes, **checks that the bytes state that same number, identity,
   generation, epoch and event id**, saves the pending row, advances `minted_seq` and replaces the receipts read (the watermark may not go down). A refusal
   (`halted`, `pending_exists`, `bad_report`, `bad_receipts`, `bad_generation`) changes nothing.
3. **Acceptance** (`gwstate.accept`, one transaction): only for HTTP status 202, an object answer whose `accepted` is exactly `true` and whose `event_id` is the
   pending report's own, in a consistent state. It deletes the pending row and moves `acked_seq`. Anything else returns False and changes nothing.
4. **The start-up check** (`gwstate.start`, `gwstate.inconsistency`), per generation: the two number pictures; the pending row's number, bytes, derived event id,
   idempotency key (equal to the event id, as the HTTP layer requires), incarnation and epoch mapping; integer bounds; no orphan pending, receipt or epoch row;
   no second unfinished generation. `0/0` and a return to a fully acknowledged generation are valid. Anything else sets `halt = state_unavailable` (an earlier
   halt, for example `continuity_conflict`, is kept: only `reset` clears either).
5. **D-203, the missing state:** a complete absence (no identity and no pending, progress, epoch or receipt row) is a first start with a fresh incarnation. An
   absent identity with ANY surviving row halts as `state_unavailable` and is never read as a first start.
6. **Reset** (`gwstate.reset`, one transaction): the old incarnation, its halt and its pending bytes (with generation, number and event id) go to the archive; every
   progress, epoch, pending and receipt row is emptied; a fresh identity is created.

## Choices made (each tested)

- **Where the halt lives when there is no identity row.** D-203 says an absent identity with surviving rows halts, but the halt word is a column of the identity row.
  The halt is therefore **not stored** in that case (`Start.persisted` is False); the damage is found again by every start, and a mint is refused because there is no
  identity. `reset` recovers it and archives what was left with a null incarnation. I did not invent a marker table.
- **The archive is not one of the "surviving rows".** D-203 lists pending, progress, epoch and receipt rows. An archive with nothing else is therefore a first start.
- **The idempotency key equals the event id**, because the HTTP layer checks the `Idempotency-Key` of a reading against its event id.
- **Where a report states its own facts.** `report_facts` reads `event_id` at the top of the document and `reader_incarnation`, `generation`, `seq` and
  `ledger_epoch` in its `payload`. Step 3 builds those bytes; if their layout differs, that one function changes.
- **A mint that would find the two numbers apart without a pending row** cannot be reached as its own case: such a state is inconsistent and is refused as `halted`.
- **After a reset the epochs start again from 1** (they only grow within one incarnation).

## Tests (`tests/test_cardr1reader2_state.py`, 33)

`Schema` (6): version 5 and the seven tables; a version 4 file gains them empty and keeps its rows; a failed migration leaves it whole; the schema refuses what it
can (eleven cases); the incarnation and the archive cannot be changed; **no table or column can hold a name or a path** (no column is named for one, and every
text column refuses names, paths and the empty string). `StartUpCheck` (9): first start; two first starts differ; `0/0`; a minted state and the accepted state after
it; **a return to an earlier fully acknowledged generation (valid, a new epoch)**; **nineteen inconsistent pictures** (minted ahead of acked, acked ahead, two ahead, a
pending row on equal numbers, **a pending row of the wrong number**, **pending bytes whose event id does not match**, another incarnation, generation, number or epoch,
an unmapped or foreign epoch, bytes that are not a report, **an orphan pending row**, a generation without an epoch, **two unfinished generations**, orphan receipt
rows), each halting, persisted, and refusing a mint; a kept halt; **D-203: each kind of surviving row without an identity halts and nothing is made up**; an archive
alone is a first start. `Mint` (9): one transaction with its receipts; the next number only after acceptance; refused while halted or before any start; refused when
the numbers are apart; reports that state other numbers; receipts that do not belong or go backwards; a bad generation; receipts replaced, not added; **a fault at each hook point of the code (not each SQL statement: see fix round 1)
 leaves the whole-database dump unchanged**. `Acceptance` (5): the exact answer; **eighteen other answers (`"true"`, 1, another id, 200, 201, 409, no
object...) change nothing**; nothing pending; an inconsistent state; a fault at each hook point. `Reset` (4): the archive and the fresh state, the same with nothing
pending, recovery from an absent identity, **a fault at each hook point leaves the old state whole**.

**Red first.** The module `gwstate` and the tables are new, so on main the whole file fails at import (33 tests).

## Existing tests that changed (each for the version number only)

The reporter schema moved from 4 to 5. `test_card7c2_state`, `test_card7l6a1_state` and `test_card7l6a2_fix1b` assert the version after an upgrade, so they now say 5;
their "newer, unknown version" cases use 6; and `test_card7l6a2_fix1b` also pops the new, empty tables when it compares the rows of an upgraded file. No assertion
was weakened.

## Honest notes

- Nothing reads a gateway, sends anything, counts a refusal or classifies an answer (step 3). No seat, lease or admission decision changes.
- The reporter state file now carries seven more tables even when the money reader is not configured; they stay empty.

## Fix round 1 (Codex's read of `25ad221`)

In plain words: a lost progress row with a surviving epoch row is now caught, a report's identity facts are checked for their exact types by one checker, the
largest numbers halt instead of overflowing, and the fault test now fails every real SQL write.

- **F1 (blocking): both directions.** `gwstate.inconsistency` now also halts on an **orphan epoch** (an epoch whose generation has no progress row) and on a progress
  row **without its receipts row**. I confirmed first that no legitimate state has either: the mint writes a generation's progress row, its epoch row and its
  receipts row in the same transaction, `start` creates none of them, and `reset` empties all together. Codex's case (report 1 accepted, then progress and receipts
  removed, the epoch left) halts and never saves report 1 again. *The progress-without-receipts check is a one-sided pair I found beyond the epoch; it is easy to
  relax if you prefer.* The "0/0" picture is therefore a generation with its epoch, progress and receipts rows.
- **F2 (blocking): exact identity facts.** `report_facts` is the one checker, used by the mint and the start-up check: `event_id` exactly a text in the form
  `derive_event_id` makes; the reader and the generation exactly 32 lowercase hex; `seq` and `ledger_epoch` exactly integers (not `true`, not `1.0`, not text, not a
  list or object) from 1 to 2^63-1; every type is checked before any comparison or lookup. A malformed report given to the mint is `bad_report` (nothing written);
  malformed stored bytes halt the start-up as `state_unavailable` with no exception. A complete valid wire report goes through Leasewise's real receiving path
  (`http_api._wire_to_reading`, then `validate_readings`) and is accepted, while every malformed variant is refused there too and by the reader. One known
  difference, in the safe direction: Leasewise accepts an epoch of 0, the reader requires at least 1 (it never makes a 0).
- **B1 (lead decision): the largest numbers.** A `minted_seq` already at 2^63-1, or a needed new epoch when the newest epoch is already at 2^63-1, halts as
  `state_unavailable`: nothing is numbered, saved or created, no new word is added, and `reset` recovers. The only write is the halt itself. The largest number
  itself can still be minted; the same generation reuses the largest epoch.
- **N1: every real write.** `tests/test_cardr1reader2_fix1.py` `EveryWrite` replaces the connection of the state with one that counts the INSERT, UPDATE and DELETE
  statements and fails the k-th, once BEFORE it runs and once AFTER it has run, for a first mint with two receipt months, a replacement mint, an acceptance and a
  reset. After each failure the whole logical dump equals the one before, and the same call then succeeds. (The older hook test only stopped at the points where
  the code calls its hook; the receipt-month delete and its inserts shared one. That wording is corrected above.)
- **Carried to step 3, not started:** B2, a reset cannot archive a structurally damaged value (the archive's constraints refuse it). The lead's decision for step 3
  is a bounded quarantine form that keeps the raw evidence, plus a documented recovery.
- **Tests:** `tests/test_cardr1reader2_fix1.py` (18). One existing test changed: the "0/0" picture of `test_cardr1reader2_state` now includes its receipts row.
  Red first against `25ad221`: 12 of the 18 fail (36 failures and 5 errors counting subtests); the other six (the Leasewise control and the real-write tests) describe
  behaviour that was already true and are evidence rather than regressions.
