"""Export a cleaned copy of this machine's Claude usage figures, labelled A, B or C (three-accounts step 0).

agenttalk keeps one small file per seat, `<state folder>/<seat>.capacity.json`, with the latest 5-hour and weekly
use of the subscription that seat runs under. Those files name the Windows user, so they never leave the machine.
This script reads them (read-only), looks each account up in a small local table of labels, and writes ONE small
file that holds only: the machine, the export time, and for each label and window the percentage used, the reset
time, the time it was observed, where it came from and how many seat files carried it.

    python export_capacity.py --state-dir <agenttalk state folder> --labels <labels.json> --machine desktop --out <file>

It is Python 3.11+ standard library only and changes nothing except `--out` (and a temporary file beside it).
It prints numbers only: never an account text, a path from a file, or a file's content.

Exit codes: 0 written; 2 refused (nothing written); 3 a problem with the arguments or the label table.
"""
import argparse
import calendar
import datetime
import getpass
import json
import math
import os
import re
import sys
import tempfile
import time

EXPORT_VERSION = 1
SCHEMA_VERSION = 2                     # the capacity-file layout this reads (agenttalk 0.99.0 and later)
SUFFIX = ".capacity.json"
MAX_FILES = 64                         # same bounds as the reporter's reader
MAX_BYTES = 16_384
MAX_ENTRIES = 4096
MAX_EPOCH = 4_102_444_800              # 2100-01-01: no time in these files is later than this
MAX_PERCENT = 1000                     # the reader allows more than 100 (over-use); a larger number is not a percentage
LABELS = ("A", "B", "C")
WINDOWS = ("five_hour", "seven_day")
SOURCES = ("claude_stream", "claude_statusline")   # the only sources whose figures are the subscription's own
CONFIDENCES = ("observed", "stale", "unknown")
WINDOW_FIELDS = {"five_hour": "primary", "seven_day": "secondary"}   # which fields of a file hold which window
ROW_KEYS = ("label", "window", "used_percent", "resets_at", "observed_at", "source", "seats")
COUNT_KEYS = ("files", "used", "old_format", "unknown_confidence", "unmapped", "other_source", "unreadable", "refused", "over_limit")
TOP_KEYS = ("export_version", "machine", "exported_at", "rows", "counts")
MACHINE_RE = re.compile(r"[a-z0-9-]{1,32}\Z")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
TIME_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z\Z", re.ASCII)

# The 27 keys agenttalk lists for a capacity file (kept equal to quota_balancer's reader by a test).
LISTED_KEYS = frozenset({
    "schema_version", "source_agent", "source", "observed_at", "confidence", "reason", "scope", "account",
    "primary_used_percent", "primary_resets_at", "primary_window_minutes", "primary_window_basis",
    "primary_status", "primary_observed_at",
    "secondary_used_percent", "secondary_resets_at", "secondary_window_minutes", "secondary_window_basis",
    "secondary_status", "secondary_observed_at",
    "last_status", "last_status_window", "last_status_at", "rate_limit_reached_type",
    "context_used_percent", "context_window_size", "context_tokens"})
TOLERATED_KEYS = frozenset({"plan_type", "limit_id"})   # real files carry them; they are never read

# Substrings that must not appear in anything written: an e-mail, a path, a config-folder hash, the local user name.
FORBIDDEN_PARTS = ("@", "\\", "/", ":\\", "home-")


class Refused(Exception):
    """The export would hold something it must not. Carries counts only, never a value."""


class BadArguments(Exception):
    pass


class _Break(ValueError):
    """The text parses as JSON but breaks a rule (a repeated key, NaN or Infinity): refused, not "look again later"."""


def _pairs(items):
    seen = {}
    for key, value in items:
        if key in seen:
            raise _Break("duplicate key")
        seen[key] = value
    return seen


def _constant(_name):
    raise _Break("not a finite number")


