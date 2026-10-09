"""Command line interface: init, config-import --file,
billing-policy-import --file, status --json, reading-import --file,
set-coverage --account --complete, reconcile --gateway (Cards 1-2b-1), and the operator's evidence commands
gateway-approve and gateway-adopt (card R1-3d); `reconcile` now only refuses and points to `gateway-adopt`."""

import argparse
import json
import sqlite3
import sys
import time

from . import errors
from .errors import QuotaError
from .http_api import DEFAULT_HOST, BindRefused, make_server
from .lease import block_clear, expire_sweep
from .store import (
    billing_policy_import,
    gateway_adopt,
    gateway_approve,
    import_config,
    init_db,
    reading_import,
    reconcile,
    set_coverage,
    policy_ids,
    set_machine_identity,
    status,
)


def _emit_error(code, message):
    # Static message only: never echo raw input or OS error text.
    payload = {"error": {"code": code, "message": message}}
    print(json.dumps(payload, separators=(",", ":")), file=sys.stderr)


def _bool(value):
    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError("must be 'true' or 'false'")


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="quota",
        description="Quota balancer inventory (Cards 1-2b-1): "
                    "init, config-import, billing-policy-import, status, "
                    "reading-import, set-coverage.",
    )
    parser.add_argument("--db", required=True, help="path to the SQLite database file")
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("init", help="create or open the store at the current schema version")
    importer = sub.add_parser(
        "config-import", help="replace the inventory from a version-1 document"
    )
    importer.add_argument("--file", required=True, help="path to the JSON document")
    policy_importer = sub.add_parser(
        "billing-policy-import",
        help="apply the card 2b-1 billing policies (one per metered account)",
    )
    policy_importer.add_argument(
        "--file", required=True, help="path to the JSON document")
    stater = sub.add_parser("status", help="print the inventory status")
    stater.add_argument("--json", action="store_true", required=True)
    ids = sub.add_parser(
        "policy-ids",
        help="list the seat and model ids a lease request names (read-only)")
    ids.add_argument("--json", action="store_true", required=True)
    reader = sub.add_parser(
        "reading-import", help="apply a card-2 reading document (fixture)"
    )
    reader.add_argument("--file", required=True, help="path to the JSON document")
    coverage = sub.add_parser(
        "set-coverage", help="set the operator-only coverage_complete flag"
    )
    coverage.add_argument("--account", required=True, help="account alias")
    coverage.add_argument("--complete", required=True, type=_bool,
                          help="the coverage_complete value: 'true' or 'false'")
    recon = sub.add_parser(
        "reconcile",
        help="refused: naming a report is required; use gateway-adopt --evidence",
    )
    recon.add_argument("--gateway", required=True, help="gateway alias")
    approver = sub.add_parser(
        "gateway-approve",
        help="approve a gateway's pair of policy fingerprints (operator-only, audited)")
    approver.add_argument("--gateway", required=True, help="gateway alias")
    approver.add_argument("--price-hash", required=True, help="64 lowercase hex characters")
    approver.add_argument("--child-cap-hash", required=True, help="64 lowercase hex characters")
    adopter = sub.add_parser(
        "gateway-adopt",
        help="adopt a quiet report as the gateway's baseline, choose its reader and "
             "resolve the open items it names (operator-only, audited)")
    adopter.add_argument("--gateway", required=True, help="gateway alias")
    adopter.add_argument("--evidence", required=True, help="the event id of the report to adopt")
    # A string, not `type=int`: a rejected value must not be echoed back by argparse.
    adopter.add_argument("--resolve", action="append", default=[],
                         help="the id of an open item to resolve; repeat it to name every open item")
    identity = sub.add_parser(
        "identity-set",
        help="enroll/update a machine's HTTP identity allowlist entry (operator-only)",
    )
    identity.add_argument("--machine", required=True, help="machine alias")
    identity.add_argument("--identity-ref", default=None,
                          help="the opaque identity value the ingress presents (visible in the process list: "
                               "prefer --identity-stdin)")
    identity.add_argument("--identity-stdin", action="store_true",
                          help="read the identity value from standard input (one line) instead of the command line")
    identity.add_argument("--enabled", required=True, type=_bool,
                          help="'true' or 'false'")
    clearer = sub.add_parser(
        "block-clear",
        help="clear a live block of an account (operator-only, audited)")
    clearer.add_argument("--account", required=True, help="account alias")
    clearer.add_argument("--model", default=None,
                         help="model alias (default: the account-wide block)")
    # Not argparse `choices`: a rejected value must not be echoed back.
    clearer.add_argument("--reason", required=True,
                         help="reset_observed | false_positive | "
                              "operator_override")
    sub.add_parser(
        "expire-sweep",
        help="move every expired active lease to uncertain (holds kept)")
    serve = sub.add_parser(
        "serve", help="run the card-5 private /v1 HTTP server (blocking)"
    )
    serve.add_argument("--host", default=DEFAULT_HOST,
                       help="bind address; must be a literal loopback IP")
    serve.add_argument("--port", type=int, default=8443)
    return parser


