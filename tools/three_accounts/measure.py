"""Measure how well Laptop B can know each subscription's use (three-accounts step 0).

Three commands, Python 3.11+ standard library only, nothing written except the file named by `--out` / `--history`
(for `from-store` on a WAL-mode store, see STORE_PROMISE):

    collect     keep the desktop's exported copy as observation records, only if it is newer than the last one kept
    from-store  turn this machine's readings in the Leasewise store into the same observation records (read-only)
    report      for each label and window, how many half-hourly samples had a fresh figure, an old figure that is
                still a floor, or nothing; plus a "possible switch" flag where a weekly reset time moved

An observation record is one line of JSON:
    {"machine", "label", "window", "used_percent", "resets_at", "observed_at", "source", "seats",
     "exported_at", "seen_at", "copy_age_s"}      (times are epoch seconds, UTC)
It holds no account text, no user name and no path. Output of every command is numbers and labels only.
"""
import argparse
import calendar
import datetime
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

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
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                rec = json.loads(line, object_pairs_hook=ec._pairs, parse_constant=ec._constant)
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
        if type(data) is not dict or tuple(sorted(data)) != ("last_accepted", "receipts_version") or data["receipts_version"] != 1 or type(data["last_accepted"]) is not dict:
            raise ValueError("shape")
        for machine, exported in data["last_accepted"].items():
            if type(machine) is not str or ec.MACHINE_RE.match(machine) is None or not ec._is_whole(exported, 1, ec.MAX_EPOCH):
                raise ValueError("entry")
    except (ValueError, RecursionError):
        raise Problem("the receipts file in the history folder is damaged; nothing was appended") from None
    return dict(data["last_accepted"])


def write_receipts(path, receipts):
    ec.write_atomic(path, json.dumps({"receipts_version": 1, "last_accepted": receipts}, indent=2, sort_keys=True) + "\n")


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
    receipts[machine] = data["exported_at"]
    write_receipts(receipts_path, receipts)
    return 0, "collect: appended=%d, copy_age_s=%d, old_format=%d, unmapped=%d" % (len(data["rows"]), max(0, now - data["exported_at"]),
                                                                                    data["counts"]["old_format"], data["counts"]["unmapped"])


# ---------------------------------------------------------------- from-store

def from_store(db_path, labels_path, out_path, now=None):
    """-> (exit code, one line of counts). The store is opened read-only; STORE_PROMISE says exactly what that leaves on disk."""
    try:
        with open(labels_path, "rb") as handle:
            table = json.loads(handle.read(65_537).decode("utf-8"), object_pairs_hook=ec._pairs)
    except (OSError, ValueError):
        raise Problem("the alias table cannot be read as a JSON object") from None
    if type(table) is not dict or not all(type(k) is str and v in ec.LABELS for k, v in table.items()):
        raise Problem("the alias table must map an account alias to exactly A, B or C")
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    seen_at = int(time.time() if now is None else now)
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
    text = "".join(json.dumps(rec, sort_keys=True) + "\n" for rec in records)
    ec.write_atomic(out_path, text)
    return 0, "from-store: records=%d, skipped=%d" % (len(records), skipped)


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
    flags = []
    for earlier, later in zip(history, history[1:], strict=False):
        moved = abs(later["resets_at"] - earlier["resets_at"])
        if moved > SWITCH_RESET_TOLERANCE_S and earlier["resets_at"] > later["observed_at"]:
            flags.append({"label": label, "kind": "possible switch", "reason": "weekly reset time changed before the earlier one passed",
                          "at": later["observed_at"], "moved_s": moved})
        elif (moved <= SWITCH_RESET_TOLERANCE_S and later["observed_at"] < earlier["resets_at"]          # the earlier period must still be live at the later figure
              and later["used_percent"] < earlier["used_percent"] - PERCENT_FALL_TOLERANCE):
            flags.append({"label": label, "kind": "possible switch", "reason": "used percentage fell within one reset period",
                          "at": later["observed_at"], "fell_points": round(earlier["used_percent"] - later["used_percent"], 2)})
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
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("collect")
    one.add_argument("--in", dest="copy", required=True)
    one.add_argument("--history", required=True)
    two = sub.add_parser("from-store", description="Read-only promise: " + STORE_PROMISE)
    two.add_argument("--db", required=True)
    two.add_argument("--labels", required=True)
    two.add_argument("--out", required=True)
    three = sub.add_parser("report")
    three.add_argument("--observations", nargs="+", required=True)
    three.add_argument("--from", dest="day_from", required=True)
    three.add_argument("--to", dest="day_to", required=True)
    three.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            code, line = collect(args.copy, args.history)
            print(line)
            return code
        if args.command == "from-store":
            code, line = from_store(args.db, args.labels, args.out)
            print(line)
            return code
        print(report_command(args.observations, args.day_from, args.day_to, args.json))
        return 0
    except Problem as why:
        print("not done: %s" % why)
        return 3
    except ValueError:
        print("not done: a date must look like 2026-10-12")
        return 3
    except OSError:
        print("not done: a file operation failed (no details are shown)")
        return 3
    except Exception:                              # last resort: never a traceback, which would carry a path
        print("not done: an unexpected problem (no details are shown)")
        return 3


if __name__ == "__main__":
    sys.exit(main())
