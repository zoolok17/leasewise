"""Shared helpers for the card L6a-2 tests (`reporter admit`). Everything is synthetic: generated ids, made-up bounds, a
fake Leasewise that answers from a script, or the real in-process handler. No real gateway, provider, key or account.
"""

import io
import json
import os
import sqlite3
import subprocess
import sys
import uuid

from quota_balancer.reporter import admit_wire as wire
from quota_balancer.reporter import cli

from tests.reporter_env import MACHINE, Rig, restrict

GATEWAY_ALIAS = "g1"
GATEWAY_ID = "7b1e3c52-0d4a-4a8e-9c61-5f2a8d3b9e01"
AGENT = "alpha"
BOUNDS = {"fresh_overhead_bound_tokens": 1000, "resume_history_bound_tokens": 5000, "growth_bound_tokens": 10}
KEY_HEADER = "Idempotency-Key"


def request_bytes(agent=AGENT, message_id="msg-0001", parent="", profile="ovh-qwen", mode="fresh", dispatch=500):
    return wire.encode_request(agent, message_id, parent, profile, mode, dispatch)


def admission_block(store_path, agents=(AGENT,), gateway=GATEWAY_ALIAS, **bounds):
    """The `[admission]` configuration text. `bounds` replace the default operator bounds; None removes one."""
    merged = dict(BOUNDS)
    merged.update(bounds)
    lines = ["[admission]", "store = %s" % json.dumps(store_path), "gateway = %s" % json.dumps(gateway),
             "gateway_agents = %s" % json.dumps(list(agents))]
    for key, value in merged.items():
        if value is not None:
            lines.append("%s = %d" % (key, value))
    return "\n".join(lines) + "\n"


class Rigged:
    """A reporter workspace with an `[admission]` block, and helpers to run `reporter admit` in this process or as a
    real child process. `server=False` is for scripted fake Leasewise answers; `server=True` is the real in-process
    handler (on the real clock, so a child process and the handler agree on the time)."""

    def __init__(self, server=False, agents=None, **bounds):
        self.rig = Rig(server=server, patch_clock=False, agents=agents)
        self.ws = self.rig.ws
        self.store = os.path.join(self.ws.dir, "admission", "admission.db")
        self.bounds = bounds
        if server:
            self.seat_alias = "s3"
            self.rig.agents_cfg = {AGENT: dict(self.rig.agents_cfg[AGENT], seat=self.seat_alias)}
        else:
            self.write_snapshot()
        self.configure()

    def write_snapshot(self):
        from tests.journal_fixture import MODEL, SEAT
        with open(self.ws.snapshot, "w", encoding="utf-8") as handle:
            json.dump({"seats": [{"machine": MACHINE, "team": "dev", "project": "org", "seat": "s6", "id": SEAT}],
                       "models": [{"account": "meter", "model": "opus", "id": MODEL}],
                       "task_classes": ["bounded_read"],
                       "gateways": [{"alias": GATEWAY_ALIAS, "id": GATEWAY_ID}]}, handle)

    def configure(self, extra="", agents=None, **bounds):
        merged = dict(self.bounds)
        merged.update(bounds)
        url = self.rig.lw.url if self.rig.lw else "http://127.0.0.1:9"
        self.ws.write_config(url, agents or self.rig.agents_cfg,
                             extra=admission_block(self.store, **merged) + extra)
        return self.ws.config

    def cleanup(self):
        self.rig.cleanup()

    # --- running it ---------------------------------------------------------------------------

    def run(self, raw, transport, **seams):
        """In this process. -> (exit code, bytes written to standard output, text written to standard error)."""
        out, err = io.BytesIO(), io.StringIO()
        seams = dict(seams, stdin=io.BytesIO(raw), stdout=out)
        import contextlib
        with contextlib.redirect_stderr(err):
            code = cli.main(["admit", "--config", self.ws.config], permissions=lambda p: None, transport=transport,
                            admit_seams=seams)
        return code, out.getvalue(), err.getvalue()

    def child(self, raw):
        """A real child process: `python -m quota_balancer reporter admit`. -> a Popen (stdin already written)."""
        proc = subprocess.Popen([sys.executable, "-m", "quota_balancer", "reporter", "admit", "--config",
                                 self.ws.config], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        proc.stdin.write(raw)
        proc.stdin.close()
        return proc

    # --- looking at the store -----------------------------------------------------------------

    def sql(self, text, params=()):
        conn = sqlite3.connect(self.store)
        try:
            return conn.execute(text, params).fetchall()
        finally:
            conn.close()

    def row(self):
        rows = self.sql("SELECT state, rebuilds, replay_tried, last_error_code, not_before_ms, lease_id, reference,"
                        " ref_sha256, max_calls, max_micro_eur, ttl_seconds, expires_ms FROM admission")
        return rows[0] if rows else None


class Script:
    """A fake Leasewise for `/v1/leases`. Each answer is (status, body dict or bytes) or an exception to raise; the
    LAST one repeats. It records (key, body bytes, timeout) of every call. A lease it grants is remembered per
    Idempotency-Key and per body, so a repeat of the same bytes is a `200` with the same lease id, the way the real
    handler replays; `on_call(index, key, body)` runs before an answer is chosen."""

    def __init__(self, *answers, on_call=None, expires_in_ms=1_800_000, enforcement="metered_advisory"):
        self.answers = list(answers)
        self.calls = []
        self.on_call = on_call
        self.granted = {}
        self.expires_in_ms = expires_in_ms
        self.enforcement = enforcement
        self.now_ms = None

    def lease_answer(self, key, body):
        lease_id, first = self.granted.setdefault(key, (str(uuid.uuid4()), body))
        if first != body:
            return error("IDEMPOTENCY_CONFLICT", 409)               # the same request id with another body
        return 200, {
            "lease_id": lease_id, "state": "active", "enforcement": self.enforcement, "money_hold": None,
            "caps": {"expected_input": 1, "input_cap": 1, "output_cap": 1, "max_calls": 2, "per_call_output_cap": 1},
            "granted_at": 1, "expires_at": (self.now_ms() if self.now_ms else 0) + self.expires_in_ms,
            "lease_token": None, "windows": [], "warnings": []}

    def __call__(self, method, path, headers, body, timeout):
        index = len(self.calls)
        self.calls.append((headers[KEY_HEADER], body, timeout))
        if self.on_call:
            self.on_call(index, headers[KEY_HEADER], body)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        if answer == "GRANT":
            status, doc = self.lease_answer(headers[KEY_HEADER], body)
        else:
            status, doc = answer
        return status, doc if isinstance(doc, bytes) else json.dumps(doc).encode()


def error(code, status):
    return status, {"error": {"code": code, "message": "x"}}


class FakeClock:
    """A monotonic clock and a wall clock the test moves; `sleep` moves them too."""

    def __init__(self):
        self.mono = 1000.0
        self.ms = 1_788_224_400_000

    def monotonic(self):
        return self.mono

    def now_ms(self):
        return self.ms

    def sleep(self, seconds):
        self.advance(seconds)

    def advance(self, seconds):
        self.mono += seconds
        self.ms += int(seconds * 1000)

    def seams(self, **more):
        return dict({"monotonic": self.monotonic, "now_ms": self.now_ms, "sleep": self.sleep}, **more)


def restricted(path):
    restrict(path)
    return path
