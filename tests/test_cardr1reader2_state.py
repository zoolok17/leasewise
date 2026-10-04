"""Card R1-READER step 2: the money reader's durable memory (`quota_balancer/reporter/gwstate.py`, DESIGN-R1-READER.md 3.1, 7, 8, 10; D-195, D-203).

* **The tables** (a reporter schema step, version 5): constraints in the schema wherever they can be written there; only numbers, hex strings,
  counters and the exact report bytes.
* **The start-up check**, per generation: exactly two consistent pictures (`minted == acked` with no pending row, or `minted == acked + 1` with a
  pending row of that number), together with the bytes, the derived event id, the incarnation and the epoch mapping; anything else halts as
  `state_unavailable` and nothing can be minted. D-203: a complete absence is a first start; an absent identity with any surviving row halts.
* **Mint** and **acceptance**, each in one transaction, and **reset** (archive, then a fresh identity): a fault after each statement leaves the
  state exactly as it was (a whole-database dump is compared).

Nothing here reads a gateway or sends anything. Synthetic values only.
"""

import contextlib
import json
import os
import re
import sqlite3
import tempfile
import unittest
from unittest import mock

from quota_balancer.reporter import gwstate, state as state_mod
from quota_balancer.reporter.gwreport import Totals
from quota_balancer.reporter.state import State
from quota_balancer.validate_readings import derive_event_id

GEN = "0123456789abcdef0123456789abcdef"
GEN2 = "fedcba9876543210fedcba9876543210"
INC = "d" * 32
NOW = 1_790_000_000_000
EMPTY = Totals(GEN, 0, (), 0)


def totals(generation=GEN, through=0, months=(), no_month=0):
    return Totals(generation, through, tuple(months), no_month)


def body_for(generation=GEN, **over):
    """A report builder: the exact bytes state the number, identity and epoch the state gives them (`over` breaks one of them)."""
    def build(incarnation, epoch, seq):
        facts = {"event_id": derive_event_id(incarnation, generation, seq), "reader_incarnation": incarnation, "generation": generation,
                 "seq": seq, "ledger_epoch": epoch}
        facts.update(over)
        return json.dumps({"event_id": facts["event_id"], "kind": "gateway_snapshot",
                           "payload": {k: facts[k] for k in ("reader_incarnation", "generation", "seq", "ledger_epoch")}},
                          sort_keys=True).encode("utf-8")
    return build


class _State(unittest.TestCase):

    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="reader2-")
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "r.db")
        self.state = State(self.path)
        self.addCleanup(self.state.close)

    def dump(self):
        return "\n".join(self.state.conn.iterdump())

    def run_start(self, incarnation=INC):
        return gwstate.start(self.state, NOW, incarnation)

    def mint(self, generation=GEN, tot=None, build=None):
        return gwstate.mint(self.state, generation, build or body_for(generation), tot or totals(generation), NOW)

    def accepted(self, event_id, status=202):
        return gwstate.accept(self.state, status, {"accepted": True, "event_id": event_id})

    def started(self):
        got = self.run_start()
        self.assertEqual((got.halt, got.incarnation), (None, INC))
        return got

    def forced(self, *statements):
        """Raw damage: statements run with the schema's own checks switched off (a restored or damaged file can hold anything)."""
        self.state.conn.execute("PRAGMA ignore_check_constraints=ON")
        try:
            for statement in statements:
                if isinstance(statement, tuple):
                    self.state.conn.execute(statement[0], statement[1])
                else:
                    self.state.conn.execute(statement)
        finally:
            self.state.conn.execute("PRAGMA ignore_check_constraints=OFF")

    def count_steps(self, action):
        seen = []
        with mock.patch.object(gwstate, "_fault", seen.append):
            action()
        return seen

    @contextlib.contextmanager
    def fault_at(self, number):
        calls = []

        def stop(step):
            calls.append(step)
            if len(calls) == number:
                raise RuntimeError("fault after " + step)
        with mock.patch.object(gwstate, "_fault", stop):
            yield

    def refused(self, word, call, *args):
        before = self.dump()
        with self.assertRaises(gwstate.Refused) as caught:
            call(*args)
        self.assertEqual(caught.exception.word, word)
        self.assertEqual(self.dump(), before)


