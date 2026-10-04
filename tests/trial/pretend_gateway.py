"""A pretend gateway program for the live-trial rehearsal (card R1-READER-4-prep). Made-up values only. Not a test: the name does not start with `test_`.

It stands in for the gateway's own program, so the reporter and Leasewise can be rehearsed end to end on a machine that has no gateway. It prints the
two read-only answers the money reader asks for, in the shapes the real program documents (report version 1, receipt page envelope 1, receipt rows that
carry `charge_period`), from a small made-up ledger file:

    python pretend_gateway.py gateway report --json
    python pretend_gateway.py gateway receipts --after N --limit M --json
    python pretend_gateway.py advance          (rehearsal only: adds one receipt of 450, so the settled money grows and a second report is different;
                                                the money no lease accounts for does not change, so nothing needs the operator)
    python pretend_gateway.py spend            (rehearsal only: money no lease accounts for goes up by 25, so Leasewise opens an item for a person)
    python pretend_gateway.py init MODE       (rehearsal only: starts the ledger over. MODE is `busy` (the default: this month already has spending), `idle-older`
                                                (nothing spent this month, and the report lists only an OLDER month) or `idle-empty` (nothing spent this month
                                                and the report lists no month at all): the quiet starts of a month a healthy gateway can have)
    python pretend_gateway.py show             (rehearsal only: prints the settled total, for the "passed" comparison)

The ledger file is named by the environment variable PRETEND_LEDGER (a path), or is `pretend-ledger.json` beside this script. It is created on first use.
Each read call is also noted (a time and the word report or receipts) in `pretend-calls.log` beside the ledger.
It never reads or writes anything else, never opens the network and never runs another program.
"""

import datetime
import json
import os
import sys

GENERATION = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"                       # made up
PRICE_HASH = "1a" * 32                                                # made up fingerprints (64 hex characters)
CAP_HASH = "2b" * 32
OPENING_OTHER_SPEND = 40                                              # money no lease owns (the "unattributed" figure starts here)
UNRECEIPTED = 0                                                       # money of turns still running: none in the rehearsal


def ledger_path():
    return os.environ.get("PRETEND_LEDGER") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "pretend-ledger.json")


def month_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")


def month_before():
    first = datetime.datetime.now(datetime.timezone.utc).replace(day=1)
    return (first - datetime.timedelta(days=1)).strftime("%Y-%m")


INITIAL = {
    "busy": {"receipts": [{"seq": 1, "actual_micro_eur": 1290}], "other_spend": OPENING_OTHER_SPEND, "previous": None},
    "idle-older": {"receipts": [], "other_spend": 0, "previous": 5000},
    "idle-empty": {"receipts": [], "other_spend": 0, "previous": None},
}


def load():
    path = ledger_path()
    if not os.path.exists(path):
        save(INITIAL["busy"])
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save(doc):
    with open(ledger_path(), "w", encoding="utf-8") as handle:
        json.dump(doc, handle)


def settled(doc):
    return sum(row["actual_micro_eur"] for row in doc["receipts"]) + doc["other_spend"] + UNRECEIPTED


def periods(doc, month):
    """The months with money, oldest first. A month with no spending has no row, as with the real program: a quiet start of a month lists an older month or none."""
    rows = []
    if doc.get("previous") is not None:
        rows.append({"period": month_before(), "committed_micro_eur": doc["previous"]})
    if settled(doc) > 0:
        rows.append({"period": month, "committed_micro_eur": settled(doc)})
    return rows


def report(doc):
    month = month_now()
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {
        "gateway_report_version": 1, "child_receipt_report_version": 2, "child_cap_ready": True,
        "generation": GENERATION, "policy_hash": PRICE_HASH, "child_cap_policy_hash": CAP_HASH,
        "periods": periods(doc, month),
        "unresolved": [], "child_receipts_through_seq": len(doc["receipts"]),
        "unreceipted_bound_actual": [{"period": month, "micro_eur": UNRECEIPTED}],
        "service_hold": False, "service_hold_reason": None,
        "observed_at": now, "child_receipts_pending": 0, "open_child_turns": 0, "open_child_turns_expired": 0, "earliest_open_expiry": None,
    }


def receipts(doc, after, limit):
    rows = [row for row in doc["receipts"] if row["seq"] > after][:limit]
    month = month_now()
    shown = [{"seq": row["seq"], "quota_lease_ref_sha256": "%064x" % (row["seq"] + 1000), "outcome": "completed", "calls": 1,
              "input_tokens": 100, "output_tokens": 50, "actual_micro_eur": row["actual_micro_eur"],
              "closed_at": "2026-01-01T00:00:00.000000Z", "charge_period": month} for row in rows]
    return {"envelope_version": 1, "generation": GENERATION, "after_seq": after, "next_seq": rows[-1]["seq"] if rows else after,
            "has_more": len([r for r in doc["receipts"] if r["seq"] > after]) > len(rows), "receipts": shown}


def option(args, name):
    return int(args[args.index(name) + 1])


def note_call(args):
    """Rehearsal bookkeeping: one line per call in `pretend-calls.log` beside the ledger, so the rehearsal can count how often the reader asks."""
    kind = "report" if args[:2] == ["gateway", "report"] else "receipts" if args[:2] == ["gateway", "receipts"] else None
    if kind:
        with open(os.path.join(os.path.dirname(ledger_path()), "pretend-calls.log"), "a", encoding="utf-8") as handle:
            handle.write("%.3f %s" % (datetime.datetime.now(datetime.timezone.utc).timestamp(), kind) + chr(10))


def main(argv):
    args = list(argv)
    note_call(args)
    if args[:3] == ["gateway", "report", "--json"] and len(args) == 3:
        print(json.dumps(report(load()), sort_keys=True, separators=(",", ":")))
        return 0
    if args[:2] == ["gateway", "receipts"] and "--after" in args and "--limit" in args and args[-1] == "--json":
        print(json.dumps(receipts(load(), option(args, "--after"), option(args, "--limit")), sort_keys=True, separators=(",", ":")))
        return 0
    if len(args) == 2 and args[0] == "init" and args[1] in INITIAL:
        save(INITIAL[args[1]])
        return 0
    if args == ["advance"]:
        doc = load()
        doc["receipts"].append({"seq": len(doc["receipts"]) + 1, "actual_micro_eur": 450})      # money a lease accounts for: the unowned part stays the same
        save(doc)
        return 0
    if args == ["spend"]:
        doc = load()
        doc["other_spend"] += 25                       # money no lease accounts for goes up: Leasewise opens an item that a person must look at
        save(doc)
        return 0
    if args == ["show"]:
        print(settled(load()))
        return 0
    sys.stderr.write("report_unavailable\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
