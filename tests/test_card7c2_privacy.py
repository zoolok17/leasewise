"""privacy_7c2: nothing private from a park marker, a folder, a fault or an answer leaves the machine
or lands anywhere it should not.

A synthetic marker carries loud markers in EVERY string field the contract has (the agent name, the
message id, the wrapper generation), and a loud folder name; other files are torn, refused or deny
access with loud exception text; the fake server answers with a loud error word. The whole path runs
(capture, the queue, the client, status, the command) and no loud string may appear in: the wire
body, the stored rows, the status JSON and text, the result line, the errors, the notes, the
counters, the logs, or anything the command prints. The wire body has exactly the allow-listed keys.
Synthetic data only.
"""

import json
import logging
import os
import time
import unittest
from unittest import mock

from quota_balancer.reporter import capture, mapper, status

from tests.reporter_env import Rig
from tests.test_card7a3 import cli
from tests.test_card7c2_delivery import LIMIT_PATH, Net
from tests.test_card7c2_reader import marker
from tests.test_card7c2_wiring import iso

LOUD = {
    "agent": "loud-agent-name-7c2", "message": "loud-message-id-7c2", "generation": "loud-generation-7c2",
    "folder": "loud-folder-name-7c2", "fault": "loud-fault-text-7c2", "server": "loud-server-word-7c2",
    "torn": "loud-torn-file-7c2", "refused": "loud-refused-file-7c2", "denied": "loud-denied-file-7c2",
}


