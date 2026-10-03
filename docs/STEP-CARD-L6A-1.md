# Step record - Card L6a-1: gateway receipts settle quota leases

Work item `card-l6a-1`. Branch `card-l6a-1`, from `ac4aab6` (card 7c-2, step 4). Contract:
`C:\lw\records\DESIGN-6A.md`, section 8 (L6a-1) and section 13 (the lead's decisions O1 to O12).
Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only. Audience: the lead and the reviewers.
Status: **built, fixture-only.**

## In plain words

A gateway keeps one final receipt for each quota lease it served: calls made, text in and out, cost,
and how it ended. This card teaches the reporter to read those receipts and send each one to Leasewise
as the final "settle" of its lease, so the lease closes with what really happened.

It does this the way card 7c-2 handles limit events: every receipt is stored first, with its message
frozen, and sent from the stored bytes; a restart loses nothing and repeats nothing. The receipts can
only come from a file for now, because agenttalk has not built its receipt command yet. The format we
read is our reading of the design, kept in one file so it is cheap to change after Codex's challenge of
the 6a design.

What is not here, on purpose: suppressing the journal's own reports for gateway-bound leases (decision
O4; it needs the binding of card L6a-2), and any contact with a live gateway.

## Decisions taken in this card

- **O2, cached input.** Every settle says `cached_input_tokens = 0`. Meaning, written in the README,
  the status text and here: the gateway records none; 0 is not proof that none was used.
- **O3, outcome words.** The receipt already carries one of Leasewise's four words
  (`completed|cancelled|failed|provider_limit`); the mapping from the wrapper's dispositions
  (`completed` to `completed`, `dead_lettered` and `delivery_failed` to `failed`, `outcome_unknown` to
  `cancelled`) is agenttalk's job at close time (DESIGN-6A 6.3). The reporter passes the word through
  and refuses any other.
- **O12, gateway ids.** `policy-ids` gains `gateways: [{alias, id}]`. The reporter takes the gateway's
  id from the saved snapshot by the alias in `gateway_receipts`; a missing, repeated or non-UUID entry
  refuses start, as 7c-2 did for accounts.
- **The lease id (the design leaves it open; the simplest closed mapping chosen).** The gateway keeps only
  the sha256 of the reference it was given. The reference handed to the gateway IS the lease id (card
  L6a-2 will do that). The reporter keeps a small table `lease_ref` (hash of the lease id, lease id),
  filled when an acquisition is acknowledged (and, on migration, from every acquisition already
  acquired). A receipt is matched by its hash at the moment it is stored. No match means the row is
  stored as failed for good with the new closed word `UNKNOWN_LEASE` (nothing is sent; its `seq` is still
  passed). A match can never be late: the reference only reaches the gateway after the lease was
  acquired.
- **`event_id`.** UUIDv5 under `mapper.NAMESPACE` of `receipt|<reference hash>|<ledger generation>`. Never
  a position, a time or a figure; reading a receipt twice, or after a restart, gives the same id.
- **`seq`.** Taken from the receipt row; `gateway_id` from the resolved configuration; `final` is true.
- **Status version 3** (the same precedent as 7c-2): a new top-level `gateway_receipts` object.

## What changed

- `quota_balancer/reporter/receipts.py` (new). The ONE adapter. Two sources: a fixture file (the whole
  list; only receipts after the last `seq` are returned) and a configured command (argument list, no
  shell, no standard input, error output discarded, a time limit, a size cap, and an environment that
  holds only what a program needs to start plus what the operator wrote). Result words: `ok`,
  `read_later` (`source_missing`, `cannot_run`, `timeout`, `exit_code`, `not_json`) and `refused`
  (`not_a_list`, `too_many`, `too_large`, `duplicate_key`, `bad_row`, `bad_order`). Closed keys, exact
  types (`type(x) is int`), bounds, no repeated JSON keys, no NaN or infinities, outcomes from the four
  words, strictly rising `seq`, and a receipt with no calls may carry no spend. One bad row refuses the
  whole read.
- `config.py`. The `gateway_receipts` block (closed keys: `gateway`, one of `fixture` or `command`,
  `timeout_seconds` 1 to 60, `max_bytes` 1024 to 4 MiB, `env`), its resolution against the snapshot, and the
  optional `gateways` list in the snapshot.
- `store.py`. `policy_ids` lists the gateways.
- `state.py`. **State version 3.** `gateway_receipt` (immutable identity and body, no delete,
  `UNIQUE(gateway_alias, generation, seq)`, `event_id` unique, a pending row must have a lease) and
  `lease_ref`. Migration from 1 or 2 inside one transaction (a failing step leaves the file as it was).
  `record_receipts` stores the new rows with `INSERT OR IGNORE` and moves the last `seq` (in `meta`, one
  per gateway alias) in the SAME transaction; a failure stores nothing and moves nothing. An older
  reporter refuses a version-3 file with the existing fixed sentence and does not touch it (D6, as in
  7c-2).
