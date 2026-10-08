"""Card 7b, step 7b-2: state version 7, the `reading` and `reading_window` tables (DESIGN-7B2.md 7.1).

Older files (versions 1 to 6) gain the two tables in one transaction and nothing else changes; a newer file is
refused. The database itself refuses a second pending row for one window, a change to a row's identity, the
deletion of a row that has not been settled, and a memory that moves backwards. Housekeeping expires a stale
pending row (only after its reset has passed AND two hours since capture) and deletes settled rows after two
days. Synthetic data only.
"""

import os
import sqlite3
import tempfile
import unittest

from quota_balancer.reporter import mapper
from quota_balancer.reporter import state as state_mod
from quota_balancer.reporter.state import READING_AGENT, State, StateError
from tests.card7b2_fixtures import FIVE_RESET_S, NOW, WEEK_RESET_S

ALIAS = "claude-main"
ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"
HOUR = 3_600_000
DAY = 24 * HOUR


def body(window="five_hour", observed=NOW, bps=200, reset_s=FIVE_RESET_S, machine="desktop", alias=ALIAS):
    key = mapper.reading_key(machine, alias, window, observed)
    return mapper.reading_body(key, ACCOUNT_ID, window, bps, reset_s * 1000, observed)


def older_ddls():
    """The DDL of each version, built from the module's own pieces (as the earlier cards' tests do)."""
    v1 = state_mod._DDL_V1
    v2 = v1 + state_mod._LIMIT_DDL
    v3 = v2 + state_mod._RECEIPT_DDL
    v4 = v3 + state_mod._SUPERSEDED_DDL
    v5 = v4 + state_mod._GWR_DDL
    v6 = v5 + state_mod._GWR_V6_DDL
    return {1: v1, 2: v2, 3: v3, 4: v4, 5: v5, 6: v6}


