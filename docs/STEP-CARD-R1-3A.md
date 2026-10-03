# Step record - Card R1-3a: migration 10, the seven evidence tables

Work item `card-r1-3a`. Branch `card-r1-3a` from main `88afedcb21486e8fb2a73d103ecb8d08a1b77a71`. Design: `C:\lw\records\DESIGN-R1-3.md`,
**revision 2** (the first build followed revision 1; this record is aligned with revision 2, sections 2, 3, 7, 10 and 13.8 to 13.10).
Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only. Audience: the lead and the reviewers. Synthetic data only.

## In plain words

Leasewise will keep every report a paid gateway sends, let an operator adopt one as the trusted baseline, and write down changes that need
a decision. This first step only creates the **empty tables** for that, and makes the configuration import forget a gateway's approvals and
adoption when the gateway is moved to another machine or account.

**Nothing reads or writes these tables until cards R1-3b (ingest) and R1-3c (the rules).** No seat, lease, report or answer changes. An older
Leasewise refuses the new store instead of misreading it.

**What revision 2 changed in the tables.** Every reader of a gateway keeps its memory under a random identity (a *reader incarnation*). The
design now keeps each identity's numbering separately, records which identity the operator chose and retires the others, and notes a new
identity once. So the first version of this card was aligned: the epoch table is keyed by reader identity (a generation may appear under
several), the identity is on every report row and every adoption, an adoption no longer stores "quiet" (only a quiet report can be adopted),
the chain state no longer has a moving quiet baseline, a new table records the readers, and the transition table gained the columns and keys
that "new reader" and "conflict" notes need.

## What was built

- **Migration 9 to 10** (`migrate._DDL_V10`, step 10 of `migrate._STEPS`, `SCHEMA_VERSION = 10`). Seven `STRICT` tables and five indexes,
  in the existing single `BEGIN IMMEDIATE` with the version stamp, so a failure part-way leaves version 9 and no new table. Tables only: no
  `ALTER`, no rebuild, no backfill, and **no row**: no approval, adoption, reader, transition or chain state. At version 10 it does nothing. The
  definitions live in one place, with the list `migrate.EVIDENCE_TABLES` and the script `MIGRATION_V10`.

  | Table | Holds | Notable checks |
  |---|---|---|
  | `gateway_epochs` | the recorded mapping (reader identity, epoch number, generation) per gateway | primary key on gateway, identity and epoch; **no uniqueness on the generation** (a restored ledger takes a new epoch; another reader may read the same ledger); a generation and an identity are 32 lowercase hex |
  | `gateway_totals` | one typed row per accepted version-4 report, joined to its immutable reading by `event_id` | amounts >= 0; `unattributed <= settled`; `quiet` and `gateway_flag` are 0 or 1; `held_over_reservation >= 0`; hashes are 64 lowercase hex; the period ends after it starts |
  | `gateway_approvals` | an approved pair of policy fingerprints per gateway | primary key on gateway and both hashes |
  | `gateway_adoptions` | the adopted baseline (its four amounts copied, so the baseline never moves), with the reader identity | `kind` is `operator` or `period_roll`; an operator adoption names its operator and a roll names none; `event_id` is unique; **no `quiet` column** |
  | `gateway_readers` | one row per reader identity of a gateway | `state` is `pending`, `current` or `retired`; a pending reader has no deciding adoption and a decided one has; only a retired one has a retirement time; **at most one `current` reader per gateway** (a partial unique index) |
  | `gateway_transitions` | a detected change that needs a decision | `kind` is one of the ten closed words (now with `reader_changed`); only `owner_changed` has no event id; `target_incarnation` only for `reader_changed`, with a unique key so there is **one row per reader identity**; a conflict (`sequence_conflict`, `epoch_generation_mismatch`) stores the identity, epoch and `seq` that collided, all three or none; one per gateway, kind and event |
  | `gateway_chain_state` | the pointer to the current adoption, the newest quiet report, and four counters (late, old-chain, duplicate and retired-reader reports) | the quiet pair is both set or both null; **no `quiet_baseline_*` columns** |

