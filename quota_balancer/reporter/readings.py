"""From capacity files to one current figure per account and window (card 7b; DESIGN-7B2.md sections 3 and 4).

`capacityreader` says what each file holds. This module decides, with no clock of its own and no file access,
which figure is the freshest *current* one for each (account alias, window) and why the others were not used.
Everything is a pure function of its arguments, and `now_ms` is the reporter's clock, passed in.

The decision order for one file (1.5): `no_reading`, `source_not_accepted`, `no_figure` (or a more specific
window counter), then the freshness filter (`not_current`, `future`), then `no_account`, `unmapped` / `refused`, and only
then selection. A figure that is too old or too new says so whatever the account text holds. Counters are whole numbers
and nothing else; no text from a file is kept.
"""

import math

from . import capacityreader, mapper

FIVE_HOUR = "five_hour"
SEVEN_DAY = "seven_day"
WINDOWS = (FIVE_HOUR, SEVEN_DAY)

MINUTES_TO_WINDOW = {300: FIVE_HOUR, 10080: SEVEN_DAY}
CLAUDE_POSITION = {"primary": FIVE_HOUR, "secondary": SEVEN_DAY}   # agenttalk's `_CLAUDE_WINDOWS`
PROVIDER_OF_SOURCE = {"claude_stream": "claude", "codex_rollout": "codex"}

MAX_RESET_AHEAD_MS = 9 * 86_400_000       # a reset is at most nine days after its observation
MAX_AHEAD_MS = 25_000                      # the server refuses 30 s ahead; 25 leaves room
MAX_AGE_MS = 600_000                       # agenttalk's own limit: a figure counts for ten minutes
MAX_BPS = 10_000

# What one capture pass counts besides `COUNTERS`: the files the reader turned away or could not read yet, and the folders it
# could not list or cut short. Whole numbers only; they describe the LAST pass.
FILE_COUNTS = ("file_refused", "file_read_later", "too_many_entries", "folder_unreadable")

# What `status` shows of the last pass: the selection counters, the file counts and the capture outcomes.
CAPTURE_OUTCOMES = ("same", "same_second_conflict", "older")

COUNTERS = ("no_reading", "source_not_accepted", "no_figure", "window_incomplete", "assumed_dropped",
            "ambiguous_window", "time_unusable", "no_account", "unmapped", "refused", "not_current", "future")


class Pick:
    """The freshest current figure of one window of one account: numbers only."""

    __slots__ = ("alias", "window", "used_percent", "used_bps", "resets_at_ms", "observed_ms")

    def __init__(self, alias, window, used_percent, resets_at_ms, observed_ms):
        self.alias = alias
        self.window = window
        self.used_percent = used_percent      # the original figure: ranking uses it, never the rounded one
        self.used_bps = used_bps(used_percent)
        self.resets_at_ms = resets_at_ms
        self.observed_ms = observed_ms

    def __repr__(self):
        return "Pick(%s %s %d bps)" % (self.alias, self.window, self.used_bps)


class Selection:
    """picks: {(alias, window): Pick}; counters: {name: whole number}, every name in COUNTERS present."""

    __slots__ = ("picks", "counters")

    def __init__(self):
        self.picks = {}
        self.counters = dict.fromkeys(COUNTERS, 0)

SCAN_COUNTS = COUNTERS + FILE_COUNTS + CAPTURE_OUTCOMES


def used_bps(percent):
    """Basis points of a percentage, rounded half up and held to 0..10000 (above 100 percent is 10000)."""
    return max(0, min(MAX_BPS, math.floor(percent * 100 + 0.5)))


def _keyed(capacity):
    """-> [(window key or None, Window)], the key coming from the provider's name (Claude) or from the measured
    length alone (Codex). A window with no figure at all is left out; a dropped one carries None."""
    counters = {}
    keyed = []
    claude = capacity.source == "claude_stream"
    for window in capacity.windows:
        if window.used_percent is None and window.resets_at is None:
            continue                                  # nothing to say: not a problem
        if claude:
            key = CLAUDE_POSITION[window.position]
            if window.window_minutes != {FIVE_HOUR: 300, SEVEN_DAY: 10080}[key]:
                key = None
        elif window.basis != "measured":
            key = None
            counters["assumed_dropped"] = counters.get("assumed_dropped", 0) + 1
        else:
            key = MINUTES_TO_WINDOW.get(window.window_minutes)
        keyed.append((key, window))
    return keyed, counters


