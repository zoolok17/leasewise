"""Shared helpers for the card L6a-1 tests (gateway receipts). Everything is synthetic: fixture aliases,
generated ids and made-up figures. Nothing here reads a real gateway, ledger or key.
"""

import json
import os
import uuid

from quota_balancer.reporter import mapper
from quota_balancer.reporter.config import GatewayReceipts

GATEWAY_ALIAS = "gw-fixture"
GATEWAY_ID = "7b1e3c52-0d4a-4a8e-9c61-5f2a8d3b9e01"
GENERATION = "0123456789abcdef0123456789abcdef"


def lease():
    """A fresh lease id, the way Leasewise writes one."""
    return str(uuid.uuid4())


def receipt(lease_id, seq=1, **over):
    """One receipt as the gateway's receipt command prints it (the closed fields), for `lease_id`."""
    doc = {"seq": seq, "quota_lease_ref_sha256": mapper.lease_ref_hash(lease_id), "outcome": "completed",
           "calls": 2, "input_tokens": 1200, "output_tokens": 300, "actual_micro_eur": 450,
           "closed_at": 1_788_224_400_000, "generation": GENERATION}
    doc.update(over)
    return doc


def write_json(path, doc):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)
    return path


def write_raw(path, data):
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def fixture_source(path, **over):
    """A resolved fixture source, built directly (no configuration file)."""
    row = GatewayReceipts()
    row.gateway, row.gateway_id = GATEWAY_ALIAS, GATEWAY_ID
    row.fixture, row.command = path, None
    row.timeout_seconds, row.max_bytes, row.env = 20, 1024 * 1024, ()
    for key, value in over.items():
        setattr(row, key, value)
    return row


def command_source(command, **over):
    row = fixture_source(None, command=tuple(command), **over)
    return row


def block(fixture=None, command=None, gateway=GATEWAY_ALIAS, extra=""):
    """The configuration text of the `gateway_receipts` block."""
    lines = ["[gateway_receipts]", "gateway = %s" % json.dumps(gateway)]
    if fixture is not None:
        lines.append("fixture = %s" % json.dumps(fixture))
    if command is not None:
        lines.append("command = %s" % json.dumps(list(command)))
    return "\n".join(lines) + "\n" + extra


def configure(rig, rows=None, *, fixture=None, gateway=GATEWAY_ALIAS, add_gateway=True, extra_block="",
              limit_extra=""):
    """Write a reporter configuration with a `gateway_receipts` fixture block into `rig`'s workspace
    (and, for a rig without a server, a snapshot that lists the fixture gateway). Returns the fixture path."""
    path = fixture or os.path.join(rig.ws.dir, "receipts.json")
    if rows is not None:
        write_json(path, rows)
    if add_gateway:
        with open(rig.ws.snapshot, "r", encoding="utf-8") as handle:
            snap = json.load(handle)
        snap.setdefault("gateways", [])
        if not any(g["alias"] == GATEWAY_ALIAS for g in snap["gateways"]):
            snap["gateways"].append({"alias": GATEWAY_ALIAS, "id": GATEWAY_ID})
        write_json(rig.ws.snapshot, snap)
    url = rig.lw.url if rig.lw else "http://127.0.0.1:9"
    rig.ws.write_config(url, rig.agents_cfg, extra=block(fixture=path, gateway=gateway) + extra_block + limit_extra)
    return path
