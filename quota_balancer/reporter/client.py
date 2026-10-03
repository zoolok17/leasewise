"""The HTTP client (DESIGN-7A.md 4.5; card 7a-3).

One request at a time, a 5-second timeout on every network step, no
redirects, no proxy, one fresh connection per request. It turns every
outcome into a `decisions.Response`; what to do about it is `decisions.decide`.

Nothing here keeps or prints text from an exception: a network failure is
just "no answer". The identity travels only in the request header.
"""

import contextlib
import http.client
import ipaddress
import json
import re
import socket
import threading
import time

from . import decisions
from .decisions import Response

TIMEOUT_SECONDS = 5.0
MAX_ANSWER_BYTES = 1024 * 1024
IDENTITY_HEADER = "X-Machine-Identity"
IDEMPOTENCY_HEADER = "Idempotency-Key"
BAD_ANSWER = decisions.BAD_ANSWER
MALFORMED_RESPONSE = decisions.MALFORMED_RESPONSE

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
                   r"[0-9a-f]{12}")


class HttpClient:
    """`transport(method, path, headers, body, timeout)` -> (status, bytes)
    may raise anything; tests pass a fake one."""

    def __init__(self, scheme, host, port, identity, *, transport=None,
                 timeout=TIMEOUT_SECONDS):
        self._scheme = scheme
        self._host = host
        self._port = port
        self._identity = identity
        self._timeout = timeout
        self._transport = transport or self._http

    def __repr__(self):
        return "HttpClient(<hidden>)"

    def _http(self, method, path, headers, body, timeout):
        """One request with ONE absolute deadline for the whole exchange (connecting, sending, the answer's headers
        and the answer read in pieces). A peer that answers a little at a time cannot stretch it: each step's timeout
        is what is left, and at the deadline the connection is shut down from a watchdog so that even a blocked read
        ends. Raises `TimeoutError` when the deadline came first."""
        factory = (http.client.HTTPSConnection if self._scheme == "https"
                   else http.client.HTTPConnection)
        deadline = time.monotonic() + timeout
        conn = factory(self._host, self._port, timeout=timeout)
        held = {"sock": None}

        def stop():
            with contextlib.suppress(OSError, AttributeError):
                held["sock"].shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError, AttributeError):
                held["sock"].close()
            with contextlib.suppress(OSError):
                conn.close()

        def time_left():
            """What is left of the deadline, handed to the socket as its timeout; the exchange stops when it is used up."""
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError()
            with contextlib.suppress(OSError, AttributeError):
                held["sock"].settimeout(left)
            return left

        def connect(address, _timeout, source_address=None):
            """The connection step: with the time that is left, and the socket handed to the watchdog the moment it
            exists. `localhost` and an IP address are connected as numbers, so the resolver is never asked (a host name
            may still be looked up on the `run` path, where there is no promise; the lookup itself cannot be stopped)."""
            target = _numeric(address[0])
            if target is None:
                held["sock"] = socket.create_connection(address, time_left(), source_address)
                return held["sock"]
            sock = socket.socket(socket.AF_INET6 if ":" in target else socket.AF_INET, socket.SOCK_STREAM)
            held["sock"] = sock
            sock.settimeout(time_left())
            if source_address:
                sock.bind(source_address)
            sock.connect((target, address[1]))
            return sock

        conn._create_connection = connect               # the connection step obeys the deadline
        watchdog = threading.Timer(timeout, stop)
        watchdog.daemon = True
        watchdog.start()
        try:
            conn.connect()
            held["sock"] = conn.sock                    # the TLS socket replaces the plain one
            time_left()                                 # nothing is sent after the deadline
            conn.request(method, path, body=body, headers=headers)
            time_left()                                 # nor is the answer waited for beyond it
            answer = conn.getresponse()
            pieces, size = [], 0
            while True:
                time_left()
                piece = answer.read1(8192)
                if not piece:
                    break
                pieces.append(piece)
                size += len(piece)
                if size > MAX_ANSWER_BYTES:
                    break
            return answer.status, b"".join(pieces)
        except (OSError, http.client.HTTPException):
            if time.monotonic() >= deadline:
                raise TimeoutError() from None          # the watchdog (or the clock) ended it: the deadline came first
            raise
        finally:
            watchdog.cancel()
            conn.close()
            with contextlib.suppress(OSError, AttributeError):
                held["sock"].close()                    # a socket that never reached the connection object
            watchdog.join(1.0)                          # the watchdog thread ends in every case

    def send(self, item):
        """POST one outbox item; always returns a Response."""
        if not _allowed(item.kind, item.path):
            return Response(None, BAD_ANSWER)
        body = json.dumps(item.body, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "Content-Length": str(len(body)),
                   "Accept": "application/json",
                   "Connection": "close",
                   IDENTITY_HEADER: self._identity,
                   IDEMPOTENCY_HEADER: item.key}
        try:
            status, raw = self._transport("POST", item.path, headers, body,
                                          self._timeout)
        except Exception:               # no text is kept: see the module note
            return Response(None)
        return _response(item.kind, status, raw, self._identity)


    def post_acquisition(self, key, body_bytes, timeout=None):
        """POST stored acquisition bytes exactly as they are to `/v1/leases` (card L6a-2: `reporter admit`).
        -> (status or None, the answer as a dict or None, the closed error word or None). Never raises, keeps no text
        from a fault, and treats an answer that echoes the identity as unreadable."""
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body_bytes)),
                   "Accept": "application/json", "Connection": "close",
                   IDENTITY_HEADER: self._identity, IDEMPOTENCY_HEADER: key}
        try:
            status, raw = self._transport("POST", "/v1/leases", headers, body_bytes,
                                          self._timeout if timeout is None else min(self._timeout, timeout))
        except Exception:               # no text is kept: see the module note
            return None, None, None
        if type(status) is not int or isinstance(status, bool):
            return None, None, None
        if not raw or len(raw) > MAX_ANSWER_BYTES:
            return status, None, None
        try:
            doc = json.loads(raw.decode("utf-8"))
        except Exception:               # ValueError, UnicodeError, RecursionError...
            return status, None, None
        if not isinstance(doc, dict) or (self._identity and self._identity in json.dumps(doc)):
            return status, None, None
        error = doc.get("error")
        code = decisions.closed_code(error.get("code")) if isinstance(error, dict) else None
        return status, doc, code


