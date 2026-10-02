# Step record - Card L1b: the shadow-request contract

Work item `card-l1b`, work order tk-622846146e18. Branch `card-l1b`, built on
top of the card-l1a head `643b8c1` (which already contains main `8d11bdc`).
Approved by the desktop lead 2026-10-02 (re-baseline decision D-005, Q3).
Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only.

## In plain words

The first agenttalk integration only watches: before each agent turn it asks
Leasewise for a "shadow" lease (no money held, no slot, no permission), and
afterwards it reports what happened. Two things were in the way.

1. The wrapper cannot know how big the agent's context is before the turn
   starts, and agenttalk's "fresh/resume" is not Leasewise's
   FRESH/SUMMARY/RESUME. The only honest answer is "unknown", and Leasewise
   rejected it. Now "unknown" is a legal, clearly labelled request, accepted
   for shadow seats only, and nothing is invented to fill the gap: no mode, no
   token count.
2. The reporting rules for leases that hold no money were not pinned. They
   are now pinned by tests, and the money fields of those reports are `null`
   (not 0) when no money was reserved, as the design says.

No new reading kind, no schema change, no new route or command.

## Files

- `quota_balancer/lease.py` - the unknown-context shape, the shadow-only
  check, the unknown-context branch of grant, null money fields in settle.
- `quota_balancer/errors.py`, `quota_balancer/http_api.py` - the new error
  code `CONTEXT_UNKNOWN` (exit 2, HTTP 400, one static message).
- `tests/test_cardl1b.py` - 32 tests. `tests/test_card4_enforce_fix.py` - one
  assertion updated (below).
- `docs/STEP-CARD-L1B.md` (this record) and a README section.

## Design as implemented

**The wire form of "unknown".** `context.mode` may be `"UNKNOWN"` only
together with `initial_tokens`, `dispatch_tokens` and `growth_tokens_per_call`
all `null` and `source` = `"unknown"`; `observed_at` stays a required, fresh
UTC-ms integer (at most 120 s old, at most 30 s ahead). Any mixture (an unknown
mode with a number or a known source, or a known mode with a null size or the
source `unknown`) is `INVALID_REQUEST`. The context object keeps its closed set
of keys. A non-string mode is refused too (an unhashable value used to raise a
raw `TypeError` in the enum check).

**Shadow only.** Right after the replay check, and before the profile or any
arithmetic, an unknown context on a seat whose operator setting is not `shadow`
is refused with the new `CONTEXT_UNKNOWN` (static message "an unknown context
is accepted for shadow leases only", HTTP 400, exit 2). Shadow is never derived
from anything except the seat's own setting, so the check is exact: metered,
metered_advisory and both subscription modes are refused, even a metered seat
whose gateway reports sized-ready.

**Nothing fabricated.** For an unknown-context shadow lease the stored
`context_mode` is `UNKNOWN`; `estimate` is exactly `{"context":"unknown"}`;
`input_cap` (NOT NULL in the schema, which is not rebuilt) is the model's own
honest upper bound, `context_ceiling x max_calls`, and `policy_snapshot`
records `"input_cap_basis": "model_ceiling"` (known-context leases carry no such
key); `money_hold` is null, there is no token and no hold row, and a shadow
lease already takes no slot. The response's `caps.expected_input` is null. The
output bounds still apply: `output_cap` <= `max_calls` x the model's output
ceiling, and `per_call_output_cap` <= the model's ceiling (both
`INVALID_REQUEST`), and the profile still bounds everything as in L1a
(unknown task class `NOT_FOUND`; candidate and ceilings `POLICY_DENIED`).

**Replay.** A byte-identical replay returns the stored lease before any
context, profile or enforcement check, so it survives a later change of the
seat's setting or the profile.

**Reporting for leases that reserved no money** (shadow, subscription_*,
metered_advisory), on the existing `/v1/leases/{id}/settle`. The behaviour
already existed; it is now pinned: a non-final receipt is accepted and leaves
the lease active; a final receipt with `actual_micro_eur` null settles it; a
non-final after a final is refused with the existing `RECEIPT_CONFLICT` (409,
and, being non-final, it does not park the account); an identical replay is a
no-op; a changed replay is `IDEMPOTENCY_CONFLICT`; a non-final after the TTL is
`LEASE_EXPIRED` (a late final is accepted). **New:** when the lease reserved
no money, `released_micro_eur` and `retained_micro_eur` in the response (first
call and replay) are `null`, not 0. Metered leases are unchanged (integers; a
final without money is still refused).

## Decisions (the lead's five, then mine)

1. Wire form as specified.
2. Shadow only, `CONTEXT_UNKNOWN`, checked after the replay and before any
   arithmetic (here: before the profile too, so an unknown context on a
   non-shadow seat is `CONTEXT_UNKNOWN` even when the task class is also
   unknown).
3. No fabricated numbers, as specified (`{"context":"unknown"}`, the ceiling
   bound, the basis in the snapshot).
