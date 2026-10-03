"""The receipt source adapter (card L6a-1): the ONE place that knows how a gateway's receipts reach us.

A gateway keeps one final receipt for every quota lease it served and hands them out in PAGES (DESIGN-6A 5.4). This
module reads one page from its source and hands back a checked, closed value, or says why it could not. In this
version the only source is a fixture file (the tests, and the only source until agenttalk ships its receipt command):
either a whole ledger document `{"generation": ..., "receipts": [...]}`, from which the page after the asked-for number
is made, or an already made page, served as it is. A command source (a program the reporter would run) is NOT part of
this version: it needs its own card (L6a-1b), because the program must be started already contained.

A page is one JSON object with exactly these keys: `envelope_version` (1), `generation` (the ledger's own
identity, even when the page is empty), `after_seq` (the number asked for), `next_seq`, `has_more`, `receipts`.
A receipt has exactly the keys of `KEYS`; its token counts are whole numbers or null (unknown), never 0 by
guesswork. Numbers rise by exactly one from `after_seq`: a gap or a repeat is damage.

Nothing from a receipt that is not one of the closed fields below, and nothing from a fault's text, is ever kept. The page format is a PROPOSAL until the gateway side answers (DESIGN-6A section 11);
if it changes, only this file changes.

An answer is one of three words (`ReadResult.status`):
  ok          a checked page (possibly with no receipts);
  read_later  the source could not be read just now (a missing file, text that is not JSON): nothing is stored and it
              is asked again;
  refused     it was read but is not what the contract allows (a wrong key, type, bound, version or order, a
              repeated key, too much): nothing from it is stored, and a counter says so.
"""

import datetime
import json
import re

OK = "ok"
READ_LATER = "read_later"
REFUSED = "refused"

# The closed reasons, per status. These words (and counts of them) are all the status ever shows.
LATER_REASONS = ("source_missing", "not_json")
REFUSED_REASONS = ("not_an_object", "bad_version", "bad_envelope", "wrong_after", "too_many", "too_large",
                   "duplicate_key", "bad_row", "bad_order")

ENVELOPE_VERSION = 1
ENVELOPE_KEYS = frozenset({"envelope_version", "generation", "after_seq", "next_seq", "has_more", "receipts"})
# The closed fields of one receipt: the gateway's own columns, without the agent or the message (names stay local).
KEYS = frozenset({"seq", "quota_lease_ref_sha256", "outcome", "calls", "input_tokens", "output_tokens",
                  "actual_micro_eur", "closed_at"})
OUTCOMES = ("completed", "cancelled", "failed", "provider_limit")

MAX_ROWS = 1000                      # one page; the gateway pages its own list at 100
MAX_CALLS = 1_000_000
MAX_TOKENS = 10 ** 12
MAX_MICRO_EUR = 10 ** 12             # a million euros
INT64_MAX = 2 ** 63 - 1
_HEX64 = re.compile(r"[0-9a-f]{64}")
_GENERATION = re.compile(r"[0-9a-f]{32}")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")

DEFAULT_MAX_BYTES = 1024 * 1024


class Receipt:
    """One checked receipt, exactly as received (unknown counts stay None)."""

    __slots__ = ("seq", "ref_sha256", "outcome", "calls", "input_tokens", "output_tokens", "actual_micro_eur",
                 "closed_at")

    def __repr__(self):
        return "Receipt(<hidden>)"

    __str__ = __repr__


class Page:
    __slots__ = ("generation", "after_seq", "next_seq", "has_more", "receipts")

    def __repr__(self):
        return "Page(<hidden>)"

    __str__ = __repr__


class ReadResult:
    __slots__ = ("status", "reason", "page")

    def __init__(self, status, reason=None, page=None):
        self.status = status
        self.reason = reason
        self.page = page


class _Later(Exception):
    pass


class _Refused(Exception):
    pass


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def _count(value, high):
    """A token count: a whole number in range, or None (unknown)."""
    return value is None or _int(value, 0, high)


def _iso(text):
    if type(text) is not str or _ISO.fullmatch(text) is None:
        return False
    try:
        datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return False
    return True


def _row(doc):
    """One receipt from its JSON object, or _Refused. Closed keys, exact types, bounds."""
    if type(doc) is not dict or frozenset(doc) != KEYS:
        raise _Refused("bad_row")
    if not (_int(doc["seq"], 1, INT64_MAX) and _int(doc["calls"], 0, MAX_CALLS)
            and _count(doc["input_tokens"], MAX_TOKENS) and _count(doc["output_tokens"], MAX_TOKENS)
            and _int(doc["actual_micro_eur"], 0, MAX_MICRO_EUR) and _iso(doc["closed_at"])):
        raise _Refused("bad_row")
    if not (type(doc["quota_lease_ref_sha256"]) is str and _HEX64.fullmatch(doc["quota_lease_ref_sha256"])
            and type(doc["outcome"]) is str and doc["outcome"] in OUTCOMES):
        raise _Refused("bad_row")
    if doc["calls"] == 0 and (doc["input_tokens"] != 0 or doc["output_tokens"] != 0 or doc["actual_micro_eur"] != 0):
        raise _Refused("bad_row")        # no call: both counts and the money must be the number 0 (null is not 0)
    row = Receipt()
    row.seq, row.ref_sha256, row.outcome = doc["seq"], doc["quota_lease_ref_sha256"], doc["outcome"]
    row.calls, row.input_tokens, row.output_tokens = doc["calls"], doc["input_tokens"], doc["output_tokens"]
    row.actual_micro_eur, row.closed_at = doc["actual_micro_eur"], doc["closed_at"]
    return row


