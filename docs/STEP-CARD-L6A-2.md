# Step record - Card L6a-2: the turn admission program (`reporter admit`)

Work item `card-l6a-2`. Branch `card-l6a-2`, stacked on `card-l6a-1` (head `a41b8e60be5cf28adb2cab577f515700baa107e7`).
Contracts: the neutral spec (version 2.1, sections 4.2, 4.3, 6 and 7) and `DESIGN-6A.md` (sections 8.5 for
`gateway_agents`, 8.6, 8.7 and requirements 4, 5 and 7 of 19.1). Interpreter: CPython 3.13 (`C:\lw\venv`), standard
library only. Audience: the lead and the reviewers. The full record and the README come with step 2.

## Step 1: the store and `reporter admit`

Status: **step 1 of 2 built, fixture-only.**

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

### Limits

- The lock bounds waiting; it does not promise fairness. Heavy contention can answer `store_busy`.
- Replaying a stored admission is not a per-attempt authority check; leases stay advisory.
- `recall`, `pending`, `closed`, the journal suppression, the README and the end-to-end proof are step 2.

### Gates

Free memory at the start: 10.4 GB. The full suite in chunks, each under 8 minutes, proven to cover every test file once
(16 + 1 + 7 + 2 + 1 + 2 + 42 = 71 files, equal to the 71 `test_*.py` files):

| Chunk | Tests |
|---|---|
| `test_card[1-4]*.py` | 454 |
| `test_card5*.py` | 122 |
| `test_cardl1*.py` | 215 |
| `test_cardl6*.py` | 40 |
| `test_cardr1*.py` | 16 |
| `test_billing*.py` | 46 |
| `test_card7*.py` | 815 (2 skipped, as before) |
| **Total** | **1708** (equals the `discover` collection) |

`ruff --select F` is clean on the changed files.
