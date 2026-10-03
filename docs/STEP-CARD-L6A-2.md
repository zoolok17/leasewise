# Step record - Card L6a-2: the turn admission program (`reporter admit`)

Work item `card-l6a-2`. Branch `card-l6a-2`, stacked on `card-l6a-1` (head `a41b8e60be5cf28adb2cab577f515700baa107e7`).
Contracts: the neutral spec (version 2.1, sections 4.2, 4.3, 6 and 7) and `DESIGN-6A.md` (sections 8.5 for
`gateway_agents`, 8.6, 8.7 and requirements 4, 5 and 7 of 19.1). Interpreter: CPython 3.13 (`C:\lw\venv`), standard
library only. Audience: the lead and the reviewers. The full record and the README come with step 2.

## Step 1: the store and `reporter admit`

Status: **built, fixture-only** (step 1: the store and `admit`; step 2: the other three questions, the incremental
binding read, the journal rule, the README and the end-to-end proof).

### In plain words

Before a paid turn starts, the wrapper asks one question: may this turn run, and under what limits? `reporter admit` is
the program that answers it. It asks Leasewise for a lease, writes the answer in the admission store (a small database
file), and gives the **same answer** every time it is asked about the same message, however often it is run, killed or
run at the same time as itself.

What keeps that true:

1. **A saved grant is final.** If the message already has a grant, the answer comes from the file. Leasewise is not asked
   again.
2. **One helper per message at a time.** A lock owned by the operating system (one of 64 small files) makes "look up,
   ask, save" one section per message. A helper that dies releases it by dying; nobody cleans anything up.
3. **Saved before sent.** The request, and every later body, is written down before it is sent. A lost answer is asked
   again with the very same bytes, and Leasewise recognises them and returns the lease it already made.
4. **One deadline.** The whole run has 20 seconds, under the hook's 25, and it is one absolute deadline: the time left
   is given to the connection, to each piece of Leasewise's answer, to every wait for the store file and to the open
   retry (fix round 1; the first build only capped each request on its own). If it runs out the connection is closed and
   the answer is `held` with `store_busy`; what was learned is kept, so the next run continues instead of starting
   over. A grant Leasewise made but we could not save in time is recovered by resending the same stored bytes.
5. **Saved before printed.** The grant is stored before anything is written to standard output.

### What was built

- `reporter/admission_store.py` (extended, one module for the layout): the new `Writer`, and in the layout a durable
  counter for the order `pending` will use (`admission.return_seq` and the one-row table `admission_meta`; requirement 5,
  used in step 2) and `admission.replay_tried` (recovery progress). The layout version stays 1: no store of an earlier
  layout was ever written.
- `reporter/admission_lock.py`: 64 stripe files `lock-00` to `lock-3f` in `admission.locks` beside the store, chosen by
  the SHA-256 of the key. POSIX `fcntl.flock`; Windows `msvcrt.locking` on one byte at offset 0. Only non-blocking tries,
  inside a deadline. Not fair: under heavy contention a helper may answer `store_busy`.
- `reporter/admit_wire.py`: strict JSON both ways (no booleans or floats as integers, no repeated key, no NaN, nothing
  after the object, closed keys, every bound of spec 4.2, the 512-byte caps measured while reading) and the six closed
  `held` words.
- `reporter/admit.py`: the flow and its deadline. `reporter/cli.py`: the verb `admit`. `reporter/client.py`:
  `post_acquisition` (the stored bytes to `/v1/leases` and nowhere else). `reporter/config.py`: the `[admission]` block.
- The grant request (8.7): `FRESH` or `RESUME` from the context mode, never `UNKNOWN` or `SUMMARY`;
  `dispatch_tokens` is `dispatch_bytes`; `source` is `conservative_bound`; the bounds are the operator's. A missing
  bound, a sum above the configured model ceiling, or `resume` with no resume bound is `held` (`context_unprovable`);
  an agent not in `gateway_agents` is `held` (`not_configured`). Neither asks Leasewise or creates a file.
- The answer: the reference is the lease id (36 characters); the caps are null while leases are metered-advisory;
  `ttl_seconds` is the lease's remaining time, 1 to 86 400, and a lease with no time left is `held` (its grant is still
  kept, because the receipt needs the binding).

