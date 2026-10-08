# Step record - Card 7b: account allowance figures (the 5-hour and weekly windows)

Work item `card-7b`. Branch `card-7b2`, from main `ba17d0e`. Contract: `C:\lw\records\DESIGN-7B2.md`,
revision 3, SHA-256 `2a96a4ff0dce5fb59d706d08371dd328cedd4621506e96dc14c7bc00ce227493`. This record grows
one step at a time. Audience: the lead and the reviewers. Interpreter: CPython 3.13 (`C:\lw\venv`),
standard library only.

## In plain words

Leasewise will show how full each account's 5-hour and weekly allowances are. agenttalk already writes one
small usage file for every seat. The card reads those files, picks the freshest figure for each account
and each allowance, and sends it to Leasewise.

**Step 7b-1 (this step) builds only the careful reading and the choosing.** It does not store, send or
configure anything. Nothing a seat does, and nothing Leasewise shows, changes yet; the later steps
(7b-2 to 7b-4) add the settings, the storage, the sending and the status view.

What the new code does, in everyday terms:

- **It trusts a file only if the file looks exactly like agenttalk's description.** A file that is too
  big, repeats a name, holds a nonsense number, has a missing item, or has an item of the wrong kind is
  turned away, and nothing from it is used. A file that is only half written (agenttalk can rewrite a file
  in place on Windows) is looked at again on the next round instead of being judged.
- **An empty file is not a broken file.** A seat that has not worked yet, or a manual refresh for another
  seat, produces a file with no figures and often no account name. That is normal. It is counted
  ("no reading", "no account") and never reported as a problem. A file that is *both* broken and empty is
  still turned away: the shape check comes first.
- **Only a fresh figure counts.** A figure is used for 10 minutes after the time the source gave it, by
  Leasewise's own clock. A figure from the future, or an old one, is counted and left out.
- **One account can have several seat files.** For each allowance the newest fresh figure wins. The 5-hour
  and weekly figures may come from different files.
- **Guesses are not sent.** For Codex only an allowance whose length was measured is used. For Claude the
  provider's own names ("five_hour", "seven_day") decide which allowance is which.
- **No text is kept.** The account name from a file is compared with the operator's table and then
  forgotten. No message from a failed read is kept.

## Step 7b-1

### What changed

- **The neutral move (commit 1, behaviour-preserving).** The parts of the park-marker reader that do not
  know about park markers moved to `quota_balancer/reporter/strictfile.py`: the three outcomes, the
  one-object-no-duplicates JSON read, the whole-number check, the agenttalk time parser, the link test and
  the bounded folder walk. `parkreader.py` imports them under its old names, so its 82 tests (`test_card7c2_reader`,
  `_capture`, `_e2e`, `_privacy`) pass without a single edit.
- **`quota_balancer/reporter/capacityreader.py` (spec 1).** The per-key table of 1.2 (27 listed keys, all
  required; `plan_type` and `limit_id` tolerated and ignored; any other flat key tolerated), the strict
  rules of 1.3 (16,384 bytes; no duplicate keys; no `NaN`, `Infinity` or overflow such as `1e999`; flat
  only; a boolean is not a number; closed words; bounds), the three outcomes, the walk limits (4,096
  entries visited, 64 files read, links skipped) and `verdict`, which is steps 1 and 2 of the decision
  order. The result of a good file is a `Capacity` that holds numbers, times and the account text, and
  hides all of it from `repr` and `str`.
- **`quota_balancer/reporter/readings.py` (spec 3 and 4).** `select(files, mapping, now_ms)` is a pure
  function. It runs the rest of the decision order of 1.5 (`no_figure` or a window counter, `no_account`,
  `unmapped` / `refused`), maps windows (4.1), judges currency (3.1) and picks the freshest figure with the
  tie rules. A `Pick` is `alias`, `window`, `used_bps` (percent times 100, rounded half up, held to 0..10000),
  `resets_at_ms` and `observed_ms`. The mapping is an argument here: `{account text: (alias, provider)}`;
  the real table is step 7b-2.
- **Fixtures.** `tests/fixtures/capacity/` holds 11 SYNTHETIC files plus `manifest.synthetic.json`. They
  were made once by agenttalk 0.99.0's own writer (`CapacitySnapshot.unknown`, a stream reading, a
  status-line snapshot, a Codex rollout record, then `for_publication` and `to_dict`) in the 0.99.0
  runtime, started with `-I`, with the agenttalk, local-data, temp and home folders pointed at scratch
  folders first. Each account is a placeholder (`claude:<os-user>:home-0123abcd`). The manifest names the
  version and the call for each file. A test asserts that every fixture has exactly the 29 keys of table 1.2.

### Decisions inside the step (where the spec left room)

- A window time or `observed_at` that does not parse makes **that window** unusable (`time_unusable`); the
  file stays valid. `last_status_at`, which is checked and dropped, is different: a malformed one refuses
  the file, because it is a field the file should hold in agenttalk's own form.
- A time more than 30 seconds ahead of the clock is `time_unusable` (the shared parser's rule), so `future`
  is only seen for 25 to 30 seconds ahead. See "Found in the spec" below.
- Counters: a file with no figure at all adds `no_figure`; a file whose windows were dropped for a
  reason adds only the specific counters (`window_incomplete`, `assumed_dropped`, `ambiguous_window`,
  `time_unusable`), never also `no_figure`. A Claude window of the wrong length and a Codex length of
  neither 300 nor 10,080 minutes count as `window_incomplete`.
- `account` as an empty string is refused (the writer never writes one).
- A listed key that is missing refuses the file (spec 1.3), unlike the park marker, where it is read again later.

### Tests

- `tests/test_card7b2_reader.py`: the fixture shape and manifest; the three outcomes; every rule of 1.3
  with a file that breaks only that rule; the table of 1.2 (every missing key refused, null allowed
  exactly where the writer can write it); scanning and its limits; what is kept; and the decision order
  of 1.5, including the malformed-plus-unknown combinations, which must stay refused.
- `tests/test_card7b2_select.py`: currency edges, newest wins, each tie rule, two windows from two files,
  two accounts of one alias, window mapping for both providers, usability, rounding, and the payload units.
- `tests/card7b2_fixtures.py`: shared helpers.
- Mutation check: 20 deliberate breaks of the rules (a limit moved by one, a tie rule flipped, a check
  removed) were run against the tests; 18 failed the tests at once. The other two were an exact size
  boundary (a test was added) and the missing-key check, which a later check backs up (it stays, as the
  explicit rule).

### Gates

See the reply for the counts of this step.

### Found in the spec

1. `parked_at_ms` refuses a time more than 30 s ahead, while 3.1 calls 25 to 30 s `future`; the two
   overlap, so `future` is seen only for 25 to 30 s ahead and later times are `time_unusable`. Harmless, but
   the two words describe one state.
2. 1.2 says the window times are "an agenttalk time" but does not say whether a malformed `last_status_at`
   refuses the file; it is refused here.
3. 1.5 puts the decision order in the reader; it is split here (`capacityreader.verdict` for steps 1 and 2,
   `readings.select` for the rest) because steps 3 to 6 need the mapping.