4. Reporting contract pinned; null money fields.
5. Replay first.

Mine:

6. **Where `CONTEXT_UNKNOWN` sits among errors.** After the static request
   shape (a mixture is always `INVALID_REQUEST`, even on a non-shadow seat) and
   after the replay; before the profile checks.
7. **Unknown + the output bounds.** The unknown path repeats the two output
   checks the estimate does (total output <= calls x model ceiling; per-call
   cap <= ceiling) because the estimate, which carries them, is not run.
8. **The basis key only appears for unknown contexts**, so no known-context
   snapshot changes.
9. **The `released`/`retained` null rule is keyed on `money_hold` being null**
   (the lease reserved nothing), not on the enforcement name, so any future
   non-money mode behaves the same.
10. **No reading kind and no per-model usage fields** (the design keeps the
    usage kind out until its own validator ships).

## Rows -> tests (`tests/test_cardl1b.py`)

| Row | Tests |
|---|---|
| unknown_context_shadow_granted | `UnknownContextShadowGranted`: granted to a shadow seat (stored mode `UNKNOWN`, no token, no hold, no money); no slot taken (three in a row); the profile still bounds it; output bounds still apply; `observed_at` must be fresh (120 s and 30 s boundaries); a replay returns the stored lease even after the seat was reconfigured, while a new request is then refused |
| unknown_context_non_shadow_refused | `UnknownContextNonShadowRefused`: metered, advisory and both subscription seats get `CONTEXT_UNKNOWN` with the static message and no row; a metered seat with a ready gateway too; the check precedes the profile and the arithmetic; the code is in the closed set with exit 2 and HTTP 400; known contexts still work everywhere |
| unknown_context_mixed_fields_invalid | `UnknownContextMixedFieldsInvalid`: 18 mixtures (sizes with UNKNOWN, known sources, bad or missing `observed_at`, extra key, lower-case or list mode, known modes with null sizes or source `unknown`) are `INVALID_REQUEST`, also on a non-shadow seat |
| unknown_context_no_fabrication | `test_nothing_is_fabricated_in_storage`, `test_the_bound_follows_max_calls`, `test_a_known_context_keeps_the_estimate_and_no_basis_key` |
| non_money_reporting | `NonMoneyReporting`: for shadow, advisory and both subscription seats a non-final, a second non-final and a final with null money; non-final after final -> `RECEIPT_CONFLICT` without parking; identical replay a no-op (non-final and final); changed replay conflicts; non-final after the TTL refused, late final accepted |
| non_money_null_release | the same tests assert null money fields on first call and replay; `test_metered_leases_keep_integer_money_fields` pins the metered side |
| HTTP path | `HttpPath`: shadow grant with an unknown context -> 201, no token, null hold and `expected_input`, stored `UNKNOWN`; non-shadow -> 400 `CONTEXT_UNKNOWN`; mixed -> 400 `INVALID_REQUEST`; per-attempt and final reports -> 200 with null money fields, replay, then 409 for a non-final after final |
| privacy_l1b | `PrivacyL1b`: sentinels in the mode, source, `observed_at` and sizes are never in an error or any stored row; the `CONTEXT_UNKNOWN` message carries no request text; the stored unknown rows hold only closed text; a wire sentinel in the mode is not echoed |

## Pre-existing tests changed

- `tests/test_card4_enforce_fix.py::test_advisory_settle_null_money`: it
  asserted `released_micro_eur == 0` and `retained_micro_eur == 0` for an
  advisory lease, the very drift this card fixes (card 5 recorded it as a
  residual risk). It now asserts both are `None`, with a comment; the state
  assertion is unchanged. No other test changed (the money assertions for
  metered leases all pass unchanged).

## Evidence

- **Red first.** Commit `ff929e5` (tests only, on the card-l1a head):
  `python -m unittest tests.test_cardl1b` -> `Ran 32 tests ... FAILED
  (failures=7, errors=17)`: 24 of 32 red (no `CONTEXT_UNKNOWN`, the unknown
  shape is rejected, settle returns 0/0). The 8 that pass on the old code are
  guards that must keep holding (for example known contexts and the metered
  side).
- **Green.** `tests.test_cardl1b`: 32 tests OK. Full suite `python -m unittest discover -s tests`: `Ran 741 tests in 292.0s ... OK` (709 on the card-l1a head + 32). With TMP/TEMP at an empty folder the run left nothing behind.
- **Mutation spot-check.** 11 single-point mutations (the shadow-only check,
  both halves of the unknown shape, the known-source check, the ceiling bound,
  the basis key, the output bound, both null rules, the unknown estimate, the
  non-string mode guard): all killed.

## Judged not testable

Concurrency between a grant and a seat reconfiguration (both under `BEGIN
IMMEDIATE`; not new).

## Residual risk

