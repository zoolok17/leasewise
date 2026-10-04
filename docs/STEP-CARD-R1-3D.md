# Step record - Card R1-3d: the operator's commands over a gateway's evidence, the refusing `reconcile`, the `status` block, the read-only builder

Work item `card-r1-3d`. Branch `card-r1-3d` from main `ea702daa2de5d9390928a2f3b5df75d620706ab3`. Design: `C:\lw\records\DESIGN-R1-3.md` (revision 2, SHA-256
`71c07b9c...3ab9`; the body wins over the decision log, 13.12): 2.1 to 2.4, 6, 8.1 to 8.3, 9, 10.1, 10.2. Interpreter: CPython 3.13 (`C:\lw\venv`), standard
library only. Synthetic reports only; no gateway, key or provider is touched.

## In plain words

Leasewise already judges every gateway money report and writes down problems as open items (card R1-3c). This card gives the **operator** the commands to
act on them and shows the result in `status`:

- **approve** a gateway's pair of price and child-cap fingerprints;
- **adopt** one quiet report as the trusted starting point. The same step chooses the one reader to trust, retires every other waiting reader for good,
  closes the open items the operator names, and writes an audit line with no amounts;
- **see** all of it in `status`, including why the newest report cannot be adopted yet.

The old `reconcile`, which cleared problems without naming any evidence, now refuses and points to `gateway-adopt`. A read-only builder turns the trusted
chain into the input of the billing evaluator. **No seat, lease or admission decision changes:** nothing in the grant path reads the evidence until card
R1-5 (a test checks that `lease.py`, `http_api.py`, `money.py`, `reduce.py` and `billing_eval.py` do not mention the builder).

## What was built

1. **`gateway-approve`** (`store.gateway_approve`): records the pair, audited (`gateway_approved`). A hash that is not 64 lowercase hex characters (a null
   child-cap fingerprint included) and an unknown gateway are the existing `INVALID_CONFIG` sentence and write nothing. Approving the same pair twice
   changes nothing, not even the audit.
2. **`gateway-adopt`** (`store.gateway_adopt`): `BEGIN IMMEDIATE`, then all ten items of the predicate are read again inside the transaction
   (`evidence_view.adopt_blockers`), then the steps of 2.3 in order, all or nothing: the epoch mapping if new; the adoption row; the reader made `current`
   and every other waiting or current reader retired (`_promote_reader` retires first); the named transitions resolved (a retired reader's `reader_changed`
   is resolved as retired: its reader row says so); the chain state and `gateways.state` (`ok` when nothing is left open); one audit row with the adoption
   id, the chosen and retired incarnations and the resolved ids. The fault-injection point `store._after_adopt_step` lets a test stop after each step.
   A refusal is `ADOPTION_REFUSED` (exit 2) with the fixed sentence; the reason is never in the sentence.
3. **`reconcile`** (`store.reconcile`) raises `EVIDENCE_REQUIRED` before it opens the store, so it changes nothing, whatever the alias.
4. **Two closed words** in `errors.py` (`ADOPTION_REFUSED`, `EVIDENCE_REQUIRED`), exit 2, with their fixed sentences, through the existing code-list and
   message tests (no new test needed: the registry tests iterate every code).
5. **The `status` evidence block** (`evidence_view.project`, called from `store.status`): `state`, `approved`, `adopted`, `latest`, `readers`,
   `last_quiet`, `open_transitions`, `adopt_blockers`, the four counters, `action`. The block is absent for a gateway with no version-4 report. The two keys
   of card R1-3e (`unreceipted_micro_eur`, `receipts_exceed_settled`) are not there. `adopt_blockers` uses the closed words of 8.3, in the design's order.
6. **The read-only builder** (`evidence_view.usable_chain`, `usable`, `inputs`; `store.evidence_inputs`): per gateway of an account, the event ids of the
   usable chain (the adopted report and every later in-order report of the same reader, epoch, generation and month; late, repeated or colliding reports
   are left out), the stored reading envelopes, the open items' reports as unresolved, and a closed word per gateway saying why a gateway is not usable.
   A gateway that is not usable is listed with an **empty** chain, which the evaluator reads as unusable.
7. **The evaluator's reading check accepts a version-4 reading** (`billing_eval._validate_reading`): a payload that names a reader incarnation is
   validated against exactly the version-4 keys. Before this, the real path refused every version-4 payload, so R1-3c's evaluator change had only been
   exercised over a hand-made context (the lesson `leasewise-evaluator-rejects-v4-payloads`).
8. **Two small folds:** M-022 (the generation case of `tests/test_cardr13c_evidence.py` is renamed to what it proves,
   `..._shows_the_identity_mismatch_and_no_roll`) and M-021 C1(c) (`docs/STEP-CARD-R1-M-FU.md` names `c48ee96` and `45c09ec` as the red-first baselines,
   with the test revision `185489e`; both counts were re-run and match).

## The two carried requirements

- **(i) The evidence comes from the adopted chain, never from the legacy `gateways` columns.** `evidence_view` never reads `last_seq`, `ledger_epoch`,
  `state` or `last_report_at`. Tests: with those columns forced to "ok, fresh, newest" and no adoption, the gateway is `unadopted` with an empty chain; with
  an adopted chain and a later report of another reader that moves the legacy columns on, the gateway is `transition_open` with an empty chain; a report
  of another epoch, generation or reader is never in the chain, even with every item marked resolved.