### Decisions taken (please check them)

- **Configuration.** One block, `[admission]`: `store`, `gateway` (an alias from the policy-ids snapshot),
  `gateway_agents`, and the optional bounds `fresh_overhead_bound_tokens`, `resume_history_bound_tokens`,
  `growth_bound_tokens`, `context_ceiling_tokens`. `store` must be the same file as `gateway_receipts.admission_store`.
  The seat, model and call limits come from the agent's own table, as for the journal pipeline.
- **`profile`** is checked against its bound and otherwise unused in step 1.
- **Back-off kept in the store:** 10 s after an outage, 30 s when the account is parked, 60 s after a refusal.
- **Rebuild budget.** At most 3 rebuilds per recovery episode. A run that ends because time ran out keeps its progress
  (rebuild count and which saved bodies were replayed); a run that ends in any other `held` starts the next episode
  afresh. Otherwise three short outages would use up a message for good.
- **Opening a new store file** can meet a lock the database's own wait does not cover (two helpers switching a new file
  to WAL at once). A real two-process test showed it; opening is now retried inside the lock-wait bound.
- **A lease that is not paid** (a shadow lease, for instance) is `held` (`refused`) and no grant is saved.

### Tests (97 new, in six files)

| Requirement | Test |
| --- | --- |
| Competing helpers: two, then many real processes, one request, one lease, one stored grant, one answer | `test_card7l6a2_real.Processes` (`test_two_real_processes...`, `test_many_real_processes...`) |
| A delayed stale response; the winning grant found by replay; nothing overwritten | `test_card7l6a2_admit.Late` |
| A fault between Leasewise's grant and the save | `Stops.test_a_stop_after_the_answer_and_before_the_grant_is_saved_replays_the_same_bytes` |
| Lost standard output | `Stops.test_lost_standard_output_is_harmless`, `Stops.test_a_stop_after_the_grant_is_saved...` |
| The request and every body saved before it is sent | `Basics.test_the_request_is_saved_before_it_is_sent...`, `Rebuilds.test_a_stale_estimate_is_rebuilt...` |
| The lock: a killed holder releases the key; a bounded wait; 64 files at most; the Windows byte lock | `test_card7l6a2_lock` (`Death`, `Waiting`, `Stripes`), `Deadline.test_the_wait_for_the_key_is_bounded_and_answers_busy`, `Processes.test_a_real_process_waits_for_the_key...` |
| The deadline (requirement 4): worst path within 20 s, then continues from the stored progress | `Deadline.test_the_worst_path_answers_busy_within_20_seconds_and_the_next_run_continues_where_it_stopped` |
| Strict JSON and the size caps, each at the cap and one past it | `test_card7l6a2_wire` (19 tests) |
| `context_unprovable` and `not_configured` | `Unconfigured` |
| Every `held` word and its back-off | `Holds` |
| The real handler: null caps, no hold, no token, the same lease again, no shadow fallback, an old estimate is stale | `test_card7l6a2_real.Handler` |
| The reference hash agrees on both sides, and the reporter reads the grant as a binding | `Basics.test_the_reference_hash_is_the_sha256_of_its_ascii_bytes_on_both_sides`, `Handler.test_the_reporter_reads_the_saved_grant_as_a_binding` |
| The writer, the immutability triggers, the durable order counter | `test_card7l6a2_store` |
| The configuration block | `test_card7l6a2_config` |

Honest note: the tests were written after the code, not red first. They were checked afterwards by four deliberate
breakages (no saved-progress shortcut, four rebuilds instead of three, no lock, no "request saved" hand-off), each of
which made a test fail; and the two-process test failed on the real race above before it was fixed.

### Limits of step 1

- The lock bounds waiting; it does not promise fairness. Heavy contention can answer `store_busy`.
- Replaying a stored admission is not a per-attempt authority check; leases stay advisory.

## Step 2: the other three questions, the incremental read, one authority per message

### In plain words

Step 1 made `admit` give one lease and one answer. Step 2 finishes our side of the hook:

