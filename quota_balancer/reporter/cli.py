"""`python -m quota_balancer reporter run|drain|status|admit|resume|reset --config FILE`.

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

from . import admit as admit_mod
from . import admit_wire
from . import config as cfg
from . import gwstate
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
STATUS_VERSION_CODE = "STATUS_VERSION"
MSG_STATUS_VERSION = ("the running reporter's status has a version this program does not know: "
                      "upgrade this program, or stop the running reporter")


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
    admit = sub.add_parser("admit", help="the turn admission program: read one request on standard input, "
                                         "answer once on standard output")
    admit.add_argument("--config", required=True, help="the reporter configuration file (a secret)")
    for name, text in (("resume", "clear the money reader's `send_refused` halt and give its pending report a new retry budget"),
                       ("reset", "archive the money reader's whole state and start a fresh reader identity")):
        one = sub.add_parser(name, help=text + " (local and audited; it never talks to Leasewise; needs --confirm)")
        one.add_argument("--config", required=True, help="the reporter configuration file (a secret)")
        one.add_argument("--confirm", action="store_true", help="really do it")
    return parser


def main(argv=None, *, permissions=None, transport=None, clock=None,
         stop=None, admit_seams=None):
    """`permissions`, `transport`, `clock`, `stop` and `admit_seams` are test seams."""
    args = _parser().parse_args(argv)
    try:
        config = cfg.load_config(args.config, permissions=permissions)
        if args.verb == "status":
            return _status(config, args)
        if args.verb == "admit":
            return _admit(config, transport, admit_seams or {})
        if args.verb in ("resume", "reset"):
            return _operator(config, args, clock)
        snapshot = cfg.load_snapshot(config.policy_ids_path)
        agents = cfg.resolve_agents(config, snapshot)
        limit_sources = cfg.resolve_limit_sources(config, snapshot)
        reading_sources = cfg.resolve_reading_sources(config, snapshot)
        gateway_receipts = cfg.resolve_gateway_receipts(config, snapshot)
        gateway_report = cfg.resolve_gateway_report(config, snapshot)
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
                            limit_sources=limit_sources, reading_sources=reading_sources,
                            gateway_receipts=gateway_receipts, gateway_report=gateway_report, **kwargs)
        return _go(reporter, args.verb)
    except Exception:
        _error(INTERNAL, MSG_INTERNAL)
        return EXIT_INTERNAL
    finally:
        state.close()


MSG_CONFIRM = "this changes the money reader's state: run it again with --confirm"
MSG_NOT_RESUMABLE = ("only a reader stopped by repeated definitive refusals (send_refused) can be resumed; after a collision or a damaged state "
                     "use `reset`")
MSG_RESET_TOO_LARGE = ("the old state is too large to archive for the reset; nothing was changed: move the reporter's state file aside "
                       "and keep it, then start the reporter again (an absent state is a first start)")
CONFIRM_CODE = "CONFIRM_REQUIRED"
NOT_RESUMABLE_CODE = "NOT_RESUMABLE"
RESET_TOO_LARGE_CODE = "RESET_TOO_LARGE"


def _operator(config, args, clock):
    """`reporter resume --confirm` and `reporter reset --confirm` (card R1-READER step 3a): local, audited, never talk to Leasewise. Without
    --confirm nothing is opened or changed. A state owned by a running reporter is refused (stop it first). Every message is a fixed sentence."""
    if not args.confirm:
        _error(CONFIRM_CODE, MSG_CONFIRM)
        return EXIT_REFUSED
    now = int((clock or (lambda: int(time.time() * 1000)))())
    try:
        os.makedirs(os.path.dirname(config.state_path), exist_ok=True)
        state = State(config.state_path)
    except StateOwned:
        _error(STATE_OWNED, MSG_OWNED)
        return EXIT_REFUSED
    except (OSError, StateError):
        _error(STATE_FAILED, MSG_STATE)
        return EXIT_REFUSED
    try:
        if args.verb == "resume":
            gwstate.resume(state, now)
            print(json.dumps({"resumed": True}, separators=(",", ":")))
        else:
            fresh = gwstate.reset(state, now)
            print(json.dumps({"reset": True, "reader_incarnation": fresh}, separators=(",", ":")))
        return EXIT_OK
    except gwstate.Refused as exc:
        if exc.word == gwstate.NOT_RESUMABLE:
            _error(NOT_RESUMABLE_CODE, MSG_NOT_RESUMABLE)
        elif exc.word == gwstate.RESET_TOO_LARGE:
            _error(RESET_TOO_LARGE_CODE, MSG_RESET_TOO_LARGE)
        else:
            _error(INTERNAL, MSG_INTERNAL)
        return EXIT_REFUSED
    except Exception:
        _error(INTERNAL, MSG_INTERNAL)
        return EXIT_INTERNAL
    finally:
        state.close()


MSG_ADMIT_OFF = "the configuration has no admission block"
MSG_ADMIT_BAD = "the request is not allowed"
MSG_ADMIT_FAILED = "the admission could not be completed"


def _admit(config, transport, seams):
    """`reporter admit` (card L6a-2): one request in, one answer out, and nothing else. Standard output carries the
    answer only; every failure is a fixed sentence on standard error with a non-zero exit."""
    try:
        snapshot = cfg.load_snapshot(config.policy_ids_path)
        agents = cfg.resolve_agents(config, snapshot)
        admission = cfg.resolve_admission(config, snapshot)
    except cfg.ConfigError as exc:
        _error(exc.code, exc.message)
        return EXIT_REFUSED
    if admission is None:
        _error(cfg.CONFIG_INVALID, MSG_ADMIT_OFF)
        return EXIT_REFUSED
    stdin = seams.get("stdin") or sys.stdin.buffer
    stdout = seams.get("stdout") or sys.stdout.buffer
    try:
        raw = stdin.read(admit_wire.MAX_REQUEST_BYTES + 1)         # one byte over the cap is enough to refuse it
        client = HttpClient(config.scheme, config.host, config.port, config.identity, transport=transport)
        env = admit_mod.Environment(admission, agents, client, **{k: v for k, v in seams.items()
                                                                  if k in ("now_ms", "monotonic", "sleep", "fault")})
        code, out = admit_mod.run(raw, env)
    except Exception:                  # nothing from the fault is printed
        _error("ADMIT_FAILED", MSG_ADMIT_FAILED)
        return admit_mod.EXIT_FAILED
    if code == admit_mod.EXIT_FAILED:
        _error("ADMIT_FAILED", MSG_ADMIT_FAILED)         # an operation that could not complete: no answer, exit 3
        return admit_mod.EXIT_FAILED
    if code != admit_mod.EXIT_OK:
        _error("ADMIT_REQUEST", "%s: %s" % (MSG_ADMIT_BAD, out.decode("ascii")))
        return EXIT_REFUSED
    stdout.write(out)
    stdout.flush()
    return EXIT_OK


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
                state, set(config.agents), now, config.silent_after_seconds,
                config.limit_sources, config.gateway_receipts,
                config.admission.gateway_agents if config.admission is not None else (), config.reading_sources)
        finally:
            state.close()
    except StateOwned:
        report = status_mod.read_snapshot(
            status_mod.snapshot_path(config.state_path))
        if report is None:
            _error(STATE_OWNED, MSG_OWNED + " (no snapshot yet)")
            return EXIT_REFUSED
        version = report.get("status_version")
        if type(version) is not int or not 1 <= version <= status_mod.STATUS_VERSION:
            # Older versions are shown as they are (the text view says what is missing); a version
            # this program does not know is never guessed at.
            _error(STATUS_VERSION_CODE, MSG_STATUS_VERSION)
            return EXIT_REFUSED
    except StateError:
        _error(STATE_FAILED, MSG_STATE)
        return EXIT_REFUSED
    if args.json:
        print(json.dumps(report, sort_keys=True, indent=2))
    else:
        print(status_mod.render_text(report))
    return EXIT_OK
