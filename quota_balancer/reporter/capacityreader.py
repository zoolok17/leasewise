"""The strict reader for agenttalk's capacity files (card 7b; DESIGN-7B2.md section 1).

agenttalk keeps one small file per seat, `state/<seat>.capacity.json`, and lists the fields other
programs may rely on (its README, "Capacity"). This reader takes that list literally and trusts nothing else:

  - a bounded folder listing, bounded files, one flat JSON object;
  - no duplicate keys, no `NaN`, `Infinity` or a number that overflows to one, no nested value anywhere;
  - every listed key present (the writer always writes all of them), every value checked for type, word,
    length and range; `null` is normal where the writer can write it.

Every file ends in one of three outcomes, and a file is judged again on the next scan:

  OK          a valid version-2 file (a `Capacity` comes back);
  READ_LATER  missing, cannot be opened, not whole UTF-8 or not whole JSON (agenttalk may write a file in
              place on Windows, so a reader can see half of it): look again on the next scan;
  REFUSED     it parsed but breaks a rule (a missing key, a wrong type, a word or number out of range...).

A file that is only *empty* is not broken: an unknown snapshot, a stale one and an unbound refresh come
back OK. What to do with them is decided in `readings.select` (section 1.5), after this module has
checked the file's shape; `verdict` gives the first two steps of that order.

Nothing here keeps text from a file or from an exception. The account text is held in memory for one scan
and leaves this module only to be compared with the operator's table; no other text survives.
"""

import datetime
import math
import os
import re

from . import strictfile
from .events import _NAME
from .strictfile import OK, READ_LATER, REFUSED

CAPACITY_SUFFIX = ".capacity.json"
STATE_FOLDER = ("state",)
MAX_BYTES = 16_384
MAX_ENTRIES = 4096
MAX_FILES = 64
SCHEMA_VERSION = 2
MAX_EPOCH = strictfile.MAX_EPOCH
MAX_PERCENT = 1000
MAX_MINUTES = 100_000
MAX_ACCOUNT = 160
MAX_SOURCE_AGENT = 128
MAX_WORD = 64

SOURCES = ("claude_stream", "claude_statusline", "codex_rollout", "unknown")
CONFIDENCES = ("observed", "stale", "unknown")
STATUSES = ("allowed", "allowed_warning", "rejected")
BASES = ("measured", "assumed")

# The README's list (27 keys); the writer always writes every one of them.
LISTED_KEYS = frozenset({
    "schema_version", "source_agent", "source", "observed_at", "confidence", "reason", "scope", "account",
    "primary_used_percent", "primary_resets_at", "primary_window_minutes", "primary_window_basis",
    "primary_status", "primary_observed_at",
    "secondary_used_percent", "secondary_resets_at", "secondary_window_minutes", "secondary_window_basis",
    "secondary_status", "secondary_observed_at",
    "last_status", "last_status_window", "last_status_at", "rate_limit_reached_type",
    "context_used_percent", "context_window_size", "context_tokens"})
# Not in the README's list ("may change"); the real files carry them, so they are tolerated and ignored.
TOLERATED_KEYS = frozenset({"plan_type", "limit_id"})

NO_READING = "no_reading"
SOURCE_NOT_ACCEPTED = "source_not_accepted"

_TIME_SHAPE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


class Window:
    """What the rest of the reporter may know about one window of a file: numbers and a time, no text."""

    __slots__ = ("position", "used_percent", "resets_at", "window_minutes", "basis", "observed_ms")

    def __init__(self, position, used_percent, resets_at, window_minutes, basis, observed_ms):
        self.position = position              # "primary" or "secondary"
        self.used_percent = used_percent      # a finite number, or None
        self.resets_at = resets_at            # whole epoch seconds, or None
        self.window_minutes = window_minutes  # whole minutes, or None
        self.basis = basis                    # "measured", "assumed" or None
        self.observed_ms = observed_ms        # epoch milliseconds, or None when no usable time exists

    def __repr__(self):
        return "Window(<hidden>)"


class Capacity:
    """A valid version-2 file, reduced to what selection needs. The account text is hidden from repr."""

    __slots__ = ("stem", "source", "confidence", "account", "windows")

    def __init__(self, stem, source, confidence, account, windows):
        self.stem = stem                      # the file name's stem; only for a deterministic tie-break
        self.source = source
        self.confidence = confidence
        self.account = account                # text or None; compared with the table, never kept elsewhere
        self.windows = windows                # (primary, secondary)

    def __repr__(self):
        return "Capacity(<hidden>)"

    __str__ = __repr__


class Scan:
    """The result of looking at the folder once."""

    __slots__ = ("files", "read_later", "refused", "too_many_entries", "folder_unreadable")

    def __init__(self):
        self.files = []
        self.read_later = 0
        self.refused = 0
        self.too_many_entries = False         # a flag, never the size of the folder
        self.folder_unreadable = False


def _text(value, limit, nullable=False):
    if value is None:
        return nullable
    return type(value) is str and 0 < len(value) <= limit


def _word(value, words, nullable=False):
    if value is None:
        return nullable
    return type(value) is str and value in words


def _number(value, low, high, nullable=True):
    if value is None:
        return nullable
    if type(value) is not int and type(value) is not float:
        return False                          # a boolean is not a number
    return math.isfinite(value) and low <= value <= high


def _any_number(value):
    if value is None:
        return True
    if type(value) is not int and type(value) is not float:
        return False
    return math.isfinite(value)


def _time_text(value, nullable=True):
    if value is None:
        return nullable
    return type(value) is str and len(value) <= MAX_WORD


