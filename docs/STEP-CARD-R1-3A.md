# Step record - Card R1-3a: migration 10, the six evidence tables

Work item `card-r1-3a`. Branch `card-r1-3a` from main `88afedcb21486e8fb2a73d103ecb8d08a1b77a71`. Design: `C:\lw\records\DESIGN-R1-3.md`,
revision 1 (SHA-256 `96a11ccdc8b7deba510a01d610241fe4a6f032045c7ba047a479dfd7c6a60cb5`), sections 3, 7 and 9. Interpreter: CPython 3.13
(`C:\lw\venv`), standard library only. Audience: the lead and the reviewers. Synthetic data only.

## In plain words

Leasewise will keep every report a paid gateway sends, let an operator adopt one as the trusted baseline, and write down changes that
need a decision. This first step only creates the **empty tables** for that, and makes the configuration import forget a gateway's
approvals and adoption when the gateway is moved to another machine or account.

**Nothing reads or writes these tables until cards R1-3b (ingest) and R1-3c (the rules).** No seat, lease, report or answer changes. An
older Leasewise refuses the new store instead of misreading it.

## What was built

- **Migration 9 to 10** (`migrate._DDL_V10`, step 10 of `migrate._STEPS`, `SCHEMA_VERSION = 10`). Six `STRICT` tables and three indexes,
  in the existing single `BEGIN IMMEDIATE` with the version stamp, so a failure part-way leaves version 9 and no new table. Tables only:
  no `ALTER`, no rebuild, no backfill, and **no row**: no approval, adoption, transition or chain state. At version 10 it does nothing.
  The definitions live in one place (the card may change after Codex reads the design), with the list `migrate.EVIDENCE_TABLES`.

  | Table | Holds | Notable checks |
  |---|---|---|
  | `gateway_epochs` | the recorded pair (reader's epoch number, generation) per gateway | one generation has one epoch and one epoch one generation (`UNIQUE`, primary key); a generation is 32 lowercase hex |
  | `gateway_totals` | one typed row per accepted version-4 report, joined to its immutable reading by `event_id` | amounts >= 0; `unattributed <= settled`; `quiet` and `gateway_flag` are 0 or 1; `held_over_reservation >= 0`; hashes are 64 lowercase hex; the period ends after it starts |
  | `gateway_approvals` | an approved pair of policy fingerprints per gateway | primary key on gateway and both hashes |
  | `gateway_adoptions` | the adopted baseline (its four amounts copied, so the baseline never moves) | `kind` is `operator` or `period_roll`; an operator adoption names its operator and a roll names none; `event_id` is unique |
  | `gateway_transitions` | a detected change that needs a decision | `kind` is one of the nine closed words; only `owner_changed` has no event id; one per gateway, kind and event |
  | `gateway_chain_state` | the pointer to the current adoption, the quiet baseline, and three counters | the two quiet pairs are both set or both null |

- **`import_config` effects** (`store._forget_evidence`, called where the existing code already resets a re-pointed gateway's report
  columns): delete the gateway's approvals; set its chain state's `current_adoption_id` to null (the adoption rows stay as history);
  insert one `owner_changed` transition with a null event id. A row of chain state is **never created** by this (a gateway that never
  had evidence gets only the transition). A change of `share_micro_eur` only touches none of it, and neither does re-importing the same
  document. The transition is written for every re-pointed gateway, even one that never had evidence, as the brief says; it is
  resolved later by naming it in an adoption.
- **Removal guards:** `REMOVAL_GUARDS["gateways"]` lists the six tables, so removing a gateway that has evidence is refused as
  `INVALID_CONFIG`, like a gateway with receipts. The existing test that compares the lists with the live foreign keys passes.
- **Existing tests that moved with the version:** the assertions that the store is at version 9, the status document's
  `schema_version`, the expected table list, and the "a newer version is refused" tests (the newer version is now 11).
  `test_card1.py`, `test_card2b1.py`, `test_card4.py` and `test_cardl1c.py`. No other expectation changed.

## Tests (`tests/test_cardr13a_schema.py`, 17)

- `Tables`: `test_the_version_is_ten_and_the_six_tables_exist_with_their_columns`, `test_the_indexes_exist`,
  `test_the_checks_refuse_what_a_later_card_must_never_write` (seven bad rows), `test_the_other_tables_refuse_their_closed_words`.
- `Migration`: `test_a_fresh_store_and_a_migrated_version_nine_store_end_with_the_same_schema`,
  `test_the_migration_is_tables_only_and_invents_no_row`,
  `test_the_migration_is_atomic_a_failure_part_way_leaves_version_nine_and_no_new_table`, `test_the_migration_is_idempotent`,
  `test_the_older_reader_refuses_a_version_ten_store_before_touching_it` (the file bytes are unchanged).
- `Repointing`: to another machine, to another account, a gateway with no evidence, a share change only (also re-importing the same
  document).
- `Removal`: a gateway with no evidence is still removed; a gateway with a row in **each** evidence table in turn is refused and
  nothing changes; the guard list equals the live foreign keys both ways; a list with any one table left out is caught.

**Red first.** Against `store.py` of main, the `Repointing` and `Removal` tests fail (5 failures and 5 errors, counting subtests); the
migration tests cannot run at all against main's `migrate.py` (the tables do not exist). With one table removed from the guard list,
the guard-list test fails (shown once, then restored). The per-table refusal test would still pass without the guard, because the
database's own foreign key also refuses the removal; the guard-list test is what proves the list is complete.

## Honest notes

- Nothing reads these tables yet, so the schema is proved by its own tests only; the first real use is R1-3b.
- If Codex's read of the design changes sections 3 or 7, the change is one place (`_DDL_V10`) plus the tests of this file.
