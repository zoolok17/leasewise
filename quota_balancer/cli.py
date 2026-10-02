"""Command line interface: init, config-import --file,
billing-policy-import --file, status --json, reading-import --file,
set-coverage --account --complete, reconcile --gateway (Cards 1-2b-1)."""

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
        help="clear a gateway's reconciliation_required state (operator-only)",
    )
    recon.add_argument("--gateway", required=True, help="gateway alias")
    identity = sub.add_parser(
        "identity-set",
        help="enroll/update a machine's HTTP identity allowlist entry (operator-only)",
    )
    identity.add_argument("--machine", required=True, help="machine alias")
    identity.add_argument("--identity-ref", required=True,
                          help="the opaque identity value the ingress presents")
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


def main(argv=None):
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
        elif args.verb == "identity-set":
            set_machine_identity(args.db, args.machine, args.identity_ref,
                                 args.enabled)
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