def load_flat_json(path):
    """-> ("ok", object) | ("unreadable", None) | ("refused", None). Keeps no text."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError:
        return "unreadable", None
    if len(raw) > MAX_BYTES:
        return "refused", None
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (_Break, RecursionError):
        return "refused", None
    except ValueError:
        return "unreadable", None                  # half a file (a writer may replace it in place) or not JSON: look again later
    if type(data) is not dict or any(type(value) in (dict, list) for value in data.values()):
        return "refused", None
    return "ok", data


def _is_whole(value, low, high):
    return type(value) is int and low <= value <= high


def _is_number(value):
    """A percentage: an int or a float (never a bool) between 0 and MAX_PERCENT. An int is range-checked as an int, before anything turns it into a float."""
    if type(value) is int:
        return 0 <= value <= MAX_PERCENT
    if type(value) is float:
        return math.isfinite(value) and 0 <= value <= MAX_PERCENT
    return False


def parse_time(text):
    """An agenttalk time ("2026-10-12T08:00:00Z") to epoch seconds, or None if it is not a real moment."""
    if type(text) is not str:
        return None
    found = TIME_RE.match(text)
    if found is None:
        return None
    year, month, day, hour, minute, second = (int(part) for part in found.groups())
    if not 1970 <= year <= 2099:
        return None
    try:
        moment = datetime.datetime(year, month, day, hour, minute, second)    # refuses 2026-02-30, month 13, hour 24 and the like
    except ValueError:
        return None
    return calendar.timegm(moment.timetuple())


def figures_of(data):
    """The usable figures of one parsed capacity file.

    -> ("figures", {window: (used_percent, resets_at, observed_at)}) | ("old_format"|"unknown_confidence"|"other_source"|"refused", None)
    """
    version = data.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION or type(data.get("account")) is not str:
        return "old_format", None
    if not LISTED_KEYS <= set(data) or not set(data) <= LISTED_KEYS | TOLERATED_KEYS:
        return "refused", None
    if data["confidence"] not in CONFIDENCES or type(data["source"]) is not str or data["scope"] != "account":
        return "refused", None
    if data["confidence"] == "unknown":
        return "unknown_confidence", None
    if data["source"] not in SOURCES or not data["account"].startswith("claude:"):
        return "other_source", None
    file_time = parse_time(data["observed_at"])
    if file_time is None:
        return "refused", None
    found = {}
    for window, position in WINDOW_FIELDS.items():
        used, resets = data[position + "_used_percent"], data[position + "_resets_at"]
        own = data[position + "_observed_at"]
        if used is None or resets is None:
            continue                                 # this window has no figure in this file
        if not _is_number(used) or not _is_whole(resets, 1, MAX_EPOCH):
            return "refused", None
        seen = file_time if own is None else parse_time(own)
        if seen is None:
            return "refused", None
        found[window] = (float(used), resets, seen)
    return "figures", found


def read_state_folder(state_dir):
    """-> (list of (account, source, figures) per usable file, counts). Counts hold numbers only."""
    counts = dict.fromkeys(COUNT_KEYS, 0)
    usable = []
    names = []
    try:
        with os.scandir(state_dir) as entries:
            for number, entry in enumerate(entries):
                if number >= MAX_ENTRIES:
                    counts["over_limit"] += 1
                    break
                name = entry.name
                if not name.endswith(SUFFIX) or NAME_RE.match(name[:-len(SUFFIX)]) is None:
                    continue
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    if getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400:   # a Windows reparse point (a junction)
                        continue
                except OSError:
                    counts["unreadable"] += 1
                    continue
                names.append(name)
    except OSError:
        counts["unreadable"] += 1
        return usable, counts
    names.sort()
    if len(names) > MAX_FILES:
        counts["over_limit"] += len(names) - MAX_FILES
        names = names[:MAX_FILES]
    for name in names:
        counts["files"] += 1
        outcome, data = load_flat_json(os.path.join(state_dir, name))
        if outcome != "ok":
            counts[outcome] += 1
            continue
        kind, found = figures_of(data)
        if kind != "figures":
            counts[kind] += 1
            continue
        usable.append((data["account"], data["source"], found))
    return usable, counts


def read_label_table(path):
    """The local table {exact account text: "A"|"B"|"C"}. Raises BadArguments without quoting anything from it."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(65_537)
        table = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (OSError, ValueError, RecursionError):
        raise BadArguments("the label table cannot be read as a JSON object") from None
    if len(raw) > 65_536 or type(table) is not dict or not table:
        raise BadArguments("the label table must be a small, non-empty JSON object")
    for account, label in table.items():
        if type(label) is not str or label not in LABELS or type(account) is not str or not account:
            raise BadArguments("every entry of the label table must map an account text to exactly A, B or C")
    return table


def build_rows(usable, table, counts):
    """Per label and window the newest observation across that label's seat files."""
    best = {}
    seats = {}
    for account, source, found in usable:
        label = table.get(account)
        if label is None:
            counts["unmapped"] += 1
            continue
        counts["used"] += 1
        seats[label] = seats.get(label, 0) + 1
        for window, (used, resets, seen) in found.items():
            current = best.get((label, window))
            if current is None or seen > current["observed_at"]:
                best[(label, window)] = {"label": label, "window": window, "used_percent": used, "resets_at": resets, "observed_at": seen, "source": source}
    rows = []
    for key in sorted(best):
        row = best[key]
        row["seats"] = seats[key[0]]
        rows.append(row)
    return rows


