"""Measure how well Laptop B can know each subscription's use (three-accounts step 0).

Four commands, Python 3.11+ standard library only, nothing written except the file named by `--out` / `--history`
(for `from-store` on a WAL-mode store, see STORE_PROMISE):

    collect     keep the desktop's exported copy as observation records, only if it is newer than the last one kept
    from-store  turn this machine's readings in the Leasewise store into the same observation records (read-only)
    now         one table: the newest figure of each label and window, its age and reset time (read-only, writes nothing)
    report      for each label and window, how many half-hourly samples had a fresh figure, an old figure that is
                still a floor, or nothing; plus a "possible switch" flag where a weekly reset time moved

An observation record is one line of JSON:
    {"machine", "label", "window", "used_percent", "resets_at", "observed_at", "source", "seats",
     "exported_at", "seen_at", "copy_age_s"}      (times are epoch seconds, UTC)
It holds no account text, no user name and no path. Output of every command is numbers and labels only.
"""
import calendar
import contextlib
import datetime
import decimal
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True         # the import below must not leave a cache file beside the tool (the read-only commands write nothing)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import export_capacity as ec  # noqa: E402  (the same rules check the desktop's copy)

STORE_MACHINE = "laptop-b"             # the machine whose store readings this command turns into records
STORE_SOURCE = "leasewise-store"
STORE_WINDOWS = {"five_hour": "five_hour", "seven_day": "seven_day"}
STORE_PROMISE = ("the database file itself stays byte-identical and nothing is written through the connection; because the live store is in WAL "
                 "mode, SQLite itself may leave its own <db>-wal and <db>-shm files beside it, and nothing else is created.")  # same words as the step record (D-348)
OBSERVATION_KEYS = ("machine", "label", "window", "used_percent", "resets_at", "observed_at", "source", "seats", "exported_at", "seen_at", "copy_age_s")

SAMPLE_STEP_S = 1800                   # the half-hour grid the stop rule is counted on (:00 and :30 UTC)
FRESH_WINDOW_S = 600                   # agenttalk treats a figure as current for 10 minutes
STOP_UNKNOWN_SHARE = 0.5               # "more than half unknown" is the design's stop rule
SWITCH_RESET_TOLERANCE_S = 300         # one weekly reset time normally differs by seconds between reads; more than 5 minutes is a different one
PERCENT_FALL_TOLERANCE = 1.0           # a percentage is rounded, so a fall of up to one point inside one reset period is noise
WEEKLY = "seven_day"
MAX_HISTORY_BYTES = 50_000_000         # a week of 5-minute copies is far smaller; a bigger file is not ours
MAX_RECEIPT_BYTES = 65_536             # the receipts file holds one number per machine
RECEIPTS_NAME = "receipts.json"
DESKTOP_MACHINE = "desktop"            # the machine name the desktop's export carries
DESKTOP_LATE_S = 900                   # a copy older than 15 minutes is "late" (design section 2): three missed 5-minute copies
COPY_CLOCK_SKEW_S = 60                 # a copy time up to a minute ahead is clock drift between machines; more than that is a clock problem worth saying
SWITCH_LOOKBACK_S = 7 * 86400          # the table shows a possible switch only if it happened in the last 7 days
WEEK_S = 7 * 86400                     # a weekly allowance resets every 7 days, so a label's own reset times repeat in whole weeks
EXPLAIN_LOOKBACK_S = 12 * 3600         # the observed change (T2) looks at the last 12 hours only
EXPLAIN_MIN_RISE = 1.0                 # a rise under one point is within the rounding of a stored percentage, so it is not reported
LOCK_NAME = ".collect.lock"            # exactly one collector writes a history folder


class Problem(Exception):
    """A plain-words problem with an argument; never carries a value read from a file."""


# ---------------------------------------------------------------- records

def observation(machine, row, exported_at, seen_at):
    return {"machine": machine, "label": row["label"], "window": row["window"], "used_percent": row["used_percent"], "resets_at": row["resets_at"],
            "observed_at": row["observed_at"], "source": row["source"], "seats": row["seats"], "exported_at": exported_at,
            "seen_at": seen_at, "copy_age_s": max(0, seen_at - exported_at)}


