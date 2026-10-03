# Step record - Card 7a-3: the reporter program

Work item `card-7a-3`, work order tk-ca82509bbeb3. Branch `card-7a-3`, from main
`795ff51` (card 7a-2 merged). Contract: `C:\lw\records\DESIGN-7A.md` (frozen copy
`DESIGN-7A-draft5.md`); **Part 12 and its addenda win over Part 4**.
Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only.
Audience: the lead and the reviewers, then the operator (the runbook is in the README).

## In plain words

Card 7a-2 built the part of the reporter that decides what to tell Leasewise. This
card makes it a program. It talks to Leasewise over its private HTTP interface,
has three commands (`run`, `drain`, `status`), reads a private configuration file,
and comes with a runbook. It is proven end to end against a real Leasewise
server running inside the test process: hand-made journals go in, and the stored
leases and receipts are compared with the journal's facts, including an outage of
three simulated hours and a restart at every step.

What it does not do: change agenttalk, schedule anything, or leave this machine.
Everything in the tests is synthetic, the server listens on loopback only, and
the identity value is an obvious fake.

## Files

- `quota_balancer/reporter/config.py` - the TOML file, its checks, the
  permission check, and resolution against a saved `policy-ids --json` snapshot.
- `quota_balancer/reporter/client.py` - the HTTP client (one request in flight,
  5-second timeouts, no redirects, no proxy).
- `quota_balancer/reporter/run.py` - one cycle (round, then send) and the loops
  behind `drain` and `run`.
- `quota_balancer/reporter/status.py` - `status`, its text view, and the copy
  `run` leaves for readers while it owns the state.
- `quota_balancer/reporter/cli.py` - the three commands; `quota_balancer/cli.py`
  hands `reporter ...` to it.
- `quota_balancer/reporter/state.py` - small additions: a `note` table (last
  401, a refused journal, the last good call per agent) and `clear_agent_stops`.
- Tests: `tests/reporter_env.py` (helpers), `tests/test_card7a3.py`,
  `tests/test_card7a3_commands.py`, `tests/test_card7a3_e2e.py`,
  `tests/test_card7a3_cross.py` (optional).
- README section "Card 7a-3 - the reporter program" (quick start, the file, the
  commands, the runbook).

## What I chose (for the lead)

1. **A start forgives configuration stops.** Design 4.5 stops an agent on a
   configuration fault but names no way back. The reporter clears all stops when
   it starts, so an operator who fixed the cause only restarts. Items that already
   failed for good stay failed. A still-broken agent stops again on its next answer.
2. **Windows permission rule** (replaced in fix round 1, see below): the access
   list is read as security identifiers and only you, SYSTEM and Administrators
   may hold an entry; the file must have its own list. Elsewhere: no group or
   other permission bit.
3. **Plain `http` is allowed for loopback only**; any other host needs `https`.
   The identity travels in a header.
4. **`status` while `run` owns the state.** A second reader cannot open an
   exclusively locked file, so `run` writes `<state>.status.json` every cycle and
   `status` shows it, labelled as a copy. With no owner, `status` reads the state.
5. **A refused journal stops reading, not sending.** An unknown schema version
   (or a segment shorter than its stored position) is recorded durably,
   printed loudly on every `run` cycle, and gives exit code 4 for `drain`.
   Items already stored are still sent.
6. **The back-off streak** counts consecutive blocked cycles and ends only when a
   request got through. A cycle that found nothing due (an item still waiting out
   its own back-off) neither grows nor ends it. A failed request ends the cycle:
   every other request would meet the same wall.
7. **Optional `team` key** in an agent table, to tell apart two seats of one
   machine that share an alias. Without it that case is refused as ambiguous
   (the brief gave no other way out).
8. **Timeouts are per network step** (connect, send, read), 5 seconds each, not a
   total deadline.
9. **The state file must not be deleted** while Leasewise holds its leases (a
   rebuilt request would be refused as a changed replay); the runbook says so.

