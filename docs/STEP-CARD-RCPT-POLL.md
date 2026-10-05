# Step record - Card RCPT-POLL: the reporter reads gateway receipts at two speeds

Work item `card-rcpt-poll`. Branch `card-rcpt-poll` from main `3fc5b193534095499217ad8fbe91244c0b357ec4`. Design: `C:\lw\records\DESIGN-RCPT-POLL.md`
(SHA-256 `5682e777...0b8e`, with lead decision D-234): Option 2. Interpreter: CPython 3.13, standard library only. Synthetic values and fake commands only; no
gateway, no agenttalk program.

## In plain words

The reporter used to start the gateway's receipts program on every cycle: 12 times a minute at the default 5 seconds, up to 17,280 times a day, even when
nobody used the gateway. Now it asks at two speeds. While a lease is waiting for its receipt it begins a read at most every **15 seconds**; when nothing waits, at
most every **300 seconds**. Nothing is lost by this. Each question is "give me what comes after the last number I hold", every receipt is stored once and is
settled under an id made only from the receipt and the ledger, so a skipped question only makes a receipt arrive later.

| | before | after, a lease waiting all day | after, nothing waiting |
|---|---|---|---|
| program starts per day and gateway, default case (one page per pass, no backlog) | 17,280 | 5,760 | 288 |

These numbers are limits on read **passes**. Each page of a pass can start another program, and a pass that left pages behind is followed by the next
cycle's pass at once, skipping the interval. With more pages or a backlog there are more starts than the table shows, until the backlog is read.

Raising `poll_seconds` was not the answer (D-217, D-234): that one number also paces the journal, the limit capture and every upload.

## What changed

1. **Two settings in `[gateway_receipts]`** (`config.py`): `waiting_interval_seconds` (default 15, 5 to 60) and `idle_interval_seconds` (default 300, 60 to 900).
   Whole numbers only (a bool, a text and a float are refused); one fixed sentence names the key and its range and never echoes the file. **A fixture source
   refuses them**, like `timeout_seconds` and `env`: it starts no program, so a pace setting could only mislead; its two values read as none.
2. **The pace** (`run.py`, `Reporter._receipt_read_due`): the command is started only when the interval for the current state has passed since the last
   **attempt** (a failed read counts), on `self.monotonic`, kept in memory (a restart reads at once). A read that left pages behind (six pages, or the budget ended it) is
   followed by a read at once. Waiting means the count `status` shows: `State.awaiting_receipts` (`state.py`) is now the one query, used by `status._awaiting` and by
   the pace; a grant past its own expiry does not count, so a lease that never gets a receipt cannot keep the fast speed. Nothing is written on a skipped cycle.
3. **Everything else runs every cycle**: the grant copy and summary, `release_waiting`, the journal, limits, uploads and the money reader (which reads its own
   receipts by its own position, `gwcycle.py`).
4. **No schema change.** Reporter schemas 5 and 6 are untouched. The README (the command-source section of card L6a-1) has the two keys and the plain explanation.

## Why a skip loses and doubles nothing

A read asks for the page after the highest number stored (`receipts.py`, `state.current_cursor`); `capture_page` stores with `INSERT OR IGNORE`; the settle's
`event_id` comes only from the reference hash and the generation (`mapper.receipt_event_id`); a new ledger generation is noticed on the next read and each
generation keeps its cursor. Receipts are permanent in the ledger (D-234, Q1).

## Tests

`tests/test_cardrcptpoll.py`, 23 tests, driven by a virtual clock (no sleeps): the defaults, the bounds of both keys and the refusals; idle: 3 reads in 645 seconds of
5-second cycles; a lease that waits is read every 15 seconds; a grant that arrives while idle speeds it up at once; the waiting state ends and the speed goes back to
300; a grant past its expiry does not keep it fast; a failed read counts as an attempt; a restart reads at once; a backlog is read through at once; a skipped cycle
writes nothing; a skipped cycle still copies grants and delivers stored receipts; a growing ledger read at two speeds gives every receipt once, the same captures as
a fixture run; a generation change inside a skipped stretch is followed; a fixture source is unchanged; the money reader's reports are unaffected. **Red first:** with
the pace forced to "always due", 13 of the 23 fail. Four existing tests in `test_card7l6a1b_wired.py` that ran two cycles back to back now move the driven clock
between them (their rig has a clock now).

## Gates

See the reply to the lead: full suite in four chunks plus `test_card4_ops`, ruff (the operator's command) and bandit counts, `git status`, identity, LF.

## Tidy (card RCPT-POLL-tidy, after Codex's read)

- The test "a skipped read still runs the other lanes" now gives them real work after the first cycle (a journal message and its acquisition upload) and asserts the
  journal was read (`new_events` 3), `/v1/leases` was called, and the receipts command was not started again. With `Reporter._read` disabled, or `Reporter._send`
  disabled, in memory, it fails; with both restored it passes.
- The estimates above are labelled as the default, one-page, no-backlog case and as limits on read passes, not program starts.
- A test docstring (`WrongAccount` in `test_cardr1reader3b_cycle.py`) now says the recorder appends after the call returns.
