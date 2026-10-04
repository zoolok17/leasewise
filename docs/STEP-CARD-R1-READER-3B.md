# Step record - Card R1-READER, step 3b: the money reader's whole cycle

Work item `card-r1-reader-3b`. Branch `card-r1-reader-3b` from main `61867067bc9c27563597ea638168fe4cfc2fbd43`. Design: `C:\lw\records\DESIGN-R1-READER.md`
(SHA-256 `1de762ac...db5d`): sections 5, 6, 7, 8 (steps 2 and 4 to 8, wired with step 3a's 1, 3 and 9), 9, 12, 13, 14. Interpreter: CPython 3.13, standard library
only. Synthetic values and a fake runner only; agenttalk is never run.

## In plain words

Until now the money reader had every part but the loop that joins them. This step adds the loop. Each cycle it runs the gateway's report command, reads the
answer strictly, reads the receipts up to the report's own number, works out the month's figures, saves one numbered report as exact bytes in Leasewise's
version-4 format, and sends it. If anything on the way fails there is no report and one closed word, counted in `status`. A waiting report is always sent
first, and the gateway is not run while one waits. **Nothing here runs against a live gateway:** live use waits for the maintenance window and step 4.

## What was built

1. **The configuration table `gateway_report`** (`config.GatewayReport`, `_gateway_report`, `resolve_gateway_report`): the gateway alias, the gateway's account
   alias, the `command`, a time-out, a byte limit and `env`. Windows only; the receipt command's rules (absolute `.exe`, a list, no shell, no quote or control
   character, no `--after`/`--limit`/`--json`); it must END with `runner.REPORT_TAIL`; it needs `gateway_receipts` read by a **command** for the **same
   gateway**. Without the table nothing new happens.
2. **The cycle** (`gwcycle.run_lane`, steps 1 to 9; steps 4 to 8 in `make_report`), run as the money reader's lane `Reporter._send_report` (3a's isolation stays: an
   unexpected failure is caught at the lane and counted as `state_unavailable`).
3. **The bytes** (`gwwire.build_report`): one pure function giving the wire document `{event_id, account_id, observed_at, kind, payload}` with the nine
   version-3 fields and the five version-4 keys, keys sorted, no spaces. The identity facts sit where `gwstate.report_facts` reads them. `event_id` is
   `derive_event_id`, and the idempotency key equals it (as `mint` saves it).
4. **The words** (`gwcycle.CYCLE_WORDS` = `status_failed` + `gwreport.WORDS`): each is counted once per cycle that ended with it, under the existing counter scheme
   (`money_reader.counts`). Status version stays 4: the keys are additions inside an existing block.
5. `gwstate.saved_totals` reads the receipts already read for a generation (reads only). **No schema change.**

## Choices made (each tested), and two things for the lead

- **Both points below are now lead decision D-217** (`DESIGN-R1-READER.md`, just before section 1, SHA-256 `fa05155a...5abe`). It is built as follows.
- **`account` is a key the design did not name** (the one addition to the table of design 13). Leasewise accepts a gateway's report only from the machine **and
  the account** that own the gateway (`store._reduce_gateway_snapshot`: an account of null is "INVALID_CONFIG"), and the policy-ids snapshot lists accounts and gateways
  separately, so the reader cannot learn it by itself. It is resolved against the snapshot like the gateway. Found when the first end-to-end send was refused.
  **A wrong value** gives a definitive refusal from Leasewise for every report; after five in a row the reader stops as `send_refused`. (The first version of this
  record said to fix the value and run `resume`; that was wrong, see "Review follow-up".)
- **`interval_seconds`** (`[gateway_report]`, a whole number, default 60, 30 to 100): a new report is made at most once per interval, measured on a monotonic clock
  kept in memory (`gwcycle.Pacer`, set when a report is made, not when one is tried). A restart reports at once. A waiting report is still sent every cycle under the
  existing back-off; a cycle that skips because of the interval writes nothing and counts no word. The interval is a speed limit, not a freshness promise (see
  "Review follow-up"); raising `poll_seconds` instead would not work, because the same cycle carries the other uploads.
- **The month and the observed time** are both taken from the reader's clock at the moment it has the snapshot (after the command returned).
- **A mint that is refused** (`gwstate.Refused`: halted, a pending row, a bad report or receipts) is counted as `state_unavailable`; design 9 has no other word for it.
- **The gateway is not run while a report waits**, and also not while the reader is halted or its state is damaged (a halted reader with nothing waiting is found
  by the start-up check, which counts `reader_halted` or `state_unavailable`).
- **Receipt reading has no per-cycle page budget:** each page read is bounded by the command's own time-out, the saved position moves only after every page up to `N`
  was read (design 7), so a long backlog is read in one cycle. The stop signal ends it between pages.