## Requirements -> tests

Files: `A` = `tests/test_card7a3.py`, `B` = `tests/test_card7a3_commands.py`,
`C` = `tests/test_card7a3_e2e.py`, `X` = `tests/test_card7a3_cross.py`.

### 4.1 shape and configuration

| Requirement | Tests |
|---|---|
| `python -m quota_balancer reporter run\|drain\|status` | B `DrainSubprocess.test_drain_over_the_real_module_entry_point`, `Drain.test_the_commands_are_run_drain_and_status_only` |
| restrictive permissions, checked where the OS allows | A `ConfigPermissions` (POSIX bits, Windows access lists read as identifiers: canned readings and the real file, unreadable list, every command uses the real check) |
| holds URL, identity, machine alias, one table per agent | A `ConfigFile` (loads with defaults; missing, foreign and unsafe keys; ceilings; URL rules; identity characters; relative paths) |
| compound keys resolved from a `policy-ids --json` snapshot | A `ConfigResolution` (ids; other machine's seat; model by account; the real command's output) |
| an ambiguous bare alias is refused | A `ConfigResolution.test_a_bare_seat_alias_that_is_ambiguous_is_refused`, `..._model_alias_...`, `test_a_unique_bare_model_alias_needs_no_account` |
| an unconfigured agent is counted, never sent | A `ConfigResolution.test_an_agent_without_a_table_is_unconfigured_counted_and_never_sent` |
| one owner of the state; a clear static message | B `OneOwner` (in-process, across two real processes, a running `run`, status with no copy) |

### 4.5 the error table and the real client

| Row | Tests |
|---|---|
| network error, timeout, 5xx, 429 -> retry, back-off 5 s .. 15 min | A `ClientTable.test_no_answer_5xx_429_and_a_bad_success_are_retried_later` (11 kinds), `test_one_failed_request_ends_the_cycle...`; B `RunStopsAndResumes.test_run_waits_longer_while_leasewise_does_not_answer`; C `Outage` |
| 401 retried, code kept (L2) | A `test_a_401_is_retried_with_its_code_kept`, `test_after_the_identity_is_fixed_the_same_bytes_go_through`, `ClientRealServer.test_an_empty_identity_is_a_401_and_is_retried`; B `StatusFields.test_the_last_401_is_shown_with_its_code` |
| `ESTIMATE_STALE` rebuilds, at most 3, kept across restarts | A `test_estimate_stale_rebuilds_up_to_three_times_then_fails`, `test_the_rebuild_budget_survives_a_restart`, `ClientRealServer.test_a_stale_estimate_is_rebuilt_and_then_granted`; C `Outage` |
| other 400 / `INVALID_REQUEST` -> failed, never rebuilt | A `test_any_other_invalid_request_fails_for_good_and_never_rebuilds` |
| `POLICY_DENIED`, `NOT_FOUND`, `FORBIDDEN`, `CONTEXT_UNKNOWN` -> agent stopped | A `test_a_configuration_fault_stops_that_agent_and_says_which`, `ClientRealServer` (wrong identity, policy denial, unknown lease); B `ConfigFaultStop` |
| `LEASE_EXPIRED`, `RECEIPT_CONFLICT` on a non-final -> dropped | A `test_report_rows_lease_expired_and_conflict_drop_the_report_only`, `ClientRealServer.test_an_expired_lease_...`, `test_a_non_final_after_the_final_is_a_receipt_conflict`; C `Outage` |
| `IDEMPOTENCY_CONFLICT` -> failed | A `test_an_idempotency_conflict_is_a_bug_not_a_stop`, `ClientRealServer.test_changed_content_under_one_key_is_an_idempotency_conflict` |
| one request in flight, 5-second timeouts, headers | A `test_only_one_request_is_ever_in_flight`, `test_the_headers_carry_the_identity_and_the_idempotency_keys`, `test_a_dead_port_gives_no_answer_within_the_timeout`, `test_the_client_only_posts_to_lease_paths` |
| replays are the same bytes | A `ClientRealServer.test_acquire_report_final_and_the_replays`; C `Restarts` |
| `expire-sweep` is explicit, never a timer | B `StatusFields.test_open_and_incomplete_messages_point_to_expire_sweep`, `Drain.test_the_commands_are_run_drain_and_status_only` (no schedule verb) |

