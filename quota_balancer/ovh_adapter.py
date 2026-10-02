"""Pure OVH billing adapter (card 2b-3, docs/DESIGN.md section 11).

`poll_once` turns ONE OVH "project usage, current" HTTP answer into either a
version-3 reading-import document with exactly one provider_billing reading,
or a closed failure. Everything it touches is injected: the credential, the
transport, the clock and the jitter. It reads no file, no environment
variable and no credential store, opens no connection, keeps no state, loops
never and retries never: exactly one transport call per invocation.

Output (nothing else, no messages, no raw bodies):
  success  {"document": <reading-import document>, "next_attempt_at": <ms>}
  failure  {"failure": <one of FAILURES>, "next_attempt_at": <ms>}

A caller bug (a bad jitter, alias, credential shape or clock) raises
QuotaError(INVALID_CONFIG) with the static message, before any transport
call; it is not a provider failure and never echoes the offending value.

Money is never a binary float: the body is parsed with
parse_float=Decimal and the total is ceil(priceInUcents / 100) in integer
arithmetic (1 EUR = 10^8 ucents = 10^6 micro-EUR).
"""

import collections
import dataclasses
import datetime
import hashlib
import json
import re
import ssl
import uuid
from collections.abc import Mapping
from decimal import Decimal, localcontext
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from .errors import CODE_INVALID_CONFIG, MSG_INVALID_CONFIG, QuotaError
from .reduce import FUTURE_SLOP_MS
from .validate import ALIAS_RE
from .validate_readings import (
    CURRENCY_EUR,
    DOC_VERSION_BILLING,
    INT64_MAX,
    KIND_PROVIDER_BILLING,
    _is_one_utc_month_period,
)

OVH_EU_BASE_URL = "https://eu.api.ovh.com/1.0"

# The closed failure enum (section 11).
FAILURE_AUTH = "auth"
FAILURE_TRANSPORT = "transport"
FAILURE_RATE_LIMIT = "rate_limit"
FAILURE_SCHEMA = "schema"
FAILURE_CURRENCY = "currency"
FAILURE_PERIOD = "period"
FAILURE_TLS = "tls"
FAILURES = (FAILURE_AUTH, FAILURE_TRANSPORT, FAILURE_RATE_LIMIT,
            FAILURE_SCHEMA, FAILURE_CURRENCY, FAILURE_PERIOD, FAILURE_TLS)

# The name-based namespace of the reading event ids: an unchanged poll gives
# a byte-identical document, which ingest treats as a replay.
EVENT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL,
                             "urn:leasewise:ovh-adapter:event-id:v1")

JITTER_MAX_SECONDS = 90
MAX_POLL_SECONDS = 7 * 86_400
# A Retry-After further out than this is ignored as implausible: it would
# silence the poller for days on one header.
MAX_RETRY_AFTER_SECONDS = 86_400
MAX_BODY_BYTES = 1_048_576
TIMEOUT_SECONDS = 30
AI_CATEGORY = "ai-endpoints"
# Money in a body is bounded far below int64 before any arithmetic.
_MAX_EUR = Decimal(10) ** 13
_UCENTS_PER_MICRO_EUR = 100
_MICRO_EUR = Decimal(10) ** -6

_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
_MS = datetime.timedelta(milliseconds=1)
_TIME_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,9}))?Z")
_PROJECT_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_PRINTABLE_RE = re.compile(r"[\x21-\x7e]{1,512}")

TransportResponse = collections.namedtuple(
    "TransportResponse", ("status", "headers", "body"))


class TlsError(Exception):
    """Raised by a transport whose TLS handshake or verification failed."""


@dataclasses.dataclass(frozen=True, repr=False)
class OvhCredential:
    """Application key, application secret, consumer key and the project
    id. Injected, never read from anywhere; its repr shows no value."""

    application_key: str
    application_secret: str
    consumer_key: str
    project_id: str

    def __repr__(self):
        return "OvhCredential(<redacted>)"


