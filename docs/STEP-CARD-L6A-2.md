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
4. **One deadline.** The whole run has 20 seconds, under the hook's 25. If it runs out it answers `held` with
   `store_busy` and keeps what it learned, so the next run continues instead of starting over.
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

### Gates

Free memory at the start: 8.9 GB. The full suite, in chunks each under 8 minutes, proven to cover every test file once
(16 + 1 + 7 + 2 + 1 + 2 + 45 = 74 files, equal to the 74 `test_*.py` files):

| Chunk | Tests |
|---|---|
| `test_card[1-4]*.py` | 454 |
| `test_card5*.py` | 122 |
| `test_cardl1*.py` | 215 |
| `test_cardl6*.py` | 40 |
| `test_cardr1*.py` | 16 |
| `test_billing*.py` | 46 |
| `test_card7*.py` | 901 (2 skipped, as before) |
| **Total** | **1794** (equals the `discover` collection) |

Scanner gate: `ruff check --isolated --no-cache --select E4,E7,E9,F,B quota_balancer tests` reports 74 (the baseline of main
`363c87e`, none in a file this card changed); `bandit -r quota_balancer -q` reports 27 (Low 6, Medium 20, High 1, the accepted
OVH SHA-1; baseline 34). The new `nosec` notes say why: a fixed-column query in the store writer, and the operator's own
configured command started without a shell in `proctree.py`.