def _identity_value(args):
    """The identity value: from --identity-ref, or one line of standard input (--identity-stdin), exactly one of the two.
    A missing, doubled or empty value is the static INVALID_REQUEST; the value is never echoed."""
    if args.identity_stdin == (args.identity_ref is not None):
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    value = sys.stdin.readline().rstrip("\r\n") if args.identity_stdin else args.identity_ref
    if not value:
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    return value


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["reporter"]:
        # The observe-only reporter (card 7a-3) has its own options; it takes
        # the journal and a configuration file, not the store path.
        from .reporter.cli import main as reporter_main
        return reporter_main(argv[1:])
    args = _build_parser().parse_args(argv)
    try:
        if args.verb == "init":
            init_db(args.db)
        elif args.verb == "config-import":
            import_config(args.db, args.file)
        elif args.verb == "billing-policy-import":
            billing_policy_import(args.db, args.file)
        elif args.verb == "reading-import":
            reading_import(args.db, args.file)
        elif args.verb == "set-coverage":
            set_coverage(args.db, args.account, args.complete)
        elif args.verb == "reconcile":
            reconcile(args.db, args.gateway)
        elif args.verb == "gateway-approve":
            gateway_approve(args.db, args.gateway, args.price_hash, args.child_cap_hash)
            print(json.dumps({"approved": True}, separators=(",", ":")))
        elif args.verb == "gateway-adopt":
            print(json.dumps({"adoption_id": gateway_adopt(args.db, args.gateway, args.evidence, args.resolve)},
                             separators=(",", ":")))
        elif args.verb == "identity-set":
            set_machine_identity(args.db, args.machine, _identity_value(args), args.enabled)
        elif args.verb == "block-clear":
            print(json.dumps(block_clear(
                args.db, args.account, args.model, args.reason,
                int(time.time() * 1000)), separators=(",", ":")))
        elif args.verb == "expire-sweep":
            print(json.dumps(expire_sweep(
                args.db, int(time.time() * 1000)), separators=(",", ":")))
        elif args.verb == "policy-ids":
            print(json.dumps(policy_ids(args.db), sort_keys=True,
                             separators=(",", ":")))
        elif args.verb == "serve":
            try:
                make_server(args.db, host=args.host, port=args.port).serve_forever()
            except BindRefused:
                _emit_error(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
                return errors.EXIT_CODES[errors.CODE_INVALID_REQUEST]
        else:
            print(json.dumps(status(args.db), separators=(",", ":")))
        return 0
    except QuotaError as exc:
        _emit_error(exc.code, exc.message)
        return errors.EXIT_CODES[exc.code]
    except sqlite3.Error:
        # Last-resort guard: no raw driver text reaches the operator.
        _emit_error(errors.CODE_STORE_ERROR, errors.MSG_STORE_FAILED)
        return errors.EXIT_CODES[errors.CODE_STORE_ERROR]
    except Exception:
        # Last resort (defense in depth): an unexpected internal fault -
        # e.g. a value that slips past validation and overflows a driver
        # conversion - still emits the static error contract, never a
        # traceback or a path.
        _emit_error(errors.CODE_STORE_ERROR, errors.MSG_STORE_FAILED)
        return errors.EXIT_CODES[errors.CODE_STORE_ERROR]


if __name__ == "__main__":
    sys.exit(main())