def usable_windows(capacity):
    """-> ({window key: Window}, {counter: count}) for the windows of one file that can become a reading (4.1)."""
    keyed, counters = _keyed(capacity)
    if capacity.source == "codex_rollout":
        seen = {}
        for key, _ in keyed:
            if key is not None:
                seen[key] = seen.get(key, 0) + 1
        double = {key for key, count in seen.items() if count > 1}
        if double:
            counters["ambiguous_window"] = len(double)
            keyed = [(None if key in double else key, window) for key, window in keyed]
    usable = {}
    for key, window in keyed:
        if key is None:
            if capacity.source == "codex_rollout" and window.basis == "measured":
                if window.window_minutes in MINUTES_TO_WINDOW:
                    continue                          # already counted as ambiguous
                counters["window_incomplete"] = counters.get("window_incomplete", 0) + 1
            elif capacity.source == "claude_stream":
                counters["window_incomplete"] = counters.get("window_incomplete", 0) + 1
            continue
        if window.used_percent is None or window.resets_at is None:
            counters["window_incomplete"] = counters.get("window_incomplete", 0) + 1
        elif window.observed_ms is None:
            counters["time_unusable"] = counters.get("time_unusable", 0) + 1
        elif not window.observed_ms < window.resets_at * 1000 <= window.observed_ms + MAX_RESET_AHEAD_MS:
            counters["window_incomplete"] = counters.get("window_incomplete", 0) + 1
        else:
            usable[key] = window
    return usable, counters


def select(files, mapping, now_ms):
    """-> Selection for the `Capacity` objects of one scan.

    `mapping` is {account text: (alias, provider)}, the operator's table (section 2; built in a later step).
    A figure is current when `-25 s <= now - observed <= 600 s`: newer than that is `future`, older is
    `not_current`. The freshest current figure of a (alias, window) wins; a tie on the second goes to the
    larger `used_percent`, then the smaller reset time, then the smaller file stem."""
    result = Selection()
    counters = result.counters
    candidates = {}
    for capacity in files:
        stop = capacityreader.verdict(capacity)
        if stop is not None:
            counters[stop] += 1
            continue
        usable, window_counters = usable_windows(capacity)
        for name, count in window_counters.items():
            counters[name] += count
        if not usable:
            if not window_counters:
                counters["no_figure"] += 1
            continue
        current = {}
        for key, window in usable.items():          # freshness comes before any word about the account
            age = now_ms - window.observed_ms
            if age < -MAX_AHEAD_MS:
                counters["future"] += 1
            elif age > MAX_AGE_MS:
                counters["not_current"] += 1
            else:
                current[key] = window
        if not current:
            continue
        if capacity.account is None:
            counters["no_account"] += 1
            continue
        row = mapping.get(capacity.account)
        if row is None:
            counters["unmapped"] += 1
            continue
        alias, provider = row
        if provider != PROVIDER_OF_SOURCE[capacity.source]:
            counters["refused"] += 1
            continue
        for key, window in current.items():
            candidates.setdefault((alias, key), []).append((window, capacity.stem))
    for (alias, key), found in candidates.items():
        window, _ = min(found, key=lambda item: (-item[0].observed_ms, -item[0].used_percent,
                                                  item[0].resets_at, item[1]))
        result.picks[(alias, key)] = Pick(alias, key, window.used_percent, window.resets_at * 1000, window.observed_ms)
    return result


def _merge(picks):
    """The freshest pick per (alias, window) over the folders: newest observation, then the larger ORIGINAL percentage
    (basis points are rounded and capped, so two different percentages can share them), then the smaller reset (the tie
    rules of 3.1; the file stem is only known inside one folder, and a perfect tie gives the same bytes without it)."""
    best = {}
    for pick in picks:
        key = (pick.alias, pick.window)
        old = best.get(key)
        if old is None or (-pick.observed_ms, -pick.used_percent, pick.resets_at_ms) < (-old.observed_ms, -old.used_percent, old.resets_at_ms):
            best[key] = pick
    return best


def run(state, sources, machine, now_ms):
    """Scan every configured folder once, choose the freshest current figure of each (account alias, window) and store
    the new ones (card 7b; DESIGN-7B2.md 7.6). `sources` are the resolved `readings` rows. -> the number of NEW rows.

    A row belongs to one folder: a file is attributed only by the rows of the folder it was read in. Folders that name
    one alias take part in one selection. The body is built field by field from checked numbers and stored frozen."""
    counts = dict.fromkeys(COUNTERS + FILE_COUNTS, 0)
    by_folder = {}
    ids = {}
    for source in sources:
        folder = by_folder.setdefault(source.folder, (source.bus_root, {}))
        folder[1][source.agenttalk_account] = (source.account, source.provider)
        ids[source.account] = source.account_id
    picks = []
    for bus_root, mapping in by_folder.values():
        scanned = capacityreader.scan(bus_root, now_ms)
        counts["file_refused"] += scanned.refused
        counts["file_read_later"] += scanned.read_later
        counts["too_many_entries"] += 1 if scanned.too_many_entries else 0
        counts["folder_unreadable"] += 1 if scanned.folder_unreadable else 0
        chosen = select(scanned.files, mapping, now_ms)
        for name, value in chosen.counters.items():
            counts[name] += value
        picks.extend(chosen.picks.values())
    items = []
    for (alias, window), pick in sorted(_merge(picks).items()):
        key = mapper.reading_key(machine, alias, window, pick.observed_ms)
        items.append((alias, window, mapper.reading_body(key, ids[alias], window, pick.used_bps, pick.resets_at_ms, pick.observed_ms)))
    return state.record_readings(items, counts, now_ms)
