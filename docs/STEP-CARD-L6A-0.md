# Step record - Card L6a-0: unknown token counts on a gateway's final receipt

Work item `card-l6a-0`, work order tk-f1228faf07be. Branch `card-l6a-0`, from main `fa3da16f7ce0507ca18c28bac74744027ec00c54`
(card 7c-2 merged). Spec: `C:\lw\records\DESIGN-6A.md` section 8.2 and decision O16 (section 16; file sha256
`f1480c4dab396a2937e0be28b520d35e38e6ecf61886e48da9eb381837345898`). Interpreter: CPython 3.13 (`C:\lw\venv`),
standard library only. Audience: the lead and the reviewers. A small Leasewise-only server change.

## In plain words

A gateway that "charges the reserve" for an attempt it could not prove knows how much money that cost but not
how many tokens were used. Leasewise refused any final receipt with an unknown (null) token count for a lease
that is not shadow, so the lease could never settle, and keeping the receipt waiting for ever was rejected.

Now a final receipt may leave the input and/or output count unknown in exactly one case: it comes from the
lease's own gateway, it is final, it carries the money, its cached count is a whole number, and the lease is
`metered` or `metered_advisory`. The lease settles, a token hold is kept whole (an unknown is never a zero), and
the lease is marked `usage_unknown` so nobody reads the missing numbers as zero. Everything else is as before.

## What changed

`quota_balancer/lease.py`:
- **The check** (was at :1230-1232, now after the gateway comparison). Unknown counts for a lease that is not
  shadow are refused unless `_unknown_counts_allowed(receipt, lease)` holds: `final`; `gateway_id` present and equal
  to the lease's own; `actual_micro_eur` present; `cached_input_tokens` present; enforcement `metered` or
  `metered_advisory`. **The gateway comparison now runs first**, so another gateway stays `RECEIPT_CONFLICT`
  (and parks the account on a final receipt) instead of being reported as an invalid shape. That is the one
  visible change of order: a non-shadow receipt with a null count AND a foreign gateway used to be
  `INVALID_REQUEST` and is now `RECEIPT_CONFLICT`.
- **The holds** (was :1335-1347). For a hold whose metric is tokens and a null `input_tokens`, `settled_actual`
  is the hold's `reserved` amount (kept whole, no refund) and no token overrun is computed. Money holds are
  unchanged. A null is never turned into 0.
- **The mark.** No new column. `usage_unknown(conn, lease_id)` is true when the lease is not shadow and has a
  final receipt with a null input or output count. The audit row written at settle (was :1414-1418) is
  `{"final": 1}` as before, plus `"usage_unknown": 1` for the same case. A shadow lease's unknown counts are its
  normal state (card L1c) and are not marked.
- **Replay and identity** needed no change: the identity comparison already treats a null as equal to the same
  null (a changed body is `IDEMPOTENCY_CONFLICT`, an identical one a replay).

`README.md`: a new section "Card L6a-0" and a pointer in the L1c section. It says in plain words that **any later
report or analytic must treat a null token count as unknown, never as 0**.

No store change. `receipts` already allows null counts (card L1c rebuilt it, `migrate.py` :499-511); a test
asserts that on the live store before anything relies on it.

## Tests (`tests/test_cardl6a0_null_counts.py`, 26 new)

Red first: before the change 19 of the 24 tests written first failed (15 errors, 4 failures); the other 5 are the
refusals that already held. Two changes followed: the subscription test was split in two (one per seat) and a test
for the enforcement list was added, 26 in all, all passing.

| Group | What it proves |
| --- | --- |
| Accepted (5) | both counts null with a cost settles and marks the lease (stored as NULL, audit detail, predicate); input null with output known and the reverse; known counts are not marked and the audit row is exactly as before; the predicate is false with no final receipt, for a missing lease and after a non-final receipt; a zero-spend final with unknown counts |
| Refused (10) | without the cost, with a null gateway, with another gateway (`RECEIPT_CONFLICT`, account parked), non-final, unknown cached count: all `INVALID_REQUEST` or conflict as specified and nothing stored; subscription leases stay strict (both kinds); the rule names its enforcements; a shadow lease is unchanged; a metered lease still needs the cost |
| Identity (2) | an identical replay is a replay and changes nothing; a changed body under the same event id is `IDEMPOTENCY_CONFLICT` (four variants, including null versus 0) |
| Holds (4) | a metered lease with holds keeps its token hold whole and settles the money hold at the cost; spend over the hold records the money overrun and no token overrun; known counts settle the token hold as before; the account budget sum counts the cost and ignores tokens |
| Store (2) | the live store's `receipts` token columns are nullable, on an imported and on a fresh store |
| Over HTTP (3) | the real handler: a null-count final settles with `200`; the same again is `200`, a changed body `409`; each refusal over the wire |

As a check, six deliberate breakages were tried (the `final` condition, the "own gateway" equality, the cost
condition, the enforcement list, the hold rule, the predicate). Four were caught at once, and the enforcement list
was caught after its test was added.

**One guard is defence in depth:** the "equals the lease's own gateway" condition repeats what the earlier gateway
comparison already enforces, so no test can fail it on its own. It stays because the spec lists it. The enforcement
list is only reachable through a state the grant path does not produce today, so its test sets the state by hand.

**One existing test changed.** `tests/test_cardl1c.py` `test_null_is_refused_for_every_non_shadow_enforcement`
pinned "a null count is refused for every non-shadow lease". It now skips the one combination the new rule
accepts (a final receipt on a metered or metered-advisory lease, own gateway, with the money, input or output
null) and says where that case is tested; every other combination is still refused there.

## Gates

Memory free at the start: 7.4 GB. The full suite in chunks, each under 8 minutes, with the new file in a new
chunk `test_cardl6*.py`; the counts are in the reply. `ruff --select F` is clean on the changed files. `git status`
is clean, the identity is `leasewise-team`, the index is LF.

## Limits

- The gateway side that produces such receipts does not exist yet (card 6a); today only a test or an operator can
  send one.
- No report reads token columns yet; a future one must treat null as unknown.
- If Codex's delta read of the 6a design changes section 8.2, the change lands in this card's next commit.
