"""Command line interface: init, config-import --file, status --json,
reading-import --file, set-coverage --account --complete (Cards 1-2)."""

import argparse
import json
import sqlite3
import sys

from . import errors
from .errors import QuotaError
from .store import import_config, init_db, reading_import, set_coverage, status


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
        description="Quota balancer inventory (Cards 1-2): "
                    "init, config-import, status, reading-import, set-coverage.",
    )
    parser.add_argument("--db", required=True, help="path to the SQLite database file")
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("init", help="create or open the store at the current schema version")
    importer = sub.add_parser(
        "config-import", help="replace the inventory from a version-1 document"
    )
    importer.add_argument("--file", required=True, help="path to the JSON document")
    stater = sub.add_parser("status", help="print the inventory status")
    stater.add_argument("--json", action="store_true", required=True)
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
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        if args.verb == "init":
            init_db(args.db)
        elif args.verb == "config-import":
            import_config(args.db, args.file)
        elif args.verb == "reading-import":
            reading_import(args.db, args.file)
        elif args.verb == "set-coverage":
            set_coverage(args.db, args.account, args.complete)
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


if __name__ == "__main__":
    sys.exit(main())
