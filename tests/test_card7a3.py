"""Card 7a-3: the reporter program (docs/STEP-CARD-7A-3.md;
DESIGN-7A.md 4.1, 4.5, 4.9 and Part 12).

Rows: config_file, config_permissions, config_resolution, client_table,
client_real_server, commands, status_fields, errors_and_stops, privacy_7a3.
The end-to-end runs are in test_card7a3_e2e.py; the optional cross-boundary
run is in test_card7a3_cross.py. Everything is synthetic: a fake identity
value, loopback only, hand-made journals.
"""

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from unittest import mock

from quota_balancer.reporter import client as client_mod
from quota_balancer.reporter import config as cfg
from quota_balancer.reporter import decisions, events, rounds
from quota_balancer.reporter import state as state_mod
from quota_balancer.reporter import status as status_mod
from quota_balancer.reporter.cli import main as reporter_main
from quota_balancer.reporter.state import State, StateOwned

from tests.journal_fixture import MODEL, SEAT, USAGE, iso
from tests.reporter_env import (FAKE_IDENTITY, FAKE_URL_MARK, Net, Rig,
                                Workspace, agent_table, config_text, restrict)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NO_CHECK = lambda path: None            # noqa: E731  (tests of the parser only)
WINDOWS = sys.platform == "win32"


def cli(argv, **kw):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = reporter_main(argv, **kw)
    return code, out.getvalue(), err.getvalue()


def one_message(journal, now, mid="m1", disposition="completed",
                stream="g1", usage=USAGE):
    s = journal.add_stream(stream, now - 100_000)
    s.dispatch(mid, "t-" + mid, now - 50_000, message_at=now - 60_000,
               usage=usage)
    s.disposed(mid, disposition, now - 40_000, message_at=now - 60_000)
    return s


class Script:
    """A scripted fake transport. Each answer is (status, body) or an
    exception to raise; the last one repeats."""

    def __init__(self, *answers, on_call=None):
        self.answers = list(answers)
        self.sent = []
        self.on_call = on_call

    def __call__(self, method, path, headers, body, timeout):
        self.sent.append({"path": path, "headers": dict(headers),
                          "body": json.loads(body), "timeout": timeout})
        if self.on_call:
            self.on_call(len(self.sent))
        answer = self.answers.pop(0) if len(self.answers) > 1 \
            else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        status, doc = answer
        raw = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
        return status, raw


def granted(lease_id=None):
    return 201, {"lease_id": lease_id or str(uuid.uuid4()),
                 "state": "active"}


def error(status, code):
    return status, {"error": {"code": code, "message": "x"}}


def sample_text(tmp, **over):
    values = dict(url="http://127.0.0.1:8443", journal_root=tmp,
                  state_path=os.path.join(tmp, "s", "r.db"),
                  snapshot_path=os.path.join(tmp, "p.json"),
                  agents={"alpha": agent_table()})
    values.update(over)
    return config_text(values.pop("url"), values.pop("journal_root"),
                       values.pop("state_path"), values.pop("snapshot_path"),
                       values.pop("agents"), **values)


