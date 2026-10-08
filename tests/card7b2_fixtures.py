"""Shared helpers for the card 7b tests: the labelled synthetic capacity files and a fixed clock.

The files in `tests/fixtures/capacity/` were made once by agenttalk 0.99.0's own writer (see
`manifest.synthetic.json` there for the call that made each one). Every account and user is a placeholder.
"""

import calendar
import copy
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "capacity")


def utc_ms(text):
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")) * 1000


def utc_s(text):
    return utc_ms(text) // 1000


T0 = "2026-10-08T12:00:00Z"                    # the moment the fixtures were made (their observed_at)
NOW = utc_ms(T0)
FIVE_RESET_S = utc_s("2026-10-08T16:00:00Z")   # the fixtures' five-hour reset
WEEK_RESET_S = utc_s("2026-10-12T00:00:00Z")   # the fixtures' weekly reset
CLAUDE_ACCOUNT = "claude:<os-user>:home-0123abcd"
CODEX_ACCOUNT = "codex:<os-user>:home-4567ef01"
MAPPING = {CLAUDE_ACCOUNT: ("alias-claude", "claude"), CODEX_ACCOUNT: ("alias-codex", "codex")}


def fixture(name):
    """A fresh copy of one fixture as a dictionary."""
    with open(os.path.join(FIXTURES, name + ".synthetic.json"), "r", encoding="ascii") as handle:
        return json.load(handle)


def fixture_names():
    names = sorted(n[:-len(".synthetic.json")] for n in os.listdir(FIXTURES)
                   if n.endswith(".synthetic.json") and not n.startswith("manifest"))
    return names


def edited(name, **over):
    doc = copy.deepcopy(fixture(name))
    doc.update(over)
    return doc


def put(folder, doc, stem=None, raw=None):
    """Write one capacity file named for its source_agent (or `stem`); `raw` writes exact bytes."""
    stem = stem or doc["source_agent"]
    path = os.path.join(folder, stem + ".capacity.json")
    with open(path, "wb") as handle:
        handle.write(raw if raw is not None else json.dumps(doc, indent=2).encode("utf-8"))
    return path
