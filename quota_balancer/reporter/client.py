"""The HTTP client (DESIGN-7A.md 4.5; card 7a-3).

One request at a time, a 5-second timeout on every network step, no
redirects, no proxy, one fresh connection per request. It turns every
outcome into a `decisions.Response`; what to do about it is `decisions.decide`.

Nothing here keeps or prints text from an exception: a network failure is
just "no answer". The identity travels only in the request header.
"""

import http.client
import json
import re

from . import decisions
from .decisions import Response

TIMEOUT_SECONDS = 5.0
MAX_ANSWER_BYTES = 1024 * 1024
IDENTITY_HEADER = "X-Machine-Identity"
IDEMPOTENCY_HEADER = "Idempotency-Key"
BAD_ANSWER = "BAD_ANSWER"           # a success that lacked the lease id

_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
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
        factory = (http.client.HTTPSConnection if self._scheme == "https"
                   else http.client.HTTPConnection)
        conn = factory(self._host, self._port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=headers)
            answer = conn.getresponse()
            return answer.status, answer.read(MAX_ANSWER_BYTES + 1)
        finally:
            conn.close()

    def send(self, item):
        """POST one outbox item; always returns a Response."""
        if not _safe_path(item.path):
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
        return _response(item.kind, status, raw)


def _safe_path(path):
    parts = path.split("/")
    if parts[:2] != ["", "v1"] or parts[2:3] != ["leases"]:
        return False
    if len(parts) == 3:
        return True
    return len(parts) == 5 and parts[4] == "settle" \
        and _UUID.fullmatch(parts[3]) is not None


def _response(kind, status, raw):
    if type(status) is not int or isinstance(status, bool):
        return Response(None)
    code, lease_id = None, None
    if raw and len(raw) <= MAX_ANSWER_BYTES:
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            doc = None
        if isinstance(doc, dict):
            error = doc.get("error")
            if isinstance(error, dict):
                value = error.get("code")
                if isinstance(value, str) and _CODE.fullmatch(value):
                    code = value
            value = doc.get("lease_id")
            if isinstance(value, str) and _UUID.fullmatch(value):
                lease_id = value
    if status in (200, 201) and kind == decisions.ACQUIRE and lease_id is None:
        # Never mark an acquisition acquired without the lease it names:
        # ask again with the same bytes (a replay answers with the same id).
        return Response(None, BAD_ANSWER)
    return Response(status, code, lease_id)
