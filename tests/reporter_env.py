"""Shared test environment for card 7a-3: a real, in-process Leasewise server
over a store built through config-import v2, a saved `policy-ids --json`
snapshot, a restricted configuration file, and a controllable clock.

Everything is synthetic: the identity value is an obvious fake, the server is
bound to loopback, and nothing leaves this machine.
"""

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from unittest import mock

from quota_balancer import http_api, store
from tests.test_cardl1a import run_cli
from tests.test_cardl1b import doc as base_doc

FAKE_IDENTITY = "FAKE-IDENTITY-reporter-test-0001"
FAKE_URL_MARK = "reporter-sentinel-host.invalid"      # never resolved
MACHINE = "desktop"
PROFILE = {"expected_calls": 1, "max_calls": 2, "output_cap": 1000,
           "per_call_output_cap": 500, "ttl_ms": 1_800_000}


def leasewise_doc():
    """base doc plus a second shadow seat (`s7`) on the desktop, so two agents
    can report."""
    doc = base_doc()
    team = {"project": "org", "team": "dev"}
    doc["seats"].append({"team": team, "machine": MACHINE, "alias": "s7",
                         "role": "main", "enforcement": "shadow"})
    return doc


def build_store(db, tmp, doc=None):
    """A fresh store: init, config-import v2, the enrolled fake identity."""
    assert run_cli("--db", db, "init").returncode == 0
    path = os.path.join(tmp, "leasewise-config.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc or leasewise_doc(), handle)
    done = run_cli("--db", db, "config-import", "--file", path)
    assert done.returncode == 0, done.stderr
    store.set_machine_identity(db, MACHINE, FAKE_IDENTITY, True)


class Clock:
    """Milliseconds the test controls. `install` makes the in-process server
    read the same clock (the way the card-L1d tests move it)."""

    def __init__(self, start_ms=None):
        import time
        self.ms = int(time.time() * 1000) if start_ms is None else start_ms

    def __call__(self):
        return self.ms

    def advance(self, seconds):
        self.ms += int(seconds * 1000)

    def time(self):                   # the module surface http_api uses
        return self.ms / 1000.0


class Leasewise:
    """A store plus a server on 127.0.0.1 (ephemeral port)."""

    def __init__(self, tmp, clock=None, doc=None, template=None):
        self.tmp = tmp
        self.db = os.path.join(tmp, "inventory.db")
        self.clock = clock
        self._patch = None
        self.template = template
        if template:                      # a store made once, copied per test
            shutil.copyfile(template, self.db)
        else:
            build_store(self.db, tmp, doc)
        if clock is not None:
            self._patch = mock.patch.object(http_api, "time", clock)
            self._patch.start()
        self.server = http_api.make_server(self.db, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self._patch:
            self._patch.stop()

    def snapshot(self, path):
        """The saved `policy-ids --json` output (the command itself, unless
        the store is a copy: then the same function in this process)."""
        if self.template:
            text = json.dumps(store.policy_ids(self.db), sort_keys=True,
                              separators=(",", ":"))
        else:
            done = run_cli("--db", self.db, "policy-ids", "--json")
            assert done.returncode == 0, done.stderr
            text = done.stdout
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def rows(self, sql, params=()):
        import sqlite3
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


def restrict(path):
    """Make a file readable by its owner only, the way the runbook says."""
    if sys.platform == "win32":
        who = "%s\\%s" % (os.environ.get("USERDOMAIN", ""),
                          os.environ["USERNAME"])
        icacls = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                              "System32", "icacls.exe")
        # The runbook command: reset to the folder's list, drop everything
        # inherited, then give the file to its owner alone.
        for args in (["/reset"], ["/inheritance:r", "/grant:r", who + ":F"]):
            done = subprocess.run([icacls, path, *args], capture_output=True)
            assert done.returncode == 0, done.stdout
    else:
        os.chmod(path, 0o600)


def agent_table(seat="s6", **over):
    row = {"seat": seat, "account": "meter", "model": "opus",
           "task_class": "bounded_read", "effort": "opus"}
    row.update(PROFILE)
    row.update(over)
    return row


def config_text(url, journal_root, state_path, snapshot_path, agents,
                machine=MACHINE, identity=FAKE_IDENTITY, extra=""):
    def quote(value):
        return json.dumps(value)        # a JSON string is a valid TOML string

    lines = ["version = 1", "base_url = " + quote(url),
             "identity = " + quote(identity), "machine = " + quote(machine),
             "journal_root = " + quote(journal_root),
             "state_path = " + quote(state_path),
             "policy_ids = " + quote(snapshot_path), extra]
    for name, row in agents.items():
        lines.append("")
        lines.append("[agents.%s]" % name)
        for key, value in row.items():
            if value is not None:
                lines.append("%s = %s" % (key, quote(value)))
    return "\n".join(lines) + "\n"


class Workspace:
    """Folders and files one reporter test needs, under a temp folder."""

    def __init__(self, scratch=None):
        base = scratch or os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-7a3-")
        self.dir = self._tmp.name
        self.journal = os.path.join(self.dir, "journal")
        os.makedirs(self.journal)
        self.state = os.path.join(self.dir, "state", "reporter.db")
        os.makedirs(os.path.dirname(self.state))
        self.snapshot = os.path.join(self.dir, "policy-ids.json")
        self.config = os.path.join(self.dir, "reporter.toml")

    def cleanup(self):
        self._tmp.cleanup()

    def write_config(self, url, agents, **kw):
        text = config_text(url, self.journal, self.state, self.snapshot,
                           agents, **kw)
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write(text)
        restrict(self.config)
        return self.config


def deep(value):
    return copy.deepcopy(value)


class Crash(BaseException):
    """A simulated crash: the reporter is simply dropped where this lands. A
    BaseException so that no `except Exception` in the code under test can
    swallow it (a real crash is not catchable either)."""


class Net:
    """The transport between the reporter and the server. It counts requests,
    can be switched off (every request then fails like a dead network),
    notes whether two requests were ever in flight at once, and can raise
    before a given request."""

    def __init__(self):
        self.real = None
        self.down = False
        self.calls = []                 # (path, idempotency key, timeout)
        self.in_flight = 0
        self.max_in_flight = 0
        self.before = None              # before(index) may raise

    def __call__(self, method, path, headers, body, timeout):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            index = len(self.calls)
            self.calls.append((path, headers.get("Idempotency-Key"), timeout))
            if self.before:
                self.before(index)
            if self.down:
                raise ConnectionRefusedError("refused: " + FAKE_URL_MARK)
            return self.real(method, path, headers, body, timeout)
        finally:
            self.in_flight -= 1


class Rig:
    """A workspace, a clock, a real in-process Leasewise and a reporter that
    can be closed and opened again (a restart)."""

    def __init__(self, agents=None, journal_agent="alpha", server=True,
                 patch_clock=True, template=None):
        from tests.journal_fixture import MODEL, SEAT, Journal
        self.ws = Workspace()
        self.clock = Clock()
        if server:
            self.lw = Leasewise(self.ws.dir,
                                self.clock if patch_clock else None,
                                template=template)
            self.lw.snapshot(self.ws.snapshot)
            url = self.lw.url
        else:
            # No server: a hand-written snapshot, and a transport the test
            # supplies. Nothing listens on this address.
            self.lw = None
            url = "http://127.0.0.1:9"
            with open(self.ws.snapshot, "w", encoding="utf-8") as handle:
                json.dump({"seats": [{"machine": MACHINE, "team": "dev",
                                      "project": "org", "seat": "s6",
                                      "id": SEAT}],
                           "models": [{"account": "meter", "model": "opus",
                                       "id": MODEL}],
                           "task_classes": ["bounded_read"]}, handle)
        self.agents_cfg = {"alpha": agent_table()} if agents is None else agents
        self.ws.write_config(url, self.agents_cfg)
        self.net = Net()
        self.journal = Journal(self.ws.journal, journal_agent)
        self.state = None
        self.reporter = None

    def cleanup(self):
        self.close()
        if self.lw:
            self.lw.close()
        self.ws.cleanup()

    def open(self, fault=None, stop=None, transport=None):
        from quota_balancer.reporter import config as cfg
        from quota_balancer.reporter.client import HttpClient
        from quota_balancer.reporter.run import Reporter
        from quota_balancer.reporter.state import State
        config = cfg.load_config(self.ws.config)
        snapshot = cfg.load_snapshot(config.policy_ids_path)
        agents = cfg.resolve_agents(config, snapshot)
        limit_sources = cfg.resolve_limit_sources(config, snapshot)       # card 7c-2; () without the block
        gateway_receipts = cfg.resolve_gateway_receipts(config, snapshot)  # card L6a-1; None without the block
        self.state = State(self.ws.state)
        real = HttpClient(config.scheme, config.host, config.port,
                          config.identity)._http
        self.net.real = real
        client = HttpClient(config.scheme, config.host, config.port,
                            config.identity, transport=transport or self.net)
        self.reporter = Reporter(config, agents, self.state, client,
                                 clock=self.clock, rng=lambda: 0.5,
                                 stop=stop, fault=fault,
                                 limit_sources=limit_sources, gateway_receipts=gateway_receipts)
        return self.reporter

    def close(self):
        if self.state is not None:
            self.state.close()
            self.state = None
        self.reporter = None

    def restart(self, **kw):
        self.close()
        return self.open(**kw)

    def drain_all(self, limit=40):
        """Cycle until a cycle sends nothing and reads nothing."""
        for _ in range(limit):
            result = self.reporter.cycle()
            if result.blocked is None and not result.sent \
                    and not result.new_events:
                return result
        raise AssertionError("the reporter never settled")
