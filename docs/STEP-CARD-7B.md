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

## Step 7b-3

Branch `card-7b2`, on top of step 7b-2 (`a3a94d3`). Spec: `DESIGN-7B2.md` revision 3, the send side of 6.3, and
7.2 to 7.5. The lead's answers to the 7b-2 notes: the counter names are accepted (7b-4 shows them in `status`); the
window memory is the guard after pruning; a settled row never returns to `pending` (added here).

### In plain words

Steps 7b-1 and 7b-2 read the usage files and keep the newest figure per account and allowance on disk. Step 7b-3
**sends** those figures to Leasewise.

- **At most one accepted figure per account and allowance every two minutes.** After Leasewise accepts a figure, the
  next figure of the same allowance waits until two minutes have passed. The other allowance is not held back.
- **A new allowance period goes at once.** If the reset time of a figure differs from the one last accepted, the
  figure is not held back; the next figure of that new period waits again.
- **A retry never moves the clock of the gate.** If Leasewise does not answer, answers "try again later" or does
  not know us (401), the figure stays and is tried again after its own short wait (5 seconds growing to 15
  minutes). Only an accepted answer starts the two minutes.
- **A refusal fails only that figure.** Any other refusal (for example "this machine is not allowed to report for the
  account") fails that one figure for good, with Leasewise's own word, and never stops a seat or any other figure.
- **After an outage only the newest figure per allowance is sent**, then the two-minute gate holds the next one.
- **A clock that moved back an hour does no harm.** A gate more than two minutes ahead, or a retry wait more than 15
  minutes ahead, is pulled back to those limits, and the pulled-back value is what is stored.
- **A crash between sending and writing down the answer** sends the same bytes again after the restart (Leasewise
  treats the repeat as the same figure) and the gate moves then.
- **Nothing blocks anything else.** Readings are sent after limit events and before receipts and leases, each in its
  own pass, so a parked account, a refused lease or a lease retry never holds a reading back, and a reading retry
  never holds a lease back. While readings are configured the wait between cycles never exceeds 60 seconds.
- **A settled figure never becomes "waiting" again** (a database rule).

Nothing a seat does changes: the seats run in shadow, and a test shows the lease outcome is the same with readings
sent to a real in-process Leasewise.

### What changed

- `decisions.py`: the kind `READING`, deciding like `LIMIT`: 401 retry (`UNAUTHENTICATED`); no answer, 429 and 5xx retry;
  200, 201 and 202 done; any other 4xx fails that row with its closed word. Never an agent stop, a rebuild or a drop.
- `client.py`: the (kind, path) pair `READING` to exactly `/v1/readings` (no suffix, no query, no other kind); the
  answer is read for its status and error word only. The money reader's `post_reading` lane is untouched.
- `state.py`: `next_reading_item` (the clamps, then the offer rule, in one transaction, oldest first; a stored body that
  no longer fits the allow-list is not sent: that row fails once with `INVALID_REQUEST`, counted as `body_invalid`);
  `apply` moves `gate_ms` and `last_sent_reset_ms` only on an accepted answer, in the same transaction that marks the
  row `sent`; the trigger `reading_no_return`. The trigger was added to the version 7 definition; no version 7 file exists
  outside the tests yet, so the version does not change again.
- `run.py`: the readings send pass after limit events; the 60-second wait cap now also applies while readings are on.

### Decisions inside the step

- The no-return rule is a trigger on the state column: only a move to `pending` from another state is refused.
  A pending row can still move on to any state.
- `body_invalid` is a cumulative counter under `_readings`; the row fails once so it is not counted every cycle.
- The `Item` for a reading carries agent `_readings` and no message, so the generic answer handling (`last_ok`,
  credential notes) works unchanged.

### Tests

- `tests/test_card7b2_delivery.py` (34 + 1): the answers (200, 201, 202 done; each refusal permanent with its closed
  word; 401, 429, 5xx, no answer and unusable 2xx retried), the gate (first figure at once, held for one pace, opens at
  exactly the pace, a retry does not move it, a new reset passes, the other window is free, two sends are at least a
  pace apart, outage then restart sends one row per window, a restart keeps the gate, a crash between answer and
  marking), the clock moved back (clamps and their exact edges, stored and kept through a restart), isolation in both
  directions and the pass order, the 60-second cap, the no-return rule, a damaged stored body, and the shadow lease
  outcome with the real in-process Leasewise.
- `tests/test_card7c2_client.py`: the reading pair (exact path only; other kinds and other paths refused; status and
  word only; unusable 2xx retried; wire bytes; the money reader's lane untouched).
- `tests/test_card7b2_wiring.py`: now expects the readings to be sent in the same cycle.

### Not built yet

`reporter status` (the counters, the last stored sample, the stated gaps), the README, `docs/DESIGN.md`, `privacy_7b2`,
the end-to-end test over the real Leasewise reading handler and the upgrade test (step 7b-4).

### Gates

See the reply for the counts of this step.

## Fix round 1 (steps 7b-1 and 7b-2)

Codex read steps 7b-1 and 7b-2 at `a3a94d3` and asked for three fixes. They are three commits on top of 7b-3, one per
finding. Codex's final read after step 7b-4 covers them too.

### In plain words

- **Where a file lies must never change the figure.** When one account's files lay in two folders, each folder chose
  its best figure and rounded it to hundredths of a percent before the two were compared. Two different percentages
  could look equal after rounding (10.001 and 10.004, or 110 and 150 once capped), and then the earlier reset won
  instead of the larger percentage. The reset time tells Leasewise which allowance period a figure belongs to, so this
  was a wrong figure, not only a display detail. Now the original percentage decides, and rounding happens after the choice.
- **A date that cannot exist is refused.** The reader accepted `2026-99-99T99:99:99Z` in the "last status" time because
  it only looked at the pattern. It now has to be a real day and a real clock time. An old (or future) real time is
  still fine: a diagnostic time does not have to be fresh.
- **A stale figure says "stale".** A file whose figures were all 700 seconds old used to be counted as "account not in
  the table" or "no account". Freshness is now judged first; the account words come only for figures that are current.

### What changed

- `readings.py`: a pick keeps its original percentage (`used_percent`) and the cross-folder merge ranks by it
  (newest observation, larger original percentage, smaller reset). A perfect tie gives the same bytes without the file
  stem. In `select`, the `not_current` and `future` filter now comes before `no_account`, `unmapped` and the provider
  check; the window counters are kept.
- `capacityreader.py`: `last_status_at` must be a real moment. This is the only time the reader checks by shape and
  then drops. `observed_at` and the two window times are parsed (so an impossible one is already `time_unusable` for
  that window and never refuses the file, as the spec says).

### Tests

- `test_card7b2_identity.FoldersDoNotChangeTheResult` (5): the same two files in one folder and split across two, in
  every file order, give identical stored bytes; the rounded-equal and the capped-equal cases; equal percentages; a
  perfect tie; a newer observation still beats a larger percentage.
- `test_card7b2_reader.ImpossibleTimes` (4): ten impossible times refused in a populated and in an unknown snapshot; an
  old and a future real time accepted; the other times only make their window unusable.
- `test_card7b2_select.FreshnessFirst` (7): stale and future figures with a null and with an unmapped account, with a
  wrong-provider row, one stale and one current window, and the window counters kept.
- **Red first:** each new test was run on the 7b-3 head before its fix: 2 of 5 folder tests failed, all impossible-time
  checks failed (30 subtests), and the freshness tests failed.

### Two of Codex's notes, written down so nothing is over-promised

- **Same-key conflict detection holds only while the row exists.** After a settled row is pruned (two days after its
  capture), a repeated offer of that figure is counted `older` (the window memory remembers the last observation),
  not as a conflict. Never promise to detect a conflicting body for ever.
- **Pruning counts from capture time.** A row that is settled late (for example sent after a long outage) may be pruned at
  once, because its two days started when it was captured. Its window memory and the counters remain.

## Step 7b-4

Branch `card-7b2`, on top of fix round 1 (`6527f7f`). Spec: `DESIGN-7B2.md` revision 3, sections 6.6, 8, 9, 10 and the 7b-4
row of section 11. The lead's answers to the 7b-3 notes: the repeated garbled 202 is accepted as specified (said in the
README and visible in `status` as a retry); a stored body that no longer fits the allow-list fails once as
`INVALID_REQUEST`, counted `body_invalid`, and `status` shows it.

### In plain words

The reporter now reads, stores and sends usage figures. This step makes that **visible and honest**.

- **`reporter status` shows readings.** Whether they are on; which accounts (the Leasewise name and the provider, never the
  agenttalk account text); what the last look found; how many figures were stored, replaced, dropped and deleted; what is
  waiting, sent or failed, and why; and, for every account and allowance, the **last figure the reporter stored** (that
  survives the deletion of old rows). It also says, in the words of the design, what readings cannot do.
- **Three lines ask the operator to act:** a machine that is not an authorized reporter for an account (`FORBIDDEN`), seat
  files whose account is not in the table, and a seat file that has not followed the documented format for ten looks in a
  row. An empty file never raises the last one.
- **The README explains how to switch readings on**, where to find the account text, what is sent and what never is, and
  every limit. Older README sentences that readings made untrue were corrected (see below).
- **The design record says the enforcement cards are paused** (the operator's decision of 2026-10-07) and replaces the 7b
  row with the version 2 contract.
- **The last tests prove the whole path** against the real Leasewise reading handler and store reducer.

### What changed

- `status.py`: `STATUS_VERSION` 5, the `readings` object (`_readings`), the gap sentence `READING_GAPS`, the three operator
  lines, the text view line (`readings (best effort): ...`, `readings: off`, or `OLDER_READINGS` for a snapshot of a
  reporter from before status version 5, which never claims readings are off). The status file stays ASCII.
- `state.py`: the `refused_streak` counter (scans in a row that turned a file away; reset by a clean scan).
- `readings.py`: `SCAN_COUNTS`, the closed list of counters a scan reports.
- `run.py`, `cli.py`: `status` gets the configured readings rows.
- `README.md`: the section "Card 7b - usage readings" after "Card 7c-2 - limit events".
- `docs/DESIGN.md`: the pause paragraph under "Remaining cards", the bold `PAUSED (2026-10-07, D-288).` at the start of rows
  6a, 6b and 7d, the 7b row replaced by the version 2 contract, and one "Superseded" sentence in section 5 (the older text stays).
- Four older tests now expect status version 5 and the new `readings` key.

### README claims corrected (spec 10.1)

The README was searched for "7b", "readings", "capacity", "statusline" and "isolated". Nothing said readings were not live,
and the words "statusline" and "isolated" did not occur in present-tense claims. Three present-tense claims that readings
made untrue were corrected: the reporter database "schema version 5, and 6 since step 3a" (now also "7 since card 7b"); the
pass order "limit, receipt, lease; `status_version` is 3" in card L6a-1 (now "was 3 then and is 5 since card 7b", with the
new order limit, reading, receipt, lease); and the historical note in card 7c-2 ("version 3 since card L6a-1, and
`status_version` is 3"), which now also says 7 and 5.

### Tests

- `test_card7b2_status.py` (17): the closed shape, off and on, the gap sentence word for word, the one text line, last stored
  figures that outlive pruning, replaced and expired counts, retries (a garbled 202 repeated) and refusals by closed word, the
  three operator lines (and that empty files never raise the format line), a capture fault and a damaged body, nothing private,
  the ASCII snapshot, and the `status` command.
- `test_card7b2_privacy.py` (6, `privacy_7b2`): loud markers (an account text with an OS user name, a seat name, a path, a
  plan, a reason, a window word, an exception text) never appear in a stored or wired body, an outbox row, the window memory,
  a counter, a note, the status JSON or text, a log line, the state file or the snapshot; every body has only the allowed keys.
- `test_card7b2_e2e.py` (10): over the real handler and store reducer, an in-process loopback server: both windows arrive;
  one window leaves the other as it was; a late reading is accepted and keeps its own time; the highest figure of a period
  stays; a larger reset before the stored reset is jitter and at or after it starts a new period; a replay is a no-op and a
  changed body is a 409; an unauthorized machine gets 403 `FORBIDDEN` and nothing is stored.
- `test_card7b2_upgrade.py` (9): the previous version's real code (from history, `ba17d0e`) writes a status snapshot and a
  state file; the new `status` shows the old snapshot as old; the state file migrates in one step; the previous reporter
  refuses the new file and changes nothing.
- `test_card7b2_readme.py` (11): the README section and its example, the gap sentence byte for byte, placeholders only, the
  corrected claims, and the `docs/DESIGN.md` edits.
- Mutation pass over `status.py` and the streak: 18 breaks, 16 caught at once; the 2 survivors (the "ten scans" threshold
  changed by one) got a test that fixes the number.

### The whole card 7b

**What exists.** The reporter reads agenttalk's per-seat usage files strictly (7b-1), maps agenttalk's account text to a
Leasewise account through the operator's table (7b-2), keeps only the latest unsent figure per window (7b-2), sends it with
a two-minute gate that only an accepted answer moves (7b-3), and shows it in `status` and the README (7b-4). Fix round 1
made the choice independent of where files lie, refused impossible dates, and judged freshness first.

**Tests.** About 350 tests across `test_card7b2_*` (reader, select, config, identity, state, wiring, delivery, status,
privacy, e2e, upgrade, readme), the neutral move guarded by the unchanged park-marker tests, and the client cases in
`test_card7c2_client.py`.

**Limits (all in the README).** Readings are best effort; an unsent higher figure replaced by a newer one is lost on
purpose; Leasewise keeps the highest figure it received per period; a garbled 202 is repeated; conflict detection holds only
while the row exists; deletion counts from capture time; the table is trusted (R-011); the state file cannot be downgraded.

**Open.** For the liaison: spec questions 12.2 (should the status of a refused window travel in the reading), 12.3 (how long
Leasewise keeps readings: about 1,400 rows a day per account at the two-minute pace) and 12.7 (the format of the account
text). For the lead before the first live run: 12.4 (the lead checks that the reporter's machine is an authorized
reporter for the Claude and Codex accounts of the machine; otherwise every reading fails for good with `FORBIDDEN`).