- **`import_config` effects** (`store._forget_evidence`, called where the existing code already resets a re-pointed gateway's report
  columns): delete the gateway's approvals; set its chain state's `current_adoption_id` to null (the adoption, reader and epoch rows stay as
  history); insert one `owner_changed` transition with a null event id. A row of chain state is **never created** by this (a gateway that
  never had evidence gets only the transition). A change of `share_micro_eur` only touches none of it, and neither does re-importing the same
  document. The transition is written for every re-pointed gateway, even one that never had evidence, as the brief says; it is resolved
  later by naming it in an adoption.
- **Removal guards:** `REMOVAL_GUARDS["gateways"]` lists the seven tables, so removing a gateway that has evidence is refused as
  `INVALID_CONFIG`, like a gateway with receipts. The existing test that compares the lists with the live foreign keys passes.
- **Existing tests that moved with the version** (unchanged by revision 2): the assertions that the store is at version 9, the status
  document's `schema_version`, the expected table list (now seven names), the "newer version is refused" tests (the newer version is now 11),
  and the helpers that build a store from the migration scripts (they also run `MIGRATION_V10`): `test_card1.py`, `test_card2b1.py`,
  `test_card4.py`, `test_card4_seat_kind_fix.py`, `test_card5.py` and `test_cardl1c.py`. No other expectation changed.

## Tests (`tests/test_cardr13a_schema.py`, 20)

- `Tables`: `test_the_version_is_ten_and_the_seven_tables_exist_with_their_columns`, `test_the_indexes_exist`,
  `test_the_checks_refuse_what_a_later_card_must_never_write` (eight bad rows, including an identity that is not 32 hex),
  `test_the_other_tables_refuse_their_closed_words`,
  `test_epochs_are_kept_per_reader_incarnation_and_a_generation_may_appear_under_several`,
  `test_readers_have_one_state_a_deciding_adoption_and_at_most_one_current_per_gateway`,
  `test_a_reader_changed_transition_is_one_row_per_incarnation_and_a_conflict_stores_the_identity_that_collided`.
- `Migration`: `test_a_fresh_store_and_a_migrated_version_nine_store_end_with_the_same_schema`,
  `test_the_migration_is_tables_only_and_invents_no_row`,
  `test_the_migration_is_atomic_a_failure_part_way_leaves_version_nine_and_no_new_table`, `test_the_migration_is_idempotent`,
  `test_the_older_reader_refuses_a_version_ten_store_before_touching_it` (the file bytes are unchanged).
- `Repointing`: to another machine (also: the reader and epoch rows stay as history), to another account, a gateway with no evidence, a
  share change only (also re-importing the same document).
- `Removal`: a gateway with no evidence is still removed; a gateway with a row in **each** of the seven evidence tables in turn is refused
  and nothing changes; the guard list equals the live foreign keys both ways; a list with any one table left out is caught.

**Red first.** Against `store.py` of main, the `Repointing` and `Removal` tests fail; the migration and table tests cannot run against main's
`migrate.py` (the tables do not exist). With one table removed from the guard list, the guard-list test fails (shown once for the first build,
then restored). The per-table removal test would still pass without the guard, because the database's own foreign key also refuses the removal;
the guard-list test is what proves the list is complete. The three new reader tests describe new schema rules and fail against the first
build's tables (no `gateway_readers`, no `reader_incarnation`).

## Honest notes

- Nothing reads these tables yet, so the schema is proved by its own tests only; the first real use is R1-3b.
- The table definitions are in one place (`_DDL_V10`); a later change from Codex's reads is that place plus the tests of this file.
- The reading source (the program that sends the reports) is not part of this card or of R1-3: it is the separate card R1-READER.