def forbidden_values(obj, user_name):
    """The number of strings in `obj` (keys and values, any depth) that hold something that must not leave the machine."""
    total = 0
    if type(obj) is str:
        low = obj.lower()
        if any(part in low for part in FORBIDDEN_PARTS) or (user_name and user_name in low):
            total += 1
    elif type(obj) is dict:
        for key, value in obj.items():
            total += forbidden_values(key, user_name) + forbidden_values(value, user_name)
    elif type(obj) is list:
        for value in obj:
            total += forbidden_values(value, user_name)
    return total


def check_export(document, user_name=None):
    """Raise Refused unless `document` is exactly an export of known words, labels and numbers with nothing unsafe in it."""
    if user_name is None:
        try:
            user_name = getpass.getuser().lower()
        except Exception:                          # no way to know the local name: refuse rather than guess
            raise Refused("user name unknown") from None
    if type(document) is not dict or tuple(sorted(document)) != tuple(sorted(TOP_KEYS)):
        raise Refused("shape")
    if document["export_version"] != EXPORT_VERSION or type(document["machine"]) is not str or MACHINE_RE.match(document["machine"]) is None:
        raise Refused("shape")
    if not _is_whole(document["exported_at"], 1, MAX_EPOCH) or type(document["rows"]) is not list or len(document["rows"]) > len(LABELS) * len(WINDOWS):
        raise Refused("shape")
    for row in document["rows"]:
        if type(row) is not dict or tuple(sorted(row)) != tuple(sorted(ROW_KEYS)):
            raise Refused("shape")
        if row["label"] not in LABELS or row["window"] not in WINDOWS or row["source"] not in SOURCES:
            raise Refused("word outside its set")
        if not _is_number(row["used_percent"]) or not _is_whole(row["resets_at"], 1, MAX_EPOCH) or not _is_whole(row["observed_at"], 1, MAX_EPOCH):
            raise Refused("number")
        if not _is_whole(row["seats"], 1, MAX_FILES):
            raise Refused("number")
    counts = document["counts"]
    if type(counts) is not dict or tuple(sorted(counts)) != tuple(sorted(COUNT_KEYS)) or not all(_is_whole(value, 0, 1_000_000) for value in counts.values()):
        raise Refused("counts")
    if forbidden_values(document, user_name):
        raise Refused("unsafe value")


def write_atomic(path, text):
    """Write `text` to a new temporary file beside `path`, then rename it into place. No half file ever has the final name.

    The temporary file is created exclusively with an unpredictable name (so it can never be a file that already exists, such as a link
    to another file), and on a failure only that file is removed.
    """
    folder = os.path.dirname(os.path.abspath(path))
    descriptor, temp = tempfile.mkstemp(prefix=".write-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def build_export(state_dir, table, machine, now):
    usable, counts = read_state_folder(state_dir)
    rows = build_rows(usable, table, counts)
    return {"export_version": EXPORT_VERSION, "machine": machine, "exported_at": int(now), "rows": rows, "counts": counts}


def run(state_dir, labels_path, machine, out, now=None, user_name=None):
    """-> (exit code, one line of counts). Writes `out` only when the export passed every check."""
    if MACHINE_RE.match(machine or "") is None:
        raise BadArguments("--machine must be 1 to 32 characters of a-z, 0-9 and -")
    table = read_label_table(labels_path)
    document = build_export(state_dir, table, machine, time.time() if now is None else now)
    try:
        check_export(document, user_name)
    except Refused as why:
        return 2, "refused (%s): nothing written; %s" % (why, summary(document["counts"]))
    write_atomic(out, json.dumps(document, indent=2, sort_keys=True) + "\n")
    return 0, "written: %d row(s); %s" % (len(document["rows"]), summary(document["counts"]))


def summary(counts):
    return ", ".join("%s=%d" % (key, counts[key]) for key in COUNT_KEYS)


ARGUMENT_SENTENCE = "not done: the command line is not valid; see --help"


class ArgumentProblem(Exception):
    """A command-line error. It carries nothing: argparse's own text would repeat the value the person typed, which may be a private path."""


class QuietParser(argparse.ArgumentParser):
    """Sub-parsers made by `add_subparsers` use the same class, so every argument error ends here and never prints a value."""

    def error(self, message):
        raise ArgumentProblem


def main(argv=None):
    parser = QuietParser(description=__doc__.split("\n")[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument("--machine", required=True)
    parser.add_argument("--out", required=True)
    try:
        args = parser.parse_args(argv)
    except ArgumentProblem:
        print(ARGUMENT_SENTENCE)
        return 3
    try:
        code, line = run(args.state_dir, args.labels, args.machine, args.out)
    except BadArguments as why:
        print("not exported: %s" % why)
        return 3
    except OSError:
        print("not exported: a file operation failed (no details are shown)")
        return 3
    except Exception:                              # last resort: never a traceback, which would carry a path
        print("not exported: an unexpected problem (no details are shown)")
        return 3
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