class Schema(_State):

    def test_the_file_is_version_6_with_the_seven_tables(self):
        self.assertEqual(state_mod.SCHEMA_VERSION, 6)
        self.assertEqual(self.state.one("PRAGMA user_version")[0], 6)
        names = {r[0] for r in self.state.all("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(set(gwstate.TABLES) <= names)

    def test_a_version_4_file_gains_the_tables_empty_and_keeps_every_old_row(self):
        path = os.path.join(self._tmp.name, "old.db")
        original = (state_mod.SCHEMA_VERSION, state_mod._DDL)
        state_mod.SCHEMA_VERSION, state_mod._DDL = 4, state_mod._DDL_V1 + state_mod._LIMIT_DDL + state_mod._RECEIPT_DDL + state_mod._SUPERSEDED_DDL
        try:
            old = State(path)
            with old.txn():
                old.conn.execute("INSERT INTO counter(agent, name, value) VALUES('alpha','reports_dropped',3)")
            old.close()
        finally:
            state_mod.SCHEMA_VERSION, state_mod._DDL = original
        raw = sqlite3.connect(path)
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 4)
        raw.close()
        migrated = State(path)
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.one("PRAGMA user_version")[0], 6)
        self.assertEqual(migrated.all("SELECT agent, name, value FROM counter"), [("alpha", "reports_dropped", 3)])
        for table in gwstate.TABLES:
            self.assertEqual(migrated.all("SELECT * FROM " + table), [], table)                         # fixed names

    def test_a_failed_migration_leaves_a_version_4_file_whole(self):
        path = os.path.join(self._tmp.name, "old2.db")
        original = (state_mod.SCHEMA_VERSION, state_mod._DDL)
        state_mod.SCHEMA_VERSION, state_mod._DDL = 4, state_mod._DDL_V1 + state_mod._LIMIT_DDL + state_mod._RECEIPT_DDL + state_mod._SUPERSEDED_DDL
        try:
            State(path).close()
        finally:
            state_mod.SCHEMA_VERSION, state_mod._DDL = original
        with mock.patch.object(state_mod, "_GWR_DDL", gwstate.GWR_DDL + ("THIS IS NOT SQL",)):
            with self.assertRaises(state_mod.StateError):
                State(path)
        raw = sqlite3.connect(path)
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual(raw.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'gwr_%'").fetchone()[0], 0)
        raw.close()

    def test_the_schema_refuses_what_it_can(self):
        self.started()
        self.mint()
        bad = [
            ("a second pending row", "INSERT INTO gwr_pending(id, generation, seq, body, event_id, idempotency_key, made_ms) VALUES(2,?,1,x'7b7d',?,?,1)"
                                     .replace("?,1,x", "?,1,x"), (GEN, derive_event_id(INC, GEN, 1), derive_event_id(INC, GEN, 1))),
            ("minted two ahead of acked", "UPDATE gwr_seq SET minted_seq=5", ()),
            ("acked above minted", "UPDATE gwr_seq SET acked_seq=9", ()),
            ("a negative number", "UPDATE gwr_seq SET minted_seq=-1", ()),
            ("a number above the 64-bit bound", "UPDATE gwr_seq SET minted_seq=9223372036854775808", ()),
            ("an unknown halt word", "UPDATE gwr_identity SET halt='because'", ()),
            ("a short incarnation", "INSERT INTO gwr_archive(reader_incarnation, archived_ms) VALUES('abc', 1)", ()),
            ("a month that is not a month", "INSERT INTO gwr_receipt_months VALUES(?, '2026-13', 1, 1)", (GEN,)),
            ("a pending report that is not JSON", "UPDATE gwr_pending SET body=x'ff00'", ()),
            ("an event id that is not an id", "UPDATE gwr_pending SET event_id='alice-the-agent'", ()),
            ("an identity row that is not row 1", "INSERT INTO gwr_identity(id, reader_incarnation, created_ms) VALUES(2, ?, 1)", (INC,)),
        ]
        for label, sql, params in bad:
            with self.subTest(label):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.state.conn.execute(sql, params)

    def test_the_incarnation_never_changes_and_the_archive_is_never_changed_or_erased(self):
        self.started()
        with self.assertRaises(sqlite3.DatabaseError):
            self.state.conn.execute("UPDATE gwr_identity SET reader_incarnation=?", ("e" * 32,))
        gwstate.reset(self.state, NOW, "e" * 32)
        with self.assertRaises(sqlite3.DatabaseError):
            self.state.conn.execute("UPDATE gwr_archive SET halt='send_refused'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.state.conn.execute("DELETE FROM gwr_archive")

    def test_no_table_or_column_can_hold_a_name_or_a_path(self):
        """Privacy: every column is a number, a hex string, a month, an enumerated word, or the exact report bytes; no column is named for a name."""
        self.started()
        self.mint()
        forbidden = re.compile(r"agent|message|lease|path|name|user|account|machine|alias|(^|_)ref(_|$)")
        for table in gwstate.TABLES:
            columns = self.state.all("PRAGMA table_info(" + table + ")")                                  # fixed names
            self.assertFalse([c[1] for c in columns if forbidden.search(c[1])], table)
        # every TEXT column refuses a name-like string and a path, with every other value valid
        loud = ("alice-the-agent", "C:\\Users\\someone\\x", "/home/x", "")
        statements = [
            ("gwr_identity.reader_incarnation", "INSERT INTO gwr_identity(id, reader_incarnation, created_ms) VALUES(2,?,1)"),
            ("gwr_identity.halt", "UPDATE gwr_identity SET halt=?"),
            ("gwr_epoch.generation", "INSERT INTO gwr_epoch(ledger_epoch, generation) VALUES(9,?)"),
            ("gwr_seq.generation", "INSERT INTO gwr_seq(generation, minted_seq, acked_seq) VALUES(?,0,0)"),
            ("gwr_pending.generation", "UPDATE gwr_pending SET generation=?"),
            ("gwr_pending.event_id", "UPDATE gwr_pending SET event_id=?"),
            ("gwr_pending.idempotency_key", "UPDATE gwr_pending SET idempotency_key=?"),
            ("gwr_receipts.generation", "INSERT INTO gwr_receipts(generation, read_through_seq, no_month) VALUES(?,0,0)"),
            ("gwr_receipt_months.generation", "INSERT INTO gwr_receipt_months VALUES(?, '2026-10', 1, 1)"),
            ("gwr_receipt_months.period", "INSERT INTO gwr_receipt_months VALUES(?, ?, 1, 1)"),
            ("gwr_archive.reader_incarnation", "INSERT INTO gwr_archive(reader_incarnation, archived_ms) VALUES(?,1)"),
            ("gwr_archive.halt", "INSERT INTO gwr_archive(halt, archived_ms) VALUES(?,1)"),
            ("gwr_archive.pending_generation", "INSERT INTO gwr_archive(pending_generation, pending_seq, pending_event_id, pending_body, archived_ms)"
                                               " VALUES(?,1,?,x'7b7d',1)"),
            ("gwr_archive.pending_event_id", "INSERT INTO gwr_archive(pending_generation, pending_seq, pending_event_id, pending_body, archived_ms)"
                                             " VALUES(?,1,?,x'7b7d',1)"),
        ]
        for label, sql in statements:
            for value in loud:
                with self.subTest(label, value=value[:8]):
                    params = tuple(value if mark == "?" else mark for mark in ["?"] * sql.count("?"))
                    if label.endswith("pending_generation"):
                        params = (value, derive_event_id(INC, GEN, 1))
                    elif label.endswith("pending_event_id"):
                        params = (GEN, value)
                    elif label.endswith("receipt_months.period"):
                        params = (GEN, value)
                    elif label.endswith("receipt_months.generation") or label.endswith("gwr_receipts.generation"):
                        params = (value,)
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.state.conn.execute(sql, params)


class StartUpCheck(_State):

    def test_a_complete_absence_is_a_first_start_with_a_fresh_incarnation(self):
        got = gwstate.start(self.state, NOW)                                         # a random incarnation
        self.assertEqual((got.halt, got.fresh, got.persisted), (None, True, True))
        self.assertRegex(got.incarnation, r"^[0-9a-f]{32}$")
        self.assertEqual(self.state.all("SELECT reader_incarnation, halt, created_ms FROM gwr_identity"), [(got.incarnation, None, NOW)])
        again = gwstate.start(self.state, NOW + 1)
        self.assertEqual((again.halt, again.incarnation, again.fresh), (None, got.incarnation, False))

    def test_two_first_starts_make_two_different_incarnations(self):
        first = gwstate.start(self.state, NOW).incarnation
        gwstate.reset(self.state, NOW)
        second = self.state.one("SELECT reader_incarnation FROM gwr_identity")[0]
        self.assertNotEqual(first, second)

    def test_zero_over_zero_is_valid(self):
        self.started()
        self.forced(("INSERT INTO gwr_epoch(ledger_epoch, generation) VALUES(1,?)", (GEN,)),
                    ("INSERT INTO gwr_seq(generation, minted_seq, acked_seq) VALUES(?,0,0)", (GEN,)),
                    ("INSERT INTO gwr_receipts(generation, read_through_seq, no_month) VALUES(?,0,0)", (GEN,)))
        self.assertIsNone(gwstate.inconsistency(self.state))
        self.assertIsNone(self.run_start().halt)

    def test_a_minted_report_with_its_pending_row_is_valid_and_so_is_the_accepted_state_after_it(self):
        self.started()
        seq, event_id, _body = self.mint()
        self.assertEqual(seq, 1)
        self.assertIsNone(self.run_start().halt)
        self.assertTrue(self.accepted(event_id))
        self.assertIsNone(self.run_start().halt)

    def test_a_return_to_an_earlier_fully_acknowledged_generation_is_valid_and_takes_a_new_epoch(self):
        self.started()
        for generation in (GEN, GEN2, GEN):
            self.accepted(self.mint(generation)[1])
        self.assertEqual(self.state.all("SELECT ledger_epoch, generation FROM gwr_epoch ORDER BY 1"), [(1, GEN), (2, GEN2), (3, GEN)])
        self.assertEqual(self.state.all("SELECT generation, minted_seq, acked_seq FROM gwr_seq ORDER BY generation"),
                         [(GEN, 2, 2), (GEN2, 1, 1)])                                   # the numbers of a generation keep growing
        self.assertIsNone(self.run_start().halt)
        self.assertEqual(self.mint(GEN2)[0], 2)

    def halts(self, label):
        got = self.run_start()
        self.assertEqual(got.halt, gwstate.STATE_UNAVAILABLE, label)
        self.assertEqual(self.state.one("SELECT halt FROM gwr_identity")[0], gwstate.STATE_UNAVAILABLE, label)
        self.refused(gwstate.HALTED, self.mint)

    def test_every_inconsistent_picture_halts_and_nothing_can_be_minted(self):
        eid1, eid2 = derive_event_id(INC, GEN, 1), derive_event_id(INC, GEN, 2)
        pending = "INSERT INTO gwr_pending(id, generation, seq, body, event_id, idempotency_key, made_ms) VALUES(1,?,?,?,?,?,1)"
        cases = {}

        def case(label, *statements):
            cases[label] = statements

        base = [("INSERT INTO gwr_epoch(ledger_epoch, generation) VALUES(1,?)", (GEN,))]
        case("minted ahead of acked without a pending row", *base, ("INSERT INTO gwr_seq VALUES(?,1,0)", (GEN,)))
        case("acked ahead of minted", *base, ("INSERT INTO gwr_seq VALUES(?,0,1)", (GEN,)))
        case("minted two ahead of acked", *base, ("INSERT INTO gwr_seq VALUES(?,2,0)", (GEN,)))
        case("a pending row on equal numbers", *base, ("INSERT INTO gwr_seq VALUES(?,1,1)", (GEN,)),
             (pending, (GEN, 2, body_for()(INC, 1, 2), eid2, eid2)))
        case("a pending row of the wrong number", *base, ("INSERT INTO gwr_seq VALUES(?,2,1)", (GEN,)),
             (pending, (GEN, 1, body_for()(INC, 1, 1), eid1, eid1)))
        case("pending bytes whose event id does not match", *base, ("INSERT INTO gwr_seq VALUES(?,1,0)", (GEN,)),
             (pending, (GEN, 1, body_for(event_id=eid2)(INC, 1, 1), eid1, eid1)))
        case("a derived event id that is not the one for this incarnation", *base, ("INSERT INTO gwr_seq VALUES(?,1,0)", (GEN,)),
             (pending, (GEN, 1, body_for()(INC, 1, 1), eid2, eid2)))
        case("an idempotency key that is not the event id", *base, ("INSERT INTO gwr_seq VALUES(?,1,0)", (GEN,)),
             (pending, (GEN, 1, body_for()(INC, 1, 1), eid1, eid2)))
        case("pending bytes of another incarnation", *base, ("INSERT INTO gwr_seq VALUES(?,1,0)", (GEN,)),
             (pending, (GEN, 1, body_for(reader_incarnation="e" * 32)(INC, 1, 1), eid1, eid1)))
        case("pending bytes of another generation", *base, ("INSERT INTO gwr_seq VALUES(?,1,0)", (GEN,)),
             (pending, (GEN, 1, body_for(generation=GEN2)(INC, 1, 1), eid1, eid1)))
        case("pending bytes of another number", *base, ("INSERT INTO gwr_seq VALUES(?,1,0)", (GEN,)),
             (pending, (GEN, 1, body_for(seq=7)(INC, 1, 1), eid1, eid1)))
        case("pending bytes naming an epoch the mapping does not have", *base, ("INSERT INTO gwr_seq VALUES(?,1,0)", (GEN,)),
             (pending, (GEN, 1, body_for(ledger_epoch=4)(INC, 1, 1), eid1, eid1)))
        case("pending bytes naming an epoch of another generation", ("INSERT INTO gwr_epoch(ledger_epoch, generation) VALUES(1,?)", (GEN,)),
             ("INSERT INTO gwr_epoch(ledger_epoch, generation) VALUES(2,?)", (GEN2,)), ("INSERT INTO gwr_seq VALUES(?,1,0)", (GEN,)),
             (pending, (GEN, 1, body_for(ledger_epoch=2)(INC, 1, 1), eid1, eid1)))
        case("pending bytes that are not a report", *base, ("INSERT INTO gwr_seq VALUES(?,1,0)", (GEN,)),
             (pending, (GEN, 1, b"{}", eid1, eid1)))
        case("an orphan pending row (a generation without progress)", *base, ("INSERT INTO gwr_seq VALUES(?,0,0)", (GEN2,)),
             (pending, (GEN, 1, body_for()(INC, 1, 1), eid1, eid1)))
        case("a generation with progress and no epoch", ("INSERT INTO gwr_seq VALUES(?,0,0)", (GEN,)))
        case("two unfinished generations", *base, ("INSERT INTO gwr_epoch(ledger_epoch, generation) VALUES(2,?)", (GEN2,)),
             ("INSERT INTO gwr_seq VALUES(?,1,0)", (GEN,)), ("INSERT INTO gwr_seq VALUES(?,1,0)", (GEN2,)),
             (pending, (GEN, 1, body_for()(INC, 1, 1), eid1, eid1)))
        case("a receipt row without progress", ("INSERT INTO gwr_receipts VALUES(?,3,0)", (GEN,)))
        case("a receipt month without its receipt row", *base, ("INSERT INTO gwr_seq VALUES(?,0,0)", (GEN,)),
             ("INSERT INTO gwr_receipt_months VALUES(?, '2026-10', 1, 1)", (GEN,)))
        for label, statements in cases.items():
            with self.subTest(label):
                self.setUp()                                                          # a fresh file for each picture
                self.started()
                self.forced(*statements)
                self.halts(label)

    def test_a_halt_already_set_is_kept_and_a_clean_state_with_a_halt_stays_halted(self):
        self.started()
        self.forced("UPDATE gwr_identity SET halt='continuity_conflict'")
        self.forced(("INSERT INTO gwr_seq VALUES(?,2,0)", (GEN,)))                  # also inconsistent
        self.assertEqual(self.run_start().halt, gwstate.CONTINUITY_CONFLICT)         # only `reset` clears either

    def test_absence_of_the_identity_with_any_surviving_row_halts_and_is_never_a_first_start(self):
        survivors = {
            "a pending row": [("INSERT INTO gwr_epoch(ledger_epoch, generation) VALUES(1,?)", (GEN,)),
                              ("INSERT INTO gwr_seq VALUES(?,1,0)", (GEN,)),
                              ("INSERT INTO gwr_pending(id, generation, seq, body, event_id, idempotency_key, made_ms) VALUES(1,?,1,x'7b7d',?,?,1)",
                               (GEN, derive_event_id(INC, GEN, 1), derive_event_id(INC, GEN, 1)))],
            "a progress row": [("INSERT INTO gwr_seq VALUES(?,0,0)", (GEN,))],
            "an epoch row": [("INSERT INTO gwr_epoch(ledger_epoch, generation) VALUES(1,?)", (GEN,))],
            "a receipt row": [("INSERT INTO gwr_seq VALUES(?,0,0)", (GEN,)), ("INSERT INTO gwr_receipts VALUES(?,5,0)", (GEN,))],
            "a receipt month row": [("INSERT INTO gwr_seq VALUES(?,0,0)", (GEN,)), ("INSERT INTO gwr_receipts VALUES(?,5,0)", (GEN,)),
                                    ("INSERT INTO gwr_receipt_months VALUES(?, '2026-10', 1, 1)", (GEN,))],
        }
        for label, statements in survivors.items():
            with self.subTest(label):
                self.setUp()
                self.forced(*statements)
                before = self.dump()
                got = self.run_start()
                self.assertEqual((got.halt, got.fresh, got.persisted, got.incarnation), (gwstate.STATE_UNAVAILABLE, False, False, None))
                self.assertEqual(self.state.all("SELECT * FROM gwr_identity"), [])      # no identity was made up
                self.assertEqual(self.dump(), before)                                   # and nothing else changed
                self.assertEqual(self.run_start().halt, gwstate.STATE_UNAVAILABLE)      # found again by every start
                self.refused(gwstate.HALTED, self.mint)

    def test_an_archive_alone_is_not_a_surviving_row_it_is_a_first_start(self):
        self.started()
        gwstate.reset(self.state, NOW, "e" * 32)
        self.state.conn.execute("DELETE FROM gwr_identity")                              # the identity is gone; only the archive is left
        got = self.run_start("f" * 32)
        self.assertEqual((got.halt, got.fresh, got.incarnation), (None, True, "f" * 32))


class Mint(_State):

    def test_the_first_report_is_numbered_saved_and_kept_with_its_receipts_in_one_step(self):
        self.started()
        seen = []

        def build(incarnation, epoch, seq):
            seen.append((incarnation, epoch, seq))
            return body_for()(incarnation, epoch, seq)
        tot = totals(through=7, months=(("2026-09", 2, 30), ("2026-10", 1, 5)), no_month=3)
        seq, event_id, body = gwstate.mint(self.state, GEN, build, tot, NOW + 5)
        self.assertEqual((seq, event_id, seen), (1, derive_event_id(INC, GEN, 1), [(INC, 1, 1)]))
        self.assertEqual(self.state.all("SELECT generation, seq, body, event_id, idempotency_key, made_ms, refusals FROM gwr_pending"),
                         [(GEN, 1, body, event_id, event_id, NOW + 5, 0)])
        self.assertEqual(self.state.all("SELECT * FROM gwr_seq"), [(GEN, 1, 0)])
        self.assertEqual(self.state.all("SELECT * FROM gwr_epoch"), [(1, GEN)])
        self.assertEqual(self.state.all("SELECT * FROM gwr_receipts"), [(GEN, 7, 3)])
        self.assertEqual(self.state.all("SELECT * FROM gwr_receipt_months ORDER BY period"), [(GEN, "2026-09", 2, 30), (GEN, "2026-10", 1, 5)])
        self.assertIsNone(gwstate.inconsistency(self.state))

    def test_the_next_number_follows_only_the_acceptance_of_the_last(self):
        self.started()
        first = self.mint()
        self.refused(gwstate.PENDING_EXISTS, self.mint)                              # the pending report is the only thing that may exist
        self.assertTrue(self.accepted(first[1]))
        second = self.mint(tot=totals(through=4))
        self.assertEqual(second[0], 2)
        self.assertEqual(second[1], derive_event_id(INC, GEN, 2))
        self.assertEqual(self.state.all("SELECT read_through_seq FROM gwr_receipts"), [(4,)])

    def test_a_mint_is_refused_while_halted_and_before_any_start(self):
        self.refused(gwstate.HALTED, self.mint)                                       # no identity at all
        self.started()
        for word in gwstate.HALT_WORDS:
            with self.subTest(word):
                self.forced(("UPDATE gwr_identity SET halt=?", (word,)))
                self.refused(gwstate.HALTED, self.mint)
        self.forced("UPDATE gwr_identity SET halt=NULL")
        self.mint()

    def test_a_mint_is_refused_when_the_numbers_are_apart_without_a_pending_row(self):
        self.started()
        self.forced(("INSERT INTO gwr_epoch(ledger_epoch, generation) VALUES(1,?)", (GEN,)), ("INSERT INTO gwr_seq VALUES(?,1,0)", (GEN,)))
        self.refused(gwstate.HALTED, self.mint)                                       # inconsistent: nothing was made up to repair it

    def test_a_report_that_does_not_state_the_numbers_the_state_gave_it_is_refused_whole(self):
        self.started()
        for label, build in (("another seq", body_for(seq=9)), ("another epoch", body_for(ledger_epoch=9)),
                             ("another incarnation", body_for(reader_incarnation="e" * 32)), ("another generation", body_for(generation=GEN2)),
                             ("another event id", body_for(event_id=derive_event_id(INC, GEN, 5))),
                             ("not bytes", lambda i, e, s: "text"), ("empty", lambda i, e, s: b""), ("not a report", lambda i, e, s: b"[]"),
                             ("too large", lambda i, e, s: b"x" * (gwstate.MAX_BODY_BYTES + 1))):
            with self.subTest(label):
                self.refused(gwstate.BAD_REPORT, self.mint, GEN, None, build)
        self.assertEqual(self.state.all("SELECT COUNT(*) FROM gwr_pending"), [(0,)])

    def test_receipts_that_do_not_belong_or_go_backwards_are_refused_whole(self):
        self.started()
        self.refused(gwstate.BAD_RECEIPTS, self.mint, GEN, totals(GEN2))
        self.refused(gwstate.BAD_RECEIPTS, self.mint, GEN, totals(through=-1))
        self.refused(gwstate.BAD_RECEIPTS, self.mint, GEN, totals(months=(("2026-13", 1, 1),)))
        self.refused(gwstate.BAD_RECEIPTS, self.mint, GEN, totals(months=(("alice-the-agent", 1, 1),)))
        first = self.mint(tot=totals(through=5))
        self.accepted(first[1])
        self.refused(gwstate.BAD_RECEIPTS, self.mint, GEN, totals(through=4))        # the watermark never goes down
        self.assertEqual(self.state.all("SELECT read_through_seq FROM gwr_receipts"), [(5,)])

    def test_a_bad_generation_is_refused(self):
        self.started()
        for generation in ("", "g" * 32, GEN.upper(), GEN + "0", None, 5):
            with self.subTest(generation=generation):
                self.refused(gwstate.BAD_GENERATION, self.mint, generation)

    def test_the_receipts_of_a_generation_are_replaced_not_added_to(self):
        self.started()
        first = self.mint(tot=totals(through=3, months=(("2026-10", 3, 9),)))
        self.accepted(first[1])
        self.mint(tot=totals(through=5, months=(("2026-10", 5, 20),)))
        self.assertEqual(self.state.all("SELECT * FROM gwr_receipt_months"), [(GEN, "2026-10", 5, 20)])

    def test_a_fault_after_each_statement_of_a_mint_leaves_the_state_exactly_as_it_was(self):
        self.started()
        steps = self.count_steps(lambda: self.mint(tot=totals(through=2, months=(("2026-10", 2, 8),), no_month=1)))
        self.assertGreaterEqual(len(steps), 6)
        for number in range(1, len(steps) + 1):
            self.setUp()
            self.started()
            before = self.dump()
            with self.subTest(after=steps[number - 1]):
                with self.fault_at(number):
                    with self.assertRaises(RuntimeError):
                        self.mint(tot=totals(through=2, months=(("2026-10", 2, 8),), no_month=1))
                self.assertEqual(self.dump(), before)
                self.mint(tot=totals(through=2, months=(("2026-10", 2, 8),), no_month=1))         # and without a fault it goes through


class Acceptance(_State):

    def pending_state(self):
        self.started()
        return self.mint()[1]

    def test_the_exact_answer_clears_the_pending_row_and_advances_acked(self):
        event_id = self.pending_state()
        self.assertTrue(self.accepted(event_id))
        self.assertEqual(self.state.all("SELECT * FROM gwr_pending"), [])
        self.assertEqual(self.state.all("SELECT minted_seq, acked_seq FROM gwr_seq"), [(1, 1)])

    def test_anything_else_changes_nothing(self):
        event_id = self.pending_state()
        before = self.dump()
        other = derive_event_id(INC, GEN, 2)
        answers = [(202, {"accepted": "true", "event_id": event_id}), (202, {"accepted": 1, "event_id": event_id}),
                   (202, {"accepted": False, "event_id": event_id}), (202, {"event_id": event_id}),
                   (202, {"accepted": True, "event_id": other}), (202, {"accepted": True}), (202, {"accepted": True, "event_id": None}),
                   (202, {"accepted": True, "event_id": 5}), (200, {"accepted": True, "event_id": event_id}),
                   (201, {"accepted": True, "event_id": event_id}), (409, {"accepted": True, "event_id": event_id}),
                   (True, {"accepted": True, "event_id": event_id}), ("202", {"accepted": True, "event_id": event_id}),
                   (202, None), (202, "accepted"), (202, [event_id]), (202, 5), (None, None)]
        for status, answer in answers:
            with self.subTest(status=status, answer=answer):
                self.assertFalse(gwstate.accept(self.state, status, answer))
                self.assertEqual(self.dump(), before)

    def test_an_answer_with_nothing_pending_changes_nothing(self):
        self.started()
        before = self.dump()
        self.assertFalse(self.accepted(derive_event_id(INC, GEN, 1)))
        self.assertEqual(self.dump(), before)

    def test_an_inconsistent_state_accepts_nothing(self):
        event_id = self.pending_state()
        self.forced("UPDATE gwr_pending SET seq=2")
        before = self.dump()
        self.assertFalse(self.accepted(event_id))
        self.assertEqual(self.dump(), before)

    def test_a_fault_after_each_statement_of_an_acceptance_leaves_the_state_exactly_as_it_was(self):
        event_id = self.pending_state()
        before = self.dump()
        steps = self.count_steps(lambda: self.assertTrue(self.accepted(event_id)))
        self.assertEqual(steps, ["deleted", "acked"])
        for number in (1, 2):
            self.setUp()
            event_id = self.pending_state()
            before = self.dump()
            with self.fault_at(number):
                with self.assertRaises(RuntimeError):
                    self.accepted(event_id)
            self.assertEqual(self.dump(), before)
            self.assertTrue(self.accepted(event_id))                                                 # and the same answer still works afterwards


class Reset(_State):

    def test_reset_archives_the_old_state_and_starts_a_fresh_one(self):
        self.started()
        seq, event_id, body = self.mint(tot=totals(through=3, months=(("2026-10", 3, 9),)))
        self.forced("UPDATE gwr_identity SET halt='send_refused'")
        fresh = gwstate.reset(self.state, NOW + 9, "e" * 32)
        self.assertEqual(fresh, "e" * 32)
        self.assertEqual(self.state.all("SELECT reader_incarnation, halt, pending_generation, pending_seq, pending_event_id, pending_body, archived_ms "
                                        "FROM gwr_archive"), [(INC, "send_refused", GEN, 1, event_id, body, NOW + 9)])
        self.assertEqual(self.state.all("SELECT reader_incarnation, halt, created_ms FROM gwr_identity"), [("e" * 32, None, NOW + 9)])
        for table in ("gwr_epoch", "gwr_seq", "gwr_pending", "gwr_receipts", "gwr_receipt_months"):
            self.assertEqual(self.state.all("SELECT * FROM " + table), [], table)                  # fixed names
        self.assertIsNone(gwstate.start(self.state, NOW + 10).halt)
        again = gwstate.mint(self.state, GEN, body_for(), totals(), NOW + 11)
        self.assertEqual(again[0], 1)
        self.assertEqual(again[1], derive_event_id("e" * 32, GEN, 1))
        self.assertNotEqual(again[1], event_id)                                                   # a new incarnation can never collide with an old report

    def test_a_reset_with_nothing_pending_and_no_halt_archives_the_incarnation_alone(self):
        self.started()
        gwstate.reset(self.state, NOW, "e" * 32)
        self.assertEqual(self.state.all("SELECT reader_incarnation, halt, pending_generation, pending_body FROM gwr_archive"), [(INC, None, None, None)])
        gwstate.reset(self.state, NOW + 1, "f" * 32)
        self.assertEqual([r[0] for r in self.state.all("SELECT reader_incarnation FROM gwr_archive ORDER BY id")], [INC, "e" * 32])

    def test_a_reset_clears_the_halt_and_recovers_from_an_absent_identity_with_surviving_rows(self):
        self.forced(("INSERT INTO gwr_epoch(ledger_epoch, generation) VALUES(1,?)", (GEN,)), ("INSERT INTO gwr_seq VALUES(?,0,0)", (GEN,)))
        self.assertEqual(self.run_start().halt, gwstate.STATE_UNAVAILABLE)
        gwstate.reset(self.state, NOW, "e" * 32)
        self.assertEqual(self.state.all("SELECT reader_incarnation, halt, pending_seq FROM gwr_archive"), [(None, None, None)])
        got = self.run_start("e" * 32)
        self.assertEqual((got.halt, got.incarnation), (None, "e" * 32))

    def test_a_fault_after_each_statement_of_a_reset_leaves_the_old_state_whole(self):
        def prepare():
            self.setUp()
            self.started()
            self.mint(tot=totals(through=3, months=(("2026-10", 3, 9),)))
        prepare()
        seen = self.record_steps()
        self.assertGreaterEqual(len(seen), 8)
        for number in range(1, len(seen) + 1):
            prepare()
            before = self.dump()
            with self.subTest(after=seen[number - 1]):
                with self.fault_at(number):
                    with self.assertRaises(RuntimeError):
                        gwstate.reset(self.state, NOW, "e" * 32)
                self.assertEqual(self.dump(), before)                                              # the old state, not a mix

    def record_steps(self):
        """The steps of one successful reset, on a copy of the state."""
        copy_path = os.path.join(self._tmp.name, "copy.db")
        self.state.conn.execute("VACUUM INTO ?", (copy_path,))
        other = State(copy_path)
        self.addCleanup(other.close)
        seen = []
        with mock.patch.object(gwstate, "_fault", seen.append):
            gwstate.reset(other, NOW, "e" * 32)
        return seen


if __name__ == "__main__":
    unittest.main()