@dataclasses.dataclass(frozen=True)
class AdapterConfig:
    """The local aliases the document carries (operator-owned config)."""

    machine: str
    account: str
    scope_alias: str
    scope_revision: int


class _Failure(Exception):
    def __init__(self, failure):
        super().__init__(failure)
        self.failure = failure


def _invalid():
    raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)


def _is_int(value):
    return type(value) is int


def _is_alias(value):
    return isinstance(value, str) and ALIAS_RE.fullmatch(value) is not None


def sign_request(credential, url, timestamp):
    """The headers of OVH's documented signed GET with an empty body:
    X-Ovh-Signature = "$1$" + sha1(secret+consumer+GET+url+body+timestamp),
    each joined by "+". `timestamp` is epoch seconds."""
    stamp = str(timestamp)
    # SHA-1 is what the OVH signature scheme mandates, not a design choice.
    digest = hashlib.sha1("+".join((
        credential.application_secret, credential.consumer_key, "GET", url,
        "", stamp)).encode("utf-8")).hexdigest()
    return {
        "X-Ovh-Application": credential.application_key,
        "X-Ovh-Consumer": credential.consumer_key,
        "X-Ovh-Timestamp": stamp,
        "X-Ovh-Signature": "$1$" + digest,
    }


def _validate_inputs(credential, clock, jitter_seconds, config, poll_seconds,
                     base_url):
    if not isinstance(credential, OvhCredential):
        _invalid()
    for part in (credential.application_key, credential.application_secret,
                 credential.consumer_key):
        if not isinstance(part, str) or not _PRINTABLE_RE.fullmatch(part):
            _invalid()
    if (not isinstance(credential.project_id, str)
            or not _PROJECT_RE.fullmatch(credential.project_id)):
        _invalid()
    if not isinstance(config, AdapterConfig):
        _invalid()
    for alias in (config.machine, config.account, config.scope_alias):
        if not _is_alias(alias):
            _invalid()
    if not _is_int(config.scope_revision) or not (
            1 <= config.scope_revision <= INT64_MAX):
        _invalid()
    if not _is_int(jitter_seconds) or not (
            0 <= jitter_seconds <= JITTER_MAX_SECONDS):
        _invalid()
    if not _is_int(poll_seconds) or not 1 <= poll_seconds <= MAX_POLL_SECONDS:
        _invalid()
    if not isinstance(base_url, str):
        _invalid()
    if not callable(clock):
        _invalid()
    now_ms = clock()
    if not _is_int(now_ms) or not 0 <= now_ms <= INT64_MAX // 4:
        _invalid()
    return now_ms


def _get_header(headers, name):
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == name:
            return value
    return None


def _retry_after_ms(headers, now_ms):
    """Retry-After as an epoch-ms instant: delta-seconds or an HTTP-date, at
    most a day ahead; None when absent or invalid."""
    value = _get_header(headers, "retry-after")
    if not isinstance(value, str):
        return None
    text = value.strip(" \t")
    if not text:
        return None
    if text.isascii() and text.isdigit():
        if len(text) > 12 or int(text) > MAX_RETRY_AFTER_SECONDS:
            return None
        return now_ms + int(text) * 1000
    try:
        # An HTTP-date names its zone: a naive result raises TypeError here.
        when = parsedate_to_datetime(text)
        target = (when - _EPOCH) // _MS
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if target > now_ms + MAX_RETRY_AFTER_SECONDS * 1000:
        return None
    return target


def _no_constants(_name):
    raise ValueError("non-finite number")


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _parse_json(raw):
    if isinstance(raw, str):
        try:
            raw = raw.encode("utf-8")
        except UnicodeEncodeError:
            raise _Failure(FAILURE_SCHEMA)
    if len(raw) > MAX_BODY_BYTES:
        raise _Failure(FAILURE_SCHEMA)
    try:
        return json.loads(bytes(raw).decode("utf-8"), parse_float=Decimal,
                          parse_constant=_no_constants,
                          object_pairs_hook=_no_duplicates)
    except (ValueError, RecursionError):
        raise _Failure(FAILURE_SCHEMA)