class Collector(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


class Privacy(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        self.bus = os.path.join(self.rig.ws.dir, LOUD["folder"])
        self.folder = os.path.join(self.bus, "state", "usage-limit-park")
        os.makedirs(self.folder)
        self.logs = Collector()
        logging.getLogger().addHandler(self.logs)
        logging.getLogger().setLevel(logging.DEBUG)
        self.addCleanup(logging.getLogger().removeHandler, self.logs)

    def put(self, name, doc=None, raw=None):
        with open(os.path.join(self.folder, name + ".json"), "wb") as handle:
            handle.write(raw if raw is not None else json.dumps(doc).encode("utf-8"))

    def everything(self, reporter, result, extra=()):
        """Every place a private string could land, as text."""
        parts = [json.dumps(result.as_dict()), json.dumps(reporter.report(), sort_keys=True),
                 status.render_text(reporter.report()), "\n".join(self.logs.lines)]
        parts += [str(e) for e in extra]
        state = self.rig.state
        for (table,) in state.all("SELECT name FROM sqlite_master WHERE type='table'"):
            parts.append(json.dumps(state.all('SELECT * FROM "%s"' % table), default=str))
        snapshot = status.snapshot_path(state.path)
        if os.path.exists(snapshot):
            parts.append(open(snapshot, "r", encoding="ascii").read())
        return "\n".join(parts)

    def test_no_marker_string_appears_anywhere_and_the_wire_body_has_only_the_allowed_keys(self):
        now = self.rig.clock.ms
        reset = now // 1000 + 3600
        # the loud valid marker: every string field the contract has
        self.put(LOUD["agent"], marker(agent=LOUD["agent"], now_ms=now, message_id=LOUD["message"],
                                       wrapper_generation=LOUD["generation"], parked_at=iso(now - 60_000),
                                       reset_epoch=reset, wake_epoch=reset + 30))
        # a second seat on the same limit, a torn file, a refused one with a loud extra key, a file that denies access
        self.put("agent-two", marker(agent="agent-two", now_ms=now, message_id=LOUD["message"],
                                     parked_at=iso(now - 30_000), reset_epoch=reset, wake_epoch=reset + 30))
        self.put(LOUD["torn"], raw=('{"agent": "%s", ' % LOUD["torn"]).encode())
        self.put(LOUD["refused"], dict(marker(agent=LOUD["refused"], now_ms=now), **{LOUD["refused"]: LOUD["refused"]}))
        self.put(LOUD["denied"], marker(agent=LOUD["denied"], now_ms=now))
        net = Net(limit=[(202, {"block_id": "x", "retry_at": None, "state": "parked"})])
        reporter = self.rig.open(transport=net)
        from tests.test_card7c2_capture import source
        reporter.limit_sources = (source(self.bus),)
        real_open = open

        def deny(path, *args, **kwargs):
            if str(path).endswith(LOUD["denied"] + ".json"):
                raise PermissionError(LOUD["fault"])
            return real_open(path, *args, **kwargs)
        with mock.patch("builtins.open", deny):
            result = reporter.cycle()
        # one event, one request, to the limit path only; the body has exactly the allow-listed keys
        self.assertEqual(net.paths(), [LIMIT_PATH])
        body = json.loads(net.calls[0][2])
        self.assertEqual(set(body), set(mapper.LIMIT_BODY_KEYS))
        self.assertEqual(self.rig.state.all("SELECT state FROM limit_event"), [("sent",)])
        wire = net.calls[0][2].decode("utf-8")
        text = self.everything(reporter, result, extra=[wire])
        for name, loud in LOUD.items():
            self.assertNotIn(loud, text, name)
        # and the folder's loud name is not in the configuration's own error paths either
        self.assertNotIn(LOUD["folder"], text)

    def test_a_loud_server_answer_and_a_loud_fault_leak_nothing(self):
        now = self.rig.clock.ms
        net = Net(limit=[(400, {"error": {"code": LOUD["server"], "message": LOUD["server"]}})])
        reporter = self.rig.open(transport=net)
        from tests.test_card7c2_capture import source
        reporter.limit_sources = (source(self.bus),)
        self.put(LOUD["agent"], marker(agent=LOUD["agent"], now_ms=now, message_id=LOUD["message"],
                                       wrapper_generation=LOUD["generation"], parked_at=iso(now - 60_000),
                                       reset_epoch=now // 1000 + 3600))
        result = reporter.cycle()
        self.assertEqual(self.rig.state.all("SELECT state, last_error_code FROM limit_event"),
                         [("failed_permanent", "UNKNOWN_CODE")])
        with mock.patch.object(capture, "run", side_effect=RuntimeError(LOUD["fault"])):
            result = reporter.cycle()
        text = self.everything(reporter, result)
        for name, loud in LOUD.items():
            self.assertNotIn(loud, text, name)

    def test_the_command_prints_no_marker_string(self):
        now = int(time.time() * 1000)
        reset = now // 1000 + 3600
        self.put(LOUD["agent"], marker(agent=LOUD["agent"], now_ms=now, message_id=LOUD["message"],
                                       wrapper_generation=LOUD["generation"], parked_at=iso(now - 60_000),
                                       reset_epoch=reset))
        with open(self.rig.ws.snapshot, "r", encoding="utf-8") as handle:
            snap = json.load(handle)
        snap["accounts"] = [{"alias": "claude-main", "id": "11111111-1111-4111-8111-111111111111"}]
        with open(self.rig.ws.snapshot, "w", encoding="utf-8") as handle:
            json.dump(snap, handle)
        extra = ("[[limit_events.sources]]\nbus_root = %s\nos_user = \"operator-desktop\"\nprovider = \"claude\"\n"
                 "account = \"claude-main\"\n" % json.dumps(self.bus))
        path = self.rig.ws.write_config("http://127.0.0.1:9", self.rig.agents_cfg, extra=extra)
        net = Net()
        code, out, err = cli(["drain", "--config", path], permissions=lambda p: None, transport=net)
        code2, out2, err2 = cli(["status", "--config", path, "--json"], permissions=lambda p: None)
        code3, out3, err3 = cli(["status", "--config", path], permissions=lambda p: None)
        self.assertEqual((code, code2, code3), (0, 0, 0))
        printed = "\n".join((out, err, out2, err2, out3, err3, "\n".join(self.logs.lines)))
        for name, loud in LOUD.items():
            self.assertNotIn(loud, printed, name)
        self.assertEqual(net.paths(), [LIMIT_PATH])

    def test_the_search_itself_would_notice_a_leak(self):
        reporter = self.rig.open(transport=Net())
        result = reporter.cycle()
        self.assertNotIn(LOUD["agent"], self.everything(reporter, result))
        with self.rig.state.txn():
            self.rig.state.set_note(LOUD["agent"], LOUD["message"], 1)                     # a deliberate leak
            self.rig.state.bump(LOUD["generation"], "x")
        text = self.everything(reporter, result, extra=[LOUD["folder"]])
        for key in ("agent", "message", "generation", "folder"):
            self.assertIn(LOUD[key], text, key)                                             # all four are found

    def test_the_stored_rows_hold_only_the_allow_listed_fields(self):
        now = self.rig.clock.ms
        reporter = self.rig.open(transport=Net())
        from tests.test_card7c2_capture import source
        reporter.limit_sources = (source(self.bus),)
        self.put(LOUD["agent"], marker(agent=LOUD["agent"], now_ms=now, message_id=LOUD["message"],
                                       wrapper_generation=LOUD["generation"], parked_at=iso(now - 60_000),
                                       reset_epoch=now // 1000 + 3600))
        reporter.cycle()
        columns = [r[1] for r in self.rig.state.all("PRAGMA table_info(limit_event)")]
        self.assertEqual(columns, ["id", "event_id", "account_alias", "body", "state", "tries",
                                   "not_before_ms", "last_error_code", "captured_ms"])
        stored = json.loads(self.rig.state.one("SELECT body FROM limit_event")[0])
        self.assertEqual(set(stored), set(mapper.LIMIT_BODY_KEYS))
        counters = self.rig.state.all("SELECT agent, name FROM counter WHERE agent='_limit_events'")
        for _agent, name in counters:
            self.assertRegex(name, r"^(captured|capture_fault|scan:[a-z_]+)$")                # closed words only


if __name__ == "__main__":
    unittest.main()