class _Tmp(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-7a3-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.abspath(self._tmp.name)


# --- 4.1 the configuration file -------------------------------------------------

class ConfigFile(_Tmp):
    def test_a_complete_file_loads_with_its_defaults(self):
        config = cfg.parse_config(sample_text(self.tmp))
        self.assertEqual((config.scheme, config.host, config.port),
                         ("http", "127.0.0.1", 8443))
        self.assertEqual(config.machine, "desktop")
        self.assertEqual((config.poll_seconds, config.silent_after_seconds),
                         (5, 86400))
        self.assertEqual(config.agents["alpha"]["seat"], "s6")
        self.assertEqual(config.agents["alpha"]["ttl_ms"], 1_800_000)

    def test_a_missing_key_is_named_and_a_foreign_key_is_refused(self):
        text = sample_text(self.tmp).replace('machine = "desktop"\n', "")
        with self.assertRaises(cfg.ConfigError) as caught:
            cfg.parse_config(text)
        self.assertIn("machine", caught.exception.message)
        with self.assertRaises(cfg.ConfigError) as caught:
            cfg.parse_config(sample_text(self.tmp, extra='extra_key = "x"'))
        self.assertNotIn("extra_key", caught.exception.message)
        agents = {"alpha": agent_table(colour="blue")}
        with self.assertRaises(cfg.ConfigError):
            cfg.parse_config(sample_text(self.tmp, agents=agents))

    def test_an_agent_table_needs_every_ceiling(self):
        row = agent_table()
        del row["ttl_ms"]
        with self.assertRaises(cfg.ConfigError) as caught:
            cfg.parse_config(sample_text(self.tmp, agents={"alpha": row}))
        self.assertIn("ttl_ms", caught.exception.message)

    def test_ceilings_are_positive_whole_numbers(self):
        for bad in (0, -1, True, "5", 2 ** 63):
            with self.subTest(bad=bad), self.assertRaises(cfg.ConfigError):
                cfg.parse_config(sample_text(self.tmp, agents={
                    "alpha": agent_table(max_calls=bad)}))

    def test_plain_http_is_for_loopback_only(self):
        for good in ("http://127.0.0.1:1", "http://localhost:9",
                     "https://leasewise.example.net", "http://[::1]:5/"):
            with self.subTest(good=good):
                cfg.parse_config(sample_text(self.tmp, url=good))
        for bad in ("http://leasewise.example.net", "ftp://127.0.0.1",
                    "http://user:pw@127.0.0.1", "http://127.0.0.1/v1",
                    "http://127.0.0.1?x=1", "127.0.0.1:80", "http://"):
            with self.subTest(bad=bad), self.assertRaises(cfg.ConfigError):
                cfg.parse_config(sample_text(self.tmp, url=bad))

    def test_an_identity_a_header_cannot_carry_is_refused(self):
        for bad in ("two words", "line\nbreak", "tab\there", "café", ""):
            with self.subTest(bad=bad), self.assertRaises(cfg.ConfigError):
                cfg.parse_config(sample_text(self.tmp, identity=bad))

    def test_relative_paths_and_unknown_versions_are_refused(self):
        with self.assertRaises(cfg.ConfigError):
            cfg.parse_config(sample_text(self.tmp, journal_root="journal"))
        with self.assertRaises(cfg.ConfigError):
            cfg.parse_config(sample_text(self.tmp).replace("version = 1",
                                                           "version = 2"))

    def test_bad_toml_and_odd_agent_names_have_fixed_messages(self):
        for text in ("this is = = not toml", "[agents.\"bad name\"]\n"):
            with self.subTest(text=text), self.assertRaises(cfg.ConfigError):
                cfg.parse_config(text)
        with self.assertRaises(cfg.ConfigError) as caught:
            cfg.parse_config("secret = = SENTINEL-TOML-TEXT")
        self.assertNotIn("SENTINEL", caught.exception.message)

    def test_the_loaded_file_hides_its_secrets_when_printed(self):
        config = cfg.parse_config(sample_text(self.tmp))
        for text in (repr(config), str(config)):
            self.assertNotIn(FAKE_IDENTITY, text)
            self.assertNotIn("8443", text)
        client = client_mod.HttpClient("http", "127.0.0.1", 8443,
                                       FAKE_IDENTITY)
        self.assertNotIn(FAKE_IDENTITY, repr(client))

    def test_an_unreadable_or_oversized_file_is_refused(self):
        with self.assertRaises(cfg.ConfigError):
            cfg.load_config(os.path.join(self.tmp, "none.toml"),
                            permissions=NO_CHECK)
        big = os.path.join(self.tmp, "big.toml")
        with open(big, "w") as handle:
            handle.write("#" * (cfg.MAX_FILE_BYTES + 1))
        with self.assertRaises(cfg.ConfigError):
            cfg.load_config(big, permissions=NO_CHECK)


class ConfigPermissions(_Tmp):
    """The check runs when the file is loaded, before it is read."""

    def test_a_file_other_users_can_read_is_refused_on_posix(self):
        for mode, ok in ((0o100600, True), (0o100400, True),
                         (0o100640, False), (0o100604, False),
                         (0o100660, False), (0o100644, False)):
            with self.subTest(mode=oct(mode)):
                fake = types.SimpleNamespace(st_mode=mode)
                with mock.patch.object(cfg.os, "stat", return_value=fake):
                    if ok:
                        cfg._check_posix("x")
                    else:
                        with self.assertRaises(cfg.ConfigError) as caught:
                            cfg._check_posix("x")
                        self.assertEqual(caught.exception.code,
                                         cfg.CONFIG_PERMISSIONS)

    ME = "S-1-5-21-1111111111-2222222222-3333333333-1001"
    OTHER = "S-1-5-21-1111111111-2222222222-3333333333-1002"

    def acl(self, *entries, user=None):
        """A canned reading: [(sid, kind, inherited)] and the current user's SID."""
        return lambda path: (list(entries), user or self.ME)

    def test_windows_accepts_only_you_system_and_administrators(self):
        for entries in (
                [(self.ME, "allow", False)],
                [(self.ME, "allow", False), (cfg.SID_SYSTEM, "allow", False)],
                [(self.ME, "allow", False), (cfg.SID_SYSTEM, "allow", False),
                 (cfg.SID_ADMINISTRATORS, "allow", False)],
                [(cfg.SID_SYSTEM, "allow", False)],
                # a refusal grants nobody anything
                [(self.ME, "allow", False), ("S-1-5-32-545", "deny", False)]):
            with self.subTest(entries=entries):
                cfg._check_windows("x", self.acl(*entries))

    def test_windows_refuses_any_other_identity_whatever_it_is_called(self):
        for label, entries in (
                ("a localised broad group (Users)",
                 [(self.ME, "allow", False), ("S-1-5-32-545", "allow", False)]),
                ("Everyone", [(self.ME, "allow", False),
                              ("S-1-1-0", "allow", False)]),
                ("Authenticated Users", [(self.ME, "allow", False),
                                         ("S-1-5-11", "allow", False)]),
                ("an unrelated user", [(self.ME, "allow", False),
                                       (self.OTHER, "allow", False)]),
                ("only an unrelated user", [(self.OTHER, "allow", False)]),
                ("an inherited entry of an allowed identity",
                 [(self.ME, "allow", True)]),
                ("inherited Administrators",
                 [(self.ME, "allow", False),
                  (cfg.SID_ADMINISTRATORS, "allow", True)])):
            with self.subTest(label):
                with self.assertRaises(cfg.ConfigError) as caught:
                    cfg._check_windows("x", self.acl(*entries))
                self.assertEqual(caught.exception.code,
                                 cfg.CONFIG_PERMISSIONS)

    def test_windows_refuses_an_entry_it_cannot_classify(self):
        for entries in (
                [(self.ME, "allow", False), (None, "other", False)],
                [(self.ME, "allow", False), ("S-1-5-18", "other", False)],
                [(self.ME, "allow", False), ("not-a-sid", "allow", False)],
                [(self.ME, "allow", False), (123, "allow", False)],
                [(self.ME, "allow", False), (cfg.SID_SYSTEM, "audit", False)]):
            with self.subTest(entries=entries):
                with self.assertRaises(cfg.ConfigError):
                    cfg._check_windows("x", self.acl(*entries))

    def test_windows_refuses_a_file_whose_list_cannot_be_read(self):
        def broken(path):
            raise OSError("no")
        for reader in (lambda p: None, lambda p: ([], self.ME),
                       lambda p: ([(self.ME, "allow", False)], None),
                       lambda p: ([(self.ME, "allow", False)], "junk"),
                       broken):
            with self.subTest(reader=reader):
                with self.assertRaises(cfg.ConfigError):
                    cfg._check_windows("x", reader)

    @unittest.skipUnless(WINDOWS, "the Windows access-list check")
    def test_the_real_check_on_a_real_windows_file(self):
        """The runbook command makes the file pass; a grant to anyone else, by
        any name, does not (the identities are read from the system)."""
        path = os.path.join(self.tmp, "r.toml")
        with open(path, "w") as handle:
            handle.write(sample_text(self.tmp))
        with self.assertRaises(cfg.ConfigError):      # inherits from the folder
            cfg.load_config(path)
        restrict(path)
        cfg.load_config(path)
        icacls = os.path.join(os.environ["SystemRoot"], "System32",
                              "icacls.exe")
        for sid in ("S-1-1-0", "S-1-5-11", "S-1-5-32-545", "S-1-5-19"):
            with self.subTest(sid):
                subprocess.run([icacls, path, "/grant", "*%s:R" % sid],
                               capture_output=True, check=True)
                with self.assertRaises(cfg.ConfigError):
                    cfg.load_config(path)
                restrict(path)           # the runbook command removes it again
                cfg.load_config(path)

    @unittest.skipUnless(WINDOWS, "reads the real identities")
    def test_the_real_reader_gives_identities_not_names(self):
        path = os.path.join(self.tmp, "r.toml")
        with open(path, "w") as handle:
            handle.write("x")
        restrict(path)
        entries, me = cfg._read_windows_acl(path)
        self.assertTrue(entries and all(
            cfg._SID_TEXT.fullmatch(sid) for sid, _k, _i in entries))
        self.assertEqual({sid for sid, _k, _i in entries}, {me})

    @unittest.skipIf(WINDOWS, "POSIX mode bits")
    def test_the_real_check_on_a_real_posix_file(self):
        path = os.path.join(self.tmp, "r.toml")
        with open(path, "w") as handle:
            handle.write(sample_text(self.tmp))
        os.chmod(path, 0o644)
        with self.assertRaises(cfg.ConfigError):
            cfg.load_config(path)
        os.chmod(path, 0o600)
        cfg.load_config(path)

    def test_the_commands_load_the_file_with_the_real_check(self):
        """No command takes a way around the check."""
        path = os.path.join(self.tmp, "open.toml")
        with open(path, "w") as handle:
            handle.write(sample_text(self.tmp))
        if WINDOWS:
            icacls = os.path.join(os.environ["SystemRoot"], "System32",
                                  "icacls.exe")
            subprocess.run([icacls, path, "/grant", "Everyone:R"],
                           capture_output=True, check=True)
        else:
            os.chmod(path, 0o666)
        for verb in ("run", "drain", "status"):
            with self.subTest(verb=verb):
                code, out, err = cli([verb, "--config", path])
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertEqual(json.loads(err)["error"]["code"],
                                 cfg.CONFIG_PERMISSIONS)


# --- 4.1 compound keys resolved from a policy-ids snapshot -------------------------

def snapshot(seats=None, models=None, classes=("bounded_read",)):
    return {"seats": seats if seats is not None else [
        {"machine": "desktop", "team": "dev", "project": "org",
         "seat": "s6", "id": SEAT}],
        "models": models if models is not None else [
            {"account": "meter", "model": "opus", "id": MODEL}],
        "task_classes": list(classes)}


class ConfigResolution(_Tmp):
    def resolve(self, snap, **row):
        config = cfg.parse_config(sample_text(
            self.tmp, agents={"alpha": agent_table(**row)}))
        path = os.path.join(self.tmp, "snap.json")
        with open(path, "w") as handle:
            json.dump(snap, handle)
        return cfg.resolve_agents(config, cfg.load_snapshot(path))

    def test_a_seat_and_a_model_resolve_to_their_ids(self):
        got = self.resolve(snapshot())["alpha"]
        self.assertEqual((got.seat_id, got.model_id), (SEAT, MODEL))
        self.assertEqual((got.task_class, got.effort, got.ttl_ms),
                         ("bounded_read", "opus", 1_800_000))

    def test_the_seat_is_looked_up_on_this_machines_name(self):
        other = snapshot(seats=[{"machine": "worker", "team": "dev",
                                 "project": "org", "seat": "s6", "id": SEAT}])
        with self.assertRaises(cfg.ConfigError) as caught:
            self.resolve(other)
        self.assertEqual(caught.exception.code, cfg.CONFIG_UNRESOLVED)

    def test_a_bare_seat_alias_that_is_ambiguous_is_refused(self):
        twin = "11111111-1111-4111-8111-1111111111aa"
        seats = [{"machine": "desktop", "team": "dev", "project": "org",
                  "seat": "s6", "id": SEAT},
                 {"machine": "desktop", "team": "ops", "project": "org",
                  "seat": "s6", "id": twin}]
        with self.assertRaises(cfg.ConfigError) as caught:
            self.resolve(snapshot(seats=seats))
        self.assertIn("ambiguous", caught.exception.message)
        # naming the team is the way out
        self.assertEqual(self.resolve(snapshot(seats=seats),
                                      team="ops")["alpha"].seat_id, twin)

    def test_a_bare_model_alias_that_is_ambiguous_is_refused(self):
        twin = "22222222-2222-4222-8222-2222222222aa"
        models = [{"account": "meter", "model": "opus", "id": MODEL},
                  {"account": "other", "model": "opus", "id": twin}]
        with self.assertRaises(cfg.ConfigError):
            self.resolve(snapshot(models=models), account=None)
        # the account in the table makes the key compound
        self.assertEqual(self.resolve(snapshot(models=models),
                                      account="other")["alpha"].model_id,
                         twin)
        self.assertEqual(self.resolve(snapshot(models=models),
                                      account="meter")["alpha"].model_id,
                         MODEL)

    def test_a_unique_bare_model_alias_needs_no_account(self):
        got = self.resolve(snapshot(), account=None)["alpha"]
        self.assertEqual(got.model_id, MODEL)

    def test_unknown_seats_models_and_task_classes_are_refused(self):
        for label, snap, row in (
                ("seat", snapshot(seats=[]), {}),
                ("model", snapshot(models=[]), {}),
                ("account", snapshot(), {"account": "nobody"}),
                ("class", snapshot(classes=("other",)), {})):
            with self.subTest(label), self.assertRaises(cfg.ConfigError):
                self.resolve(snap, **row)

    def test_a_snapshot_of_the_wrong_shape_is_refused(self):
        for text in ("not json", "[]", '{"seats": 1}',
                     '{"seats": [{"machine": 1}], "models": [], '
                     '"task_classes": []}'):
            path = os.path.join(self.tmp, "snap.json")
            with open(path, "w") as handle:
                handle.write(text)
            with self.subTest(text=text), self.assertRaises(cfg.ConfigError):
                cfg.load_snapshot(path)

    def test_the_real_policy_ids_output_resolves(self):
        rig = Rig()
        self.addCleanup(rig.cleanup)
        reporter = rig.open()
        self.assertEqual(set(reporter.agents), {"alpha"})
        self.assertEqual(reporter.agents["alpha"].task_class, "bounded_read")

    def test_an_agent_without_a_table_is_unconfigured_counted_and_never_sent(
            self):
        rig = Rig(agents={"alpha": agent_table()}, journal_agent="beta")
        self.addCleanup(rig.cleanup)
        one_message(rig.journal, rig.clock.ms)
        reporter = rig.open()
        result = reporter.drain()
        self.assertEqual((result.new_events, result.sent), (3, 0))
        self.assertEqual(rig.net.calls, [])
        report = reporter.report()
        beta = report["agents"]["beta"]
        self.assertFalse(beta["configured"])
        self.assertEqual(beta["events_read"], 3)
        self.assertEqual(beta["acquisitions"]["pending"], 0)
        self.assertEqual(rig.lw.rows("SELECT COUNT(*) FROM leases"), [(0,)])
        self.assertNotIn("beta", report["silent_configured_seats"])


# --- 4.5 the real client: every row of the table ------------------------------------

class ClientTable(unittest.TestCase):
    """Against a fake transport: what the real server cannot be made to do."""

    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        self.stream = one_message(self.rig.journal, self.rig.clock.ms)

    def go(self, script, cycles=1):
        self.script = script
        self.rig.open(transport=script)
        result = None
        for _ in range(cycles):
            result = self.rig.reporter.cycle()
        return result

    def acq(self):
        return self.rig.state.one(
            "SELECT state, tries, last_error_code, rebuilds, not_before_ms,"
            " lease_id FROM acquisition")

    def test_a_good_answer_records_the_lease_and_goes_on(self):
        lease = str(uuid.uuid4())
        result = self.go(Script(granted(lease), (200, {}), (200, {})))
        self.assertEqual((result.sent, result.blocked), (3, None))
        self.assertEqual(self.acq()[0], "acquired")
        self.assertEqual(self.acq()[5], lease)
        paths = [s["path"] for s in self.script.sent]
        self.assertEqual(paths, ["/v1/leases"] + ["/v1/leases/%s/settle"
                                                  % lease] * 2)

    def test_the_headers_carry_the_identity_and_the_idempotency_keys(self):
        self.go(Script(granted(), (200, {}), (200, {})))
        acquire, report, final = self.script.sent
        body = acquire["body"]
        self.assertEqual(acquire["headers"]["X-Machine-Identity"],
                         FAKE_IDENTITY)
        self.assertEqual(acquire["headers"]["Idempotency-Key"],
                         body["request_id"])
        self.assertEqual(report["headers"]["Idempotency-Key"],
                         report["body"]["event_id"])
        self.assertFalse(report["body"]["final"])
        self.assertEqual(final["headers"]["Idempotency-Key"],
                         final["body"]["event_id"])
        self.assertTrue(final["body"]["final"])
        for sent in self.script.sent:
            self.assertEqual(sent["timeout"], 5.0)

    def test_the_bodies_are_observe_only(self):
        self.go(Script(granted(), (200, {}), (200, {})))
        acquire, report, final = self.script.sent
        self.assertEqual(acquire["body"]["context"]["mode"], "UNKNOWN")
        self.assertIsNone(acquire["body"]["context"]["initial_tokens"])
        self.assertIsNone(acquire["body"]["gateway_id"])
        for sent in (report, final):
            self.assertIsNone(sent["body"]["actual_micro_eur"])
            self.assertIsNone(sent["body"]["seq"])
            self.assertIsNone(sent["body"]["gateway_id"])

    def test_no_answer_5xx_429_and_a_bad_success_are_retried_later(self):
        rows = (("refused", ConnectionRefusedError("x")),
                ("timeout", socket.timeout("x")),
                ("reset", ConnectionResetError("x")),
                ("os error", OSError("x")),
                ("500", error(500, "STORE_ERROR")),
                ("502 html", (502, b"<html>bad gateway</html>")),
                ("503", error(503, "STORE_ERROR")),
                ("429", error(429, "CONCURRENCY_LIMIT")),
                ("success without a lease id", (201, {})),
                ("success that is not json", (200, b"ok")),
                ("success with a foreign lease id",
                 (201, {"lease_id": "../../etc/passwd"})))
        for label, answer in rows:
            with self.subTest(label):
                self.rig.restart(transport=Script(answer))
                result = self.rig.reporter.cycle()
                state, tries, code, rebuilds, not_before, lease = self.acq()
                self.assertEqual(result.blocked, "unreachable")
                self.assertEqual((state, lease), ("pending", None))
                self.assertEqual(rebuilds, 0)
                self.assertGreater(not_before, self.rig.clock.ms)
                self.assertLessEqual(not_before - self.rig.clock.ms, 900_000)
                self.assertEqual(self.rig.state.one(
                    "SELECT COUNT(*) FROM final WHERE state='sent'")[0], 0)
                # nothing is lost: the stored body is the original bytes
                self.assertEqual(self.rig.state.one(
                    "SELECT body_original=body_current FROM acquisition")[0],
                    1)
                self.rig.state.conn.execute(
                    "UPDATE acquisition SET tries=0, not_before_ms=0")

    def test_one_failed_request_ends_the_cycle_instead_of_trying_the_rest(
            self):
        self.stream.dispatch("m2", "t-m2", self.rig.clock.ms - 30_000,
                             message_at=self.rig.clock.ms - 31_000)
        self.go(Script(ConnectionRefusedError("x")))
        self.assertEqual(len(self.script.sent), 1)

    def test_a_401_is_retried_with_its_code_kept(self):
        result = self.go(Script(error(401, "UNAUTHENTICATED")))
        self.assertEqual(result.blocked, "credential")
        state, tries, code, _r, not_before, _l = self.acq()
        self.assertEqual((state, tries, code), ("pending", 1,
                                                "UNAUTHENTICATED"))
        self.assertGreater(not_before, self.rig.clock.ms)
        note = self.rig.state.note(status_mod.last_401_note())
        self.assertEqual(note, ("UNAUTHENTICATED", self.rig.clock.ms))
        # a 401 from something that is not Leasewise (no error body) too
        self.rig.restart(transport=Script((401, b"")))
        self.rig.state.conn.execute("UPDATE acquisition SET not_before_ms=0")
        self.assertEqual(self.rig.reporter.cycle().blocked, "credential")
        # nobody is stopped and nothing failed for good
        self.assertEqual(self.rig.state.one(
            "SELECT COUNT(*) FROM agent_state")[0], 0)
        self.assertEqual(self.acq()[0], "pending")

    def test_after_the_identity_is_fixed_the_same_bytes_go_through(self):
        script = Script(error(401, "UNAUTHENTICATED"))
        self.go(script)
        first = script.sent[0]
        self.rig.clock.advance(3600)
        good = Script(granted(), (200, {}), (200, {}))
        self.rig.restart(transport=good)
        result = self.rig.reporter.cycle()
        self.assertEqual(result.sent, 3)
        again = good.sent[0]
        self.assertEqual(again["body"], first["body"])      # the stored bytes
        self.assertEqual(again["headers"]["Idempotency-Key"],
                         first["headers"]["Idempotency-Key"])

    def test_estimate_stale_rebuilds_up_to_three_times_then_fails(self):
        def tick(_n):
            self.rig.clock.advance(1)
        script = Script(error(400, "ESTIMATE_STALE"), on_call=tick)
        result = self.go(script)
        self.assertEqual(len(script.sent), 4)             # 1 + three rebuilds
        self.assertEqual(result.blocked, None)
        state, tries, code, rebuilds, _nb, _l = self.acq()
        self.assertEqual((state, code, rebuilds),
                         ("failed_permanent", "ESTIMATE_STALE", 3))
        seen = [s["body"]["context"]["observed_at"] for s in script.sent]
        self.assertEqual(len(set(seen)), 4)               # fresh time each try
        original, current = self.rig.state.one(
            "SELECT body_original, body_current FROM acquisition")
        self.assertEqual(json.loads(original)["context"]["observed_at"],
                         seen[0])
        self.assertEqual(json.loads(current)["context"]["observed_at"],
                         seen[3])
        self.assertNotEqual(original, current)

    def test_the_rebuild_budget_survives_a_restart(self):
        script = Script(error(400, "ESTIMATE_STALE"), on_call=lambda n: None)
        answered = []

        def crash(stage):                   # the reporter dies on answer 3
            if stage == "answered":
                answered.append(stage)
                if len(answered) == 3:
                    raise RuntimeError("crash")
        self.rig.open(transport=script, fault=crash)
        with self.assertRaises(RuntimeError):
            self.rig.reporter.cycle()
        self.assertEqual(self.acq()[3], 2)                # two rebuilds kept
        script2 = Script(error(400, "ESTIMATE_STALE"))
        self.rig.restart(transport=script2)
        self.rig.reporter.cycle()
        self.assertEqual(len(script2.sent), 2)           # 3rd rebuild + fail
        self.assertEqual(self.acq()[:1], ("failed_permanent",))

    def test_any_other_invalid_request_fails_for_good_and_never_rebuilds(self):
        for code, stored in (("INVALID_REQUEST", "INVALID_REQUEST"),
                             ("SCHEMA_UNSUPPORTED", "SCHEMA_UNSUPPORTED"),
                             ("NO_SUCH_CODE", "UNKNOWN_CODE")):
            with self.subTest(code):
                self.rig.restart(transport=Script(error(400, code)))
                result = self.rig.reporter.cycle()
                state, _t, last, rebuilds, _nb, _l = self.acq()
                self.assertEqual((state, last, rebuilds),
                                 ("failed_permanent", stored, 0))
                self.assertEqual(self.rig.state.one(
                    "SELECT body_original=body_current FROM acquisition")[0],
                    1)
                self.assertEqual(result.blocked, None)
                self.rig.state.conn.execute(
                    "UPDATE acquisition SET state='pending', last_error_code"
                    "=NULL")

    def test_a_configuration_fault_stops_that_agent_and_says_which(self):
        for status, code in ((403, "POLICY_DENIED"), (404, "NOT_FOUND"),
                             (403, "FORBIDDEN"), (400, "CONTEXT_UNKNOWN")):
            with self.subTest(code):
                self.rig.restart(transport=Script(error(status, code)))
                self.rig.reporter.cycle()
                self.assertEqual(self.acq()[0], "failed_permanent")
                self.assertEqual(self.rig.state.one(
                    "SELECT failed_code FROM agent_state")[0], code)
                report = self.rig.reporter.report()
                self.assertEqual(
                    report["agents"]["alpha"]
                    ["stopped_by_configuration_fault"], {"code": code})
                self.rig.state.conn.execute("DELETE FROM agent_state")
                self.rig.state.conn.execute(
                    "UPDATE acquisition SET state='pending', last_error_code"
                    "=NULL")

    def test_an_idempotency_conflict_is_a_bug_not_a_stop(self):
        self.go(Script(error(409, "IDEMPOTENCY_CONFLICT")))
        self.assertEqual(self.acq()[0], "failed_permanent")
        self.assertEqual(self.acq()[2], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.rig.state.one(
            "SELECT COUNT(*) FROM agent_state")[0], 0)

    def test_report_rows_lease_expired_and_conflict_drop_the_report_only(self):
        for status, code in ((410, "LEASE_EXPIRED"), (409, "RECEIPT_CONFLICT")):
            with self.subTest(code):
                rig = Rig(server=False)
                self.addCleanup(rig.cleanup)
                one_message(rig.journal, rig.clock.ms)
                rig.open(transport=Script(granted(), error(status, code),
                                          (200, {})))
                result = rig.reporter.cycle()
                self.assertEqual(result.sent, 2)       # acquisition + final
                self.assertEqual(rig.state.one(
                    "SELECT state, last_error_code FROM report"),
                    ("dropped", code))
                self.assertEqual(rig.state.counter("alpha",
                                                   "reports_dropped"), 1)
                self.assertEqual(rig.state.one(
                    "SELECT state FROM final")[0], "sent")

    def test_a_report_or_final_that_gets_5xx_is_retried_and_loses_nothing(self):
        script = Script(granted(), error(503, "STORE_ERROR"))
        self.go(script)
        self.assertEqual(self.rig.state.one(
            "SELECT state, tries FROM report"), ("pending", 1))
        self.assertEqual(self.rig.state.one(
            "SELECT state FROM final")[0], "pending")

    def test_only_one_request_is_ever_in_flight(self):
        net = Net()
        net.real = Script(granted(), (200, {}), (200, {}))
        self.go(net)
        self.assertEqual((len(net.calls), net.max_in_flight), (3, 1))

    def test_an_error_code_that_is_not_a_leasewise_word_is_one_fixed_word(self):
        for odd in ("lowercase", "WITH SPACE", "X" * 90, "<script>"):
            with self.subTest(odd):
                self.rig.restart(transport=Script(
                    (400, {"error": {"code": odd}})))
                self.rig.reporter.cycle()
                code = self.rig.state.one(
                    "SELECT last_error_code FROM acquisition")[0]
                self.assertEqual(code, "UNKNOWN_CODE")
                self.rig.state.conn.execute(
                    "UPDATE acquisition SET state='pending', last_error_code"
                    "=NULL")

    def test_a_redirect_is_not_followed_and_fails_the_item(self):
        self.go(Script((302, b"")))
        self.assertEqual(self.acq()[0], "failed_permanent")
        self.assertEqual(len(self.script.sent), 1)


class ClientRealServer(unittest.TestCase):
    """The rows a real in-process Leasewise can produce."""

    def setUp(self):
        self.rig = Rig()
        self.addCleanup(self.rig.cleanup)
        self.stream = one_message(self.rig.journal, self.rig.clock.ms)

    def acq(self):
        return self.rig.state.one(
            "SELECT state, tries, last_error_code, rebuilds, lease_id "
            "FROM acquisition")

    def test_acquire_report_final_and_the_replays(self):
        self.rig.open()
        result = self.rig.reporter.cycle()
        self.assertEqual((result.sent, result.blocked), (3, None))
        self.assertEqual(self.rig.lw.rows(
            "SELECT enforcement, state FROM leases"), [("shadow", "settled")])
        # a lost answer is a replay of the same bytes: the same lease, a no-op
        item = self.acquisition_item()
        first = self.rig.reporter.client.send(item)
        again = self.rig.reporter.client.send(item)
        self.assertEqual((first.status, again.status), (200, 200))
        self.assertEqual(first.lease_id, self.acq()[4])
        self.assertEqual(self.rig.lw.rows("SELECT COUNT(*) FROM leases"),
                         [(1,)])

    def acquisition_item(self):
        row = self.rig.state.one(
            "SELECT id, body_current, request_id FROM acquisition")
        return state_mod.Item(decisions.ACQUIRE, "alpha", "m1", "/v1/leases",
                              json.loads(row[1]), row[2],
                              ("acquisition", row[0]), None)

    def test_a_wrong_identity_is_forbidden_and_stops_the_agent(self):
        self.rig.open()
        wrong = client_mod.HttpClient(
            self.rig.reporter.config.scheme, self.rig.reporter.config.host,
            self.rig.reporter.config.port, "FAKE-WRONG-IDENTITY")
        self.rig.reporter.client = wrong
        result = self.rig.reporter.cycle()
        self.assertEqual(result.blocked, None)
        self.assertEqual(self.rig.state.one(
            "SELECT failed_code FROM agent_state")[0], "FORBIDDEN")
        self.assertEqual(self.acq()[0], "failed_permanent")
        self.assertEqual(self.rig.lw.rows("SELECT COUNT(*) FROM leases"),
                         [(0,)])

    def test_an_empty_identity_is_a_401_and_is_retried(self):
        self.rig.open()
        cfg_ = self.rig.reporter.config
        self.rig.reporter.client = client_mod.HttpClient(
            cfg_.scheme, cfg_.host, cfg_.port, "")
        result = self.rig.reporter.cycle()
        self.assertEqual(result.blocked, "credential")
        self.assertEqual(self.acq()[:3], ("pending", 1, "UNAUTHENTICATED"))
        self.assertEqual(self.rig.state.note("last_401")[0],
                         "UNAUTHENTICATED")

    def test_a_stale_estimate_is_rebuilt_and_then_granted(self):
        # The body is built now; an hour passes before the first request
        # (an outage), so the server finds the estimate stale.
        self.rig.net.before = lambda i: self.rig.clock.advance(3600)             if i == 0 else None
        self.rig.open()
        result = self.rig.reporter.cycle()
        self.assertEqual(result.blocked, None)
        state, tries, code, rebuilds, lease = self.acq()
        self.assertEqual((state, rebuilds), ("acquired", 1))
        self.assertEqual(self.rig.net.calls[0][1], self.rig.net.calls[1][1])
        self.assertEqual(self.rig.lw.rows("SELECT COUNT(*) FROM leases"),
                         [(1,)])

    def test_an_expired_lease_drops_the_report_and_still_takes_the_final(self):
        rig = self.rig
        s = self.stream
        s.dispatch("m2", "t-m2", rig.clock.ms - 5_000,
                   message_at=rig.clock.ms - 6_000, ended=False)
        rig.open()
        rig.reporter.cycle()               # m2 acquired; m1 fully reported
        self.assertEqual(rig.lw.rows(
            "SELECT COUNT(*) FROM leases WHERE state='active'"), [(1,)])
        s.dispatch("m2", "t-m2", rig.clock.ms - 5_000, started=False,
                   message_at=rig.clock.ms - 6_000)
        s.disposed("m2", "completed", rig.clock.ms - 4_000,
                   message_at=rig.clock.ms - 6_000)
        rig.clock.advance(2 * 3600)        # beyond the 30 minute lease
        rig.reporter.cycle()
        self.assertEqual(rig.state.one(
            "SELECT state, last_error_code FROM report WHERE message_id="
            "'m2'"), ("dropped", "LEASE_EXPIRED"))
        self.assertEqual(rig.state.one(
            "SELECT state FROM final WHERE message_id='m2'")[0], "sent")
        self.assertEqual(rig.lw.rows(
            "SELECT COUNT(*) FROM leases WHERE state='settled'"), [(2,)])

    def test_a_non_final_after_the_final_is_a_receipt_conflict(self):
        self.rig.open()
        self.rig.reporter.cycle()
        row = self.rig.state.one("SELECT lease_id FROM acquisition")
        body = json.loads(self.rig.state.one(
            "SELECT body FROM report")[0])
        body["event_id"] = str(uuid.uuid4())
        item = state_mod.Item(decisions.REPORT, "alpha", "m1",
                              "/v1/leases/%s/settle" % row[0], body,
                              body["event_id"], ("report", 1), row[0])
        answer = self.rig.reporter.client.send(item)
        self.assertEqual((answer.status, answer.code),
                         (409, "RECEIPT_CONFLICT"))
        self.assertEqual(decisions.decide(decisions.REPORT, answer).action,
                         decisions.DROP)

    def test_changed_content_under_one_key_is_an_idempotency_conflict(self):
        self.rig.open()
        self.rig.reporter.cycle()
        item = self.acquisition_item()
        item.body["ttl_ms"] = 1_000_000
        answer = self.rig.reporter.client.send(item)
        self.assertEqual((answer.status, answer.code),
                         (409, "IDEMPOTENCY_CONFLICT"))
        self.assertEqual(decisions.decide(decisions.ACQUIRE, answer).action,
                         decisions.FAIL)

    def test_an_unknown_lease_is_not_found(self):
        self.rig.open()
        body = {"event_id": str(uuid.uuid4()), "outcome": "completed",
                "calls": 1, "input_tokens": None, "cached_input_tokens": None,
                "output_tokens": None, "actual_micro_eur": None,
                "gateway_id": None, "seq": None, "final": False}
        lease = str(uuid.uuid4())
        item = state_mod.Item(decisions.REPORT, "alpha", "m1",
                              "/v1/leases/%s/settle" % lease, body,
                              body["event_id"], ("report", 1), lease)
        answer = self.rig.reporter.client.send(item)
        self.assertEqual((answer.status, answer.code), (404, "NOT_FOUND"))

    def test_a_request_beyond_the_profile_is_a_policy_denial(self):
        row = agent_table(max_calls=3)       # the profile allows 2
        rig = Rig(agents={"alpha": row})
        self.addCleanup(rig.cleanup)
        one_message(rig.journal, rig.clock.ms)
        rig.open()
        rig.reporter.cycle()
        self.assertEqual(rig.state.one(
            "SELECT failed_code FROM agent_state")[0], "POLICY_DENIED")

    def test_an_unreachable_server_is_retried_not_failed(self):
        self.rig.net.down = True
        self.rig.open()
        result = self.rig.reporter.cycle()
        self.assertEqual(result.blocked, "unreachable")
        self.assertEqual(self.acq()[:3], ("pending", 1, None))

    def test_a_dead_port_gives_no_answer_within_the_timeout(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        client = client_mod.HttpClient("http", "127.0.0.1", port,
                                       FAKE_IDENTITY, timeout=1.0)
        item = self.acquisition_item_for(client)
        answer = client.send(item)
        self.assertIsNone(answer.status)

    def acquisition_item_for(self, client):
        return state_mod.Item(decisions.ACQUIRE, "alpha", "m1", "/v1/leases",
                              {"request_id": "x"}, "x", ("acquisition", 1),
                              None)

    def test_the_client_only_posts_to_lease_paths(self):
        client = client_mod.HttpClient("http", "127.0.0.1", 9, FAKE_IDENTITY,
                                       transport=Script((200, {})))
        for path in ("/v1/status", "/v1/leases/../x/settle",
                     "/v1/leases/not-a-uuid/settle", "//evil/v1/leases",
                     "/v1/leases/%s/check" % uuid.uuid4()):
            item = state_mod.Item(decisions.REPORT, "a", "m", path, {}, "k",
                                  ("report", 1), None)
            with self.subTest(path=path):
                answer = client.send(item)
                self.assertEqual(answer.code, client_mod.BAD_ANSWER)
        self.assertEqual(client._transport.sent, [])


if __name__ == "__main__":
    unittest.main()