- **(ii) The real path accepts a version-4 reading.** Tests go from stored readings, through the builder, into `billing_eval._validate_reading` and
  `evaluate_billing`, not through a hand-made context. The check is still exact: a missing or extra key, a wrong type, or a null hash pair is refused.

## Choices made (each tested)

- **`open_transitions_unnamed` in `status`** means "name these ids": `status` asks the predicate with nothing named, so the word is present whenever
  something is open. The adopting command itself compares the named ids with the open ones: one left out, or an id that is not open, is refused.
- **"Newest report"** in `status` and the blockers is the last version-4 report that arrived for the gateway, whoever sent it. A report of a pending
  reader is therefore shown with its own blockers (and an open `reader_changed` after an adoption).
- **The state `none`** is in the closed list of states but is never printed: with no version-4 report the whole block is absent (8.3).
- **The state order** follows 2.4: `unadopted` (no adoption, or its reader is not current), `unapproved`, `transition_open`, `stale`, `period_mismatch`,
  else `usable`.
- **A malformed `--resolve` value** (not a whole number) is the existing `INVALID_CONFIG` sentence and is never echoed. `--resolve` is read as text so
  that the command line parser does not echo it either. A malformed `--evidence` is simply a report that is not bound (`not_bound`).
- **Duplicates cannot be made by ingest:** the event id is derived from the report's own identity, so the same identity and content under another event
  id is refused by the validator. The `duplicate_reports` counter is not exercised through ingest here (R1-3c covers it with the pure rules).

## Existing tests that changed (each with the reason; no check was weakened)

- `tests/test_card2.py`, `test_reconciliation_sticky_epoch` and `test_reconciliation_sticky_hash`: they used `reconcile` to clear the sticky state and
  asserted exit 0 and `ok`. `reconcile` now refuses by design (F10), so they assert **exit 2 and that the state stays `reconciliation_required`**, and
  say where the clearing is tested. They use version-2 readings, which can never be adopted. **The same two sticky states, with the same "no self-clear" checks,
  are cleared by `gateway-adopt` in `tests/test_cardr13d_operator.py`** (`test_an_epoch_change_that_set_reconciliation_required_is_cleared_only_by_
  adopting_a_named_report`, `test_the_hash_change_sticky_state_is_cleared_the_same_way_after_the_new_pair_is_approved`). Three comments in the same file
  that said "until an operator runs reconcile" now say `gateway-adopt`.
- `tests/test_cardr13c_evidence.py`: one test renamed (M-022); nothing else.
- README: the example and the verb list use `gateway-adopt`; a new section describes the card.

## Tests (`tests/test_cardr13d_operator.py`, 59)

`Approve` (6): recorded and audited once; null and malformed fingerprints; unknown gateway; a changed pair is unapproved; the command output and exit codes;
a move to another machine deletes the approval and the route back (approve, adopt the new owner's reader naming `owner_changed`) works. `Predicate` (17):
**every item on its own** (1 bound, twice; 2 valid; 3 current month; 4 fresh at 120 000 and 120 001 ms and in the future; 5 approved; 6 identity and a
retired reader; 7 newest and contested; 8 ready; 9 every open item named, one left out refused, an id that is not open refused; 10 quiet), each with the
refusal, the word in `status`, and **the whole database compared before and after**. `Adoption` (11): the rows and the audit without amounts; **a fault after
each of the steps 2 to 7 leaves the database exactly as it was**; two pending readers, one adoption; I1/I2/I3 choosing I3 and retiring I1 and I2 with the
`reader_changed` rows resolved as retired; a retired reader is stored, counted, never current and never re-opens anything (one row per identity); a resolved
conflict replayed after the adoption does not re-open; the two sticky states cleared; no lease, receipt or adjustment touched; the command line;
two adoptions in a row. `Reconcile` (3). `Status` (9): absent without a version-4 report, closed keys, the states in order, `period_mismatch`, every field
after an adoption, the action sentence, the counters, nothing echoed, the command line. `Builder` (13): usable right after adoption, the chain's
contents, a late report, each not-usable reason, a gateway with no report, the legacy columns, other epochs/generations/readers, read-only and not called
from the grant path, **a stored version-4 reading through `_validate_reading`, exactness of that check, stored readings through the builder into
`evaluate_billing`**, an unusable gateway and an open item.

**Red first, against main `ea702da`.** The module `evidence_view` and the commands are new, so the whole file fails at import on main (59 tests, 1 error).
For the evaluator change alone, with the old `_validate_reading` put back and everything else kept, **4 of the 13 builder tests fail** (the four that go
through the real path).

## Honest notes and limits (10.2)

- Nothing in the grant path reads the evidence, so nothing about a seat, lease or admission changes.
- **A mistaken reader choice is permanent for that identity.** A retired reader never becomes current again; the way back is to give the intended reader a
  new state (a new incarnation) and adopt that.
- The wait for a quiet moment is the operator's: a busy gateway cannot be adopted until a report says it is quiet. Whether a report that is not quiet may
  support enforced admission is a gate of card R1-5.
- The 120-second freshness uses the report's own time against Leasewise's clock; there is no skew compensation.
- The CLI tests that need the real clock (`test_the_command_prints_the_adoption_id...`) send a report stamped at the time of the test.
- The comparison of receipts with the report (`unreceipted_micro_eur`, `receipts_exceed_settled`) is card R1-3e.