class _Tmp(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH") or None
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-7b2st-")
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "state.db")
        self.opened = []

    def open(self, path=None):
        state = State(path or self.path)
        self.opened.append(state)
        self.addCleanup(state.close)
        return state

    def raw(self, sql, params=(), path=None):
        conn = sqlite3.connect(path or self.path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def make_old(self, version, path=None):
        """A real file of this version, with a few rows in the tables that existed then."""
        path = path or self.path
        conn = sqlite3.connect(path, isolation_level=None)
        for statement in older_ddls()[version]:
            conn.execute(statement)
        conn.execute("INSERT INTO acquisition(agent, message_id, turn_uuid, request_id, body_original, body_current)"
                     " VALUES('alpha','m1','t1','r1','{}','{}')")
        conn.execute("INSERT INTO counter(agent, name, value) VALUES('alpha','reports_dropped',3)")
        if version >= 2:
            conn.execute("INSERT INTO limit_event(event_id, account_alias, body, captured_ms) VALUES('e1','a','{}',5)")
        if version >= 3:
            conn.execute("INSERT INTO receipt_source(alias, gateway_id, created_ms) VALUES('g','gid',5)")
        if version >= 4:
            conn.execute("INSERT INTO superseded(agent, message_id, at_ms, after_send) VALUES('alpha','m0',5,0)")
        conn.execute("PRAGMA user_version=%d" % version)
        conn.close()

    def user_tables(self, path=None):
        return [r[0] for r in self.raw("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
                                       path=path)]

    def data(self, tables, path=None):
        return {t: self.raw("SELECT * FROM %s ORDER BY 1" % t, path=path) for t in tables}

    def schema(self, path=None):
        return self.raw("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name", path=path)


class Schema(_Tmp):
    def test_a_fresh_file_is_version_7_with_both_tables_the_index_and_the_triggers(self):
        state = self.open()
        self.assertEqual(state_mod.SCHEMA_VERSION, 7)
        self.assertEqual(state.one("PRAGMA user_version")[0], 7)
        self.assertEqual([r[1] for r in state.all("PRAGMA table_info(reading)")],
                         ["id", "event_id", "account_alias", "window_key", "body", "state", "tries", "not_before_ms",
                          "last_error_code", "observed_ms", "resets_ms", "captured_ms"])
        self.assertEqual([r[1] for r in state.all("PRAGMA table_info(reading_window)")],
                         ["account_alias", "window_key", "last_observed_ms", "last_reset_ms", "last_used_bps", "gate_ms",
                          "last_sent_reset_ms"])
        names = {r[0] for r in state.all("SELECT name FROM sqlite_master WHERE type IN ('trigger','index')")}
        self.assertTrue({"reading_one_pending", "reading_immutable", "reading_keep", "reading_window_forward"} <= names)

    def test_the_agent_word_cannot_be_an_agent_name(self):
        from quota_balancer.reporter.events import _NAME
        self.assertEqual(READING_AGENT, "_readings")
        self.assertIsNone(_NAME.fullmatch(READING_AGENT))
        self.assertNotEqual(READING_AGENT, state_mod.LIMIT_AGENT)

    def test_both_tables_are_strict(self):
        state = self.open()
        for statement in ("INSERT INTO reading(event_id, account_alias, window_key, body, observed_ms, resets_ms, captured_ms)"
                          " VALUES('e','a','five_hour','{}','text',1,1)",
                          "INSERT INTO reading_window(account_alias, window_key, last_observed_ms, last_reset_ms, last_used_bps)"
                          " VALUES('a','five_hour','x',1,1)"):
            with self.assertRaises(sqlite3.Error):
                state.conn.execute(statement)

    def test_a_second_open_is_a_no_op(self):
        self.open().close()
        schema = self.schema()
        self.open().close()
        self.assertEqual(self.schema(), schema)
        self.assertEqual(self.raw("PRAGMA user_version"), [(7,)])


class Migration(_Tmp):
    def test_every_older_version_migrates_in_one_step_with_its_data_untouched(self):
        fresh = os.path.join(self._tmp.name, "fresh.db")
        self.open(fresh).close()
        fresh_schema = self.schema(fresh)
        for version in range(1, 7):
            with self.subTest(version=version):
                path = os.path.join(self._tmp.name, "v%d.db" % version)
                self.make_old(version, path)
                tables = self.user_tables(path)
                self.assertNotIn("reading", tables)
                before = self.data(tables, path)
                self.assertEqual(self.raw("PRAGMA user_version", path=path), [(version,)])
                self.open(path).close()
                self.assertEqual(self.raw("PRAGMA user_version", path=path), [(7,)])
                self.assertEqual(self.data(tables, path), before)
                self.assertEqual(self.raw("SELECT COUNT(*) FROM reading", path=path), [(0,)])
                self.assertEqual(self.raw("SELECT COUNT(*) FROM reading_window", path=path), [(0,)])
                self.assertEqual(self.schema(path), fresh_schema)

    def test_a_migrated_file_works(self):
        for version in (1, 6):
            with self.subTest(version=version):
                path = os.path.join(self._tmp.name, "w%d.db" % version)
                self.make_old(version, path)
                state = self.open(path)
                self.assertEqual(state.record_readings([(ALIAS, "five_hour", body())], {}, NOW), 1)
                self.assertEqual(state.one("SELECT state FROM reading")[0], "pending")

    def test_a_failed_migration_changes_nothing(self):
        for version in (1, 3, 6):
            with self.subTest(version=version):
                path = os.path.join(self._tmp.name, "f%d.db" % version)
                self.make_old(version, path)
                before = (self.schema(path), self.data(self.user_tables(path), path))
                original = state_mod._READING_DDL
                state_mod._READING_DDL = original + ("THIS IS NOT SQL",)
                try:
                    with self.assertRaises(StateError):
                        State(path)
                finally:
                    state_mod._READING_DDL = original
                self.assertEqual(self.raw("PRAGMA user_version", path=path), [(version,)])
                self.assertEqual((self.schema(path), self.data(self.user_tables(path), path)), before)
                self.assertEqual(self.open(path).one("PRAGMA user_version")[0], 7)

    def test_a_newer_file_is_refused_and_left_alone(self):
        self.open().close()
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA user_version=8")
        conn.commit()
        conn.close()
        schema = self.schema()
        with self.assertRaises(StateError) as caught:
            State(self.path)
        self.assertEqual(str(caught.exception), "the state file version is not supported")
        self.assertEqual((self.raw("PRAGMA user_version"), self.schema()), ([(8,)], schema))

    def test_a_version_6_reporter_would_refuse_this_file_as_newer(self):
        self.open().close()
        self.assertEqual(self.raw("PRAGMA user_version"), [(7,)])


class _Rows(_Tmp):
    def setUp(self):
        super().setUp()
        self.state = self.open()

    def add(self, window="five_hour", state="pending", alias=ALIAS, observed=NOW, reset_ms=FIVE_RESET_S * 1000, captured=NOW,
            event=None):
        self.serial = getattr(self, "serial", 0) + 1
        event = event or "e-%d" % self.serial
        self.state.conn.execute(
            "INSERT INTO reading(event_id, account_alias, window_key, body, state, observed_ms, resets_ms, captured_ms)"
            " VALUES(?,?,?,?,?,?,?,?)", (event, alias, window, "{}", state, observed, reset_ms, captured))
        return event

    def states(self):
        return self.state.all("SELECT event_id, state FROM reading ORDER BY id")


class Triggers(_Rows):
    def test_the_identity_columns_cannot_change(self):
        event = self.add()
        for column, value in (("event_id", "x"), ("account_alias", "other"), ("window_key", "seven_day"), ("body", "{\"a\":1}"),
                              ("observed_ms", 1), ("resets_ms", 1), ("captured_ms", 1)):
            with self.subTest(column=column):
                with self.assertRaises(sqlite3.IntegrityError) as caught:
                    self.state.conn.execute("UPDATE reading SET %s=? WHERE event_id=?" % column, (value, event))
                self.assertIn("immutable", str(caught.exception))

    def test_the_delivery_columns_can_change(self):
        event = self.add()
        self.state.conn.execute("UPDATE reading SET tries=2, not_before_ms=99, last_error_code='X', state='sent' WHERE event_id=?", (event,))
        self.assertEqual(self.state.one("SELECT tries, not_before_ms, last_error_code, state FROM reading")[:], (2, 99, "X", "sent"))

    def test_only_a_sent_expired_or_superseded_row_can_be_deleted(self):
        for state in ("pending", "failed_permanent"):
            event = self.add(state=state, window="five_hour" if state == "pending" else "seven_day")
            with self.assertRaises(sqlite3.IntegrityError):
                self.state.conn.execute("DELETE FROM reading WHERE event_id=?", (event,))
            self.assertEqual(self.state.one("SELECT COUNT(*) FROM reading WHERE event_id=?", (event,))[0], 1)
        for number, state in enumerate(("sent", "expired", "superseded")):
            event = self.add(state=state, observed=NOW + number + 1)
            self.state.conn.execute("DELETE FROM reading WHERE event_id=?", (event,))
            self.assertEqual(self.state.one("SELECT COUNT(*) FROM reading WHERE event_id=?", (event,))[0], 0)

    def test_a_pending_row_cannot_be_deleted_by_a_bulk_delete(self):
        self.add(state="sent", observed=NOW + 1)
        self.add(state="pending")
        with self.assertRaises(sqlite3.IntegrityError):
            self.state.conn.execute("DELETE FROM reading")
        self.assertEqual(self.state.one("SELECT COUNT(*) FROM reading")[0], 2)

    def test_the_closed_words_are_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.add(window="monthly")
        with self.assertRaises(sqlite3.IntegrityError):
            self.add(state="queued")

    def test_the_event_id_is_unique(self):
        self.add(event="same", state="sent")
        with self.assertRaises(sqlite3.IntegrityError):
            self.add(event="same", state="sent", observed=NOW + 5)

    def test_the_memory_moves_forward_only_but_the_gate_is_an_ordinary_column(self):
        self.state.conn.execute("INSERT INTO reading_window(account_alias, window_key, last_observed_ms, last_reset_ms, last_used_bps)"
                                " VALUES(?,?,?,?,?)", (ALIAS, "five_hour", NOW, 5, 200))
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.state.conn.execute("UPDATE reading_window SET last_observed_ms=? WHERE account_alias=?", (NOW - 1, ALIAS))
        self.assertIn("forward", str(caught.exception))
        self.state.conn.execute("UPDATE reading_window SET last_observed_ms=? WHERE account_alias=?", (NOW, ALIAS))
        self.state.conn.execute("UPDATE reading_window SET last_observed_ms=? WHERE account_alias=?", (NOW + 1, ALIAS))
        self.state.conn.execute("UPDATE reading_window SET gate_ms=?, last_sent_reset_ms=? WHERE account_alias=?", (NOW + 500, 7, ALIAS))
        self.state.conn.execute("UPDATE reading_window SET gate_ms=?, last_sent_reset_ms=NULL WHERE account_alias=?", (3, ALIAS))
        self.state.conn.execute("UPDATE reading_window SET last_reset_ms=1, last_used_bps=1 WHERE account_alias=?", (ALIAS,))
        self.assertEqual(self.state.one("SELECT gate_ms, last_sent_reset_ms, last_used_bps FROM reading_window")[:], (3, None, 1))

    def test_the_memory_row_cannot_be_renamed(self):
        self.state.conn.execute("INSERT INTO reading_window(account_alias, window_key, last_observed_ms, last_reset_ms, last_used_bps)"
                                " VALUES(?,?,?,?,?)", (ALIAS, "five_hour", NOW, 5, 200))
        for column, value in (("account_alias", "x"), ("window_key", "seven_day")):
            with self.assertRaises(sqlite3.IntegrityError):
                self.state.conn.execute("UPDATE reading_window SET %s=?" % column, (value,))


class OnePending(_Rows):
    def test_a_second_pending_row_for_one_window_is_refused_by_the_database(self):
        self.add(observed=NOW)
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.add(observed=NOW + 1000)
        self.assertIn("UNIQUE", str(caught.exception))
        self.assertEqual(len(self.states()), 1)

    def test_other_windows_aliases_and_states_do_not_collide(self):
        self.add(window="five_hour")
        self.add(window="seven_day", reset_ms=WEEK_RESET_S * 1000)
        self.add(window="five_hour", alias="other")
        for number, state in enumerate(("sent", "expired", "superseded", "failed_permanent")):
            self.add(window="five_hour", state=state, observed=NOW + 10 + number)
        self.assertEqual(len(self.states()), 7)

    def test_a_new_pending_row_is_possible_once_the_old_one_is_settled(self):
        event = self.add(observed=NOW)
        with self.assertRaises(sqlite3.IntegrityError):
            self.add(observed=NOW + 1000)
        self.state.conn.execute("UPDATE reading SET state='superseded' WHERE event_id=?", (event,))
        self.add(observed=NOW + 1000)
        self.assertEqual([s for _, s in self.states()], ["superseded", "pending"])

    def test_the_record_step_never_leaves_two_pending(self):
        for step in range(5):
            self.state.record_readings([(ALIAS, "five_hour", body(observed=NOW + step * 1000, bps=100 + step))], {}, NOW + step * 1000)
            pending = self.state.one("SELECT COUNT(*) FROM reading WHERE state='pending'")[0]
            self.assertEqual(pending, 1)
        self.assertEqual(self.state.one("SELECT COUNT(*) FROM reading WHERE state='superseded'")[0], 4)


class Recording(_Rows):
    def test_a_new_figure_is_stored_with_its_memory_and_counted(self):
        self.assertEqual(self.state.record_readings([(ALIAS, "five_hour", body())], {"unmapped": 3}, NOW + 7), 1)
        row = self.state.one("SELECT event_id, account_alias, window_key, state, tries, not_before_ms, observed_ms, resets_ms,"
                             " captured_ms FROM reading")
        self.assertEqual(row[1:], (ALIAS, "five_hour", "pending", 0, 0, NOW, FIVE_RESET_S * 1000, NOW + 7))
        self.assertEqual(self.state.one("SELECT * FROM reading_window")[:], (ALIAS, "five_hour", NOW, FIVE_RESET_S * 1000, 200, 0, None))
        self.assertEqual(self.state.counter(READING_AGENT, "captured"), 1)
        self.assertEqual(self.state.counter(READING_AGENT, "scan:unmapped"), 3)
        self.assertEqual(self.state.note("readings_last_scan")[1], NOW + 7)

    def test_the_scan_counts_are_absolute_and_the_captured_count_is_cumulative(self):
        self.state.record_readings([(ALIAS, "five_hour", body())], {"unmapped": 3}, NOW)
        self.state.record_readings([], {"unmapped": 1}, NOW + 1)
        self.assertEqual(self.state.counter(READING_AGENT, "scan:unmapped"), 1)
        self.state.record_readings([(ALIAS, "five_hour", body(observed=NOW + 1000))], {"unmapped": 0}, NOW + 2)
        self.assertEqual(self.state.counter(READING_AGENT, "captured"), 2)

    def test_the_outcome_counts_of_a_scan_are_absolute(self):
        self.state.record_readings([(ALIAS, "five_hour", body())], {}, NOW)
        self.state.record_readings([(ALIAS, "five_hour", body())], {}, NOW + 1)
        self.assertEqual(self.state.counter(READING_AGENT, "scan:same"), 1)
        self.state.record_readings([], {}, NOW + 2)
        self.assertEqual(self.state.counter(READING_AGENT, "scan:same"), 0)

    def test_a_figure_whose_row_was_pruned_is_still_not_stored_again(self):
        self.state.record_readings([(ALIAS, "five_hour", body())], {}, NOW)
        self.state.conn.execute("UPDATE reading SET state='sent'")
        self.state.housekeep_readings(NOW + 3 * DAY)
        self.assertEqual(self.state.one("SELECT COUNT(*) FROM reading")[0], 0)
        self.assertEqual(self.state.record_readings([(ALIAS, "five_hour", body())], {}, NOW + 3 * DAY), 0)      # the memory says: not newer
        self.assertEqual(self.state.counter(READING_AGENT, "scan:older"), 1)
        self.assertEqual(self.state.record_readings([(ALIAS, "five_hour", body(observed=NOW + 1000))], {}, NOW + 3 * DAY), 1)

    def test_a_body_that_is_not_the_allow_listed_shape_stores_nothing(self):
        bad = body()
        bad["payload"]["windows"][0]["status"] = "rejected"
        for items in ([(ALIAS, "five_hour", bad)], [(ALIAS, "five_hour", body()), (ALIAS, "seven_day", bad)]):
            with self.assertRaises(StateError) as caught:
                self.state.record_readings(items, {"a": 1}, NOW)
            self.assertEqual(str(caught.exception), "a reading body is not valid")
            self.assertEqual(self.state.one("SELECT COUNT(*) FROM reading")[0], 0)
            self.assertEqual(self.state.one("SELECT COUNT(*) FROM reading_window")[0], 0)
            self.assertEqual(self.state.counter(READING_AGENT, "captured"), 0)

    def test_an_alias_or_window_that_does_not_fit_the_body_stores_nothing(self):
        for item in (("Bad Alias", "five_hour", body()), (ALIAS, "monthly", body()), (ALIAS, "seven_day", body()),
                     (None, "five_hour", body()), (ALIAS, None, body())):
            with self.assertRaises(StateError):
                self.state.record_readings([item], {}, NOW)
        self.assertEqual(self.state.one("SELECT COUNT(*) FROM reading")[0], 0)

    def test_a_body_that_no_longer_matches_is_not_sent(self):
        self.state.record_readings([(ALIAS, "five_hour", body())], {}, NOW)
        self.assertEqual(self.state.valid_reading_body(self.state.one("SELECT body FROM reading")[0]), True)
        self.assertEqual(self.state.valid_reading_body("{\"event_id\": 1}"), False)
        self.assertEqual(self.state.valid_reading_body("not json"), False)

    def test_a_fault_is_counted_without_text(self):
        self.state.note_readings_fault(NOW)
        self.state.note_readings_fault(NOW + 5)
        self.assertEqual(self.state.counter(READING_AGENT, "capture_fault"), 2)
        self.assertEqual(self.state.note("readings_last_fault"), (None, NOW + 5))

    def test_the_limit_event_lane_is_not_touched(self):
        self.state.record_readings([(ALIAS, "five_hour", body())], {}, NOW)
        self.state.note_readings_fault(NOW)
        self.assertEqual(self.state.all("SELECT * FROM counter WHERE agent=?", (state_mod.LIMIT_AGENT,)), [])


class Housekeeping(_Rows):
    def test_a_pending_row_expires_only_after_its_reset_and_two_hours(self):
        now = NOW + 10 * HOUR
        cases = {
            "reset in the future, captured long ago": dict(reset_ms=now + 1, captured=now - 9 * HOUR),
            "reset passed, captured one hour ago": dict(reset_ms=now - HOUR, captured=now - HOUR),
            "reset passed, captured exactly two hours ago": dict(reset_ms=now - 3 * HOUR, captured=now - 2 * HOUR),
            "reset passed, captured two hours and a millisecond ago": dict(reset_ms=now - 3 * HOUR, captured=now - 2 * HOUR - 1),
            "reset exactly now, captured long ago": dict(reset_ms=now, captured=now - 5 * HOUR),
            "reset a millisecond ahead, captured long ago": dict(reset_ms=now + 1, captured=now - 5 * HOUR),
        }
        expected = {"reset in the future, captured long ago": "pending", "reset passed, captured one hour ago": "pending",
                    "reset passed, captured exactly two hours ago": "pending",
                    "reset passed, captured two hours and a millisecond ago": "expired",
                    "reset exactly now, captured long ago": "expired", "reset a millisecond ahead, captured long ago": "pending"}
        for label, kw in cases.items():
            with self.subTest(label):
                self.state.conn.execute("DELETE FROM reading WHERE state='expired'")
                self.state.conn.execute("DELETE FROM reading WHERE state='sent'")
                self.state.conn.execute("UPDATE reading SET state='sent'")
                self.add(**kw)
                expired, pruned = self.state.housekeep_readings(now)
                got = self.state.one("SELECT state FROM reading WHERE state IN ('pending','expired')")[0]
                self.assertEqual(got, expected[label])
                self.assertEqual(expired, 1 if expected[label] == "expired" else 0)

    def test_expiry_is_counted_and_only_pending_rows_expire(self):
        now = NOW + 10 * HOUR
        self.add(window="five_hour", reset_ms=now - 3 * HOUR, captured=now - 5 * HOUR)
        self.add(window="seven_day", state="failed_permanent", reset_ms=now - 3 * HOUR, captured=now - 5 * HOUR)
        self.add(window="seven_day", state="sent", observed=NOW + 1, reset_ms=now - 3 * HOUR, captured=now - 5 * HOUR)
        self.assertEqual(self.state.housekeep_readings(now), (1, 0))
        self.assertEqual(self.state.counter(READING_AGENT, "expired"), 1)
        self.assertEqual(sorted(s for _, s in self.states()), ["expired", "failed_permanent", "sent"])

    def test_an_expired_row_is_never_offered_as_pending(self):
        now = NOW + 10 * HOUR
        self.add(reset_ms=now - 3 * HOUR, captured=now - 5 * HOUR)
        self.state.housekeep_readings(now)
        self.assertEqual(self.state.all("SELECT 1 FROM reading WHERE state='pending'"), [])
        self.add(observed=NOW + 5000, reset_ms=now + HOUR, captured=now)             # a new pending row is then possible
        self.assertEqual(self.state.one("SELECT COUNT(*) FROM reading WHERE state='pending'")[0], 1)

    def test_settled_rows_are_deleted_after_two_days_and_the_rest_never(self):
        now = NOW + 10 * DAY
        for number, state in enumerate(("sent", "expired", "superseded")):
            self.add(state=state, observed=NOW + number, captured=now - 2 * DAY - 1)             # older than two days: deleted
            self.add(state=state, observed=NOW + 100 + number, captured=now - 2 * DAY)           # exactly two days: kept
        self.add(state="failed_permanent", window="seven_day", observed=NOW + 7, captured=now - 9 * DAY)
        self.add(window="seven_day", observed=NOW + 8, captured=now - 9 * DAY, reset_ms=now + DAY)
        self.state.conn.execute("INSERT INTO reading_window(account_alias, window_key, last_observed_ms, last_reset_ms, last_used_bps)"
                                " VALUES(?,?,?,?,?)", (ALIAS, "five_hour", NOW, 5, 200))
        expired, pruned = self.state.housekeep_readings(now)
        self.assertEqual((expired, pruned), (0, 3))
        self.assertEqual(sorted(s for _, s in self.states()), ["expired", "failed_permanent", "pending", "sent", "superseded"])
        self.assertEqual(self.state.one("SELECT COUNT(*) FROM reading_window")[0], 1)
        self.assertEqual(self.state.counter(READING_AGENT, "pruned"), 3)

    def test_a_row_that_expires_is_kept_for_two_days_from_its_capture(self):
        now = NOW + 10 * HOUR
        self.add(reset_ms=now - 3 * HOUR, captured=now - 3 * HOUR)
        self.state.housekeep_readings(now)
        self.assertEqual([s for _, s in self.states()], ["expired"])
        self.state.housekeep_readings(now - 3 * HOUR + 2 * DAY)
        self.assertEqual([s for _, s in self.states()], ["expired"])
        self.state.housekeep_readings(now - 3 * HOUR + 2 * DAY + 1)
        self.assertEqual(self.states(), [])

    def test_the_memory_is_never_pruned(self):
        self.state.record_readings([(ALIAS, "five_hour", body())], {}, NOW)
        self.state.housekeep_readings(NOW + 400 * DAY)
        self.assertEqual(self.state.one("SELECT COUNT(*) FROM reading_window")[0], 1)

    def test_housekeeping_is_one_transaction(self):
        now = NOW + 10 * HOUR
        self.add(reset_ms=now - 3 * HOUR, captured=now - 5 * HOUR)
        original = self.state.bump

        def broken(agent, name, amount=1):
            if name == "pruned":
                raise RuntimeError("fault")
            original(agent, name, amount)
        self.add(window="seven_day", state="sent", observed=NOW + 1, captured=now - 3 * DAY)
        self.state.bump = broken
        try:
            with self.assertRaises(RuntimeError):
                self.state.housekeep_readings(now)
        finally:
            self.state.bump = original
        self.assertEqual(sorted(s for _, s in self.states()), ["pending", "sent"])
        self.assertEqual(self.state.counter(READING_AGENT, "expired"), 0)


if __name__ == "__main__":
    unittest.main()