- **Three more questions.** `recall` ("what was the answer for this message?"), `pending` ("which admitted messages are
  not closed yet?") and `closed` ("this one is closed"). Only `admit` can answer `held`; if another question cannot be
  answered now, the program prints nothing and exits with code 3.
- **No lifetime limit.** The receipt reader copies new grants a batch at a time, after a number the store gives every grant
  when it is saved, so the 100,000-grant limit of card L6a-1 is gone.
- **One authority per paid message.** A message bound to a paid lease is never also reported through the journal
  pipeline. The gateway's receipt is the only thing that settles it.
- **Two things the lead added.** The receipt reader and the `[admission]` block must name the same store file (checked at
  start-up, with a fixed sentence); and a message keeps at most 16 saved bodies, after which `admit` holds it as
  `refused`, `status` shows it, and nothing is deleted.

### What was built

- **The wire** (`reporter/admit_wire.py`): one parser for the four requests, each with exactly its own keys; answers of
  their own shapes with the measured sizes (`unknown` 26 bytes, `closed` 21, `pending` empty 35, 64 entries at the maximum
  identifier sizes 8,290, under the 9,216 cap); a strict reader for every answer (`parse_op_answer`), where `held` belongs
  to `admit` alone.
- **`recall`** reads the store read-only (`mode=ro`), takes no lock and can never create a file or a row. A grant whose time
  is used up is still recalled (with the least time the wire allows, 1 s), because its reference is what a close needs.
- **`pending`** (requirement 5): never-returned entries first, then by the durable return number, then by row. The same
  transaction gives every returned entry the next number, so an entry that cannot be closed yet goes to the back of the
  line. The clock is never used for the order. With no store file it answers an empty list and creates nothing.
- **`closed`** waits for the message's key lock for at most the bounded wait (10 s), records `closed_ms` once (a trigger
  keeps it), and answers `ok`; a repeat, an unknown message and a missing store are no-ops. It does not close a message
  that was never granted.
- **The grant order value.** Every grant gets the next `grant_seq` in the same short transaction that saves it. The reader
  (`read_bindings(path, after, limit)`) takes the next batch (500) in that order, so a grant saved late with a lower row id
  but a higher number is still found, and nothing is read twice. The reporter keeps the last number it copied in its state,
  saved with each batch. The 100,000-row whole-store read and the `history_limit` state are removed. The relation check
  (reference equals lease id, hash is the SHA-256 of it) runs on every batch.
- **Journal suppression** (8.5). An agent in `gateway_agents` has no journal configuration in the rounds, so nothing is
  acquired, reported or finalised for it, and the events skipped are counted (`suppressed`); its observations are kept. For
  other agents, a message with a copied binding creates no journal acquisition, report or final. Journal rows queued before
  the binding appeared get one `superseded` record (a new immutable table, never a deletion); the sending query skips
  them; an acquisition already sent is counted as `superseded_after_send`. A missing or busy store changes nothing.
- **Status.** `gateway_receipts.admission_store` gains `at_body_cap` (a count of messages held at the cap, from a read-only
  look at the store each cycle) with its own action sentence; `journal_suppression` shows `gateway_agents`, `suppressed`,
  `superseded` and `superseded_after_send`. Superseded rows are not counted as waiting, and a declared agent's open
  messages no longer ask for an `expire-sweep`. The shape of the per-agent entries is unchanged.
- **The body cap** (`admission_store.MAX_BODIES` = 16): checked before a rebuild; at the cap the message is held as
  `refused` with the visible reason `BODY_CAP`; replaying a saved body is still allowed.

### Decisions taken (please check them)

- The hook program is one command, `reporter admit`, and the operation is named by `op` in the request.
- `suppressed` counts journal events skipped, not messages.
- A message at the body cap stays held until an operator looks at it; no automatic recovery was added.
- A batch that fails the relation check leaves the store `unreadable` and the reader stuck at that batch until the store is
  fixed; nothing later is copied meanwhile. That is deliberate: copying past a damaged row could misroute a receipt.
- The two layouts changed in this step (the order values, the close trigger, the body-cap constant) are still version 1:
  no store of an earlier layout was ever written.

### Tests (new in step 2: 53 in this card's files, which now total 150, and 2 more in the L6a-1 files, now 224)