def _no_repeats(pairs):
    keys = [key for key, _value in pairs]
    if len(set(keys)) != len(keys):
        raise _Refused("duplicate_key")
    return dict(pairs)


def _no_constant(_name):
    raise _Refused("bad_row")            # NaN and the infinities are not numbers a receipt may carry


def _loads(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_no_repeats, parse_constant=_no_constant)
    except _Refused:
        raise
    except (ValueError, RecursionError):         # UnicodeError is a ValueError
        raise _Later("not_json") from None


def parse_page(raw, after_seq, limit=MAX_ROWS):
    """The bytes a source gave -> a Page, or _Later/_Refused. Every rule of the page is checked here, so a page that
    is stored is a page that was whole: the version, the closed keys, the generation, the asked-for number, the
    receipts, and that the numbers rise by exactly one. `limit` is the page length that was asked for: a longer page
    is refused before anything is read from it."""
    doc = _loads(raw)
    if type(doc) is not dict:
        raise _Refused("not_an_object")
    if "envelope_version" in doc and (type(doc["envelope_version"]) is not int
                                      or doc["envelope_version"] != ENVELOPE_VERSION):
        raise _Refused("bad_version")
    if frozenset(doc) != ENVELOPE_KEYS:
        raise _Refused("bad_envelope" if "envelope_version" in doc else "bad_version")
    if not (type(doc["generation"]) is str and _GENERATION.fullmatch(doc["generation"])
            and _int(doc["after_seq"], 0, INT64_MAX) and _int(doc["next_seq"], 0, INT64_MAX)
            and type(doc["has_more"]) is bool and type(doc["receipts"]) is list):
        raise _Refused("bad_envelope")
    if doc["after_seq"] != after_seq:
        raise _Refused("wrong_after")
    if len(doc["receipts"]) > min(limit, MAX_ROWS):
        raise _Refused("too_many")
    rows = [_row(item) for item in doc["receipts"]]
    expected = after_seq
    for row in rows:
        if row.seq != expected + 1:
            raise _Refused("bad_order")      # a gap or a repeat
        expected = row.seq
    if doc["next_seq"] != expected or (doc["has_more"] and not rows):
        raise _Refused("bad_envelope")
    page = Page()
    page.generation, page.after_seq, page.next_seq = doc["generation"], after_seq, expected
    page.has_more, page.receipts = doc["has_more"], tuple(rows)
    return page


def _fixture_page(raw, after_seq, max_bytes, limit):
    """A fixture holds either a made page (served as it is) or a whole ledger `{"generation", "receipts"}`, from
    which the page after `after_seq` is made the way the gateway would make it."""
    doc = _loads(raw)
    if type(doc) is dict and frozenset(doc) == {"generation", "receipts"} and type(doc["receipts"]) is list:
        rows = [item for item in doc["receipts"]
                if type(item) is not dict or type(item.get("seq")) is not int or item["seq"] > after_seq]
        shown = rows[:limit]
        last = shown[-1]["seq"] if shown and type(shown[-1]) is dict and type(shown[-1].get("seq")) is int \
            else after_seq
        doc = {"envelope_version": ENVELOPE_VERSION, "generation": doc["generation"], "after_seq": after_seq,
               "next_seq": last, "has_more": len(rows) > len(shown), "receipts": shown}
        raw = json.dumps(doc).encode("utf-8")
        if len(raw) > max_bytes:
            raise _Refused("too_large")
    return parse_page(raw, after_seq, limit)


def _read_fixture(path, max_bytes):
    try:
        with open(path, "rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError:
        raise _Later("source_missing") from None
    if len(raw) > max_bytes:
        raise _Refused("too_large")
    return raw


def read_page(source, after_seq):
    """One page of `source` after `after_seq` -> ReadResult. Never raises on bad input."""
    try:
        page = _fixture_page(_read_fixture(source.fixture, source.max_bytes), after_seq, source.max_bytes,
                             source.page_limit)
    except _Later as exc:
        return ReadResult(READ_LATER, exc.args[0])
    except _Refused as exc:
        return ReadResult(REFUSED, exc.args[0])
    return ReadResult(OK, None, page)
