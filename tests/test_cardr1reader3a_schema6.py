"""Card R1-READER step 3a: the reporter state's schema step 5 to 6.

Main already carries reporter schema version 5 (step 2) WITHOUT the archive's quarantine column and the audit table. A version number is a promise that
every file at that version has the same tables, so the additions are a step of their own: a file at version 5 is upgraded to 6 in one transaction, a fresh
file equals an upgraded one, a fault inside the upgrade leaves version 5 whole, and a reader that knows only version 5 refuses a version-6 file.

Synthetic values only.
"""

import contextlib
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from quota_balancer.reporter import gwstate, state as state_mod
from quota_balancer.reporter.state import State, StateError

from tests import test_cardr1reader2_state as base

V5_DDL = (state_mod._DDL_V1 + state_mod._LIMIT_DDL + state_mod._RECEIPT_DDL + state_mod._SUPERSEDED_DDL + gwstate.GWR_DDL)


class Schema6(unittest.TestCase):

    def setUp(self):
        scratch = os.environ.get("AGENTTALK_SCRATCH")
        if scratch:
            os.makedirs(scratch, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=scratch, prefix="reader-s6-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def path(self, name):
        return os.path.join(self.dir, name)

    @contextlib.contextmanager
    def as_version_5(self):
        """The reporter as main has it: version 5, without the quarantine column and the audit table."""
        with mock.patch.object(state_mod, "SCHEMA_VERSION", 5), mock.patch.object(state_mod, "_DDL", V5_DDL):
            yield

    def make_v5(self, name="v5.db"):
        """A real version-5 file with rows in the reader's tables (made by the version-5 definitions, through the reader's own functions)."""
        path = self.path(name)
        with self.as_version_5():
            state = State(path)
            gwstate.start(state, base.NOW, base.INC)
            gwstate.mint(state, base.GEN, base.body_for(), base.totals(through=3, months=(("2026-10", 3, 9),), no_month=1), base.NOW)
            with state.txn():
                state.conn.execute("INSERT INTO gwr_archive(reader_incarnation, halt, archived_ms) VALUES(?,?,?)", ("e" * 32, "send_refused", base.NOW))
                state.bump("alpha", "reports_dropped", 3)
            state.close()
        return path

    def raw(self, path, sql):
        conn = sqlite3.connect(path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def schema(self, path):
        return sorted(self.raw(path, "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))

    def rows(self, path):
        tables = [r[0] for r in self.raw(path, "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {t: self.raw(path, "SELECT * FROM " + t + " ORDER BY 1") for t in tables}                    # names read from sqlite_master

    def test_the_version_5_definitions_are_untouched_and_the_additions_are_their_own_step(self):
        text = " ".join(gwstate.GWR_DDL)
        self.assertNotIn("quarantine", text)
        self.assertNotIn("gwr_audit", text)
        self.assertIn("quarantine", " ".join(gwstate.GWR_V6_DDL))
        self.assertIn("gwr_audit", " ".join(gwstate.GWR_V6_DDL))
        self.assertEqual(state_mod.SCHEMA_VERSION, 7)

    def test_a_version_5_file_upgrades_to_6_and_every_old_row_is_unchanged(self):
        path = self.make_v5()
        self.assertEqual(self.raw(path, "PRAGMA user_version"), [(5,)])
        self.assertNotIn("gwr_audit", self.rows(path))
        before = self.rows(path)
        self.assertTrue(before["gwr_pending"] and before["gwr_seq"] and before["gwr_receipt_months"] and before["gwr_archive"])
        State(path).close()
        self.assertEqual(self.raw(path, "PRAGMA user_version"), [(7,)])
        after = self.rows(path)
        self.assertEqual(after.pop("gwr_audit"), [])                                                # the new table arrives empty
        self.assertEqual((after.pop("reading"), after.pop("reading_window")), ([], []))             # so do the readings tables (card 7b)
        archive = after.pop("gwr_archive")
        self.assertEqual(archive, [row + (None,) for row in before.pop("gwr_archive")])                  # the old row, plus a NULL quarantine
        self.assertEqual(after, before)                                                             # every other row, in every table, as it was

    def test_a_fresh_file_at_version_6_equals_an_upgraded_one(self):
        upgraded = self.make_v5()
        State(upgraded).close()
        fresh = self.path("fresh.db")
        State(fresh).close()
        self.assertEqual(self.raw(fresh, "PRAGMA user_version"), [(7,)])
        self.assertEqual(self.schema(fresh), self.schema(upgraded))

    def test_a_fault_inside_the_upgrade_leaves_version_5_and_no_new_table_or_column(self):
        path = self.make_v5()
        before_rows, before_schema = self.rows(path), self.schema(path)
        with mock.patch.object(state_mod, "_GWR_V6_DDL", gwstate.GWR_V6_DDL + ("THIS IS NOT SQL",)):
            with self.assertRaises(StateError):
                State(path)
        self.assertEqual(self.raw(path, "PRAGMA user_version"), [(5,)])
        self.assertEqual(self.schema(path), before_schema)
        self.assertEqual(self.rows(path), before_rows)
        columns = [r[1] for r in self.raw(path, "PRAGMA table_info(gwr_archive)")]
        self.assertNotIn("quarantine", columns)
        State(path).close()                                                                         # and the good code still upgrades it
        self.assertEqual(self.raw(path, "PRAGMA user_version"), [(7,)])

    def test_a_fault_between_the_two_statements_of_the_step_also_leaves_version_5(self):
        path = self.make_v5()
        before_schema = self.schema(path)
        first_only = gwstate.GWR_V6_DDL[:1] + ("THIS IS NOT SQL",)                                  # the column is added, then the step fails
        with mock.patch.object(state_mod, "_GWR_V6_DDL", first_only):
            with self.assertRaises(StateError):
                State(path)
        self.assertEqual(self.raw(path, "PRAGMA user_version"), [(5,)])
        self.assertEqual(self.schema(path), before_schema)

    def test_a_reader_that_knows_only_version_5_refuses_a_version_6_file_and_changes_nothing(self):
        path = self.path("six.db")
        State(path).close()
        before = (self.raw(path, "PRAGMA user_version"), self.schema(path), self.rows(path))
        with self.as_version_5():
            with self.assertRaises(StateError):
                State(path)
        self.assertEqual((self.raw(path, "PRAGMA user_version"), self.schema(path), self.rows(path)), before)

    def test_files_of_every_older_version_reach_6_in_one_step(self):
        for version, ddl in ((3, state_mod._DDL_V1 + state_mod._LIMIT_DDL + state_mod._RECEIPT_DDL),
                             (4, state_mod._DDL_V1 + state_mod._LIMIT_DDL + state_mod._RECEIPT_DDL + state_mod._SUPERSEDED_DDL)):
            with self.subTest(version=version):
                path = self.path("v%d.db" % version)
                with mock.patch.object(state_mod, "SCHEMA_VERSION", version), mock.patch.object(state_mod, "_DDL", ddl):
                    State(path).close()
                State(path).close()
                self.assertEqual(self.raw(path, "PRAGMA user_version"), [(7,)])
                names = {r[0] for r in self.raw(path, "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue(set(gwstate.TABLES) <= names)
                fresh = self.path("fresh%d.db" % version)
                State(fresh).close()
                self.assertEqual(self.schema(path), self.schema(fresh))


if __name__ == "__main__":
    unittest.main()