def _schema():
    raise _Failure(FAILURE_SCHEMA)


def _timestamp_ms(text):
    """An ISO-8601 UTC instant ('...Z', optional fraction) as epoch ms;
    None when it is not exactly that."""
    if not isinstance(text, str):
        return None
    match = _TIME_RE.fullmatch(text)
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(g) for g in match.groups()[:6])
    fraction = (match.group(7) or "") + "000"
    try:
        when = datetime.datetime(year, month, day, hour, minute, second,
                                 tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return (when - _EPOCH) // _MS + int(fraction[:3])


def _money(value):
    """A JSON number as a non-negative Decimal euro amount; schema failure
    for anything else (bool, string, null, negative, absurdly large)."""
    if type(value) is int:
        value = Decimal(value)
    if not isinstance(value, Decimal) or not value.is_finite():
        _schema()
    if value < 0 or value >= _MAX_EUR:
        _schema()
    return value


def _require_empty_usage(section):
    """hourlyUsage/monthlyUsage: every array anywhere beneath it must be
    empty. A non-empty element has a shape no capture has shown, so the
    non-AI answer is unknown (schema), never false."""
    if not isinstance(section, dict):
        _schema()
    pending = [section]
    while pending:
        for value in pending.pop().values():
            if isinstance(value, dict):
                pending.append(value)
            elif not isinstance(value, list) or value:
                _schema()


def _read_usage(body, now_ms):
    """Validate one 200 body; return (observed_at, period_start,
    period_end, total_micro_eur, non_ai_spend_present)."""
    if not isinstance(body, dict):
        _schema()
    price = body.get("totalPrice")
    if not isinstance(price, dict):
        _schema()
    code = price.get("currencyCode")
    if not isinstance(code, str):
        _schema()
    if code != CURRENCY_EUR:
        raise _Failure(FAILURE_CURRENCY)

    ucents = price.get("priceInUcents")
    if not _is_int(ucents) or not 0 <= ucents <= INT64_MAX:
        _schema()
    total_micro = -(-ucents // _UCENTS_PER_MICRO_EUR)  # ceil, integers only

    value = _money(price.get("value"))
    categories = body.get("resourcesUsage")
    if not isinstance(categories, list):
        _schema()
    non_ai = False
    category_sum = Decimal(0)
    for entry in categories:
        if not isinstance(entry, dict) or not isinstance(
                entry.get("type"), str):
            _schema()
        amount = _money(entry.get("totalPrice"))
        category_sum += amount
        if entry["type"] != AI_CATEGORY and amount > 0:
            non_ai = True
    _require_empty_usage(body.get("hourlyUsage"))
    _require_empty_usage(body.get("monthlyUsage"))

    with localcontext() as ctx:
        ctx.prec = 80
        # priceInUcents is the money; value must agree within 1 micro-EUR,
        # and the categories must add up to value within 1 micro-EUR each.
        if abs(value * 100_000_000 - ucents) > _UCENTS_PER_MICRO_EUR:
            _schema()
        if abs(category_sum - value) > _MICRO_EUR * len(categories):
            _schema()

    observed_at = _timestamp_ms(body.get("lastUpdate"))
    period = body.get("period")
    if observed_at is None or not isinstance(period, dict):
        _schema()
    start = _timestamp_ms(period.get("from"))
    end = _timestamp_ms(period.get("to"))
    if start is None or end is None:
        _schema()
    if observed_at > now_ms + FUTURE_SLOP_MS:
        _schema()
    if not _is_one_utc_month_period(start, end):
        raise _Failure(FAILURE_PERIOD)
    if not start <= observed_at <= end:
        raise _Failure(FAILURE_PERIOD)
    return observed_at, start, end, total_micro, non_ai


def _document(config, usage):
    observed_at, start, end, total_micro, non_ai = usage
    canonical = json.dumps(
        [config.machine, config.account, config.scope_alias,
         config.scope_revision, start, observed_at, total_micro, non_ai],
        separators=(",", ":"))
    return {
        "version": DOC_VERSION_BILLING,
        "readings": [{
            "event_id": str(uuid.uuid5(EVENT_NAMESPACE, canonical)),
            "machine": config.machine,
            "account": config.account,
            "kind": KIND_PROVIDER_BILLING,
            "observed_at": observed_at,
            "payload": {
                "scope_alias": config.scope_alias,
                "scope_revision": config.scope_revision,
                "period_start": start,
                "period_end": end,
                "currency": CURRENCY_EUR,
                "total_micro_eur": total_micro,
                "non_ai_spend_present": non_ai,
            },
        }],
    }


def _is_invalid_signature(body):
    """The real OVH answer to a bad signature (HTTP 400)."""
    try:
        parsed = _parse_json(body)
    except _Failure:
        return False
    return (isinstance(parsed, dict)
            and parsed.get("class") == "Client::BadRequest"
            and parsed.get("message") == "Invalid signature")


def _classify(response, now_ms):
    """The usage tuple for a 200, else raise _Failure per the status map."""
    if (not isinstance(response, tuple) or len(response) != 3
            or not isinstance(response[1], Mapping)
            or not isinstance(response[2], (bytes, bytearray, str))):
        raise _Failure(FAILURE_TRANSPORT)
    status, _headers, body = response
    if not _is_int(status):
        _schema()
    if status == 200:
        return _read_usage(_parse_json(body), now_ms)
    if status == 400:
        if _is_invalid_signature(body):
            raise _Failure(FAILURE_AUTH)
        _schema()
    if status in (401, 403):
        raise _Failure(FAILURE_AUTH)
    if status == 429:
        raise _Failure(FAILURE_RATE_LIMIT)
    if 500 <= status <= 599:
        raise _Failure(FAILURE_TRANSPORT)
    _schema()


def poll_once(*, credential, transport, clock, jitter_seconds, config,
              poll_seconds=900, base_url=OVH_EU_BASE_URL):
    """One signed GET of project usage/current -> one result dict.

    credential: OvhCredential. transport: an object with a boolean
    `verifies_tls` attribute (it must be exactly True) and
    `get(url, headers, timeout_seconds)` returning a TransportResponse
    (status, headers, body); it may raise TlsError / ssl.SSLError for a TLS
    failure, anything else is a transport failure. clock: zero-argument
    callable returning epoch milliseconds. jitter_seconds: injected integer
    in [0, 90]. config: AdapterConfig. poll_seconds: positive integer from
    the billing policy.
    """
    now_ms = _validate_inputs(credential, clock, jitter_seconds, config,
                              poll_seconds, base_url)
    next_attempt = now_ms + poll_seconds * 1000 + jitter_seconds * 1000

    def failed(failure, retry_after=None):
        return {"failure": failure,
                "next_attempt_at": max(next_attempt, retry_after or 0)}

    # The transport contract: HTTPS only and verification on, refused
    # BEFORE any call. Fixtures test this refusal, not real certificates.
    parts = urlsplit(base_url)
    if parts.scheme.lower() != "https" or not parts.netloc:
        return failed(FAILURE_TLS)
    if getattr(transport, "verifies_tls", None) is not True:
        return failed(FAILURE_TLS)

    url = "%s/cloud/project/%s/usage/current" % (
        base_url.rstrip("/"), credential.project_id)
    headers = sign_request(credential, url, now_ms // 1000)
    headers["Accept"] = "application/json"
    try:
        response = transport.get(url, headers, TIMEOUT_SECONDS)
    except (TlsError, ssl.SSLError):
        return failed(FAILURE_TLS)
    except Exception:
        return failed(FAILURE_TRANSPORT)

    try:
        usage = _classify(response, now_ms)
    except _Failure as failure:
        retry_after = None
        if failure.failure == FAILURE_RATE_LIMIT:
            retry_after = _retry_after_ms(response[1], now_ms)
        return failed(failure.failure, retry_after)
    return {"document": _document(config, usage),
            "next_attempt_at": next_attempt}
