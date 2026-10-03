"""Card R1-3a: migration 10, the seven evidence tables (DESIGN-R1-3.md sections 3 and 7, revision 2).

This step only creates the EMPTY tables that cards R1-3b and R1-3c will fill, and makes the configuration import forget a gateway's
approvals and adoption when the gateway moves to another machine or account. Nothing reads or writes the tables yet.

* the seven tables exist with their columns, checks and indexes, and a fresh store and a migrated version-9 store end with the same schema;
* the migration is tables only: atomic (a failure part-way leaves version 9 and no new table), idempotent, and it invents no row;
* the older reader (version 9) refuses a version-10 store before it touches it;
* re-pointing a kept gateway to another machine or account gives exactly three effects (its approvals deleted, its chain state
  pointing at no adoption with the adoption rows kept, one `owner_changed` transition with no event id); changing only its share
  gives none of them;
* removing a gateway that has evidence rows is refused, table by table, and the removal-guard list matches the live foreign keys;
* the reader identity model: epochs are kept per reader incarnation (a generation may appear under several), a gateway has at most one
  `current` reader, a reader's retirement is recorded, a `reader_changed` transition is one row per incarnation, and a conflict stores the
  identity that collided.

Synthetic data only.
"""

import copy
import hashlib
import os
import sqlite3
import unittest
from unittest import mock

from quota_balancer import errors, migrate, store
from quota_balancer.migrate import EVIDENCE_TABLES

from tests.test_card2b1 import ERROR_INVALID_CONFIG, VALID_DOC, _Base, _config_import, run_cli

PRICE = "a" * 64
CAP = "b" * 64
GENERATION = "c" * 32
INC = "d" * 32
INC2 = "e" * 32