def valid_observation(rec):
    if type(rec) is not dict or tuple(sorted(rec)) != tuple(sorted(OBSERVATION_KEYS)):
        return False
    if type(rec["machine"]) is not str or ec.MACHINE_RE.match(rec["machine"]) is None:
        return False
    if rec["label"] not in ec.LABELS or rec["window"] not in ec.WINDOWS or type(rec["source"]) is not str:
        return False
    if not ec._is_number(rec["used_percent"]):
        return False
    for key in ("resets_at", "observed_at", "exported_at", "seen_at"):
        if not ec._is_whole(rec[key], 0, ec.MAX_EPOCH):
            return False
    return ec._is_whole(rec["copy_age_s"], 0, ec.MAX_EPOCH) and (rec["seats"] is None or ec._is_whole(rec["seats"], 1, ec.MAX_FILES))


def read_jsonl(path):
    """-> (valid records, number of lines that were not valid records). A missing file is no records."""
    records, bad = [], 0
    try:
        size = os.path.getsize(path)
    except OSError:
        return records, bad
    if size > MAX_HISTORY_BYTES:
        raise Problem("an observation file is larger than expected")
    with open(path, "rb") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                rec = json.loads(line.decode("utf-8"), object_pairs_hook=ec._pairs, parse_constant=ec._constant)
            except (ValueError, RecursionError):
                bad += 1
                continue
            if valid_observation(rec):
                records.append(rec)
            else:
                bad += 1
    return records, bad


# ---------------------------------------------------------------- collect

def check_copy(path, user_name=None):
    """-> the parsed export, or None if the copy does not pass the same rules the export applies."""
    outcome, data = _load_export(path)
    if outcome != "ok":
        return None
    try:
        ec.check_export(data, user_name)
    except ec.Refused:
        return None
    return data


def _load_export(path):
    try:
        with open(path, "rb") as handle:
            raw = handle.read(65_537)
        if len(raw) > 65_536:
            return "refused", None
        return "ok", json.loads(raw.decode("utf-8"), object_pairs_hook=ec._pairs, parse_constant=ec._constant)
    except (OSError, ValueError, RecursionError):
        return "unreadable", None


def read_receipts(path):
    """-> {machine: exported_at of the last copy accepted}. A missing file is no receipts; a damaged one is a plain Problem."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_RECEIPT_BYTES + 1)
    except FileNotFoundError:
        return {}
    except OSError:
        raise Problem("the receipts file in the history folder cannot be read; nothing was appended") from None
    try:
        if len(raw) > MAX_RECEIPT_BYTES:
            raise ValueError("too large")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=ec._pairs, parse_constant=ec._constant)
        if type(data) is not dict or tuple(sorted(data)) != ("last_accepted", "receipts_version") or type(data["receipts_version"]) is not int or data["receipts_version"] != 1 or type(data["last_accepted"]) is not dict:
            raise ValueError("shape")
        for machine, exported in data["last_accepted"].items():
            if type(machine) is not str or ec.MACHINE_RE.match(machine) is None or not ec._is_whole(exported, 1, ec.MAX_EPOCH):
                raise ValueError("entry")
    except (ValueError, RecursionError):
        raise Problem("the receipts file in the history folder is damaged; nothing was appended") from None
    return dict(data["last_accepted"])


def write_receipts(path, receipts):
    ec.write_atomic(path, json.dumps({"receipts_version": 1, "last_accepted": receipts}, indent=2, sort_keys=True) + "\n")


@contextlib.contextmanager
def single_collector(history):
    """Holds the folder's lock for the block and yields True, or yields False at once if another process (or thread) holds it; it never waits.

    The lock is taken through the operating system on a file that is never deleted, so it is let go when the process ends, even after a crash,
    and a leftover file does not block anyone.
    """
    with open(history / LOCK_NAME, "ab") as handle:
        try:
            _lock(handle)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            _unlock(handle)


if os.name == "nt":
    import msvcrt

    def _lock(handle):
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle):
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(handle):
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def collect(copy_path, history_dir, now=None, user_name=None):
    """-> (exit code, one line of counts). Appends the copy's rows when its export time is newer than the last one accepted.

    The last accepted export time of each machine is kept in `receipts.json`, apart from the usage rows, so that an accepted copy with no
    rows still moves it forward; no usage observation is ever invented.
    """
    now = int(time.time() if now is None else now)
    data = check_copy(copy_path, user_name)
    if data is None:
        return 2, "collect: the copy did not pass the checks; nothing appended (bad=1)"
    history = Path(history_dir)
    history.mkdir(parents=True, exist_ok=True)
    with single_collector(history) as held:
        if not held:
            return 2, "collect: another collect is running on this history folder; nothing done"
        return _collect_locked(data, history, now)


def _collect_locked(data, history, now):
    """The whole sequence (read the receipts, check, append, write the receipts) runs while this process holds the folder's lock."""
    target = history / "observations.jsonl"
    receipts_path = history / RECEIPTS_NAME
    receipts = read_receipts(receipts_path)
    kept, _bad = read_jsonl(target)
    machine = data["machine"]
    last = max([receipts.get(machine, 0)] + [rec["exported_at"] for rec in kept if rec["machine"] == machine])
    if data["exported_at"] <= last:
        return 0, "collect: not newer than the last copy accepted; nothing appended (appended=0, older=1)"
    lines = "".join(json.dumps(observation(machine, row, data["exported_at"], now), sort_keys=True) + "\n" for row in data["rows"])
    if lines:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(lines)
    # Defence in depth: whatever happened, the accepted time of a machine never goes down.
    current = read_receipts(receipts_path)
    receipts = {name: max(receipts.get(name, 0), current.get(name, 0)) for name in set(receipts) | set(current)}
    receipts[machine] = max(receipts.get(machine, 0), data["exported_at"])
    write_receipts(receipts_path, receipts)
    return 0, "collect: appended=%d, copy_age_s=%d, old_format=%d, unmapped=%d" % (len(data["rows"]), max(0, now - data["exported_at"]),
                                                                                    data["counts"]["old_format"], data["counts"]["unmapped"])


