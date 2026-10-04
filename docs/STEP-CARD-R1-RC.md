# Step record - Card R1-RC: the receipt path reads pages with or without the month (closes R-017), and the digit checks

Work item `card-r1-rc`. Branch `card-r1-rc` from `card-r1-reader-1` at `b6381f18f32ebefd4547deb1f01fdce3666d68d2` (it needs `month_rows` and `MonthReceipt`;
it merges after R1-READER step 1). Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only. Synthetic receipts and agenttalk's documented page only.

## In plain words

When the operator upgrades the live gateway to agenttalk `921c82d4` or later, every receipt on a page carries the month, `charge_period`. The reporter's
receipt path (it settles seat leases from the gateway's receipts) only knew the old shape and would have refused every row from that day on: safe, but blind.
This card lets that path read **both shapes**, so it keeps working before and after the upgrade. **It reads the month and does not keep it:** what is stored
and what Leasewise receives are the same as before. It also fixes the close-time check, which accepted the digits of other scripts. **R-017 is closed by
this card.** Nothing visible changes for the operator today.

## What was built

1. **A third row shape, chosen by the caller** (`receipts.EITHER`, a value of `month_rows` next to `False` and `True`). A page whose rows ALL have
   `charge_period`, or NONE has it, is read; a page that mixes the two is `bad_row`. Month rows are checked as before, including "no calls means no month".
   The two strict shapes are unchanged for their callers (`fold_page`, the R1 reader, still needs month rows).
2. **`run.py` `_take_pages` uses `EITHER`.** `state.capture_page` stores the same fields as before, so the month is neither stored nor sent.
3. **The close-time check** `_ISO` uses `[0-9]`. This tightens the check for every caller, on purpose.
4. **The sweep** (lesson `lw.regex-digit-class-takes-other-scripts`). Every regular expression in `quota_balancer/` that uses `\d`, `\w` or `\s` (found by a
   scan of all files, including multi-line patterns checked by hand):

| Place | Input | External? | Done |
|---|---|---|---|
| `reporter/receipts.py:60` `_ISO` | a receipt's close time, printed by the gateway | yes | fixed |
| `reporter/parkreader.py:54` `_PARKED_AT` | the time in a park marker file written by the agent wrapper | yes (another program's file) | fixed |
| `reporter/config.py:468` `_SID_TEXT` | Windows security-identifier text from the operating system | the system, not a person | fixed (same folder) |
| `reporter/gwreport.py:41` `_PERIOD` | a month in the gateway report | yes | already `[0-9]` (R1-READER fix) |
| `reporter/receipts.py` `_MONTH` | a receipt's month | yes | already `[0-9]` |

   No pattern outside `reporter/` uses `\d`, `\w` or `\s`. A neighbouring instance outside regular expressions was also fixed: `events.take_snapshot`
   took a journal segment number with `str.isdigit()`, which is true for a superscript digit (and `int()` of that raises) and for full-width digits; it now
   requires ASCII digits. The other `isdigit()` calls read values the reporter wrote itself (`state.py:486`, `status.py:295`) or already check `isascii()`
   (`ovh_adapter.py:233`), and were left alone.

## Tests (`tests/test_cardr1rc_either.py`, 16)

`Pages` (9): an existing-shape page; a month page and **agenttalk's documented page byte for byte**; an empty page; a mixed page in either order is
`bad_row`; no calls with a month is refused (and with a null month accepted; calls above 0 with cost 0 may name a month); the same receipt in both shapes has
the same fields; the strict shapes are unchanged; the other checks still apply; `run.py` asks for `EITHER`. `DigitChecks` (2): a close time in full-width
digits is refused in every shape and ASCII is accepted; the park-marker time, the SID text and a segment number take ASCII digits only. `Cycle` (5), against a
real in-process Leasewise: **the same receipt in both shapes gives identical stored rows and identical receipts at Leasewise**, and the same with a null month;
a gateway that changes shape between two pages is read both times; a mixed page is refused and nothing is stored, no cursor moves and nothing is sent; a
month row with no calls and a month is refused in the cycle too.

**Red first, against `b6381f18`.** The name `receipts.EITHER` does not exist there, so the whole file fails at import. With only that name supplied (and no
other change), 9 of the 16 tests fail (8 failures and 3 errors, counting subtests).

## Existing tests

One changed, for a stated reason: `tests/test_cardr1reader1_report.py` had a pin test that `run.py` still reads the existing shape (the compatibility
finding of R1-READER step 1). This card replaces that pin with the new behaviour (`test_the_run_path_asks_for_either_shape` and the cycle tests), so the pin
is removed. A second one, for a stated reason: the test double of `read_page` in `tests/test_card7l6a1b_wired.py` (the receipt time budget tests) took
`(source, after, stop)`, and the real function is now also called with the row shape, so the double takes that argument too (one line; no assertion
changed). All other existing tests pass unchanged.

## Honest notes

- The month is read, never stored or sent on this path. If a later card needs it there, that is a schema change of its own.
- Stricter close-time check: a gateway that printed a close time with non-ASCII digits (it does not) would now be refused.
- Before the merge: this branch is built on `card-r1-reader-1`. If that head changes, this card is rebased onto the new head.