def _flat(value):
    if type(value) is float:
        return math.isfinite(value)
    return value is None or type(value) in (bool, int, str)


def _real_moment(text):
    """True if `text` is an agenttalk time that names a real calendar day and clock time. Validity only: an old or a
    future moment is fine here (freshness is judged elsewhere, and only for the figures)."""
    found = _TIME_SHAPE.fullmatch(text)
    if found is None:
        return False
    year, month, day, hour, minute, second = (int(part) for part in re.split(r"[-T:Z]", text[:-1]))
    try:
        datetime.datetime(year, month, day, hour, minute, second)
    except ValueError:
        return False
    return True


def _observed_ms(text, fallback_ms, now_ms):
    """A window's own time, else the file's. None if the text it comes from is not a usable time."""
    if text is None:
        return fallback_ms
    if _TIME_SHAPE.fullmatch(text) is None:
        return None
    return strictfile.parked_at_ms(text, now_ms)


def _check(data, stem, now_ms):
    """-> (OK, Capacity) or (REFUSED, None) for a parsed document."""
    if type(data) is not dict:
        return REFUSED, None
    keys = set(data)
    if LISTED_KEYS - keys:
        return REFUSED, None                  # a version-2 file without a documented key is never partly read
    if not all(_flat(value) for value in data.values()):
        return REFUSED, None                  # a list, an object, an infinite float: anywhere in the file
    if not strictfile.whole(data["schema_version"], SCHEMA_VERSION, SCHEMA_VERSION):
        return REFUSED, None
    agent = data["source_agent"]
    if not _text(agent, MAX_SOURCE_AGENT) or _NAME.fullmatch(agent) is None or agent != stem:
        return REFUSED, None
    if not _word(data["source"], SOURCES) or not _word(data["confidence"], CONFIDENCES):
        return REFUSED, None
    if data["scope"] != "account" or not _text(data["reason"], MAX_WORD, nullable=True):
        return REFUSED, None
    if not _text(data["account"], MAX_ACCOUNT, nullable=True):
        return REFUSED, None
    observed = data["observed_at"]
    if not _time_text(observed, nullable=False):
        return REFUSED, None
    for name in ("primary", "secondary"):
        if (not _number(data[name + "_used_percent"], 0, MAX_PERCENT)
                or not _whole_or_none(data[name + "_resets_at"], 1, MAX_EPOCH)
                or not _whole_or_none(data[name + "_window_minutes"], 1, MAX_MINUTES)
                or not _word(data[name + "_window_basis"], BASES, nullable=True)
                or not _word(data[name + "_status"], STATUSES, nullable=True)
                or not _time_text(data[name + "_observed_at"])):
            return REFUSED, None
    if (not _word(data["last_status"], STATUSES, nullable=True)
            or not _text(data["last_status_window"], MAX_WORD, nullable=True)
            or not _time_text(data["last_status_at"])
            or not _text(data["rate_limit_reached_type"], MAX_WORD, nullable=True)):
        return REFUSED, None
    if not all(_any_number(data[key]) for key in ("context_used_percent", "context_window_size", "context_tokens")):
        return REFUSED, None
    if data["last_status_at"] is not None and not _real_moment(data["last_status_at"]):
        return REFUSED, None                  # checked, then dropped: a time that is not a real moment is not the documented shape
    fallback_ms = _observed_ms(observed, None, now_ms)
    windows = tuple(
        Window(name, data[name + "_used_percent"], data[name + "_resets_at"], data[name + "_window_minutes"],
               data[name + "_window_basis"], _observed_ms(data[name + "_observed_at"], fallback_ms, now_ms))
        for name in ("primary", "secondary"))
    return OK, Capacity(stem, data["source"], data["confidence"], data["account"], windows)


def _whole_or_none(value, low, high):
    return value is None or strictfile.whole(value, low, high)


def read_capacity(path, stem, now_ms):
    """-> (outcome, Capacity or None) for one file. Never raises; keeps no text."""
    outcome, data = strictfile.load_json(path, MAX_BYTES)
    if outcome != OK:
        return outcome, None
    try:
        return _check(data, stem, now_ms)
    except Exception:                         # a bug here must not become a figure
        return REFUSED, None


def verdict(capacity):
    """Steps 1 and 2 of the decision order (DESIGN-7B2.md 1.5): `no_reading`, `source_not_accepted` or None."""
    if capacity.confidence == "unknown" or capacity.source == "unknown":
        return NO_READING
    if capacity.source == "claude_statusline":
        return SOURCE_NOT_ACCEPTED
    return None


def scan(bus_root, now_ms):
    """Look at `<bus_root>/state/` once for `*.capacity.json`. -> Scan. A folder that does not exist means
    nobody wrote anything (not an error); one that cannot be listed is `folder_unreadable`.

    At most MAX_ENTRIES directory entries are visited (the ignored ones count too: a bus folder holds many
    other files) and at most MAX_FILES capacity files, first by name, are read; more of either sets
    `too_many_entries` and the files read are still used."""
    result = Scan()
    folder = os.path.join(bus_root, *STATE_FOLDER)
    found = strictfile.walk(folder, CAPACITY_SUFFIX, lambda stem: _NAME.fullmatch(stem) is not None,
                            MAX_ENTRIES, MAX_FILES)
    result.read_later += found.read_later
    result.too_many_entries = found.too_many
    result.folder_unreadable = found.unreadable
    for name in found.names:
        outcome, capacity = read_capacity(os.path.join(folder, name), name[:-len(CAPACITY_SUFFIX)], now_ms)
        if outcome == OK:
            result.files.append(capacity)
        elif outcome == READ_LATER:
            result.read_later += 1
        else:
            result.refused += 1
    return result