| What the brief asked | Test |
| --- | --- |
| `recall`: known, unknown, never creating anything | `test_card7l6a2_ops.Recall` |
| `pending` order: tied time, a clock rollback, more than 64, an unclosable prefix then closable entries, 64 entries at the maximum sizes, an empty answer | `Pending`, `Sizes` |
| `closed` once and repeated; the bounded wait | `Closed` |
| every operation's strict JSON, its exit code when it cannot complete | `Sizes`, `Failures`, `Recall.test_recall_of_a_store_that_cannot_be_read...`, `Pending.test_a_busy_store...`, `Closed.test_closed_waits...` |
| the incremental read: batches, a restart in the middle, a late grant with a lower row id but a higher number, a relation mismatch | `test_card7l6a1_admission_store` (`Reader`), `test_card7l6a1_status.StoreTrouble.test_no_history_size_stops_the_reporter...` |
| suppression: both arrival orders, a listed agent with no binding, a binding for an unlisted agent, queued rows set aside and never sent, an acquisition already sent, the journal's own observations kept | `test_card7l6a2_suppress` (`Declared`, `Bound`, `Records`) |
| the body cap | `test_card7l6a2_admit.BodyCap`, `test_card7l6a2_suppress.BodyCapStatus` |
| the store-file mismatch refusal | `test_card7l6a2_config.Parse.test_the_store_must_be_the_same_file_the_receipt_reader_uses` (refused when the configuration is loaded, so at start-up) |
| end to end against the real in-process handler: `admit`, `recall`, `pending`, `closed`, then a receipt settles the lease | `test_card7l6a2_e2e` |

Honest note: as in step 1, the new tests were written after the code, not red first. Three deliberate breakages (journal
suppression off; `pending` ordered by row instead of by return number; the reader ignoring the grant number) each made
several tests fail.

### Limits (19.2, the ones that apply here)

- After a restart without evidence of how a turn ended, a close can say `cancelled` although the work finished; `closed` is
  only an acknowledgement and never proves a receipt exists or that its cost is zero.
- Expiry can win before a later ending is known, and with no access or no restart there is no bound on when a receipt
  arrives. A missing receipt keeps its liability and never becomes a guessed zero; awaiting work stays visible (`awaiting_receipt`,
  then `overdue`).
- The lock bounds waiting; it does not promise fairness. Heavy contention can answer `store_busy`.
- Everything stays advisory and fixture-only: replaying a stored admission is not a per-attempt authority check.
- Receipts, captures, admissions and attempts are kept without compaction.
- **The transition limit:** an agent that is not yet in `gateway_agents` can still be journalled before a binding is seen
  (an observe-only acquisition that holds nothing). Declare the agent first. This is not a second settlement authority.
- A message at the body cap stays held.

### Gates (first build, superseded by the fix round below)

Free memory at the start: 8.9 GB. The full suite, in chunks each under 8 minutes, proven to cover every test file once
(16 + 1 + 7 + 2 + 3 + 2 + 45 = 76 files, equal to the 76 `test_*.py` files):

| Chunk | Tests |
|---|---|
| `test_card[1-4]*.py` | 454 |
| `test_card5*.py` | 122 |
| `test_cardl1*.py` | 215 |
| `test_cardl6*.py` | 40 |
| `test_cardr1*.py` | 69 |
| `test_billing*.py` | 46 |
| `test_card7*.py` | 887 (2 skipped, as before) |
| **Total** | **1833** (equals the `discover` collection) |

Scanner gate: `ruff check --isolated --no-cache --select E4,E7,E9,F,B quota_balancer tests` reports 74 (the baseline of main
`363c87e`, none in a file this card changed); `bandit -r quota_balancer -q` reports 26 (Low 5, Medium 20, High 1, the accepted
OVH SHA-1; baseline 34). The only `nosec` notes this card adds are the two in `admission_store.py` (see the fix round
below for the exact list). The step-2 text that also named a command started without a shell in `proctree.py` was wrong:
that file left with the command sources (card L6a-1b), so it adds no suppression.

## Fix round 1 (Codex's review of head 3ed6c760)

### In plain words

Codex found eight real gaps. All are closed. The two that matter most: the 20-second promise now really holds even when
Leasewise answers a few bytes at a time (before, such a peer could stretch a run to 24 s), and a turn can no longer be
sent twice by the journal side just because a bookkeeping step crashed half-way. Nothing already saved is ever deleted.

