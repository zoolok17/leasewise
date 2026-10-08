"""The generic parts of a strict reader for a file another program writes (cards 7c-2 and 7b).

agenttalk keeps small JSON files that other programs may read: the park marker (card 7c-2) and the
capacity file of each seat (card 7b). Both readers take the documented contract literally and share
what does not depend on the file:

  - the three outcomes of one file: OK, READ_LATER (missing, cannot be opened, half a file: look again
    on the next scan) and REFUSED (it parsed but breaks a rule);
  - a bounded, binary read; one JSON object; no duplicate keys; no `NaN` or `Infinity`;
  - whole numbers only (a boolean is not a number); agenttalk times `YYYY-MM-DDTHH:MM:SS[.ffffff]Z`;
  - a bounded folder walk that skips links and keeps no text from a file or an exception.

Nothing here knows a file's keys. The file-specific rules stay in `parkreader` and `capacityreader`.
"""

import datetime
import json
import os
import re

OK = "ok"
READ_LATER = "read_later"
REFUSED = "refused"

MAX_EPOCH = 4_102_444_800                 # 2100-01-01: no time in these files is later than this
MAX_PARKED_AHEAD_MS = 30_000
REPARSE_POINT = 0x400
TIME_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,6}))?Z")


class Break(Exception):
    """A rule of the contract is broken (not a torn read)."""


def pairs(items):
    keys = [k for k, _ in items]
    if len(keys) != len(set(keys)):
        raise Break
    return dict(items)


def constant(_name):
    raise Break                              # NaN, Infinity, -Infinity


def is_link(entry):
    if entry.is_symlink():
        return True
    try:
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & REPARSE_POINT)  # a junction or another reparse point (Windows)


def whole(value, low, high):
    return type(value) is int and low <= value <= high


def parked_at_ms(text, now_ms):
    """The epoch milliseconds of an agenttalk time (`YYYY-MM-DDTHH:MM:SS[.ffffff]Z`), counted down,
    never rounded up; None if it is not a real time or lies more than 30 s ahead of the clock."""
    found = TIME_RE.fullmatch(text)
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


def load_json(path, max_bytes):
    """-> (OK, the parsed document) or (READ_LATER | REFUSED, None). Never raises; keeps no text.

    READ_LATER: the file cannot be opened, is not whole UTF-8, or does not parse (agenttalk may write
    a file in place on Windows, so a reader can see half of it). REFUSED: it is larger than
    `max_bytes`, repeats a key, holds `NaN` or `Infinity`, or nests so deeply that parsing fails."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError:                           # missing, a sharing violation, anything about the open
        return READ_LATER, None
    if len(raw) > max_bytes:
        return REFUSED, None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:                # half a character: a torn read
        return READ_LATER, None
    try:
        return OK, json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except ValueError:                        # half a document, or none: a torn read
        return READ_LATER, None
    except (Break, RecursionError):
        return REFUSED, None


class Walk:
    """The result of looking at one folder: the file names to read (sorted) and what went wrong."""

    __slots__ = ("names", "read_later", "too_many", "unreadable")

    def __init__(self):
        self.names = []
        self.read_later = 0
        self.too_many = False                 # a flag, never the size of the folder
        self.unreadable = False


def walk(folder, suffix, stem_ok, max_entries, max_files=None):
    """Look at `folder` once. A name counts when it ends with `suffix`, its stem passes `stem_ok`, and it is
    a regular file that is not a link. A folder that does not exist means nobody wrote anything (not an
    error); one that cannot be listed is `unreadable`.

    The walk is bounded BEFORE anything is collected: at most `max_entries` directory entries are visited,
    the ignored ones counting too; one more entry is looked at only to learn that there is more
    (`too_many`). `max_files`, when given, then keeps the first files by name and sets `too_many` if more
    were found. The folder is never counted."""
    result = Walk()
    visited = 0
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if visited == max_entries:
                    result.too_many = True
                    break
                visited += 1
                name = entry.name
                if not name.endswith(suffix) or not stem_ok(name[:-len(suffix)]):
                    continue
                try:
                    if is_link(entry) or not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    result.read_later += 1
                    continue
                result.names.append(name)
    except FileNotFoundError:
        return result
    except OSError:
        result.unreadable = True
        result.names = []
        return result
    result.names.sort()
    if max_files is not None and len(result.names) > max_files:
        result.names = result.names[:max_files]
        result.too_many = True
    return result