# ---------------------------------------------------------------- from-store

def read_store(db_path, labels_path, seen_at):
    """-> (observation records, number skipped). The store is opened read-only; STORE_PROMISE says exactly what that leaves on disk."""
    try:
        with open(labels_path, "rb") as handle:
            table = json.loads(handle.read(65_537).decode("utf-8"), object_pairs_hook=ec._pairs)
    except (OSError, ValueError):
        raise Problem("the alias table cannot be read as a JSON object") from None
    if type(table) is not dict or not all(type(k) is str and v in ec.LABELS for k, v in table.items()):
        raise Problem("the alias table must map an account alias to exactly A, B or C")
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    records = []
    skipped = 0
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        raise Problem("the store could not be opened read-only") from None
    try:
        query = ("SELECT a.alias, r.observed_at, r.payload FROM readings r JOIN accounts a ON a.id = r.account_id JOIN machines m ON m.id = r.machine_id "
                 "WHERE r.kind = 'cli_window' AND m.alias = ? ORDER BY r.observed_at")
        for alias, observed_ms, payload in conn.execute(query, (STORE_MACHINE,)):
            label = table.get(alias)
            if label is None:
                skipped += 1
                continue
            try:
                windows = json.loads(payload)["windows"]
            except (ValueError, KeyError, TypeError):
                skipped += 1
                continue
            for entry in windows if type(windows) is list else []:
                if type(entry) is not dict or STORE_WINDOWS.get(entry.get("key")) is None:
                    continue
                bps, reset_ms = entry.get("used_bps"), entry.get("resets_at")
                if type(bps) is not int or type(reset_ms) is not int or type(observed_ms) is not int:
                    skipped += 1
                    continue
                row = {"label": label, "window": STORE_WINDOWS[entry["key"]], "used_percent": bps / 100.0, "resets_at": reset_ms // 1000,
                       "observed_at": observed_ms // 1000, "source": STORE_SOURCE, "seats": None}
                rec = observation(STORE_MACHINE, row, row["observed_at"], seen_at)
                rec["copy_age_s"] = 0                      # a store reading is not a copy: it has no copy age
                if valid_observation(rec):
                    records.append(rec)
                else:
                    skipped += 1
    except sqlite3.Error:
        raise Problem("the store could not be read") from None
    finally:
        conn.close()
    return records, skipped


def from_store(db_path, labels_path, out_path, now=None):
    """-> (exit code, one line of counts). Writes the store's readings as observation records to `out_path`."""
    records, skipped = read_store(db_path, labels_path, int(time.time() if now is None else now))
    text = "".join(json.dumps(rec, sort_keys=True) + "\n" for rec in records)
    ec.write_atomic(out_path, text)
    return 0, "from-store: records=%d, skipped=%d" % (len(records), skipped)

# ---------------------------------------------------------------- now

SOURCE_NAMES = {STORE_MACHINE: "Laptop B store", DESKTOP_MACHINE: "desktop copy"}
NOW_FOOTER = ("A 5-hour figure needs a recent reading from a seat on that subscription: it is fresh for up to 10 minutes, then 'at least' until its reset, and unknown after the reset until a new reading.",
              "'At least' is a floor: the real figure may be higher.",
              "Use outside agenttalk cannot be separated from agent work with the readings available.",
              "Advice only: nothing is reserved, refused or enforced.")


def _minutes_text(seconds):
    minutes = max(0, int(seconds)) // 60
    if minutes < 60:
        return "%d min" % minutes
    if minutes < 24 * 60:
        return "%d h %d min" % divmod(minutes, 60)
    hours = minutes // 60
    return "%d d %d h" % divmod(hours, 24)


def _percent_text(value):
    """One decimal, cut and never rounded up: a floor must not read higher than the figure kept (12.36 shows 12.3, 99.99 shows 99.9)."""
    cut = decimal.Decimal(str(value)).quantize(decimal.Decimal("0.1"), rounding=decimal.ROUND_DOWN)
    return format(cut, "f").rstrip("0").rstrip(".") + "%"


def _when_text(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%a %Y-%m-%d %H:%M UTC")


def _source_text(machine):
    """Only fixed names are printed; a machine name read from a file never is."""
    return SOURCE_NAMES.get(machine, "another machine")


def now_cell(history, now):
    """-> the words for one label and window at `now`, from its observations sorted by observed_at."""
    kind, rec = classify(history, now)
    if kind == "unknown":
        past = [r for r in history if r["observed_at"] <= now]
        if not past:
            return "unknown: no reading recorded"
        return "unknown: reset since %s, no new reading" % _when_text(past[-1]["resets_at"])
    age = _minutes_text(now - rec["observed_at"])
    figure = _percent_text(rec["used_percent"])
    words = "%s (fresh, %s old)" % (figure, age) if kind == "fresh" else "at least %s (%s old)" % (figure, age)
    return "%s; resets %s; from %s" % (words, _when_text(rec["resets_at"]), _source_text(rec["machine"]))


def desktop_copy_line(history_dir, now):
    try:
        exported = read_receipts(Path(history_dir) / RECEIPTS_NAME).get(DESKTOP_MACHINE)
    except Problem:
        exported = None
    if exported is None:
        return "desktop copy time unknown (its receipt is missing or unreadable)"
    if exported - now > COPY_CLOCK_SKEW_S:
        return "desktop copy time is ahead of this machine's clock"
    age = max(0, now - exported)
    if age > DESKTOP_LATE_S:
        return "desktop copy late (%d min)" % (age // 60)
    return "desktop copy %d min old" % (age // 60)


# ---------------------------------------------------------------- explanations (--explain)
# Only five fixed sentences are ever printed here (T1 to T5 of docs/DESIGN-STEP3-EXPLANATIONS.md); the words allowed are fixed by the tests.

def fits_series(resets_at, anchor):
    """True when `resets_at` is the label's own weekly reset time `anchor`, or that plus a whole number of weeks (within the switch tolerance)."""
    weeks = round((resets_at - anchor) / WEEK_S)
    return weeks >= 0 and abs(resets_at - anchor - weeks * WEEK_S) <= SWITCH_RESET_TOLERANCE_S


def doubt_walk(label, weekly):
    """Walk a label's weekly readings (sorted by time) and decide which ones may be relied on.

    -> (eligible, episode): `eligible[i]` is False for a reading that is in doubt; `episode` describes the doubt still open after the last reading, or is None.
    A doubt starts at any possible-switch flag, whatever its age; the flagged reading and every later one are in doubt. It ends only when a later reading
    has a weekly reset time that fits the label's own series from before the flag (the last reset time before it, or that plus whole weeks). Time passing,
    another label's readings, and anything outside the readings never end it. A flag raised while the doubt is open (including the one raised by the
    returning reading itself) does not start a new doubt or move the series.
    """
    raised = {id(later): (flag, earlier) for flag, earlier, later in flag_pairs(label, weekly)}
    eligible, episode = [], None
    for rec in weekly:
        if episode is None:
            if id(rec) in raised:
                flag, earlier = raised[id(rec)]
                episode = {"flag": flag, "anchor": earlier["resets_at"], "earlier": earlier, "later": rec}
                eligible.append(False)
            else:
                eligible.append(True)
        elif fits_series(rec["resets_at"], episode["anchor"]):
            episode = None
            eligible.append(True)
        else:
            eligible.append(False)
    return eligible, episode


def reading_eligible(window, rec, weekly, eligible):
    """A weekly reading is eligible as `doubt_walk` says. A 5-hour reading has no weekly series of its own, so it follows the label's latest weekly reading at its time."""
    if window == WEEKLY:
        return eligible[weekly.index(rec)]
    state = True
    for other, ok in zip(weekly, eligible, strict=True):
        if other["observed_at"] > rec["observed_at"]:
            break
        state = ok
    return state


def _stamp(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%a %Y-%m-%d %H:%M UTC")


def _window_name(window):
    return "weekly" if window == WEEKLY else "5-hour"


def _left_text(used):
    """100 minus the stored percentage, rounded UP to one decimal: a bound on what is left is never shown smaller than it is."""
    left = (decimal.Decimal(100) - decimal.Decimal(str(used))).quantize(decimal.Decimal("0.1"), rounding=decimal.ROUND_UP)
    return format(left, "f").rstrip("0").rstrip(".") + "%"


def _sentence_t1(rec, window):
    return "If this reading and its label are right, at most %s of the %s allowance is left until %s (reading of %s)." % (
        _left_text(rec["used_percent"]), _window_name(window), _stamp(rec["resets_at"]), _stamp(rec["observed_at"]))


def _sentence_t2(rec, window, history, now, eligible_ids):
    same = [r for r in history if id(r) in eligible_ids and r["resets_at"] == rec["resets_at"] and now - EXPLAIN_LOOKBACK_S <= r["observed_at"] <= now]
    if len(same) < 2 or same[-1]["used_percent"] - same[0]["used_percent"] < EXPLAIN_MIN_RISE:
        return ""
    return " Its readings rose from %s (%s) to %s (%s)." % (_percent_text(same[0]["used_percent"]), _stamp(same[0]["observed_at"]),
                                                          _percent_text(same[-1]["used_percent"]), _stamp(same[-1]["observed_at"]))


def _sentence_t3(label, episode, weekly, other_newest):
    flag, earlier, later = episode["flag"], episode["earlier"], episode["later"]
    if "moved_s" in flag:
        reason = "its weekly reset moved from %s to %s and its figure went from %s to %s" % (
            _stamp(earlier["resets_at"]), _stamp(later["resets_at"]), _percent_text(earlier["used_percent"]), _percent_text(later["used_percent"]))
    else:
        reason = "its weekly figure fell from %s to %s within one reset period (it resets %s)" % (
            _percent_text(earlier["used_percent"]), _percent_text(later["used_percent"]), _stamp(later["resets_at"]))
    text = "%s: a possible switch was flagged on %s: %s." % (label, datetime.datetime.fromtimestamp(flag["at"], datetime.timezone.utc).strftime("%Y-%m-%d"), reason)
    same_as = [other for other, resets in other_newest if other != label and resets == weekly[-1]["resets_at"]]
    if same_as:
        text += " Its newest reading has the same reset times as %s's, so it may be %s's reading under %s's label." % (same_as[0], same_as[0], label)
    return text + " %s's readings since then are not used here until one shows %s's own reset times again." % (label, label)


def _sentence_t4(label, window, never_seen):
    return "%s %s: no %sreading has been recorded here. A turn by a seat logged into %s may supply one; check that a usable reading arrives under %s before relying on it." % (
        label, _window_name(window), "" if never_seen else "newer ", label, label)


NOTHING_TO_ADD = "Nothing to add to the table."


def explain_block(grouped, now):
    """-> the lines of the explanations block. Order: by label (A, B, C); for each, the doubt sentence (T3) first, then the 5-hour and the weekly window
    (T1 with T2 after it when the reading may be relied on, otherwise T4). T5 stands alone when nothing else applies. Nothing else is ever printed."""
    seen = {key: [r for r in recs if r["observed_at"] <= now] for key, recs in grouped.items()}
    walks = {label: doubt_walk(label, seen.get((label, WEEKLY), [])) for label in ec.LABELS}
    newest_weekly = [(label, seen[(label, WEEKLY)][-1]["resets_at"]) for label in sorted(ec.LABELS) if seen.get((label, WEEKLY))]
    lines = []
    for label in sorted(ec.LABELS):
        weekly, (eligible, episode) = seen.get((label, WEEKLY), []), walks[label]
        never_seen = not any(seen.get((label, window)) for window in ec.WINDOWS)
        if episode is not None:
            lines.append(_sentence_t3(label, episode, weekly, newest_weekly))
        ok_ids = {id(r) for window in ec.WINDOWS for r in seen.get((label, window), []) if reading_eligible(window, r, weekly, eligible)}
        for window in ec.WINDOWS:
            history = seen.get((label, window), [])
            kind, rec = classify(history, now) if history else ("unknown", None)
            if kind != "unknown" and id(rec) in ok_ids:
                lines.append("For %s: %s%s" % (label, _sentence_t1(rec, window), _sentence_t2(rec, window, history, now, ok_ids)))
            elif episode is None or not history:
                lines.append(_sentence_t4(label, window, never_seen))
    return lines or [NOTHING_TO_ADD]


def now_command(db_path, labels_path, history_dir, incomplete=(), now=None, explain=False):
    """The 'now' table as text. Reads the store and two files of the history folder; writes nothing."""
    now = int(time.time() if now is None else now)
    for label in incomplete:
        if label not in ec.LABELS:
            raise Problem("--incomplete-coverage must be A, B or C")
    store, _skipped = read_store(db_path, labels_path, now)
    kept, bad = read_jsonl(Path(history_dir) / "observations.jsonl")
    grouped = {}
    for rec in distinct(store + kept):
        grouped.setdefault((rec["label"], rec["window"]), []).append(rec)
    when = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%a %Y-%m-%d %H:%M UTC")
    lines = ["Usage now, %s" % when]
    for label in sorted(ec.LABELS):
        lines.append("")
        lines.append("Label %s" % label)
        for window in ec.WINDOWS:
            lines.append("  %-9s %s" % (window, now_cell(grouped.get((label, window), []), now)))
        flagged = sorted({datetime.datetime.fromtimestamp(f["at"], datetime.timezone.utc).strftime("%Y-%m-%d")
                          for f in switch_flags(label, grouped.get((label, WEEKLY), [])) if now - SWITCH_LOOKBACK_S <= f["at"] <= now})
        for day in flagged:
            lines.append("  possible switch on %s: which subscription this is, is uncertain (the lead confirms it in the accounts note)" % day)
        if label in incomplete:
            lines.append("  coverage incomplete: a machine that does not report also uses this subscription")
    lines.append("")
    lines.append(desktop_copy_line(history_dir, now))
    if bad:
        lines.append("(%d unreadable history line(s) skipped)" % bad)
    lines.append("")
    lines.extend(NOW_FOOTER)
    if explain:
        lines.append("")
        lines.extend(explain_block(grouped, now))
    return "\n".join(lines)


# ---------------------------------------------------------------- report

def day_bounds(day_from, day_to):
    start = calendar.timegm(datetime.date.fromisoformat(day_from).timetuple())
    end = calendar.timegm(datetime.date.fromisoformat(day_to).timetuple()) + 86400
    if end <= start:
        raise Problem("--to must not be before --from")
    return start, end


def classify(history, boundary):
    """fresh | floor | unknown for one boundary, from a label-window's observations sorted by observed_at."""
    latest = None
    for rec in history:
        if rec["observed_at"] > boundary:
            break
        latest = rec
    if latest is None:
        return "unknown", None
    if latest["resets_at"] <= boundary:
        return "unknown", None                  # its period has ended (also a figure observed at or after its own reset): reset since, no new figure
    if boundary - latest["observed_at"] <= FRESH_WINDOW_S:
        return "fresh", latest
    return "floor", latest                      # an older figure whose reset time is still ahead: "at least X%"


def distinct(records):
    seen = {}
    for rec in records:
        seen[(rec["machine"], rec["label"], rec["window"], rec["observed_at"])] = rec
    return sorted(seen.values(), key=lambda r: (r["observed_at"], r["machine"]))


def switch_flags(label, history):
    """Weekly window only: a reset time that moved while the earlier one had not passed, or a percentage that fell inside one period."""
    return [flag for flag, _earlier, _later in flag_pairs(label, history)]


def flag_pairs(label, history):
    """-> [(flag, earlier observation, later observation)]: each flag of `switch_flags` with the two readings it was raised from."""
    flags = []
    for earlier, later in zip(history, history[1:], strict=False):
        moved = abs(later["resets_at"] - earlier["resets_at"])
        if moved > SWITCH_RESET_TOLERANCE_S and earlier["resets_at"] > later["observed_at"]:
            flags.append(({"label": label, "kind": "possible switch", "reason": "weekly reset time changed before the earlier one passed",
                           "at": later["observed_at"], "moved_s": moved}, earlier, later))
        elif (moved <= SWITCH_RESET_TOLERANCE_S and later["observed_at"] < earlier["resets_at"]          # the earlier period must still be live at the later figure
              and later["used_percent"] < earlier["used_percent"] - PERCENT_FALL_TOLERANCE):
            flags.append(({"label": label, "kind": "possible switch", "reason": "used percentage fell within one reset period",
                           "at": later["observed_at"], "fell_points": round(earlier["used_percent"] - later["used_percent"], 2)}, earlier, later))
    return flags


def build_report(records, day_from, day_to):
    start, end = day_bounds(day_from, day_to)
    grouped = {}
    for rec in distinct(records):
        grouped.setdefault((rec["label"], rec["window"]), []).append(rec)
    labels = sorted({rec["label"] for rec in records if start <= rec["observed_at"] < end})     # a label is "in use" if it has an observation in the period
    report = {"from": day_from, "to": day_to, "labels": {}, "flags": [], "thresholds": {
        "sample_step_s": SAMPLE_STEP_S, "fresh_window_s": FRESH_WINDOW_S, "stop_unknown_share": STOP_UNKNOWN_SHARE,
        "switch_reset_tolerance_s": SWITCH_RESET_TOLERANCE_S, "percent_fall_tolerance": PERCENT_FALL_TOLERANCE}}
    for label in labels:
        entry = {"windows": {}, "samples": 0, "unknown": 0}
        for window in ec.WINDOWS:
            history = grouped.get((label, window), [])
            in_period = [r for r in history if start <= r["observed_at"] < end]
            tally = {"fresh": 0, "floor": 0, "unknown": 0}
            days = {}
            floors = {}
            for boundary in range(start, end, SAMPLE_STEP_S):
                kind, rec = classify(history, boundary)
                tally[kind] += 1
                day = datetime.datetime.fromtimestamp(boundary, datetime.timezone.utc).strftime("%Y-%m-%d")
                days.setdefault(day, {"fresh": 0, "floor": 0, "unknown": 0})[kind] += 1
                if kind == "floor":
                    floors[day] = rec["used_percent"]            # the last floor of the day, "at least X%"
            already_reset = sum(1 for r in in_period if r["observed_at"] >= r["resets_at"])      # figures observed at or after their own reset: counted, never fresh or floor
            entry["windows"][window] = {"observations": len(in_period), "samples": sum(tally.values()), **tally, "days": days, "last_floor_by_day": floors,
                                        "unknown_share": round(tally["unknown"] / sum(tally.values()), 4) if sum(tally.values()) else 1.0, "already_reset": already_reset}
            entry["samples"] += sum(tally.values())
            entry["unknown"] += tally["unknown"]
            if window == WEEKLY:
                report["flags"].extend(f for f in switch_flags(label, history) if start <= f["at"] < end)
        entry["unknown_share"] = round(entry["unknown"] / entry["samples"], 4) if entry["samples"] else 1.0
        entry["stop_signal"] = entry["unknown_share"] > STOP_UNKNOWN_SHARE
        report["labels"][label] = entry
    return report


def render(report):
    lines = ["Period %s to %s (UTC); samples every %d minutes" % (report["from"], report["to"], SAMPLE_STEP_S // 60)]
    for label, entry in report["labels"].items():
        lines.append("")
        lines.append("Label %s: %d samples, %.0f%% unknown (both windows pooled)%s" % (label, entry["samples"], 100 * entry["unknown_share"],
                                                                                 "  ** STOP SIGNAL **" if entry["stop_signal"] else "  (no stop signal)"))
        for window, info in entry["windows"].items():
            lines.append("  %-9s observations=%d fresh=%d floor=%d unknown=%d (%.0f%% unknown)%s" % (
                window, info["observations"], info["fresh"], info["floor"], info["unknown"], 100 * info["unknown_share"],
                "  already-reset figures=%d" % info["already_reset"] if info["already_reset"] else ""))
            for day, counts in info["days"].items():
                note = ""
                if day in info["last_floor_by_day"]:
                    note = "   at least %.1f%% (last floor of the day)" % info["last_floor_by_day"][day]
                lines.append("    %s fresh=%d floor=%d unknown=%d%s" % (day, counts["fresh"], counts["floor"], counts["unknown"], note))
    lines.append("")
    lines.append("Reading this: the stop decision pools both windows of a label, so 'no stop signal' does not show that each window is reliable;")
    lines.append("read each window's own unknown count above. A figure whose reset time has passed is never counted as fresh or as a floor.")
    if report["flags"]:
        lines.append("")
        lines.append("Possible switches (a flag is only a possibility and does not assign a label by itself):")
        for flag in report["flags"]:
            when = datetime.datetime.fromtimestamp(flag["at"], datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
            lines.append("  label %s at %s UTC: %s" % (flag["label"], when, flag["reason"]))
    return "\n".join(lines)


def report_command(paths, day_from, day_to, as_json):
    records, bad = [], 0
    for path in paths:
        part, skipped = read_jsonl(path)
        records.extend(part)
        bad += skipped
    report = build_report(records, day_from, day_to)
    report["unreadable_lines"] = bad
    return json.dumps(report, indent=2, sort_keys=True) if as_json else render(report) + ("\n(%d unreadable line(s) skipped)" % bad if bad else "")


# ---------------------------------------------------------------- command line

def main(argv=None):
    parser = ec.QuietParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("collect")
    one.add_argument("--in", dest="copy", required=True)
    one.add_argument("--history", required=True)
    two = sub.add_parser("from-store", description="Read-only promise: " + STORE_PROMISE)
    two.add_argument("--db", required=True)
    two.add_argument("--labels", required=True)
    two.add_argument("--out", required=True)
    now_parser = sub.add_parser("now", description="One table of the newest figure for each label and window. Reads the store (read-only) and the history folder; writes nothing.")
    now_parser.add_argument("--db", required=True)
    now_parser.add_argument("--labels", required=True)
    now_parser.add_argument("--history", required=True)
    now_parser.add_argument("--incomplete-coverage", action="append", default=[], metavar="LABEL")
    now_parser.add_argument("--now", type=int)
    now_parser.add_argument("--explain", action="store_true", help="print plain explanations under the table (what the readings allow, what is in doubt, what to check)")
    three = sub.add_parser("report")
    three.add_argument("--observations", nargs="+", required=True)
    three.add_argument("--from", dest="day_from", required=True)
    three.add_argument("--to", dest="day_to", required=True)
    three.add_argument("--json", action="store_true")
    try:
        args = parser.parse_args(argv)
    except ec.ArgumentProblem:
        print(ec.ARGUMENT_SENTENCE)
        return 3
    try:
        if args.command == "collect":
            code, line = collect(args.copy, args.history)
            print(line)
            return code
        if args.command == "from-store":
            code, line = from_store(args.db, args.labels, args.out)
            print(line)
            return code
        if args.command == "now":
            print(now_command(args.db, args.labels, args.history, args.incomplete_coverage, args.now, args.explain))
            return 0
        print(report_command(args.observations, args.day_from, args.day_to, args.json))
        return 0
    except Problem as why:
        print("not done: %s" % why)
        return 3
    except ValueError:
        print("not done: a date must look like 2026-10-12" if args.command == "report" else "not done: a value in the files could not be read (no details are shown)")
        return 3
    except OSError:
        print("not done: a file operation failed (no details are shown)")
        return 3
    except Exception:                              # last resort: never a traceback, which would carry a path
        print("not done: an unexpected problem (no details are shown)")
        return 3


if __name__ == "__main__":
    sys.exit(main())