- **F1, one real deadline.** One absolute deadline now runs through the whole exchange: connecting, sending, the answer
  read in pieces (the time left is checked between pieces and caps each wait; a watchdog also shuts a blocked read), every
  wait for the store file, and the open retry, which does not start an attempt it cannot finish. (Fix round 2 adds the
  connection step itself and removes the name lookup from the admission path.) At the deadline the
  connection is closed and the answer is `held` with `store_busy`, progress saved. A grant that Leasewise made but we
  could not save is recovered by the next run, which resends the same stored bytes and gets the same lease.
- **F2, the owner comes from the saved request.** The binding's gateway is read from the exact saved body that won (also
  an older body found by the replay search), never from today's configuration. A saved request is recovered before any
  check that reads today's settings; those checks apply to a new message only. (Fix round 2 completes this for an agent
  that was removed from the settings: the lease is still recovered and saved, and the turn is held.)
- **F3, custody is not permission.** A replayed paid grant whose lease is now `uncertain` (expired) or `settled` is still
  saved as the binding, so `recall`, `pending`, `closed` and receipts can use it; its time is stored as none and `admit`
  answers `held` (`refused`), so the turn does not run.
- **F4, exclusion at the sending boundary.** `State.next_item` no longer offers any acquisition, non-final or final row of
  a declared agent or of a message with a known binding, whatever the `superseded` markers say. The immutable markers and
  the counters stay; they are records, the rule is in the choice of what to send.
- **F5, reporter state version 4.** One transaction upgrades L6a-1's final version 3 (and, through it, 1 and 2); the only
  change is the `superseded` table with its triggers. The test fixture (`tests/fixtures/reporter-state-v3-l6a1.sqlite`) was
  written by the real State of the final `card-l6a-1` head (main `55b229f`). An older reporter refuses version 4 with its
  existing sentence; nothing is deleted.
- **F6, an index.** `admission_attempt_admission` on `admission_attempt(admission_id)` makes the body count behind
  `at_body_cap` an index lookup; a query-plan test checks there is no scan of that table.
- **F7, the summary is independent of receipts.** The admission summary (body cap, store state) is refreshed each cycle
  whenever `[admission]` is configured. With both blocks, the one checked store path does it (one read a cycle). A failed
  read shows the state as read and the count as unknown (`null`), never a fresh zero; the text line and the action
  sentences appear without a receipt source too.
- **F8, suppressions.** Every `nosec` names its bandit test id and a reason. The complete list for this card:
  `quota_balancer/reporter/admission_store.py:266` (`Writer.lookup`) and `:328` (`Writer.save_grant`), both
  `# nosec B608 - fixed column names, values bound`. There are no others, and no bare ones.

### Tests (new: 28 in two files; two older files changed only for the new version number)

| What | Where |
|---|---|
| F1: a real local peer that drips its answer ends within the budget plus a small cleanup; the same through the whole run; store contention near the deadline; an open that cannot finish is not started; a grant committed but not saved at the deadline is recovered with the same bytes and one lease | `test_card7l6a2_fix1.Deadline` |
| F2: save for gateway A, lose the answer, remap the alias to B, drop today's bounds, retry: the binding says A; a new message is still judged by today's settings | `test_card7l6a2_fix1.Owner` |
| F3: expired and settled replays are saved and held; real handler, crash before the save, `expire-sweep`, retry | `test_card7l6a2_fix1.Custody`, `RealHandler` |
| F4: 1,001 known-bound queued rows, an agent declared after its rows were queued, a crash between copying bindings and writing markers (acquisition, non-final and final) | `test_card7l6a2_fix1b.Exclusion` |
| F5: the real v3 fixture upgrades with nothing deleted, a failed upgrade leaves it at 3, an older reporter refuses 4, versions 1 and 2 reach 4 | `test_card7l6a2_fix1b.Upgrade` (and the updated `test_card7c2_state`, `test_card7l6a1_state`) |
| F6: no scan of the bodies table | `test_card7l6a2_fix1b.Plan` |
| F7: admission only, unreadable summary then recovery, unknown before the first read, missing store, one read when both are configured | `test_card7l6a2_fix1b.Summary`, `OneCheckedPath` |