## Tests (`tests/test_cardr1reader3b_cycle.py`, 41)

The first 33 are listed below; the interval adds 8: the default and the bounds (29, 101, 0, a negative, a string, a float and a boolean are refused; 30 and 100 are
accepted), two cycles inside the interval make one report, a waiting cycle writes nothing and counts nothing, the first cycle at the interval makes the next report, a waiting
report is resent inside the interval, a restart reports at once, a failed read does not use up the interval, and the configured interval is the one used. The test clock
drives the monotonic clock. Red first against `ecef117`: 6 of the 8 fail (the restart and failed-read tests describe behaviour that did not change).

End to end with a REAL in-process Leasewise and a fake runner returning agenttalk's documented report and page byte for byte: a cycle sends report 1 (unowned money
670, N 1, quiet); `gateway-approve` then `gateway-adopt` naming the open items; the evidence block says `usable`; a restart keeps the identity and numbers; a second
cycle sends report 2, which joins the same epoch. `Sends` (3): a down network keeps the same bytes and number across cycles without running the gateway again, then
report 1 is accepted and report 2 made and sent in the same cycle; a halted reader and a damaged state do not run the gateway. `Failures` (13): the runner's nine words
and an unconfirmed job end (`status_failed`); seven kinds of refused snapshot (`status_shape`); not ready / no binding (`child_cap_not_ready`); another generation;
a gap, a wrong asked-for number, an unknown key and an envelope version (`receipts_failed`); a failing receipt command; more promised than given; receipts above `N`
not used; an `N` below the saved position (`receipts_rolled_back`); receipts above settled; a later period; the words as counts with no value from the report. `Generations`
(1): epochs 1, 2, 3 for generations A, B, A while the numbers keep growing. `Lane` (2): a thrown failure never stops the other uploads; a reporter without the table is
unchanged. `Wire` (5): the bytes through Leasewise's real receiving path (`_wire_to_reading`, `validate_readings`), every figure in its field, the identity facts, closed keys
and equal bytes, the hold / busy / over-limit mapping, and nothing named in the bytes. `Configuration` (7): the good table; the receipt command's refusals; a shell
string and every missing or misplaced tail; the receipt source rules; closed keys and bounds; no value in a message; the snapshot resolution.

**Red first.** The cycle, the builder and the table are new, so on the previous head the whole file fails at import (33 tests).

## Review follow-up (tk-d5813e24387d, card R1-READER-3b-tidy)

In plain words: codex's read of this step found one real mistake in the advice and two smaller points. All three are fixed in the follow-up card; the code's behaviour
changes only in the third.

- **B1, the wrong-account advice was wrong.** The record, the README and the `send_refused` sentence in `status` said to fix a wrong `account` and run `resume`. A
  waiting report is saved as fixed bytes that already name the old account, and `resume` only renews its retry budget and sends the same bytes: five more refusals.
  The advice now has two cases. If the mistake is on Leasewise's side (the gateway's owner account registered wrongly), correct it there and `resume`: the same bytes
  are accepted. If the reader's own `account` was wrong, the saved report can never be accepted: fix the value, run `reporter reset --confirm` (step 3a's archive and
  fresh identity) and approve and adopt the new reader again. The waiting report is never rebuilt or edited. Changed: this record, the README (the `account` bullet and
  the table row for `send_refused`) and `status.MONEY_READER_ACTIONS["send_refused"]`. Pinned by tests that drive the real receiving path: wrong account, five
  refusals, `send_refused`; the value fixed and `resume`: the identical bytes are refused again; the account corrected on Leasewise's side and `resume`: the same bytes
  are accepted; `reset` with the value fixed: a new reader's report is accepted.
- **N1, the interval is a speed limit, not a freshness promise.** The comment in `config.py`, the README and this record claimed the newest report "can always be
  adopted". A slow receipt catch-up, the other uploads and the delivery all add age, and Leasewise rightly refuses a report that is too old. Only the words changed;
  the receiver's check is untouched.
- **N2, the two command lists differed by one in length.** The report command's checker removed the final `--json` before applying the receipt command's length bound,
  so a 17-item report command passed while a 17-item receipt command was refused. The bound now covers the complete configured list, the fixed ending included
  (`gateway_report: the command may have at most 16 items, the fixed ending included`). The receipt command is unchanged. Tests: 17 refused (red first), 16 accepted,
  the receipt command at 16 and 17.

## Limits and notes

- Live use waits for the maintenance window and step 4. Nothing was run against a real gateway.
- A report is only as exact as agenttalk's stated guarantees (receipt order, one month function); a test pins the boundary at `N`.
