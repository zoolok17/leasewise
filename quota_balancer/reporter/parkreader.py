"""The strict reader for agenttalk's park markers (card 7c-2; DESIGN-7C2.md section 3).

agenttalk keeps one small file per parked seat, `state/usage-limit-park/<agent>.json`, and documents
it as a file other programs may read (its README, "Reading the park marker from another program").
This reader takes the documented contract literally and trusts nothing else:

  - a bounded folder listing, bounded files, one flat JSON object with exactly the documented keys;
  - no duplicate keys, no `NaN` or `Infinity`, whole numbers only (a boolean is not a number);
  - every value checked for type, length and range.

Every file ends in one of three outcomes, and a file is judged again on the next scan:

  OK          the file is a valid version-1 Claude marker (a `Marker` comes back);
  READ_LATER  the file is missing, cannot be opened (a sharing violation on Windows), does not parse
              (agenttalk may write it in place on Windows, so a reader can see half a file) or lacks
              a documented key: read it again on the next scan; it is never "no park" and never "a park";
  REFUSED     it parsed but breaks a rule (a key that is not documented, a wrong type, a value out of
              range, another version, another provider...): no event.

Nothing here keeps text from a file or an exception, and the agent name, the message id and the
wrapper generation are checked and then dropped: they never leave this module.
"""

import datetime
import json
import os
import re

from .events import _NAME
from .mapper import LIMIT_WINDOWS

OK = "ok"
READ_LATER = "read_later"
REFUSED = "refused"

PARK_FOLDER = ("state", "usage-limit-park")
MAX_ENTRIES = 256
MAX_BYTES = 4096
SCHEMA_VERSION = 1
PROVIDERS = ("claude",)
WINDOWS = LIMIT_WINDOWS                  # ONE list, shared with the queue's validator (mapper)
MARKER_STATE = "usage_limit_parked"
STALE_SECONDS = 300                       # agenttalk's MARKER_STALE_SECONDS
MAX_EPOCH = 4_102_444_800                 # 2100-01-01: no marker time is later than this
MAX_RESET_AHEAD_SECONDS = 9 * 86400       # agenttalk keeps a reset at most 8 days ahead; one day of slack
WAKE_AFTER_RESET_SECONDS = 30             # the wrapper's try-again time is the reset plus this
MAX_UPDATED_AHEAD_SECONDS = 60            # a clock a little ahead of ours is fine; a lot is not
MAX_PARKED_AHEAD_MS = 30_000

KEYS = frozenset({"schema_version", "agent", "state", "provider", "window", "reset_epoch", "wake_epoch",
                  "message_id", "parked_at", "wrapper_generation", "updated_at_epoch"})
_REPARSE_POINT = 0x400
_TEXT = re.compile(r"[0-9A-Za-z._:-]{1,64}")
_PARKED_AT = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?Z")


class Marker:
    """What the rest of the reporter may know about a marker: nothing else is kept."""

    __slots__ = ("provider", "window", "reset_epoch", "updated_at_epoch", "parked_at_ms")

    def __init__(self, provider, window, reset_epoch, updated_at_epoch, parked_at_ms):
        self.provider = provider
        self.window = window                  # None when the file says nothing
        self.reset_epoch = reset_epoch        # whole seconds, or None
        self.updated_at_epoch = updated_at_epoch
        self.parked_at_ms = parked_at_ms      # epoch milliseconds, or None

    def __repr__(self):
        return "Marker(<hidden>)"


class Scan:
    """The result of looking at one folder."""

    __slots__ = ("markers", "read_later", "refused", "too_many_entries", "folder_unreadable")

    def __init__(self):
        self.markers = []
        self.read_later = 0
        self.refused = 0
        self.too_many_entries = False         # a flag, never the size of the folder
        self.folder_unreadable = False


class _Break(Exception):
    """A rule of the contract is broken (not a torn read)."""


def _pairs(items):
    keys = [k for k, _ in items]
    if len(keys) != len(set(keys)):
        raise _Break
    return dict(items)


def _constant(_name):
    raise _Break                              # NaN, Infinity, -Infinity


def _is_link(entry):
    if entry.is_symlink():
        return True
    try:
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & _REPARSE_POINT)  # a junction or another reparse point (Windows)


def _whole(value, low, high):
    return type(value) is int and low <= value <= high


