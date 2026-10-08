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

The lead accepted all of these. Finding 1 stays as built: a time more than 30 seconds ahead of the clock counts
as `time_unusable`, not `future`.

## Step 7b-2

Branch `card-7b2`, on top of step 7b-1 (`6c7b718`). Spec: `DESIGN-7B2.md` revision 3, sections 2, 6.1-6.5, 7.1,
7.6 and 8.

### In plain words

Step 7b-1 taught the reporter to read agenttalk's usage files and to pick the freshest figure for each account
and allowance. Step 7b-2 lets the operator say **which account is which**, gives every figure a **fixed identity
and a fixed message**, and **stores** the figure so that it survives a restart.

- **The operator names the accounts.** A new optional block in the reporter's settings file, `[[readings.sources]]`,
  has one row per agenttalk account: the agenttalk folder, the provider, the account text exactly as
  `agenttalk capacity` prints it, and the Leasewise account name it belongs to. A file whose account text is in no
  row is counted and ignored. The text holds the operator's user name, so it is never printed, logged, stored or
  sent; it is only compared.
- **One text, one account name.** The same account text must always mean the same Leasewise account, in every
  folder. Two different texts may share one account name (the operator says they share a budget). If the same
  folder and provider also appears under `limit_events`, both must name the same account.
- **A figure is one fact.** Its identity comes from the machine, the account name, the allowance and the
  second the source gave, never from the seat, the file or the figure itself. Two seats that report the same
  second for the same allowance are one fact.
- **The message is frozen.** It is built once from checked numbers, saved as exact text, and never rebuilt.
  It carries only an id, the account's local id, the allowance, the figure in hundredths of a percent, the reset
  and the observation time.
- **Only the newest figure waits.** For each account and allowance at most one figure is waiting. A newer figure
  replaces it (the old one is marked `superseded`). A figure that is not newer than the last one stored is ignored.
- **Old waiting figures expire.** A waiting figure is dropped only after its allowance has reset **and** two hours
  have passed since it was stored. Finished rows are deleted two days after they were stored.
- **Nothing is sent yet.** The sending, the pacing gate's update, the status view and the README are steps 7b-3 and
  7b-4. Until then the stored figures wait and are replaced or expire; nothing leaves the machine and nothing a seat
  does changes.

**A loss that is accepted on purpose** (spec question 9): if 97 percent is stored and 20 percent with the same reset
replaces it before it could be sent, only 20 percent will ever be sent. A test asserts this.

### What changed

- `quota_balancer/reporter/config.py`: the `readings` block with its closed keys and checks (spec 2.3), the
  account-text rule (provider word, user part, `:home-` and eight lowercase hexadecimal characters, at most 160
  characters), the whole-file rule that one text names one alias, the cross-check with `limit_events`, the state
  file and the configuration file not inside a readings folder, and `resolve_reading_sources` with the same
  sentences as `resolve_limit_sources`. Every refusal is a fixed sentence that names no value. The classes hide
  everything from `repr` and `str`.
- `quota_balancer/reporter/mapper.py`: `reading_key`, `reading_event_id` (prefix `reading|`), `reading_body` and
  `reading_body_text`, which checks every value (UUID text, plain integers, fixed words, a real `false`) and not
  only the key sets.
- `quota_balancer/reporter/state.py`: **state version 7.** Tables `reading` and `reading_window`, a partial unique
  index that makes a second pending row for one window impossible, triggers (identity columns never change; only a
  `sent`, `expired` or `superseded` row can be deleted; the observation memory only moves forward; a window memory
  cannot be renamed), migration of versions 1 to 6 in one transaction, `record_readings` (same key first, strictly
  newer, latest only, memory update, counters under `_readings`), `housekeep_readings`, `note_readings_fault` and
  `valid_reading_body`. The gate columns `gate_ms` and `last_sent_reset_ms` exist now and are ordinary columns, so
  the version does not change again in 7b-3.
- `quota_balancer/reporter/readings.py`: `run(state, sources, machine, now_ms)`, the capture pass (spec 7.6).
- `quota_balancer/reporter/run.py` and `cli.py`: `Reporter._capture_readings` is wired into every cycle: a scan at
  most every 30 seconds on the monotonic clock (memory only, so a restart scans at once), the housekeeping every
  cycle, and any fault caught whole, counted as `capture_fault`, with no text kept. It needs no send-side change. The
  command line resolves the readings rows at start like the limit-event rows.

### Decisions inside the step (where the spec left room)

- **Counters.** Under the agent word `_readings`: cumulative `captured`, `superseded`, `expired`, `pruned`,
  `capture_fault`; and `scan:<name>` for the last pass (absolute): the twelve selection counters of 7b-1, the file
  counts `file_refused`, `file_read_later`, `too_many_entries`, `folder_unreadable`, and the capture outcomes
  `same`, `same_second_conflict`, `older`. `file_refused` is a file the reader turned away; `refused` is a file of
  the wrong provider for its row.
- **Several folders.** Each folder is scanned once and chosen on its own rows; folders that name one alias are then
  merged on the same tie rules (newest, larger figure, smaller reset). The file stem is only known inside one folder.
- **Age of a settled row.** The two days count from the row's capture time (there is no "sent at" time).
- **A pruned row.** A figure whose row was deleted is still recognised as not newer, because the window memory is
  never pruned.
- **The version bump touched five older tests.** `test_card7c2_state`, `test_card7l6a1_state`,
  `test_card7l6a2_fix1b`, `test_cardr1reader2_state` and `test_cardr1reader3a_schema6` assert the final version
  number and compare every table; they now expect 7, the two new empty tables, and 8 as the "newer than known"
  version. Nothing else in them changed.
- **Bandit.** The fault count in `_capture_readings` uses `contextlib.suppress` so the Low count stays at 5.

### Tests

- `tests/test_card7b2_config.py` (32): the block, the closed keys, the account text, one text one alias (two
  different folders), two texts one alias, the `limit_events` cross-check, the folder rules, hiding, and resolution.
- `tests/test_card7b2_identity.py`: the key and id (stable across processes), the body and every explicit value
  check, and the capture cases of 6.5 on the real capture step over writer-made fixtures.
- `tests/test_card7b2_state.py`: the schema, the migration from every version 1 to 6 (and a failed migration), a
  newer file refused, every trigger, the one-pending rule, recording, expiry and pruning at their exact edges.
- `tests/test_card7b2_wiring.py`: the 30-second spacing, the restart, the housekeeping, fault isolation in both
  directions, and the start-up resolution or refusal.

### Not built yet

Offering a stored row for sending, the pacing gate's update on an accepted answer, the clamps for a clock that moved
back, the decisions, the client and the send pass (step 7b-3); `reporter status`, the README, `docs/DESIGN.md` and the
end-to-end test over the real Leasewise handler (step 7b-4).

### Gates

See the reply for the counts of this step.
