"""`python -m quota_balancer reporter run|drain|status --config FILE`.

Exit codes: 0 done; 2 refused to start (configuration, or the state is owned
by another reporter); 3 Leasewise did not answer or refused the identity, so
items are still waiting; 4 the journal is refused (the reporter stopped
reading it); 1 an internal fault. Every message is a fixed sentence: never a
path, a URL, the identity or an exception's text.
"""

import argparse
import json
import os
import signal
import sys
import threading
import time

from . import config as cfg
from . import status as status_mod
from .client import HttpClient
from .run import CREDENTIAL, UNREACHABLE, Reporter
from .state import State, StateError, StateOwned

EXIT_OK, EXIT_INTERNAL, EXIT_REFUSED, EXIT_BLOCKED, EXIT_JOURNAL = 0, 1, 2, 3, 4

STATE_OWNED = "STATE_OWNED"
STATE_FAILED = "STATE_FAILED"
INTERNAL = "REPORTER_FAILED"
MSG_OWNED = ("the state is owned by another reporter: `run` and `drain` "
             "cannot overlap; stop the other one first")
MSG_STATE = "the state file cannot be used"
MSG_INTERNAL = "the reporter stopped on an internal fault"


def _error(code, message):
    print(json.dumps({"error": {"code": code, "message": message}},
                     separators=(",", ":")), file=sys.stderr)


def _parser():
    parser = argparse.ArgumentParser(
        prog="quota_balancer reporter",
        description="The observe-only reporter: reads agenttalk's turn "
                    "journal and tells Leasewise.")
    sub = parser.add_subparsers(dest="verb", required=True)
    for name, text in (("run", "read and send until stopped (Ctrl-C)"),
                       ("drain", "read once, send what is due, exit"),
                       ("status", "show what the reporter knows")):
        one = sub.add_parser(name, help=text)
        one.add_argument("--config", required=True,
                         help="the reporter configuration file (a secret)")
        if name == "status":
            one.add_argument("--json", action="store_true",
                             help="machine-readable output")
    return parser


def main(argv=None, *, permissions=None, transport=None, clock=None,
         stop=None):
    """`permissions`, `transport`, `clock` and `stop` are test seams."""
    args = _parser().parse_args(argv)
    try:
        config = cfg.load_config(args.config, permissions=permissions)
        if args.verb == "status":
            return _status(config, args)
        snapshot = cfg.load_snapshot(config.policy_ids_path)
        agents = cfg.resolve_agents(config, snapshot)
        limit_sources = cfg.resolve_limit_sources(config, snapshot)
    except cfg.ConfigError as exc:
        _error(exc.code, exc.message)
        return EXIT_REFUSED
    try:
        os.makedirs(os.path.dirname(config.state_path), exist_ok=True)
        state = State(config.state_path)
    except OSError:
        _error(STATE_FAILED, MSG_STATE)
        return EXIT_REFUSED
    except StateOwned:
        _error(STATE_OWNED, MSG_OWNED)
        return EXIT_REFUSED
    except StateError:
        _error(STATE_FAILED, MSG_STATE)
        return EXIT_REFUSED
    try:
        client = HttpClient(config.scheme, config.host, config.port,
                            config.identity, transport=transport)
        kwargs = {} if clock is None else {"clock": clock}
        reporter = Reporter(config, agents, state, client,
                            stop=stop or threading.Event(),
                            limit_sources=limit_sources, **kwargs)
        return _go(reporter, args.verb)
    except Exception:
        _error(INTERNAL, MSG_INTERNAL)
        return EXIT_INTERNAL
    finally:
        state.close()


def _go(reporter, verb):
    if verb == "drain":
        result = reporter.drain()
        print(json.dumps(result.as_dict(), sort_keys=True))
        return _exit_for(result)
    _install_stop(reporter.stop)

    def show(result):
        print(json.dumps(result.as_dict(), sort_keys=True), flush=True)
        if result.journal_stopped:
            print("THE JOURNAL IS REFUSED (%s): no new input is read; see "
                  "`reporter status`" % result.journal_stopped,
                  file=sys.stderr, flush=True)

    try:
        reporter.run(on_cycle=show)
    except KeyboardInterrupt:
        pass
    return EXIT_OK


def _exit_for(result):
    if result.journal_stopped:
        print("THE JOURNAL IS REFUSED (%s): no new input is read; see "
              "`reporter status`" % result.journal_stopped, file=sys.stderr)
        return EXIT_JOURNAL
    if result.blocked in (UNREACHABLE, CREDENTIAL):
        return EXIT_BLOCKED
    return EXIT_OK


def _install_stop(stop):
    def handler(_signo, _frame):
        stop.set()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        try:
            if sig is not None:
                signal.signal(sig, handler)
        except ValueError:          # not the main thread
            pass


def _status(config, args):
    now = int(time.time() * 1000)
    try:
        # A reporter that never ran has no state file: report an empty one
        # rather than create it.
        path = config.state_path if os.path.exists(config.state_path)             else ":memory:"
        state = State(path)
        try:
            report = status_mod.build_status(
                state, set(config.agents), now, config.silent_after_seconds)
        finally:
            state.close()
    except StateOwned:
        report = status_mod.read_snapshot(
            status_mod.snapshot_path(config.state_path))
        if report is None:
            _error(STATE_OWNED, MSG_OWNED + " (no snapshot yet)")
            return EXIT_REFUSED
    except StateError:
        _error(STATE_FAILED, MSG_STATE)
        return EXIT_REFUSED
    if args.json:
        print(json.dumps(report, sort_keys=True, indent=2))
    else:
        print(status_mod.render_text(report))
    return EXIT_OK