class _Schema(_Base):

    def conn(self):
        return sqlite3.connect(self.db)

    def one(self, text, params=()):
        conn = self.conn()
        try:
            return conn.execute(text, params).fetchall()
        finally:
            conn.close()

    def run_sql(self, text, params=()):
        conn = self.conn()
        try:
            conn.execute(text, params)
            conn.commit()
        finally:
            conn.close()

    def gateway_id(self, alias):
        return self.one("SELECT id FROM gateways WHERE alias=?", (alias,))[0][0]

    def counts(self):
        return {table: self.one("SELECT COUNT(*) FROM " + table)[0][0] for table in EVIDENCE_TABLES}      # fixed names

    def reading(self, label, alias="g1"):
        """A stored reading (the evidence rows point at one)."""
        self._readings = getattr(self, "_readings", 0) + 1                        # every reading is new, even in a loop
        event = str(hashlib.md5(("%s-%d" % (label, self._readings)).encode(), usedforsecurity=False).hexdigest())
        event = "%s-%s-4%s-8%s-%s" % (event[:8], event[8:12], event[13:16], event[17:20], event[20:32])
        gateway = self.one("SELECT machine_id, account_id FROM gateways WHERE alias=?", (alias,))[0]
        self.run_sql("INSERT INTO readings(event_id, machine_id, account_id, seat_id, kind, observed_at, received_at, payload) "
                     "VALUES(?,?,?,NULL,'gateway_snapshot',1,1,'{}')", (event, gateway[0], gateway[1]))
        return event

    def seed(self, alias):
        """One row in every evidence table of `alias` (the rows a later card would write). -> the adoption id."""
        gateway = self.gateway_id(alias)
        first, second = self.reading(alias + "-1", alias), self.reading(alias + "-2", alias)
        self.run_sql("INSERT INTO gateway_epochs VALUES(?,?,?,?,?)", (gateway, INC, 1, GENERATION, first))
        for event, seq in ((first, 1), (second, 2)):
            self.run_sql("INSERT INTO gateway_totals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                event, gateway, INC, 1, GENERATION, seq, 1000, 2000, 500, 20, 5, 1, 1, 0, 0, PRICE, CAP, 0, 10, 10))
        self.run_sql("INSERT INTO gateway_approvals VALUES(?,?,?,?,?)", (gateway, PRICE, CAP, 5, "operator"))
        self.run_sql("INSERT INTO gateway_adoptions(gateway_id, event_id, reader_incarnation, ledger_epoch, generation, "
                     "period_start, seq, settled_micro_eur, reserved_micro_eur, uncertain_micro_eur, unattributed_micro_eur, "
                     "price_hash, child_cap_hash, adopted_at, operator, kind, prev_adoption_id) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                     (gateway, first, INC, 1, GENERATION, 1000, 1, 500, 20, 5, 1, PRICE, CAP, 6, "operator", "operator"))
        adoption = self.one("SELECT id FROM gateway_adoptions WHERE gateway_id=?", (gateway,))[0][0]
        self.run_sql("INSERT INTO gateway_readers VALUES(?,?,?,?,?,?,NULL)", (gateway, INC, first, 3, "current", adoption))
        self.run_sql("INSERT INTO gateway_transitions(gateway_id, kind, event_id, adoption_id, detected_at, resolved_by_adoption) "
                     "VALUES(?,?,?,?,?,NULL)", (gateway, "figure_fell", second, adoption, 11))
        self.run_sql("INSERT INTO gateway_chain_state VALUES(?,?,NULL,NULL,3,0,1,0)", (gateway, adoption))
        return adoption


class Tables(_Schema):

    EXPECTED = {
        "gateway_epochs": ["gateway_id", "reader_incarnation", "ledger_epoch", "generation", "first_event_id"],
        "gateway_totals": ["event_id", "gateway_id", "reader_incarnation", "ledger_epoch", "generation", "seq", "period_start",
                           "period_end", "settled_micro_eur", "reserved_micro_eur", "uncertain_micro_eur",
                           "unattributed_micro_eur", "receipts_seq", "quiet", "held_over_reservation", "price_hash",
                           "child_cap_hash", "gateway_flag", "observed_at", "received_at"],
        "gateway_approvals": ["gateway_id", "price_hash", "child_cap_hash", "approved_at", "operator"],
        "gateway_adoptions": ["id", "gateway_id", "event_id", "reader_incarnation", "ledger_epoch", "generation", "period_start",
                              "seq", "settled_micro_eur", "reserved_micro_eur", "uncertain_micro_eur", "unattributed_micro_eur",
                              "price_hash", "child_cap_hash", "adopted_at", "operator", "kind", "prev_adoption_id"],
        "gateway_readers": ["gateway_id", "reader_incarnation", "first_event_id", "first_seen_at", "state",
                            "decided_by_adoption", "retired_at"],
        "gateway_transitions": ["id", "gateway_id", "kind", "event_id", "adoption_id", "target_incarnation",
                                "conflict_incarnation", "conflict_epoch", "conflict_seq", "detected_at", "resolved_by_adoption"],
        "gateway_chain_state": ["gateway_id", "current_adoption_id", "last_quiet_event_id", "last_quiet_unattributed",
                                "late_reports", "old_chain_reports", "duplicate_reports", "retired_reader_reports"],
    }

    def test_the_version_is_ten_and_the_seven_tables_exist_with_their_columns(self):
        self.assertEqual(migrate.SCHEMA_VERSION, 10)
        self.assertEqual(self.one("PRAGMA user_version"), [(10,)])
        self.assertEqual(set(EVIDENCE_TABLES), set(self.EXPECTED))
        for table, columns in self.EXPECTED.items():
            with self.subTest(table=table):
                self.assertEqual([row[1] for row in self.one("PRAGMA table_info(%s)" % table)], columns)     # fixed names
                strict = self.one("SELECT strict FROM pragma_table_list WHERE name=?", (table,))
                self.assertEqual(strict, [(1,)])

    def test_the_indexes_exist(self):
        names = {row[0] for row in self.one("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({"gateway_totals_chain", "gateway_adoptions_gateway", "gateway_transitions_open",
                         "gateway_transitions_reader", "gateway_readers_current"} <= names)

    def test_the_checks_refuse_what_a_later_card_must_never_write(self):
        gateway = self.gateway_id("g1")
        event = self.reading("check")
        totals = (event, gateway, INC, 1, GENERATION, 1, 1000, 2000, 500, 20, 5, 1, 1, 0, 0, PRICE, CAP, 0, 10, 10)
        bad = {
            "a hash that is not 64 lowercase hex": totals[:15] + ("A" * 64,) + totals[16:],
            "a generation that is not 32 hex": totals[:4] + ("c" * 31,) + totals[5:],
            "an incarnation that is not 32 hex": totals[:2] + ("d" * 31,) + totals[3:],
            "a negative amount": totals[:8] + (-1,) + totals[9:],
            "unattributed above settled": totals[:8] + (499,) + totals[9:11] + (500,) + totals[12:],
            "a quiet flag that is not 0 or 1": totals[:13] + (2,) + totals[14:],
            "a period that ends before it starts": totals[:6] + (2000, 1000) + totals[8:],
            "a negative held-over count": totals[:14] + (-1,) + totals[15:],
        }
        marks = ",".join("?" * 20)
        for label, row in bad.items():
            with self.subTest(label):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.run_sql("INSERT INTO gateway_totals VALUES(" + marks + ")", row)
        self.assertEqual(self.one("SELECT COUNT(*) FROM gateway_totals"), [(0,)])
        self.run_sql("INSERT INTO gateway_totals VALUES(" + marks + ")", totals)             # the good row is accepted

    def test_the_other_tables_refuse_their_closed_words(self):
        gateway = self.gateway_id("g1")
        first = self.reading("words")
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_sql("INSERT INTO gateway_transitions(gateway_id, kind, event_id, detected_at) VALUES(?,?,?,1)",
                         (gateway, "invented_kind", first))
        with self.assertRaises(sqlite3.IntegrityError):                      # only owner_changed has no event id
            self.run_sql("INSERT INTO gateway_transitions(gateway_id, kind, event_id, detected_at) VALUES(?,'figure_fell',NULL,1)",
                         (gateway,))
        with self.assertRaises(sqlite3.IntegrityError):                      # and owner_changed never has one
            self.run_sql("INSERT INTO gateway_transitions(gateway_id, kind, event_id, detected_at) VALUES(?,'owner_changed',?,1)",
                         (gateway, first))
        adoption = ("INSERT INTO gateway_adoptions(gateway_id, event_id, reader_incarnation, ledger_epoch, generation, "
                    "period_start, seq, settled_micro_eur, reserved_micro_eur, uncertain_micro_eur, unattributed_micro_eur, "
                    "price_hash, child_cap_hash, adopted_at, operator, kind) VALUES(?,?,?,1,?,1,1,0,0,0,0,?,?,1,?,?)")
        with self.assertRaises(sqlite3.IntegrityError):                      # an operator adoption names its operator
            self.run_sql(adoption, (gateway, first, INC, GENERATION, PRICE, CAP, None, "operator"))
        with self.assertRaises(sqlite3.IntegrityError):                      # a month roll has none
            self.run_sql(adoption, (gateway, first, INC, GENERATION, PRICE, CAP, "operator", "period_roll"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_sql(adoption, (gateway, first, INC, GENERATION, PRICE, CAP, "operator", "automatic"))
        with self.assertRaises(sqlite3.IntegrityError):                      # the quiet pair is both columns or neither
            self.run_sql("INSERT INTO gateway_chain_state(gateway_id, last_quiet_event_id) VALUES(?,?)", (gateway, first))

    def test_epochs_are_kept_per_reader_incarnation_and_a_generation_may_appear_under_several(self):
        gateway = self.gateway_id("g1")
        first = self.reading("epochs")
        self.run_sql("INSERT INTO gateway_epochs VALUES(?,?,?,?,?)", (gateway, INC, 1, GENERATION, first))
        self.run_sql("INSERT INTO gateway_epochs VALUES(?,?,?,?,?)", (gateway, INC, 2, GENERATION, first))     # a return to a generation
        self.run_sql("INSERT INTO gateway_epochs VALUES(?,?,?,?,?)", (gateway, INC2, 1, GENERATION, first))    # another reader, same ledger
        with self.assertRaises(sqlite3.IntegrityError):                                    # one epoch of one reader means one row
            self.run_sql("INSERT INTO gateway_epochs VALUES(?,?,?,?,?)", (gateway, INC, 1, "f" * 32, first))
        self.assertEqual(self.one("SELECT COUNT(*) FROM gateway_epochs"), [(3,)])

    def test_readers_have_one_state_a_deciding_adoption_and_at_most_one_current_per_gateway(self):
        gateway = self.gateway_id("g1")
        first = self.reading("readers")
        adoption = self.seed("g2")                                            # any adoption id will do (foreign keys are off)
        insert = "INSERT INTO gateway_readers VALUES(?,?,?,?,?,?,?)"
        with self.assertRaises(sqlite3.IntegrityError):                      # a pending reader has no deciding adoption
            self.run_sql(insert, (gateway, INC, first, 1, "pending", adoption, None))
        with self.assertRaises(sqlite3.IntegrityError):                      # a decided reader names its adoption
            self.run_sql(insert, (gateway, INC, first, 1, "current", None, None))
        with self.assertRaises(sqlite3.IntegrityError):                      # retired has a time, and only retired has
            self.run_sql(insert, (gateway, INC, first, 1, "retired", adoption, None))
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_sql(insert, (gateway, INC, first, 1, "current", adoption, 5))
        with self.assertRaises(sqlite3.IntegrityError):                      # a state word that is not closed
            self.run_sql(insert, (gateway, INC, first, 1, "chosen", adoption, None))
        self.run_sql(insert, (gateway, INC, first, 1, "pending", None, None))
        self.run_sql(insert, (gateway, INC2, first, 2, "retired", adoption, 9))
        self.run_sql(insert, (gateway, "f" * 32, first, 3, "pending", None, None))        # several pending readers are fine
        self.run_sql("UPDATE gateway_readers SET state='current', decided_by_adoption=? WHERE gateway_id=? AND reader_incarnation=?",
                     (adoption, gateway, INC))
        with self.assertRaises(sqlite3.IntegrityError):                      # but only one current reader
            self.run_sql("UPDATE gateway_readers SET state='current', decided_by_adoption=? WHERE gateway_id=? "
                         "AND reader_incarnation=?", (adoption, gateway, "f" * 32))

    def test_a_reader_changed_transition_is_one_row_per_incarnation_and_a_conflict_stores_the_identity_that_collided(self):
        gateway = self.gateway_id("g1")
        first, second = self.reading("t1"), self.reading("t2")
        insert = ("INSERT INTO gateway_transitions(gateway_id, kind, event_id, target_incarnation, conflict_incarnation, "
                  "conflict_epoch, conflict_seq, detected_at) VALUES(?,?,?,?,?,?,?,1)")
        with self.assertRaises(sqlite3.IntegrityError):                      # reader_changed names its incarnation
            self.run_sql(insert, (gateway, "reader_changed", first, None, None, None, None))
        with self.assertRaises(sqlite3.IntegrityError):                      # and only reader_changed does
            self.run_sql(insert, (gateway, "figure_fell", first, INC, None, None, None))
        with self.assertRaises(sqlite3.IntegrityError):                      # a conflict stores incarnation, epoch and seq
            self.run_sql(insert, (gateway, "sequence_conflict", first, None, None, None, None))
        with self.assertRaises(sqlite3.IntegrityError):                      # all three or none
            self.run_sql(insert, (gateway, "sequence_conflict", first, None, INC, 1, None))
        with self.assertRaises(sqlite3.IntegrityError):                      # and only for the two conflict kinds
            self.run_sql(insert, (gateway, "figure_fell", first, None, INC, 1, 2))
        self.run_sql(insert, (gateway, "sequence_conflict", first, None, INC, 1, 2))
        self.run_sql(insert, (gateway, "epoch_generation_mismatch", first, None, INC, 1, 2))
        self.run_sql(insert, (gateway, "reader_changed", first, INC2, None, None, None))
        with self.assertRaises(sqlite3.IntegrityError):                      # a second report of the same new reader adds no row
            self.run_sql(insert, (gateway, "reader_changed", second, INC2, None, None, None))
        self.run_sql(insert, (gateway, "reader_changed", second, "f" * 32, None, None, None))   # another new reader does
        with self.assertRaises(sqlite3.IntegrityError):                      # the same collision twice is one row
            self.run_sql(insert, (gateway, "sequence_conflict", first, None, INC, 1, 2))


class Migration(_Schema):

    def downgrade_to_nine(self):
        """The store as a version-9 Leasewise left it: the seven tables dropped and the stamp set back (the data is kept)."""
        conn = self.conn()
        try:
            for table in reversed(EVIDENCE_TABLES):
                conn.execute("DROP TABLE " + table)                               # fixed names
            conn.execute("PRAGMA user_version=9")
            conn.commit()
        finally:
            conn.close()

    def schema(self, db=None):
        conn = sqlite3.connect(db or self.db)
        try:
            return sorted(conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
        finally:
            conn.close()

    def test_a_fresh_store_and_a_migrated_version_nine_store_end_with_the_same_schema(self):
        fresh = self.schema()
        self.downgrade_to_nine()
        self.assertEqual(self.one("PRAGMA user_version"), [(9,)])
        self.assertEqual(run_cli("--db", self.db, "init").returncode, 0)
        self.assertEqual(self.one("PRAGMA user_version"), [(10,)])
        self.assertEqual(self.schema(), fresh)

    def test_the_migration_is_tables_only_and_invents_no_row(self):
        gateway = self.gateway_id("g1")
        event = self.reading("legacy")                                           # a legacy snapshot already stored
        self.downgrade_to_nine()
        before = {table: self.one("SELECT * FROM " + table) for table in ("accounts", "machines", "gateways", "readings")}
        self.assertEqual(run_cli("--db", self.db, "init").returncode, 0)
        self.assertEqual(self.counts(), {table: 0 for table in EVIDENCE_TABLES})        # no approval, adoption, transition...
        for table, rows in before.items():                                              # and nothing else was touched
            self.assertEqual(self.one("SELECT * FROM " + table), rows)
        self.assertEqual(self.one("SELECT event_id FROM readings"), [(event,)])
        self.assertEqual(self.one("SELECT id FROM gateways WHERE alias='g1'"), [(gateway,)])

    def test_the_migration_is_atomic_a_failure_part_way_leaves_version_nine_and_no_new_table(self):
        self.downgrade_to_nine()
        broken = migrate._DDL_V10 + ("THIS IS NOT SQL;",)
        with mock.patch.dict(migrate._STEPS, {10: broken}):
            result = run_cli_in_process(self.db)
        self.assertNotEqual(result, 0)
        self.assertEqual(self.one("PRAGMA user_version"), [(9,)])
        names = {row[0] for row in self.one("SELECT name FROM sqlite_master")}
        self.assertEqual(names & set(EVIDENCE_TABLES), set())
        self.assertEqual(run_cli("--db", self.db, "init").returncode, 0)                 # and a later run completes it
        self.assertEqual(self.one("PRAGMA user_version"), [(10,)])

    def test_the_migration_is_idempotent(self):
        self.run_sql("INSERT INTO gateway_approvals VALUES(?,?,?,1,'operator')", (self.gateway_id("g1"), PRICE, CAP))
        before = (self.schema(), self.counts())
        with mock.patch.dict(migrate._STEPS, {10: ("THIS IS NOT SQL;",)}):              # a step that would fail if it ran again
            self.assertEqual(run_cli_in_process(self.db), 0)
        self.assertEqual(run_cli("--db", self.db, "init").returncode, 0)
        self.assertEqual((self.schema(), self.counts()), before)

    def test_the_older_reader_refuses_a_version_ten_store_before_touching_it(self):
        self.seed("g1")
        before = _bytes_of(self.db)
        with mock.patch.object(migrate, "SCHEMA_VERSION", 9):
            conn = self.conn()
            try:
                with self.assertRaises(errors.QuotaError) as caught:
                    migrate.migrate(conn)
                self.assertEqual(caught.exception.code, errors.CODE_SCHEMA_UNSUPPORTED)
                with self.assertRaises(errors.QuotaError) as caught:
                    migrate.require_supported(conn)
                self.assertEqual(caught.exception.code, errors.CODE_SCHEMA_UNSUPPORTED)
            finally:
                conn.close()
        self.assertEqual(_bytes_of(self.db), before)


class Repointing(_Schema):

    def doc(self):
        return copy.deepcopy(VALID_DOC)

    def effects(self, alias):
        gateway = self.gateway_id(alias)
        return {
            "approvals": self.one("SELECT price_hash, child_cap_hash FROM gateway_approvals WHERE gateway_id=?", (gateway,)),
            "adoptions": self.one("SELECT id, kind FROM gateway_adoptions WHERE gateway_id=?", (gateway,)),
            "chain": self.one("SELECT current_adoption_id, late_reports, duplicate_reports FROM gateway_chain_state "
                              "WHERE gateway_id=?", (gateway,)),
            "transitions": self.one("SELECT kind, event_id, resolved_by_adoption FROM gateway_transitions WHERE gateway_id=? "
                                    "ORDER BY id", (gateway,)),
        }

    def test_moving_a_gateway_to_another_machine_forgets_its_approval_and_adoption_and_records_one_transition(self):
        adoption = self.seed("g1")
        self.seed("g2")
        other_before = self.effects("g2")
        moved = self.doc()
        moved["gateways"][0]["machine"] = "worker"
        self.assertEqual(_config_import(self.db, self.tmp, moved).returncode, 0)
        got = self.effects("g1")
        self.assertEqual(got["approvals"], [])                                           # deleted
        self.assertEqual(got["adoptions"], [(adoption, "operator")])                     # kept as history
        self.assertEqual(got["chain"], [(None, 3, 1)])                                   # points at no adoption; the rest stays
        self.assertEqual(got["transitions"], [("figure_fell", got["transitions"][0][1], None), ("owner_changed", None, None)])
        self.assertEqual(self.effects("g2"), other_before)                               # another gateway is untouched
        self.assertEqual(self.one("SELECT reader_incarnation, state FROM gateway_readers WHERE gateway_id=?",
                                  (self.gateway_id("g1"),)), [(INC, "current")])           # the reader and epoch rows stay as history
        self.assertEqual(self.one("SELECT COUNT(*) FROM gateway_epochs WHERE gateway_id=?", (self.gateway_id("g1"),)), [(1,)])

    def test_moving_a_gateway_to_another_account_has_the_same_effects(self):
        doc = self.doc()
        doc["accounts"].append({"alias": "meter2", "vendor": "ovh", "kind": "metered", "budget_micro_eur": 100000000})
        self.assertEqual(_config_import(self.db, self.tmp, doc).returncode, 0)
        adoption = self.seed("g1")
        moved = copy.deepcopy(doc)
        moved["gateways"][0]["account"] = "meter2"
        self.assertEqual(_config_import(self.db, self.tmp, moved).returncode, 0)
        got = self.effects("g1")
        self.assertEqual((got["approvals"], got["chain"]), ([], [(None, 3, 1)]))
        self.assertEqual(got["adoptions"], [(adoption, "operator")])
        self.assertEqual([row[0] for row in got["transitions"]], ["figure_fell", "owner_changed"])

    def test_moving_a_gateway_that_has_no_evidence_creates_no_chain_state_and_one_transition(self):
        moved = self.doc()
        moved["gateways"][0]["machine"] = "worker"
        self.assertEqual(_config_import(self.db, self.tmp, moved).returncode, 0)
        got = self.effects("g1")
        self.assertEqual((got["approvals"], got["adoptions"], got["chain"]), ([], [], []))     # no row is invented
        self.assertEqual(got["transitions"], [("owner_changed", None, None)])

    def test_changing_only_the_share_touches_none_of_it(self):
        self.seed("g1")
        before = self.effects("g1")
        changed = self.doc()
        changed["gateways"][0]["share_micro_eur"] = 50000000
        self.assertEqual(_config_import(self.db, self.tmp, changed).returncode, 0)
        self.assertEqual(self.effects("g1"), before)
        self.assertEqual(self.one("SELECT share_micro_eur FROM gateways WHERE alias='g1'"), [(50000000,)])
        self.assertEqual(_config_import(self.db, self.tmp, changed).returncode, 0)        # and re-importing the same document
        self.assertEqual(self.effects("g1"), before)


class Removal(_Schema):

    def without_g2(self):
        doc = copy.deepcopy(VALID_DOC)
        doc["gateways"] = doc["gateways"][:1]
        doc["accounts"][0]["budget_micro_eur"] = 100000000
        return doc

    def test_removing_a_gateway_with_no_evidence_still_works(self):
        result = _config_import(self.db, self.tmp, self.without_g2())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.one("SELECT alias FROM gateways"), [("g1",)])

    def test_removing_a_gateway_with_a_row_in_any_evidence_table_is_refused_and_changes_nothing(self):
        gateway = self.gateway_id("g2")
        for table in EVIDENCE_TABLES:
            with self.subTest(table=table):
                self.seed("g2")
                self.run_sql_all_but(gateway, table)                  # rows in `table` only
                self.assertGreater(self.one("SELECT COUNT(*) FROM " + table + " WHERE gateway_id=?", (gateway,))[0][0], 0)
                before = self.counts()
                result = _config_import(self.db, self.tmp, self.without_g2())
                self.assertEqual((result.returncode, result.stderr.strip()), (2, ERROR_INVALID_CONFIG))
                self.assertEqual(self.counts(), before)
                self.assertEqual(self.one("SELECT COUNT(*) FROM gateways"), [(2,)])
                self.run_sql_all_but(gateway, None)                   # clean up for the next table

    def run_sql_all_but(self, gateway, keep_table):
        """Delete the gateway's evidence rows from every evidence table except `keep_table` (None: from all of them)."""
        conn = self.conn()
        try:
            for table in reversed(EVIDENCE_TABLES):
                if table != keep_table:
                    conn.execute("DELETE FROM " + table + " WHERE gateway_id=?", (gateway,))        # fixed names
            conn.commit()
        finally:
            conn.close()

    def declared(self, target):
        conn = self.conn()
        try:
            found = set()
            for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                for row in conn.execute("PRAGMA foreign_key_list(%s)" % table):                       # fixed names
                    if row[2] == target:
                        found.add((table, row[3]))
            return found
        finally:
            conn.close()

    def test_the_guard_list_names_every_new_table_that_points_at_a_gateway(self):
        declared = self.declared("gateways")
        self.assertTrue({(table, "gateway_id") for table in EVIDENCE_TABLES} <= declared)
        self.assertEqual(declared, set(store.REMOVAL_GUARDS["gateways"]))                  # the same set, both ways

    def test_a_guard_list_with_one_table_left_out_is_caught(self):
        for table in EVIDENCE_TABLES:
            with self.subTest(left_out=table):
                trimmed = tuple(pair for pair in store.REMOVAL_GUARDS["gateways"] if pair != (table, "gateway_id"))
                self.assertFalse(self.declared("gateways") <= set(trimmed))


def _bytes_of(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest(), sorted(os.listdir(os.path.dirname(path)))


def run_cli_in_process(db):
    """`init` run in this process (so a test can patch the migration steps). -> 0, or 2 when it refused."""
    try:
        store.init_db(db)
    except errors.QuotaError:
        return 2
    return 0


if __name__ == "__main__":
    unittest.main()