### 4.9 the runbook and `status --json`

| Field | Tests |
|---|---|
| every listed field is present | B `StatusFields.test_every_listed_field_is_present`, `test_the_text_view_says_the_same_in_plain_words` |
| streams seen / missing / with gaps / torn tails; `dropped_total` | B `StatusFromTheJournal` (dropped, gap, torn tail, missing stream) |
| messages open / complete / incomplete with reason; `outcome_unknown` | B `StatusFromTheJournal.test_outcome_unknown_and_open_...`; C `EndToEnd.test_the_status_agrees_with_the_server` |
| finals sent; late observations | C `EndToEnd.test_a_late_event_...`; B `StatusFromTheJournal.test_late_observations_...` |
| acquisitions pending / failed; reports pending / dropped | B `StatusFromTheJournal.test_acquisitions_pending_and_reports_dropped_are_counted`, `test_reports_over_the_cap_are_counted` |
| last successful call; configured seats that are silent | B `StatusFromTheJournal`, `StatusFields.test_a_configured_seat_with_no_recent_events_is_silent`, `test_a_reporter_that_never_ran_...` |
| a refused schema version, loud | B `JournalRefused` (exit 4, stderr line, status field, nothing written, cleared once fixed, every `run` cycle, segment shorter than its cursor) |
| the last 401; agents stopped with their code | B `StatusFields.test_the_last_401_...`, `ConfigFaultStop` |
| status while `run` owns the state | B `OneOwner.test_a_running_reporter_blocks_a_drain_and_status_reads_its_copy` |

### End to end, privacy

| Requirement | Tests |
|---|---|
| journals -> reporter -> real Leasewise; stored leases and receipts equal the journal facts (observe-only, one report per launched run, one final with right totals or null tokens, nothing for `outcome_unknown`) | C `EndToEnd` (six messages), `TwoAgents` |
| an outage of simulated hours, then recovery | C `Outage` (3 hours; stale estimates rebuilt; the expired lease drops its report and still takes the final) |
| restarts at every step | C `Restarts` (a crash before each request, after each answer, inside each round stage, and on every cycle) |
| the real writer's files | X `RealWriter` (skipped unless `QUOTA_REPORTER_AGENTTALK_SRC` is set) |
| sentinels never appear in output, status, state, logs, exception text | B `Privacy` (outputs, state files, snapshot; internal fault; configuration errors; reprs; a 401 body) |

## Fix round 1 (codex tk-4f6bd8f97976)

- **F1, who may read the file (Windows).** The access list is read as security
  identifiers through the Windows security API (`ctypes`, standard library), not
  as names. Allowed: the current user, `S-1-5-18` (SYSTEM) and `S-1-5-32-544`
  (Administrators). Any other identity, an unclassifiable entry, an inherited
  entry or an unreadable list is a refusal. A deny entry is ignored (it grants
  nobody anything). The runbook command now also resets unrelated grants.
  Tests: `test_card7a3.ConfigPermissions` (canned readings, the real reader,
  the real file with grants to Everyone, Authenticated Users, Users and
  LocalService, each undone by the runbook command).
- **F2, closed error words.** `decisions.closed_code`: only the codes in
  `quota_balancer/errors.py` plus `UNKNOWN_CODE`, `BAD_ANSWER` and
  `MALFORMED_RESPONSE` are ever stored or shown (applied in the client, in
  `decide`, and again when status is built). Tests: `test_card7a3_fix1.ClosedWordsF2`.
