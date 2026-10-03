"""privacy_l6a1: nothing private from a receipt, a source, a fault or an answer leaves the machine or lands
anywhere it should not.

Receipts and sources carry loud markers in EVERY string field the contract has (the outcome, the
reference, the generation, an extra key and its value) and in a loud folder name; a command source
prints loud text on its error output; the fake server answers with a loud error word; a fault carries
loud text. The whole path runs (adapter, queue, client, status, the command) and no loud string may
appear in: the wire body, the stored rows, the status JSON and text, the result line, the errors, the
notes, the counters, the logs, or anything the command prints. The wire body has exactly the allow-listed
keys, and the only identifiers on the wire and in the rows are the allow-listed ones (the lease id in the
path, the event id, the gateway id). Synthetic data only.
"""

import json
import logging
import os
import sys
import unittest
from unittest import mock

from quota_balancer.reporter import mapper, receipts, status

from tests.l6a1_env import GATEWAY_ID, add_binding, command_source, configure, ledger, lease, receipt, write_json
from tests.reporter_env import Rig
from tests.test_card7a3 import cli
from tests.test_card7l6a1_delivery import Net, error

LOUD = {
    "outcome": "loud-outcome-l6a1", "reference": "loud-reference-l6a1", "generation": "loud-generation-l6a1",
    "key": "loud-extra-key-l6a1", "value": "loud-extra-value-l6a1", "folder": "loud-folder-name-l6a1",
    "fault": "loud-fault-text-l6a1", "server": "loud-server-word-l6a1", "stderr": "loud-stderr-text-l6a1",
    "env": "loud-env-secret-l6a1",
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
        self.folder = os.path.join(self.rig.ws.dir, LOUD["folder"])
        os.makedirs(self.folder)
        self.fixture = os.path.join(self.folder, "receipts.json")
        self.logs = Collector()
        logging.getLogger().addHandler(self.logs)
        logging.getLogger().setLevel(logging.DEBUG)
        self.addCleanup(logging.getLogger().removeHandler, self.logs)

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
            with open(snapshot, "r", encoding="ascii") as handle:
                parts.append(handle.read())
        return "\n".join(parts)

    def no_loud(self, text, skip=()):
        for name, loud in LOUD.items():
            if name not in skip:
                self.assertNotIn(loud, text, name)

    def start(self, rows, net):
        self.store = os.path.join(self.rig.ws.dir, "admission.db")
        configure(self.rig, rows, fixture=self.fixture, store=self.store)
        reporter = self.rig.open(transport=net)
        return reporter

    def test_no_loud_string_appears_anywhere_and_the_wire_body_has_only_the_allowed_keys(self):
        good = lease()
        # one good receipt; the loud ones are refused as a whole list, one field at a time, first
        loud_rows = [
            receipt(lease(), 1, outcome=LOUD["outcome"]),
            receipt(lease(), 1, quota_lease_ref_sha256=LOUD["reference"]),
            receipt(lease(), 1, generation=LOUD["generation"]),
            dict(receipt(lease(), 1), **{LOUD["key"]: LOUD["value"]}),
            dict(receipt(lease(), 1), agent=LOUD["value"], message_id=LOUD["value"]),
        ]
        net = Net()
        reporter = self.start([loud_rows[0]], net)
        wires = []
        results = []
        for row in loud_rows:
            write_json(self.fixture, ledger([row]))
            results.append(reporter.cycle())
            self.assertEqual(reporter.report()["gateway_receipts"]["last_read_result"], "refused")
        self.assertEqual(net.calls, [])                                     # nothing refused was ever sent
        self.assertEqual(self.rig.state.all("SELECT * FROM receipt_capture"), [])
        self.assertEqual(reporter.report()["gateway_receipts"]["refused"]["bad_row"], len(loud_rows))
        # now the one good receipt goes through the same path
        add_binding(self.store, good)
        write_json(self.fixture, ledger([receipt(good, 1)]))
        result = reporter.cycle()
        self.assertEqual(len(net.settles()), 1)
        wire = net.settles()[0][2].decode("utf-8")
        wires.append(wire)
        self.assertEqual(set(json.loads(wire)), set(mapper.RECEIPT_BODY_KEYS))
        text = self.everything(reporter, result, extra=wires)
        self.no_loud(text)
        self.assertNotIn(LOUD["folder"], text)
        # the wire holds the allow-listed identifiers only: the event id and the gateway id (the lease id is
        # in the PATH); no reference hash, no generation, no closed_at
        body = json.loads(wire)
        self.assertEqual(body["gateway_id"], GATEWAY_ID)
        self.assertNotIn(mapper.lease_ref_hash(good), wire)
        # the status carries none of the identifiers either
        shown = json.dumps(reporter.report(), sort_keys=True) + status.render_text(reporter.report())
        for identifier in (good, mapper.lease_ref_hash(good), GATEWAY_ID, body["event_id"]):
            self.assertNotIn(identifier, shown)

    def test_a_loud_server_answer_and_a_loud_fault_leak_nothing(self):
        good = lease()
        net = Net(receipt=[error(LOUD["server"], 400)])
        reporter = self.start([receipt(good, 1)], net)
        add_binding(self.store, good)
        result = reporter.cycle()
        self.assertEqual(self.rig.state.all("SELECT state, last_error_code FROM receipt_state"),
                         [("failed_permanent", "UNKNOWN_CODE")])
        with mock.patch.object(receipts, "read_page", side_effect=RuntimeError(LOUD["fault"])):
            result = reporter.cycle()
        with mock.patch.object(self.rig.state, "capture_page", side_effect=RuntimeError(LOUD["fault"])):
            reporter.cycle()
        self.no_loud(self.everything(reporter, result))

    def test_a_command_with_loud_error_output_and_loud_environment_leaks_nothing(self):
        script = os.path.join(self.folder, "fake.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write("import json, os, sys\nsys.stderr.write(%r)\n"
                         "print(json.dumps({'envelope_version': 1, 'generation': '0' * 32, 'after_seq': int(sys.argv[2]),"
                         " 'next_seq': int(sys.argv[2]), 'has_more': False, 'receipts': []}))\n"
                         "sys.exit(7 if any(k.startswith('LOUD_ENV') for k in os.environ) else 0)\n"
                         % LOUD["stderr"])
        os.environ["LOUD_ENV_L6A1"] = LOUD["env"]
        self.addCleanup(os.environ.pop, "LOUD_ENV_L6A1", None)
        reporter = self.start([], Net())
        reporter.gateway_receipts = command_source([sys.executable, script], gateway=reporter.gateway_receipts.gateway,
                                                   gateway_id=GATEWAY_ID)
        result = reporter.cycle()
        self.assertEqual(reporter.report()["gateway_receipts"]["last_read_result"], "ok")
        self.no_loud(self.everything(reporter, result), skip=("folder",))
        # the failing run: the same, with a loud non-zero exit
        reporter.gateway_receipts = command_source([sys.executable, "-c", "import sys; sys.stderr.write(%r); sys.exit(2)"
                                                    % LOUD["stderr"]], gateway=reporter.gateway_receipts.gateway,
                                                   gateway_id=GATEWAY_ID)
        result = reporter.cycle()
        self.assertEqual(reporter.report()["gateway_receipts"]["read_later"]["exit_code"], 1)
        self.no_loud(self.everything(reporter, result), skip=("folder",))

    def test_the_command_prints_no_loud_string(self):
        good = lease()
        configure(self.rig, [receipt(good, 1, outcome=LOUD["outcome"])], fixture=self.fixture)

        net = Net()
        code, out, err = cli(["drain", "--config", self.rig.ws.config], permissions=lambda p: None, transport=net)
        code2, out2, err2 = cli(["status", "--config", self.rig.ws.config, "--json"], permissions=lambda p: None)
        code3, out3, err3 = cli(["status", "--config", self.rig.ws.config], permissions=lambda p: None)
        self.assertEqual((code, code2, code3), (0, 0, 0))
        printed = "\n".join((out, err, out2, err2, out3, err3, "\n".join(self.logs.lines)))
        self.no_loud(printed)
        self.assertNotIn(LOUD["folder"], printed)
        self.assertEqual(net.calls, [])
        self.assertEqual(json.loads(out2)["gateway_receipts"]["refused"]["bad_row"], 1)

    def test_the_search_itself_would_notice_a_leak(self):
        reporter = self.start([], Net())
        result = reporter.cycle()
        self.assertNotIn(LOUD["outcome"], self.everything(reporter, result))
        with self.rig.state.txn():
            self.rig.state.set_note(LOUD["outcome"], LOUD["value"], 1)                        # a deliberate leak
            self.rig.state.bump(LOUD["generation"], LOUD["key"])
        text = self.everything(reporter, result, extra=[LOUD["folder"]])
        for key in ("outcome", "value", "generation", "key", "folder"):
            self.assertIn(LOUD[key], text, key)                                               # all are found

    def test_the_stored_rows_hold_only_the_allow_listed_fields(self):
        good = lease()
        reporter = self.start([receipt(good, 1)], Net())
        add_binding(self.store, good)
        reporter.cycle()
        for table, expected in (
                ("receipt_capture", ["id", "source_id", "gateway_id", "generation", "seq", "ref_sha256", "outcome",
                                     "calls", "input_tokens", "output_tokens", "actual_micro_eur", "closed_at",
                                     "captured_ms"]),
                ("receipt_delivery", ["id", "capture_id", "event_id", "lease_id", "gateway_id", "body", "created_ms"]),
                ("receipt_state", ["capture_id", "state", "reason", "tries", "not_before_ms", "last_error_code"])):
            columns = [r[1] for r in self.rig.state.all("PRAGMA table_info(%s)" % table)]
            self.assertEqual(columns, expected, table)
        stored = json.loads(self.rig.state.one("SELECT body FROM receipt_delivery")[0])
        self.assertEqual(set(stored), set(mapper.RECEIPT_BODY_KEYS))
        counters = self.rig.state.all("SELECT agent, name FROM counter WHERE agent='_gateway_receipts'")
        for _agent, name in counters:
            self.assertRegex(name, r"^(stored|read_fault|generation_change|binding_changed|store:[a-z_]+|read_later:[a-z_]+|refused:[a-z_]+)$")    # closed words only
        for (code,) in self.rig.state.all("SELECT last_error_code FROM receipt_state"):
            self.assertIsNone(code)


if __name__ == "__main__":
    unittest.main()
