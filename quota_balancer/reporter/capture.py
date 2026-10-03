"""From agenttalk's park markers to frozen limit events in the outbox (card 7c-2; DESIGN-7C2.md 3-5).

One pass over the configured folders. The reader (`parkreader`) says what each file is; this module
decides what becomes an event:

  - the marker's provider picks the source for its folder (none: counted as `unmapped`);
  - only FRESH markers count: judged from the marker's own `updated_at_epoch` on the reporter's clock,
    never from a file's modified time (a stale marker may be a leftover);
  - a marker without a window or without a park time gives no event;
  - a known reset that has already passed gives no event.

One limit fact is (installation, account alias, evidence kind, window, reset), so two seats on one
account that hit the same limit make ONE event; with an unknown reset the park time joins the key.
The body is built field by field from the checked values, stored frozen, and never recomputed.
Counts of what was skipped describe the LAST scan; they carry no text from any file.
"""

from . import mapper, parkreader
from .state import LIMIT_AGENT  # noqa: F401  (re-exported: the counters of a scan are kept under this word)

# What a scan counts (closed words). `captured` (new events) is cumulative, kept by the state.
SKIP_COUNTS = ("read_later", "refused", "stale", "reset_passed", "unmapped", "window_unknown",
               "parked_at_unknown", "too_many_entries", "folder_unreadable")


def run(state, sources, machine, now_ms):
    """Scan every configured folder once and store the new events. -> the number of NEW events."""
    counts = dict.fromkeys(SKIP_COUNTS, 0)
    by_folder = {}
    for source in sources:
        by_folder.setdefault(source.folder, (source.bus_root, {}))[1][source.provider] = source
    items = []
    now_s = now_ms // 1000
    for bus_root, providers in by_folder.values():
        scanned = parkreader.scan(bus_root, now_ms)
        counts["read_later"] += scanned.read_later
        counts["refused"] += scanned.refused
        counts["too_many_entries"] += 1 if scanned.too_many_entries else 0
        counts["folder_unreadable"] += 1 if scanned.folder_unreadable else 0
        for found in scanned.markers:
            source = providers.get(found.provider)
            if source is None:
                counts["unmapped"] += 1
            elif now_s - found.updated_at_epoch > parkreader.STALE_SECONDS:
                counts["stale"] += 1
            elif found.window is None:
                counts["window_unknown"] += 1
            elif found.parked_at_ms is None:
                counts["parked_at_unknown"] += 1
            elif found.reset_epoch is not None and found.reset_epoch * 1000 <= now_ms:
                counts["reset_passed"] += 1
            else:
                key = mapper.limit_key(machine, source.account, found.window, found.reset_epoch,
                                       found.parked_at_ms)
                items.append((source.account, mapper.limit_body(
                    key, source.account_id, found.parked_at_ms, found.reset_epoch, found.window)))
    return state.record_capture(items, counts, now_ms)