- **F3, an unreadable answer is a retry.** Any failure to decode a body (nesting
  too deep, bad text, bad JSON) is classified inside the client; on 2xx, 5xx and
  401 it is retried, never an internal fault. Tests: `UnreadableAnswerF3`.
- **F4, loud permanent failures.** `status` lists failed acquisitions, reports
  and finals with their codes (up to 50, plus the total) and adds one warning
  line; a final says whether its observation was complete and that it was not
  delivered. Tests: `PermanentFailuresF4`.
- **F5, a recovered 401 is history.** The last 401 stays (code, time); a separate
  `active` flag is cleared by the next accepted request; the advice shows only
  while it is active. Tests: `CredentialHistoryF5`.
- The README now says that the 5-second timeout applies to each network step,
  not to the whole request (also item 8 above).

## Fix round 2 (codex tk-216278947bad; the last round)

- **F2, an answer that echoes the secret.** A returned lease id that equals or
  contains the configured identity value makes the whole answer unreadable: it is
  retried with the original bytes and nothing from it is stored or used to build a
  request. No other answer field is stored: the error code is mapped to the closed
  words, and the status line, reason phrase and headers are not kept. Tests:
  `test_card7a3_fix2.EchoF2`.
- **F3, empty and over-limit bodies.** They are unreadable like a decoding
  failure, by status: 2xx, 401, 429 and 5xx are retried (reports and finals stay
  pending); any other 4xx keeps the permanent handling. The README sentence now
  says so. Tests: `BodiesF3`.
- **F5, atomic recovery.** The credential notes (last 401, active flag, last good
  call) are now written inside `State.apply`, in the same transaction as the
  answer that causes them, so a crash can never leave the delivery recorded and
  the flag still raised (or the reverse). A test seam `applied` marks the moment
  after that commit. Tests: `CredentialAtomicF5` (crash after the commit, crash
  inside the transaction, a new 401).
- **F6, the fallback word.** The stored word for a failure with no code is
  `UNKNOWN_CODE`, a member of `CLOSED_CODES`. Tests: `FallbackWordF6`.

## Results

See the reply for counts. Mutation checks (one small break each, the named tests
must fail) are listed in the reply.

## Limits (stated, not hidden)

- "Complete" means "no loss the journal can see" (Part 12). Totals never feed
  admission (R-007).
- The reporter has no scheduler. Running it continuously, or `expire-sweep` on a
  timer, needs the desktop lead's approval (a task named `lw-...`).
- Nothing here has met a real deployment or a non-loopback network.

## Scope cut after fix round 2

In plain words: the reporter now accepts less, on purpose, because both fix rounds are used up.

1. **Only a JSON object counts as an answer.** Before, a reply such as `null`, a list, a number, `true` or `false`, or a string on a success status was taken as "delivered", so a final could be stored as sent without Leasewise ever confirming it. Now such a reply is treated like an unreadable one: on a success status, 401, 429 or a server error it is retried with the same bytes and nothing is stored; on any other 4xx it is a permanent refusal with no retry loop. No other shape rule was added: an object without the expected fields is accepted as before.
2. **The identity may not be, or be part of, a public error word.** Before, an identity such as `CONTEXT_UNKNOWN` was accepted, and a refusal that carried that same word as its error code passed the allow-list and was stored and shown. Configuration now refuses such an identity, ignoring case. The error message does not repeat the value. Use a long generated value.

Technical detail: `client._response` returns `_unreadable(status)` when the decoded body is not a dict; `config._identity` checks the lowered value as a substring of every lowered `decisions.CLOSED_CODES` word. Tests: `tests/test_card7a3_cut.py` (9 tests; 87 failures before the change, all passing after). Codex's boundary probe `VocabularyCollision` first asserts that the colliding identity is accepted, which is exactly what is now refused, so it fails at that line by design; with that line turned into "is refused" it passes, as do `ShapePolicy` and the adapted probes. Full suite 1112 tests OK (2 skipped).