- `input_cap` for an unknown context is a bound (ceiling x calls), not an
  estimate: a reader of the stored column must look at `estimate` and the
  snapshot's `input_cap_basis` to know that. Nothing consumes it for shadow
  leases today.
- The unknown context of a shadow lease is never amended later: a wrapper
  that learns the size can only report it in its receipts (token counts).
- A subscription lease's usage is still only the token counts in its receipts;
  the per-model usage reading kind stays a later card.

## Sync with the fixed card L1a (fix round 1, head 16a2a88)

In plain words: this branch was built on L1a's first head. L1a was then fixed,
and its fixes touch the same grant and settle code, so the fixed L1a is merged
in here. Git found no text conflicts; the risk was in how the two behaviours
combine, so that is what was checked and tested.

What had to keep working together (all verified, with the new tests
`MarkerInteractionWithL1a` in `tests/test_cardl1b.py`):
- **Replay first.** L1a's versioned replay comparison (`_stored_hash_matches`,
  the older fingerprint for snapshots without `task_class`) runs before the
  unknown-context and `CONTEXT_UNKNOWN` checks. Tested: a shadow lease stored in
  the pre-L1a format replays for its identical request even after the seat was
  reconfigured to a non-shadow mode.
- **The format marker.** The `policy_snapshot` of an unknown-context lease
  contains `task_class` (and `policy_revision`) next to `input_cap_basis`, so the
  current fingerprint is used on its replay; a changed task class conflicts.
- **Tokens.** Shadow stays non-token-bearing with the gate closed or open; a
  shadow unknown-context lease gets no token on grant or replay and cannot pass
  `/check` (`FORBIDDEN`).
- **Settle.** L1b's null money fields for leases without a money hold are
  unchanged by L1a's changes (the existing `NonMoneyReporting` tests pass).

Evidence: 4 mutations of the combined code (the current hash, the legacy hash,
the shadow token rule, the snapshot marker) are all caught. Full suite: see the
reply of this task.

## Fix round 1 (tk-e17c89165391)

In plain words: the cold read found no way for an unknown-context shadow lease to
gain spending authority. One defect remained: a model whose context ceiling is
huge but still accepted by the policy loader made the unknown-context grant fail
with a raw database error (shown to the caller as a 503 "store fault", which
invites a useless retry) instead of a clear "bad request". It is now a controlled
refusal.

| Finding | Fix | Tests (`UnknownContextInputCapBoundary`, `tests/test_cardl1b.py`) |
|---|---|---|
| F1 (major): the unknown-context input bound `context_ceiling x max_calls` was computed unchecked; with a ceiling of 4611686018427387904 and two calls the product (9223372036854775808) overflowed the column, raising `OverflowError`, which grant's `sqlite3.Error` handler does not catch | the product goes through the module's checked integer rule (`policy._check_int`, signed 64-bit) before any write: too large -> `INVALID_REQUEST` (400, exit 2, the static message) | `test_a_product_beyond_signed_64_is_invalid_request` (store: static message, no lease, hold or audit row written); `test_the_largest_product_that_fits_is_granted` (ceiling 4611686018427387903, two calls -> granted, `input_cap` 9223372036854775806 in the response and the table); `test_other_call_counts_at_the_edge` (ceiling 2^63-1: one call is granted at exactly the maximum, two calls refused); `test_over_http_the_refusal_is_400_not_503`; `test_a_known_context_path_is_unchanged_for_the_same_model` |

**Other arithmetic checked (the same class).** Everything L1b added or touched:
- `request["max_calls"] * model[4]` (the output bound, `lease.py` ~737) is only compared, never stored, and Python integers do not overflow: no gap.
- `policy.estimate` and the output/per-call checks on the known path were already checked by card 3 and are unchanged.
- `hold_sized` is `None` on the unknown path and nothing multiplies it; `money_hold` stays null.
- The settle change is a null rule with no arithmetic; the `released`/`retained` nulls are assignments.
- `expires_at = now_ms + ttl_ms` already had its own signed-64 check (`lease.py` ~856).
- The snapshot only carries `input_cap` (now checked) and constants.
So the one unchecked product was the unknown-context input cap.

**Evidence.** Red first at `f5bda58` (tests only, production unchanged):
`python -m unittest tests.test_cardl1b.UnknownContextInputCapBoundary` -> `Ran 5
tests ... FAILED (failures=1, errors=2)`: ERROR
`test_a_product_beyond_signed_64_is_invalid_request` and
`test_other_call_counts_at_the_edge` (`OverflowError: Python int too large to
convert to SQLite INTEGER`), FAIL `test_over_http_the_refusal_is_400_not_503`
(503 `STORE_ERROR`); the two boundary-fits tests pass already (they pin the
largest accepted values). Green: the 5 new tests pass; `tests.test_cardl1b`: 41 tests OK; full suite `Ran 783 tests in 318.2s ... OK` (778 before + 5), nothing left in an empty TMP.
