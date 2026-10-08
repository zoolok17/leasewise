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

import os
import re

from . import strictfile
from .events import _NAME
from .mapper import LIMIT_WINDOWS

# The generic parts live in `strictfile` (card 7b); the old names stay here, so nothing else changes.
OK = strictfile.OK
READ_LATER = strictfile.READ_LATER
REFUSED = strictfile.REFUSED

PARK_FOLDER = ("state", "usage-limit-park")
MAX_ENTRIES = 256
MAX_BYTES = 4096
SCHEMA_VERSION = 1
PROVIDERS = ("claude",)
WINDOWS = LIMIT_WINDOWS                  # ONE list, shared with the queue's validator (mapper)
MARKER_STATE = "usage_limit_parked"
STALE_SECONDS = 300                       # agenttalk's MARKER_STALE_SECONDS
MAX_EPOCH = strictfile.MAX_EPOCH          # 2100-01-01: no marker time is later than this
MAX_RESET_AHEAD_SECONDS = 9 * 86400       # agenttalk keeps a reset at most 8 days ahead; one day of slack
WAKE_AFTER_RESET_SECONDS = 30             # the wrapper's try-again time is the reset plus this
MAX_UPDATED_AHEAD_SECONDS = 60            # a clock a little ahead of ours is fine; a lot is not
MAX_PARKED_AHEAD_MS = strictfile.MAX_PARKED_AHEAD_MS

KEYS = frozenset({"schema_version", "agent", "state", "provider", "window", "reset_epoch", "wake_epoch",
                  "message_id", "parked_at", "wrapper_generation", "updated_at_epoch"})
_TEXT = re.compile(r"[0-9A-Za-z._:-]{1,64}")
_PARKED_AT = strictfile.TIME_RE


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


_Break = strictfile.Break
_pairs = strictfile.pairs
_constant = strictfile.constant
_is_link = strictfile.is_link
_whole = strictfile.whole
parked_at_ms = strictfile.parked_at_ms


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
    outcome, data = strictfile.load_json(path, MAX_BYTES)
    if outcome != OK:
        return outcome, None
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
    found = strictfile.walk(folder, ".json", lambda stem: _NAME.fullmatch(stem) is not None, MAX_ENTRIES)
    result.read_later += found.read_later
    result.too_many_entries = found.too_many
    result.folder_unreadable = found.unreadable
    for name in found.names:
        outcome, marker = read_marker(os.path.join(folder, name), name[:-5], now_ms)
        if outcome == OK:
            result.markers.append(marker)
        elif outcome == READ_LATER:
            result.read_later += 1
        else:
            result.refused += 1
    return result