Red first: the four F4 tests fail (4 of 4) with the new exclusion removed and pass with it. The F1 deadline test and the F3
and F2 tests failed on the first build's behaviour (the deadline run answered `coordinator_unreachable` after the first
request timed out; the old code saved no expired grant) and pass now.

### Gates

Chunks, each under 8 minutes: cards 1-4: 454; card 5: 122; L1: 215; L6: 40; R1: 69; billing: 46; card 7: 915 (2 skipped).
Total **1861**, equal to the `discover` collection; the 78 test files match the seven patterns exactly once. ruff (gate
set): 74 (the baseline, none in a file this card changed). bandit: 26 (Low 5, Medium 20, High 1, the accepted OVH SHA-1).

## Fix round 2 (the last): the connection and the removed agent

### In plain words

Codex's second read closed six of the eight findings and found two only half closed. Both are now closed.

- **D1, the connection obeys the 20-second limit.** Before, looking up Leasewise's address could take longer than the limit,
  and the program would then still send. Now, with `[admission]`, Leasewise's address must be an IP address or `localhost`
  (any other name is refused at start-up with one fixed sentence), so `admit` never waits on a name lookup; the
  connection is made with the time that is left, the watchdog owns its socket from the moment it exists, and nothing is
  sent after the deadline.
- **D2, a half-saved lease is recovered even if its agent was removed.** Before, removing the agent from the settings
  while its request was saved made `admit` refuse to recover, so Leasewise held a lease that our side never recorded. Now
  the saved request is resent, the binding is saved, and the turn is held as `not_configured`.

### What changed

- `config.py`: with `[admission]`, `base_url`'s host must be an IP address or `localhost`; otherwise the fixed sentence
  `base_url: with [admission], the host must be an IP address or localhost`. Without `[admission]` nothing changes.
- `client.py`: `_http` makes its own connection through the standard library's connection hook. `localhost` and IP addresses
  are connected as numbers (no resolver); the connect timeout is the time left; the socket is handed to the watchdog
  at once; the time left is checked before sending, before reading the headers and between pieces; an error that
  arrives after the deadline is reported as `TimeoutError`; the watchdog thread is always ended (joined for at most 1 s).
  Any other host name (the `run` path) still goes through the resolver, and that lookup is not bounded.