- `mapper.py`. `lease_ref_hash`, `receipt_event_id`, `receipt_body`, `receipt_body_text` (the allow-list).
- `decisions.py`. Kind `receipt`: 200/201 or `LEASE_CLOSED` is done; every other 4xx (and any other 2xx)
  fails that row with its closed word (`UNKNOWN_CODE` if it is not one of ours); 401, 429, 5xx and no
  answer retry. It can never stop an agent, rebuild or drop. New closed word `UNKNOWN_LEASE`.
- `client.py`. The kind and the path are checked as a PAIR: a receipt goes to exactly
  `/v1/leases/<uuid>/settle`. Its answer is read as status and error word only.
- `run.py`. `_receive` (one read per cycle, a fault is counted and never stops sending) and three
  separate send passes: limit, receipt, lease. `_worse` still ranks credential above unreachable above
  nothing, whichever pass ran last.
- `status.py`, `cli.py`. The `gateway_receipts` object and one text line; the command passes the block to
  `status` and resolves it at start.

## What the real Leasewise does today (found while building the end-to-end test)

A NEW final receipt for a lease that is already settled is answered `RECEIPT_CONFLICT` and parks the
account (`lease.py` `_settle`); the same event again is a `200` replay. `LEASE_CLOSED` is what a lease
check answers, not a settle, so the reporter's "LEASE_CLOSED is done" rule is covered by the
answer-table tests only. Worth knowing for L6a-2: a receipt for a lease that has no gateway (or another
one) also conflicts and parks the account.

## Tests (120 new)

| File | Tests | What it proves |
|---|---|---|
| `test_card7l6a1_adapter.py` | 27 | closed keys, types, bounds, outcomes, order, repeated keys, NaN, size and count caps; read-later vs refused; command source with fake scripts: slow, too big, bad exit, bad JSON, no inherited secret, arguments unchanged, no shell, no error text |
| `test_card7l6a1_config.py` | 21 | the block's closed keys and fixed sentences; `policy-ids` gateways; the optional list; missing, ambiguous and non-UUID gateways refuse start |
| `test_card7l6a1_state.py` | 20 | migration 1 and 2 to 3 (rows untouched, references kept, a failing step leaves the file whole, second open is a no-op); an older reporter refuses; frozen body; one row for the same receipt twice; `seq` moves only with stored rows; restart; immutability |
| `test_card7l6a1_delivery.py` | 21 | every answer (200, LEASE_CLOSED, RECEIPT_CONFLICT, other 4xx, 401 then recovery, 429, 5xx, no answer); pass order; summary ranking; the client pair check |
| `test_card7l6a1_status.py` | 14 | the object, the text line, counts, attention lines, the command, the snapshot |
| `test_card7l6a1_e2e.py` | 11 | against the REAL in-process Leasewise handler: a lease acquired, a fixture receipt, one cycle, the lease settled with the mapped values; zero spend with `failed`; all four outcomes; a second cycle sends nothing; restart in the middle; conflict for a settled lease; a lease without a gateway; an unknown reference; spend over the hold (overrun recorded) and under it |
| `test_card7l6a1_privacy.py` | 6 | loud strings in every receipt string field, an extra key, a loud folder, a loud command error output, a loud server word and a loud fault never reach the wire, rows, status, notes, counters, logs or the command's output; the search itself is shown to catch a leak |

Existing tests changed because the shapes they pin changed: `policy-ids` keys (`test_cardl1c`,
`test_card7c2_accounts`), state version 2 to 3 and the migration chain (`test_card7c2_state`), status
version 2 to 3 and the top-level key set (`test_card7c2_status`, `test_card7a3_commands`).

Tests were written together with the code, not strictly red first. As a check, twelve deliberate
breakages of the code (an answer rule, the path pair, the cached value, the pass order, `INSERT OR IGNORE`,
the repeated-key check, the `seq` move, the integer type check, the inherited environment, the error
output, the lease reference on acquisition) were each caught by a test.

## Gates

Memory free at the start: 9.5 GB. Full suite, in chunks each under 8 minutes, proven to cover every test
file once (16 + 1 + 7 + 2 + 33 = 59 files; 59 = 52 + 7 new):

| Chunk | Tests |
|---|---|
| `test_card[1-4]*.py` | 454 |
| `test_card5*.py` | 122 |
| `test_cardl1*.py` | 215 |
| `test_billing*.py` | 46 |
| `test_card7*.py` | 636 (2 skipped, as before) |
| **Total** | **1473** (equals the `discover` collection; 1353 before this card + 120) |

`ruff --select F` is clean on the card's files. (`store.py` has one unused import that was there before
this card and is left alone.)

## Limits

Fixture-only until agenttalk's receipt API ships; cached input is reported as 0; decision O4 comes with
L6a-2; live gateways need the X3 upgrade; a rebuilt gateway ledger (new generation) restarting its numbers
is not handled; a refused source stays stuck until it is fixed (status asks for attention). The README
section "Card L6a-1 - gateway receipts" says the same in operator words.