LIMIT_PATH = "/v1/limit-events"


def _numeric(host):
    """The address to connect to when the resolver need not be asked: `localhost` is IPv4 loopback (Leasewise serves
    there only) and an IP address is itself. None for a host name."""
    if host == "localhost":
        return "127.0.0.1"
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def _allowed(kind, path):
    """The kind and the path are checked as a PAIR (card 7c-2): a limit row may be sent to exactly
    LIMIT_PATH and to nothing else; a receipt row (card L6a-1) may be sent to exactly
    /v1/leases/<uuid>/settle and to nothing else; every other kind keeps the lease rule, which never
    allows LIMIT_PATH. Nothing else can reach it."""
    if kind == decisions.LIMIT:
        return path == LIMIT_PATH
    if kind == decisions.RECEIPT:
        # Card L6a-1: a receipt row settles one lease and can go nowhere else.
        return _settle_path(path)
    return _safe_path(path)


def _settle_path(path):
    parts = path.split("/")
    return len(parts) == 5 and parts[:3] == ["", "v1", "leases"] and parts[4] == "settle" \
        and _UUID.fullmatch(parts[3]) is not None


def _safe_path(path):
    parts = path.split("/")
    if parts[:2] != ["", "v1"] or parts[2:3] != ["leases"]:
        return False
    if len(parts) == 3:
        return True
    return len(parts) == 5 and parts[4] == "settle" \
        and _UUID.fullmatch(parts[3]) is not None


def _unreadable(status):
    """What an answer we cannot use (no body, a body over the cap, text that does
    not decode, a value that echoes the secret) decides, by status:
      2xx          -> no answer at all: retried with the stored bytes (a replay
                      returns the stored answer, so retrying is safe);
      401, 429     -> kept as they are: retried as always;
      5xx          -> retried, marked as unreadable;
      other 4xx    -> the permanent handling, with no retry loop."""
    if 200 <= status < 300:
        return Response(None, MALFORMED_RESPONSE)
    if status == 401 or status == 429:
        return Response(status, None)
    return Response(status, MALFORMED_RESPONSE if status >= 500 else None)


def _response(kind, status, raw, identity=""):
    if type(status) is not int or isinstance(status, bool):
        return Response(None)
    if not raw or len(raw) > MAX_ANSWER_BYTES:
        return _unreadable(status)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except Exception:                   # ValueError, UnicodeError, RecursionError...
        return _unreadable(status)
    if not isinstance(doc, dict):
        # Only a JSON object is an answer: null, a list, a number, true/false
        # or a string is as unusable as bytes that do not decode.
        return _unreadable(status)
    code, lease_id = None, None
    error = doc.get("error")
    if isinstance(error, dict):
        # Only Leasewise's own words: anything else is UNKNOWN_CODE.
        code = decisions.closed_code(error.get("code"))
    if kind in (decisions.LIMIT, decisions.RECEIPT):
        # A limit or receipt answer is its status and its error word, nothing else: no field of
        # the body (block, retry time, state, released or retained money) is read, kept or shown.
        return Response(status, code)
    value = doc.get("lease_id")
    if isinstance(value, str):
        if identity and identity in value:
            # A returned value that holds the secret is never stored or
            # used to build a request: the whole answer is unreadable.
            return _unreadable(status)
        if _UUID.fullmatch(value):
            lease_id = value
    if status in (200, 201) and kind == decisions.ACQUIRE and lease_id is None:
        # Never mark an acquisition acquired without the lease it names:
        # ask again with the same bytes (a replay answers with the same id).
        return Response(None, BAD_ANSWER)
    return Response(status, code, lease_id)
