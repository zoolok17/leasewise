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
  **A wrong value** gives a definitive refusal from Leasewise for every report; after five in a row the reader stops as `send_refused` (`resume` after the value is fixed).
- **`interval_seconds`** (`[gateway_report]`, a whole number, default 60, 30 to 100): a new report is made at most once per interval, measured on a monotonic clock
  kept in memory (`gwcycle.Pacer`, set when a report is made, not when one is tried). A restart reports at once. A waiting report is still sent every cycle under the
  existing back-off; a cycle that skips because of the interval writes nothing and counts no word. The ceiling keeps a report well inside Leasewise's 120-second freshness
  window (`evidence_view.FRESH_MS`); raising `poll_seconds` instead would not work, because the same cycle carries the other uploads.
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

## Limits and notes

- Live use waits for the maintenance window and step 4. Nothing was run against a real gateway.
- A report is only as exact as agenttalk's stated guarantees (receipt order, one month function); a test pins the boundary at `N`.
