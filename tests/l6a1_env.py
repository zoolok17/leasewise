"""Shared helpers for the card L6a-1 tests (gateway receipts). Everything is synthetic: fixture aliases,
generated ids and made-up figures. Nothing here reads a real gateway, ledger, admission store or key.
"""

import json
import os
import sqlite3
import time
import uuid

from quota_balancer.reporter import admission_store, mapper
from quota_balancer.reporter.config import GatewayReceipts

GATEWAY_ALIAS = "gw-fixture"
GATEWAY_ID = "7b1e3c52-0d4a-4a8e-9c61-5f2a8d3b9e01"
OTHER_GATEWAY_ID = "c4a2e9f0-3b7d-4c1e-8a55-0d9b2f6e7a12"
GENERATION = "0123456789abcdef0123456789abcdef"
GENERATION_B = "fedcba9876543210fedcba9876543210"
CLOSED_AT = "2026-10-03T10:00:00.000000Z"


def lease():
    """A fresh lease id, the way Leasewise writes one."""
    return str(uuid.uuid4())


def receipt(lease_id, seq=1, **over):
    """One receipt as the gateway's receipt page carries it (the closed fields), for `lease_id`."""
    doc = {"seq": seq, "quota_lease_ref_sha256": mapper.lease_ref_hash(lease_id), "outcome": "completed",
           "calls": 2, "input_tokens": 1200, "output_tokens": 300, "actual_micro_eur": 450,
           "closed_at": CLOSED_AT}
    doc.update(over)
    return doc


def ledger(rows, generation=GENERATION):
    """A fixture ledger document: the whole list of receipts of one ledger generation."""
    return {"generation": generation, "receipts": list(rows)}


def page(rows, after=0, generation=GENERATION, has_more=False, **over):
    """A receipt page (the envelope of DESIGN-6A 5.4) after `after`. `over` replaces any key."""
    rows = list(rows)
    last = rows[-1].get("seq", after) if rows and isinstance(rows[-1], dict) else after
    doc = {"envelope_version": 1, "generation": generation, "after_seq": after,
           "next_seq": last, "has_more": has_more, "receipts": rows}
    doc.update(over)
    return doc


def encode(doc):
    return json.dumps(doc).encode("utf-8")


def write_json(path, doc):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)
    return path


def write_raw(path, data):
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def fixture_source(path, store=None, **over):
    """A resolved fixture source, built directly (no configuration file)."""
    row = GatewayReceipts()
    row.gateway, row.gateway_id = GATEWAY_ALIAS, GATEWAY_ID
    row.admission_store = store or os.path.join(os.path.dirname(path or ".") or ".", "admission.db")
    row.fixture = path
    row.max_bytes, row.page_limit = 1024 * 1024, 100
    row.command, row.timeout_seconds, row.env = None, None, ()
    for key, value in over.items():
        setattr(row, key, value)
    return row


# --- the admission store (written here the way card L6a-2's `reporter admit` will) ---------------------------------

def make_store(path, bindings=(), wal=False):
    """A new admission store from the real layout, with the granted rows `bindings` (a list of lease ids, or of
    dicts with lease_id and optional gateway_id, agent, message_id)."""
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        admission_store.create(conn)
        for number, one in enumerate(bindings):
            grant(conn, one, number)
    finally:
        conn.close()
    return path


def grant(conn, one, number=0):
    """One granted admission row, in an open connection (an autocommit one, or inside a transaction)."""
    one = {"lease_id": one} if isinstance(one, str) else dict(one)
    lease_id = one["lease_id"]
    conn.execute("UPDATE admission_meta SET value=value+1 WHERE name='grant_seq'")
    conn.execute(
        "INSERT INTO admission(agent, message_id, request_id, body_current, state, lease_id, gateway_id, reference,"
        " ref_sha256, ttl_seconds, expires_ms, grant_seq) VALUES(?,?,?,?, 'granted', ?,?,?,?,?,?,"
        " (SELECT value FROM admission_meta WHERE name='grant_seq'))",
        (one.get("agent", "alpha"), one.get("message_id", "m%d-%s" % (number, uuid.uuid4().hex[:6])),
         str(uuid.uuid4()), "{}", lease_id, one.get("gateway_id", GATEWAY_ID), one.get("reference", lease_id),
         one.get("ref_sha256", mapper.lease_ref_hash(lease_id)), 1800, one.get("expires_ms", int(time.time() * 1000) + 86_400_000)))


def add_binding(path, one):
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        grant(conn, one, 99)
    finally:
        conn.close()


def block(fixture=None, gateway=GATEWAY_ALIAS, store=None, extra=""):
    """The configuration text of the `gateway_receipts` block."""
    lines = ["[gateway_receipts]", "gateway = %s" % json.dumps(gateway),
             "admission_store = %s" % json.dumps(store or "C:/nowhere/admission.db")]
    if fixture is not None:
        lines.append("fixture = %s" % json.dumps(fixture))
    return "\n".join(lines) + "\n" + extra


def configure(rig, rows=None, *, fixture=None, gateway=GATEWAY_ALIAS, add_gateway=True, extra_block="",
              limit_extra="", store=None, bindings=None, generation=GENERATION):
    """Write a reporter configuration with a `gateway_receipts` fixture block into `rig`'s workspace (and, for a
    rig without a server, a snapshot that lists the fixture gateway). `rows` become the fixture ledger;
    `bindings` (lease ids) become granted rows of a new admission store. Returns the fixture path."""
    path = fixture or os.path.join(rig.ws.dir, "receipts.json")
    if rows is not None:
        write_json(path, ledger(rows, generation))
    store = store or os.path.join(rig.ws.dir, "admission.db")
    if not os.path.exists(store):
        make_store(store, bindings or ())
    if add_gateway:
        with open(rig.ws.snapshot, "r", encoding="utf-8") as handle:
            snap = json.load(handle)
        snap.setdefault("gateways", [])
        if not any(g["alias"] == GATEWAY_ALIAS for g in snap["gateways"]):
            snap["gateways"].append({"alias": GATEWAY_ALIAS, "id": GATEWAY_ID})
        write_json(rig.ws.snapshot, snap)
    url = rig.lw.url if rig.lw else "http://127.0.0.1:9"
    rig.ws.write_config(url, rig.agents_cfg,
                        extra=block(fixture=path, gateway=gateway, store=store) + extra_block + limit_extra)
    return path