def parked_at_ms(text, now_ms):
    """The epoch milliseconds of an agenttalk time (`YYYY-MM-DDTHH:MM:SS[.ffffff]Z`), counted down,
    never rounded up; None if it is not a real time or lies more than 30 s ahead of the clock."""
    found = _PARKED_AT.fullmatch(text)
    if found is None:
        return None
    year, month, day, hour, minute, second = (int(found.group(i)) for i in range(1, 7))
    try:
        moment = datetime.datetime(year, month, day, hour, minute, second, tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    fraction = (found.group(7) or "").ljust(6, "0")
    millis = int(moment.timestamp()) * 1000 + int(fraction) // 1000
    if millis < 1 or millis > now_ms + MAX_PARKED_AHEAD_MS:
        return None
    return millis


def _check(data, stem, now_ms):
    """-> (OK, Marker) or (READ_LATER|REFUSED, None) for a parsed document."""
    if type(data) is not dict:
        return REFUSED, None
    keys = set(data)
    if keys - KEYS:
        return REFUSED, None
    if KEYS - keys:
        return READ_LATER, None               # a documented key is missing: read it again later
    if any(type(v) in (dict, list) for v in data.values()):
        return REFUSED, None                  # the marker is flat
    now_s = now_ms // 1000
    if data["schema_version"] is True or not _whole(data["schema_version"], SCHEMA_VERSION, SCHEMA_VERSION):
        return REFUSED, None
    if data["provider"] not in PROVIDERS or type(data["provider"]) is not str:
        return REFUSED, None
    if data["state"] != MARKER_STATE or type(data["agent"]) is not str or data["agent"] != stem:
        return REFUSED, None
    window = data["window"]
    if window is not None and (type(window) is not str or window not in WINDOWS):
        return REFUSED, None
    reset = data["reset_epoch"]
    if reset is not None and not _whole(reset, 1, min(MAX_EPOCH, now_s + MAX_RESET_AHEAD_SECONDS)):
        return REFUSED, None
    wake = data["wake_epoch"]
    if wake is not None and not _whole(
            wake, 1, min(MAX_EPOCH, now_s + MAX_RESET_AHEAD_SECONDS + WAKE_AFTER_RESET_SECONDS)):
        return REFUSED, None                  # the reset's range plus the wake offset; checked, never sent
    for key in ("message_id",):
        if type(data[key]) is not str or _TEXT.fullmatch(data[key]) is None:
            return REFUSED, None
    generation = data["wrapper_generation"]
    if generation is not None and (type(generation) is not str or _TEXT.fullmatch(generation) is None):
        return REFUSED, None
    if not _whole(data["updated_at_epoch"], 1, min(MAX_EPOCH, now_s + MAX_UPDATED_AHEAD_SECONDS)):
        return REFUSED, None
    parked = data["parked_at"]
    parked_ms = None
    if parked is not None:
        if type(parked) is not str:
            return REFUSED, None
        parked_ms = parked_at_ms(parked, now_ms)
        if parked_ms is None:
            return REFUSED, None
    return OK, Marker(data["provider"], window, reset, data["updated_at_epoch"], parked_ms)


def read_marker(path, stem, now_ms):
    """-> (outcome, Marker or None) for one file. Never raises; keeps no text."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError:                           # missing, a sharing violation, anything about the open
        return READ_LATER, None
    if len(raw) > MAX_BYTES:
        return REFUSED, None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:                # half a character: a torn read
        return READ_LATER, None
    try:
        data = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except ValueError:                        # half a document, or none: a torn read
        return READ_LATER, None
    except (_Break, RecursionError):
        return REFUSED, None
    try:
        return _check(data, stem, now_ms)
    except Exception:                         # a bug here must not become an event
        return REFUSED, None


def scan(bus_root, now_ms):
    """Look at `<bus_root>/state/usage-limit-park/` once. -> Scan. A folder that does not exist means
    nobody is parked (not an error); one that cannot be listed is `folder_unreadable`.

    The walk is bounded BEFORE anything is collected: at most MAX_ENTRIES directory entries are
    visited, the ignored ones counting too, then the names kept are sorted and read. One more entry is
    looked at only to learn that there is more (`too_many_entries`); the folder is never counted. So in
    a folder with more than MAX_ENTRIES entries some markers may not be seen in that scan."""
    result = Scan()
    folder = os.path.join(bus_root, *PARK_FOLDER)
    names = []
    visited = 0
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if visited == MAX_ENTRIES:
                    result.too_many_entries = True
                    break
                visited += 1
                name = entry.name
                if not name.endswith(".json") or _NAME.fullmatch(name[:-5]) is None:
                    continue
                try:
                    if _is_link(entry) or not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    result.read_later += 1
                    continue
                names.append(name)
    except FileNotFoundError:
        return result
    except OSError:
        result.folder_unreadable = True
        return result
    names.sort()
    for name in names:
        outcome, marker = read_marker(os.path.join(folder, name), name[:-5], now_ms)
        if outcome == OK:
            result.markers.append(marker)
        elif outcome == READ_LATER:
            result.read_later += 1
        else:
            result.refused += 1
    return result