- `admit.py`: `admit` no longer refuses an existing `requested` row because its agent is gone. The stored bytes are
  resent, a conflict replays the saved bodies as before, a grant is saved (owner from the winning saved body, custody kept)
  and answered `held` as `not_configured`. A stale estimate that would need a rebuild with the agent gone is `held` as
  `not_configured`, the row stays `requested` (reason `NOT_CONFIGURED`, no back-off), nothing is rebuilt or deleted. A
  stored grant of a removed agent is also answered `held` as `not_configured` on every later `admit` (custody stays;
  permission follows today's settings). A message with no saved request of an agent that is not configured is
  `not_configured` as before.

### The bound, as measured

With a connection that uses up the whole budget, or a peer that answers a few bytes at a time, the exchange ends within a
few milliseconds of the deadline (measured, worst of ten runs: 2 ms), **for plain HTTP and for HTTPS** (the HTTPS handshake
was brought under the deadline afterwards, in card L6a-1b's follow-up commit: see "The HTTPS handshake" below). The one thing
allowed past the deadline is a save that is already in hand (a grant Leasewise made), which may wait at most 0.5 s
(`SAVE_RESERVE_MS`) for the store file. The tests allow the deadline plus 1 s: `test_card7l6a2_fix2.Connect`,
`test_card7l6a2_fix1.Deadline` and `test_card7l6a1b_https`.

### Tests (new, in `test_card7l6a2_fix2.py`)

| What | Where |
|---|---|
| a host name is refused at start-up with the exact sentence; an IP address, `localhost` and an IPv6 literal are accepted; without `[admission]` a host name still works | `Address` |
| a patched resolver that sleeps 2.4 s for any non-numeric host: `admit` with an IP-literal or a `localhost` base URL is not delayed and the resolver is never asked; a host name on the `run` path still uses it | `NoLookup` |
| a patched connection step that uses up the whole budget, then connects to a local peer: it ends within the budget plus the bound and the peer sees no request (with a literal, with `localhost`, and through the whole `admit` run, where Leasewise creates no lease) | `Connect` |
| the watchdog thread has ended after a good answer, a refused connection, an expiry during connect and a blocked answer | `Watchdog` |
| the real handler: grant, crash before the local save, alpha replaced by beta, retry: the binding for alpha is saved (`read_bindings`, `recall`), `admit` is `held` as `not_configured`, `pending` and `closed` work, the same afterwards, one lease | `Removed` |
| the replay variant (an earlier body won), the rebuild-needed variant (held, row stays `requested`, no back-off, nothing rebuilt; putting the agent back lets the same message go on), and a new message of a removed agent | `Removed` |

Red first: against the previous head, 8 of the 17 new tests fail (14 failures counting sub-cases) and pass now.

### Limits (new)

- With `[admission]`, Leasewise's address must be an IP address or `localhost`.
- The `run` path without `[admission]` may still use a host name; its lookup is not bounded and it has no hook promise.
- *(Corrected by the HTTPS follow-up; the first version of this limit said the handshake was not bounded jointly.)* For an
  `https` base URL the only work outside the deadline is local: building the certificate settings and checking the
  certificate (about 50 ms of processor time, not interruptible). See "The HTTPS handshake".


### Gates (fix round 2)

Chunks, each under 8 minutes: cards 1-4: 454; card 5: 122; L1: 215; L6: 40; R1: 69; billing: 46; card 7: 932 (2 skipped).
Total **1878**, equal to the `discover` collection; the 79 test files match the seven patterns exactly once. ruff (gate
set): 74 (the baseline). bandit: 26 (Low 5, Medium 20, High 1, the accepted OVH SHA-1). The only `nosec` notes are still
`admission_store.py:266` and `:328`, both `B608 - fixed column names, values bound`.

## The HTTPS handshake (a follow-up, made on card L6a-1b after L6a-2 was merged)

Codex approved L6a-2 with one stated limit (L1): over HTTPS the secure-connection setup (the TLS handshake) could run past the
budget. In its probe a 2-second budget took 3.6 seconds. Nothing unsafe happened (no request was sent after the deadline and the
saved request stayed recoverable), but the words promised more than that.

**Cause.** The socket's timeout was set to the time left *before* the connection was made. A connection that took 1.6 s of a
2 s budget then handed the handshake the whole 2 s again (1.6 + 2.0 = 3.6 s), and the watchdog did not own the socket during the
handshake, because wrapping a socket for TLS hands its descriptor to a new object.

**Fix** (`client.py`, `_http`). For HTTPS the wrap is done in two steps: the wrap that does not touch the network, the new
socket handed to the watchdog at once (it owns the descriptor now, so the watchdog can end a blocked handshake), the timeout
set again to the time left at that moment, and then the handshake. Certificate checking is the real context's and is unchanged.

**The bound, as measured.** With a connection that takes 1.6 s of a 2 s budget, with a peer that never answers the ClientHello,
and with a peer that answers it a byte at a time, the call ends at the deadline (worst of fifteen runs: 2 ms over); before the
fix the first of these took 3.61 s. The whole `admit` route answers `held` with `store_busy` and the row stays `requested`.
Not covered, and local: building the certificate settings (about 50 ms) and checking the certificate.

**Which answer at the deadline.** A transport failure just before the deadline can be answered `coordinator_unreachable` (with the normal back-off) instead of `store_busy`; which one it is depends on which side of the deadline the failure lands. Both answers keep the saved request. The real-clock test for the whole route therefore accepts exactly one of
the two answers, and a separate test with a controlled clock pins the exact deadline answer (`store_busy`).

**Tests** (`test_card7l6a1b_https.py`, 5; no certificate is needed because the peers speak no real TLS). Red first: against
the previous client, 2 of the 5 fail (3.61 s and 3.63 s against a 2 s budget) and pass now. They cover the late connection, a
peer that accepts late and stays silent, a peer that answers a byte at a time, a deadline that passes during the connection (the
peer receives not even a ClientHello), the watchdog thread ending in each case, and the whole `admit` route.

