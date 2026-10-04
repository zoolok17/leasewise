# Step record - Card R1-3b: reading document version 4, totals and epochs at ingest, the event-id rule, the durable collision record

Work item `card-r1-3b`. Branch `card-r1-3b` from main `362bf30f1a61b9159e829605339387dde112b552`. Design: `C:\lw\records\DESIGN-R1-3.md`,
revision 2 (sections 1.3, 3 and 9; the row R1-3b of 10.1). Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only. Audience: the lead and
the reviewers. Synthetic reports only.

## In plain words

R1-3a made the evidence tables, empty. This step starts **filling two of them** when a gateway report arrives, and makes a report collision
leave a permanent record. **No evidence consumer uses the new rows for status or admission yet (ingest itself reads them for identity checks and diagnostics;
card R1-3c adds the rules on top), so no seat or lease decision changes, and no reader sends version 4
live until card R1-READER closes D1 (its start-up and continuity check).**

- **Version 4 of the report** adds five keys and requires both policy fingerprints. Versions 2 and 3 are unchanged.
- **Each accepted version-4 report is copied** into `gateway_totals`, and its sender identity and epoch are recorded with its generation.
- **A colliding report leaves a record.** The same event id with different content is refused exactly as before, and now also leaves one
  `sequence_conflict`, written inside the store's own write transaction, only for the gateway's real owner.

## What was built

1. **Reading document version 4** (`validate_readings`): `generation` and `reader_incarnation` are 32 lowercase hex; `receipts_seq` is a whole
   number >= 0 or null; `quiet` is a true boolean; `held_over_reservation` is a whole number >= 0. Every version-3 rule stays, except one:
   **version 4 requires both policy hashes** (Codex B1): a version-4 report without both is refused **by the validator** with the existing closed word
   (`INVALID_CONFIG`), before any store or SQL step, and no hash is ever made up; version 3 still accepts `policy_hashes: null`. A version-3 payload
   with the five keys, and a version-4 document with a version-3 payload, are refused (the shapes are closed). Over HTTP, a snapshot that names a
   `reader_incarnation` is read as version 4.
2. **Ingest** (`store._record_report_v4`): the report is copied into `gateway_totals` (joined by `event_id`), and a new (gateway, reader
   incarnation, epoch) is recorded in `gateway_epochs` with its generation. The same triple with another generation is the identity conflict of the
   design: the report is kept, the first mapping stays, and an `epoch_generation_mismatch` transition is written (once; its conflict columns carry the
   incarnation, epoch and number). Versions 2 and 3 never reach these tables. The legacy gateway columns are reduced exactly as before.
3. **The event-id rule** (`validate_readings.derive_event_id`): a version-4 report's event id must be the UUID (version 5, fixed namespace) derived
   from its incarnation, generation and number; any other id is refused with the closed word. The design called the event id "opaque" in 1.3; the brief
   asks for the check, so the derivation is the check and a sender must use it.
4. **The durable collision record** (`store._record_collisions`). `reading_import` takes a savepoint at the start of its write transaction. On an
   `IDEMPOTENCY_CONFLICT` (from either path: the HTTP identity check or the reading idempotency check) it rolls back to that savepoint (so **no rejected
   reading data is kept, including the other readings of the same batch**), writes the record, commits, and then returns the conflict as before. For each
   version-4 report of the refused batch whose event id is stored with different content:
   - the authenticated sender (the reading's machine and account) must be the gateway's **current** owner, **re-checked under the lock** (the payload's
     gateway name alone and any earlier authorization are not trusted); a sender that is not the owner writes **nothing**;
   - the stored report must belong to the same gateway, incarnation and epoch (provenance); otherwise nothing is written;
   - a collision from an incarnation that `gateway_readers` marks `retired` is **only counted** (`retired_reader_reports`), never a transition;
   - otherwise one `sequence_conflict` is written with the colliding incarnation, epoch and number; a repeat is a no-op (the unique key).
   The same content under the same id is a replay, not a collision.
5. **It honours the transitions it creates:** nothing here resolves or overwrites one (a test ingests later reports and compares).
6. **Wording carried from Codex** (N1, N2): the step record of R1-3a, the comment in `migrate.py` and the introduction of
   `tests/test_cardr13a_schema.py` now say that no report ingest or admission code uses the tables in R1-3a, except the import effect and the removal
   guard. In `DESIGN-R1-3.md` the sentence "a lost or restored state is a new identity" is narrowed: complete loss is; a restored older copy can keep
   its identity (10.2, R1-READER).

## How the crash point is shown

There is a fault-injection point, `store._after_collision_record(conn)`, called after the record is written and before the commit. A test replaces
it with a function that raises: the import then leaves **nothing** (no transition, no other change, compared table by table). A clean run leaves
the record and still no rejected data. Because the record is written in the same transaction as the commit, a stop can only land before it
(nothing) or after it (the record), never between a refusal and a separate write.

## Tests (`tests/test_cardr13b_ingest.py`, 35)

- `Version4` (10): every key accepted; null `receipts_seq`; each of the five keys missing; each key wrong (about 30 values); an unknown key; both
  hashes required (six bad shapes); version 3 with null hashes and version 2 unchanged; the five keys refused in version 3; version 4 needs the
  version-4 payload; version 5 unsupported.
- `EventId` (4): derived from the three parts; any other id refused; a non-UUID refused; versions 2 and 3 keep any id.
- `Ingest` (8): a report lands in totals and epochs; a second report of the same epoch; a replay changes nothing; a return to a generation under a new
  epoch and a second reader on one ledger; an epoch triple with a second generation; versions 2 and 3 never reach the tables; a non-owner machine is
  refused and stores nothing; a later report never resolves or overwrites a transition.
- `Collisions` (13): both paths each leave one durable record and no rejected data; a non-owner writes nothing on either path; a wrong account;
  no provenance (a stored version-3 reading, and a stored report of another epoch); a retired incarnation is counted, not recorded (twice); pending and
  current readers still get the record; a repeat is a no-op (both paths); the same content is a replay; the crash point; the closed word and message
  are unchanged; a version-3 collision leaves no record; a batch that collides part-way stores none of its other readings.

**Red first.** Against the code of main the file cannot run (`derive_event_id` does not exist). With only the store part removed (the validator and
HTTP changes kept), **10 failures and 2 errors** of the 35 tests describe the new ingest and collision behaviour.

## Existing tests

One expectation moved: `test_card2b1.test_billing_output_errors` used **version 4** as its example of an unknown reading document version; version 4
now exists, so it uses version 5 (still `SCHEMA_UNSUPPORTED`). No other existing test changed.

## Honest notes

- No evidence consumer uses `gateway_totals`, `gateway_epochs` or the conflict rows for status or admission decisions yet (ingest reads them for
  identity checks and diagnostics; R1-3c adds the transition rules), and no reader sends version 4 live until R1-READER closes D1.
- An authenticated owner can send conflicting content on purpose; the protection is ownership plus a durable, idempotent record. No global row bound is
  claimed (a repeated event gives one row; many distinct events give many rows, only for the owner's own gateway).
- The reading import takes the savepoint for every document, so a refused version-3 collision also rolls back to it (no change in what is stored).
