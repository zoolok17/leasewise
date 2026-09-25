# Step record — Card 3: policy arithmetic

Work order: tk-798ec5fee486 (build: Leasewise card 3 (policy arithmetic)
+ card-2 carry-over). Seat: qwen-quota-dev-2. Branch: `card-3` (built on
`origin/main` @ 05c18af, the card-2b design merges; the branch was
rebased onto it - the card-2b commits touch only docs/DESIGN.md).
Date: 2026-09-24. Interpreter: CPython 3.14.4
(standard library only: `re`, `json`, `sqlite3` in the store, `argparse`
in the CLI).

The card is the policy arithmetic from docs/DESIGN.md section 8, row 3,
plus section 3's estimate/hold formulas, section 4's composed limits,
section 6's profiles (80% sweep and fallback tiers), and section 7's
shared contract. Pure functions over fixtures: no wrapper, no HTTP, no
live provider calls, no store access in the arithmetic.

## Card-2 carry-over (committed first, on its own)

`test(card-2): same-epoch metadata ordering carry-over` — 42cef7e.

Same-epoch metadata ordering was untested: an older observation arriving
after a newer one, in the same reset epoch, must apply the high-water on
`used_bps` but must not overwrite the newer observation's metadata
(`observed_at`, `confidence`, `source_stale`, `covered_seq`,
`source_event_id`). `ReducerRules.test_older_same_epoch_observation_keeps_
newer_metadata` pins the reducer's ordering guard in
`reduce.reduce_window` (the final branch, which keeps the stored
`e_conf`/`e_stale`/`e_cov`/`e_ev` while taking the higher usage).
Mutation check performed on this card: in a scratch worktree the guard
was removed (the older sample's metadata overwrites the newer one) and
the test went red (`AssertionError: True is not false` on
`source_stale`); on the unmutated tree it is green.

## What was built

- **Schema, migration version 3** (`migrate.py`, c7f9fba). Card 1's
  version-1 DDL and card 2's version-2 DDL are kept byte for byte (the
  diff touches only the module docstring, `SCHEMA_VERSION`, and
  `_STEPS`); version 3 adds the seven section-7 policy tables —
  `projects`, `teams`, `project_accounts`, `seats`, `models`,
  `profiles`, `limits` — plus the exact `limits_scope` uniqueness index
  `ON limits(scope, scope_id, metric, period, COALESCE(window_key,''))`.
  The migration keeps the card-1/2 discipline: preliminary version read
  takes no lock; the WAL switch is retried on the busy condition;
  `BEGIN IMMEDIATE`; the version is re-read inside the lock; every
  pending step's DDL plus the version stamp commit as one transaction; a
  newer, unknown store is rejected before anything is written and left
  byte-for-byte unchanged (card-1 tests updated for the new table set
  and the version-4 "newer unknown" store).
- **`policy.py`** — the pure arithmetic (57243e4). No I/O, no clock, no
  store access; total over its validated integer inputs:
  - **Section 3 formulas.** `E = N*C + G*N*(N-1)/2`; `I =
    ceil(E*margin_bps/10000)` (default margin 12500 bps, allowed
    10000..20000); the request-sized hold `H = ceil((I*input_rate +
    O*output_rate)/1e6) + 2*M - 1` (the 2*M-1 allowance covers separate
    input/output ceilings on every attempt). Rates are integer
    micro-EUR per 1,000,000 tokens, bound to the model's
    `policy_hash`. The selected hold is always at the model's *reserve*
    rates; the tariff-rate figure and the fixed reserve `M*231999` are
    comparisons only (F05: the fixed per-attempt path is never the
    selected lease mode).
  - **Model ceiling.** `C + G*(N-1)` above the model's input ceiling
    denies `CONTEXT_PROJECTION_EXCEEDED`; the oversized bound is never
    clipped (splitting or a FRESH/SUMMARY re-estimate is the remedy).
    The per-call output cap must not exceed the model output ceiling.
  - **Composed limits (section 4).** `composed_limits` returns the
    tightest hard ceiling per (metric, period) across the
    platform/project/team/seat scopes — limits compose by their
    tightest ceiling, never by adding duplicate caps; the account
    budget (the one money home, F04) tightens (money, month). A soft
    bound is carried only while at or below the effective hard.
    `check_admission` is the admission rule: settled + unresolved holds
    + new hold <= each applicable cap, else `POLICY_DENIED`. A
    (metric, period) with no cap is unbounded.
  - **The 80% sweep (section 6).** `sweep_deferred`: a cold sweep
    defers when the window is *strictly* above 8000 bps.
  - **Fallback tiers (section 6).** `select_tier` walks the ordered
    candidates then the ordered fallback (a model alias, or the terminal
    'park', which must be last and at most once); the first unblocked
    tier wins. A fallback hit is a new estimate/request/session — only
    the tier is returned, never a per-tool hop. cold_sweep's fallback
    is [park]: no automatic premium bypass.
- **`errors.py`** — adds `INVALID_REQUEST`,
  `CONTEXT_PROJECTION_EXCEEDED`, `POLICY_DENIED` with static messages
  and exit 2 each (see Interpretation notes).
- **`tests/test_card3.py`** — the four named checks plus the rule tests
  (see Tests).
- **README** "Card 3" section.

Interpretation notes (where the card leaves a choice):
- **Rate units.** Section 3's formula divides the rate product by
  1,000,000, so the rates are integer micro-EUR per 1,000,000 tokens.
  The fixture model's rates were chosen so the section-3 worked example
  pins exactly (C=20000, N=4, G=2000, O=4000, M=5): tariff (400000,
  2700000) gives the analogous 56809 upper estimate, reserve (460000,
  3815000) gives the 68169 reservation, and 5*231999 = 1159995.
- **Exit codes.** Card 1 froze 0 = success, 2 = invalid config/schema,
  3 = store IO. All three card-3 errors are client-side request
  conditions (fix the request, re-estimate, or stay within policy), so
  all three map to exit 2 — the same class as card 1's 2. None is a
  store fault. Section 7's wire statuses (400 / 400 / 403) are card 5's
  concern; card 3 is pure and has no CLI verb.
- **"Defer above 80%"** is read literally: strictly greater than 8000
  bps defers; at exactly 8000 the sweep still runs (pinned by
  `test_sweep_defer_above_80_percent`).
- **config-import is not extended in this card.** The card-3 row is
  "pure fixtures, no wrapper" and section 8 gives no versioned import
  shape for the policy tables (card 1's row gave an exact shape). The
  seven tables are config-owned and will be loaded by a later card's
  config-import, which must keep card 2's upsert discipline (retained
  aliases updated on config-owned columns only; a removed alias
  deleted only if unreferenced). The upsert behavior is unchanged here
  and still pinned by the card-2 `ConfigImportUpsert` tests.
- **The `profiles` table is schema-only in card 3.** Its columns are
  present (candidates/context_rules/calibration JSON, the numeric caps,
  `margin_bps`, `ttl_ms`, `policy_revision`); the arithmetic takes the
  numeric caps as explicit, validated arguments, so a later card can
  read them from the row without a further migration.
- **Limit rows arrive unique.** The `limits_scope` uniqueness index
  (v3) enforces row uniqueness in the store; `composed_limits` takes
  the applicable rows for one admission, which a store projection
  cannot duplicate.

## Tests

Command (from the repository root):

```
python -m unittest discover -s tests -v
```

All 65 tests passed (`Ran 65 tests ... OK`), run in the foreground with
CPython 3.14.4 / SQLite 3.46.1: the 17 card-1 tests, the 30 card-2 tests
(including the carry-over), and the 18 card-3 tests. The four card-named
checks appear with the `test_` prefix; the bracketed names are the
card's names.

| Test (tests/test_card3.py) | Card check | Result |
|---|---|---|
| `test_round_components` (NamedChecks) | round_components | ok |
| `test_projection_denied` (NamedChecks) | projection_denied | ok |
| `test_fallback_tier` (NamedChecks) | fallback_tier | ok |
| `test_privacy_3` (NamedChecks) | privacy_3 | ok |
| `test_margin_bounds` (EstimateRules) | (rule) margin 10000..20000 | ok |
| `test_requires_n_le_m_and_bounds` (EstimateRules) | (rule) N<=M<=100 | ok |
| `test_integer_hygiene` (EstimateRules) | (contract) booleans/floats/negatives/signed-64 | ok |
| `test_hold_allowance` (EstimateRules) | (rule) 2*M-1, fixed comparison | ok |
| `test_per_call_output_cap` (EstimateRules) | (rule) per-call <= model ceiling | ok |
| `test_model_shape_closed` (EstimateRules) | (contract) closed model shape | ok |
| `test_tightest_ceiling_composition` (LimitRules) | (rule) tightest ceiling | ok |
| `test_account_budget_tightens_month_money` (LimitRules) | (rule) one money home | ok |
| `test_admission_at_cap_ok_over_cap_denied` (LimitRules) | (rule) admission rule | ok |
| `test_admission_unbounded_without_cap` (LimitRules) | (rule) no cap = unbounded | ok |
| `test_limit_row_contract` (LimitRules) | (contract) limit row shape | ok |
| `test_sweep_defer_above_80_percent` (SweepRules) | (rule) 80% sweep | ok |
| `test_cold_sweep_no_premium_bypass` (SweepRules) | (rule) no premium bypass | ok |
| `test_codes_messages_and_exit_codes` (ErrorContract) | (contract) codes/exit 2 | ok |

What each named check pins:

- **round_components** — given five calls (M=5), when the reserve
  formula (C=20000, N=4, G=2000, O=4000), then 68169: the full
  section-3 worked example (E=92000, I=115000 at the 12500 bps
  default, hold_sized=68169 at reserve rates, the tariff-rate upper
  estimate 56809 as a comparison, the fixed reserve 1159995 as a
  comparison, projected final context 26000).
- **projection_denied** — an over-context projection (C+G*(N-1) above
  the model input ceiling) denies `CONTEXT_PROJECTION_EXCEEDED` with
  the static message and exit 2, both initial-size-driven
  (250000 > 200000) and growth-driven (30000 + 5000*39 = 205000);
  the boundary at the ceiling is admitted, one token over denies.
- **fallback_tier** — a quota-blocked Codex read routing to
  bounded_read lands on the Qwen read (candidate qwen; sonnet then
  park on further blocks); critical_review falls back to the
  independent premium (opus) then park; cold_sweep's fallback has no
  premium tier — it parks.
- **privacy_3** — injected secret/path/provider-ID sentinels (shaped
  so they can never pass as a local alias) placed in every model
  string field, an integer field, a limit scope_id/window_key, and the
  tier lists/blocked set: every error renders exactly the static
  `{"error":{"code","message"}}` contract with none of the sentinels
  in it; the valid flow renders only fixture numbers, and a local
  alias survives as data, exactly as given.

### Red-before evidence

At the migration-only commit c7f9fba the store has the v3 tables but
`policy.py` does not exist. Copying `tests/test_card3.py` into a
scratch worktree checked out at c7f9fba and running it fails the whole
module (`ImportError: cannot import name 'policy' from
'quota_balancer'`), so every card-3 test is red there; each named test
run individually at c7f9fba:

```
tests.test_card3.NamedChecks.test_round_components  ... FAILED (errors=1)
tests.test_card3.NamedChecks.test_projection_denied ... FAILED (errors=1)
tests.test_card3.NamedChecks.test_fallback_tier     ... FAILED (errors=1)
tests.test_card3.NamedChecks.test_privacy_3         ... FAILED (errors=1)
```

At the arithmetic commit 57243e4 (and here, with the tests at 8031aec)
the same file runs green: `Ran 18 tests ... OK`, full suite `Ran 65
tests ... OK`.

## Demo transcript (synthetic 1750000 vs 238000 input)

Run from the repository root; the card is pure, so the demo imports the
module directly (no store, no CLI verb):

```console
$ python3 - <<'EOF'
import json
from quota_balancer import errors, policy
from quota_balancer.errors import QuotaError

model = {
    "alias": "claude",
    "context_ceiling": 200000,
    "output_ceiling": 64000,
    "tariff_input_rate": 400000,
    "tariff_output_rate": 2700000,
    "reserve_input_rate": 460000,
    "reserve_output_rate": 3815000,
    "reservation_mode": "sized",
    "policy_hash": "0123456789abcdef" * 4,
    "effort_aliases": "standard,deep",
}

# The section-3 worked example (five calls: M=5).
print("worked example (M=5):",
      json.dumps(policy.estimate(20000, 4, 2000, 4000, 5, model),
                 separators=(",", ":")))

# Synthetic seven-call RESUME at C=250000, G=0: 1750000 expected input.
print("resume  expected input:", policy.expected_input_tokens(250000, 7, 0))
try:
    policy.estimate(250000, 7, 0, 4200, 7, model)
except QuotaError as exc:
    print("resume  admission:", exc.code, "-", exc.message,
          "- exit", errors.EXIT_CODES[exc.code])

# The rotated plan (C=25000, G=3000, same seven calls): 238000 expected
# input, admitted, with its holds.
print("rotated expected input:", policy.expected_input_tokens(25000, 7, 3000))
print("rotated admission:",
      json.dumps(policy.estimate(25000, 7, 3000, 4200, 7, model),
                 separators=(",", ":")))
EOF
```

Output:

```console
worked example (M=5): {"expected_input":92000,"input_cap":115000,"hold_sized":68169,"hold_tariff_comparison":56809,"hold_fixed_comparison":1159995,"projected_final_context":26000}
resume  expected input: 1750000
resume  admission: CONTEXT_PROJECTION_EXCEEDED - the projected context exceeds the model input ceiling - exit 2
rotated expected input: 238000
rotated admission: {"expected_input":238000,"input_cap":297500,"hold_sized":152886,"hold_tariff_comparison":130353,"hold_fixed_comparison":1623993,"projected_final_context":43000}
```

The **resume** case shows the honest-input enforcement: the synthetic
seven-call RESUME at C=250000, G=0 expects 1750000 input, but its
projection (250000) exceeds the 200000 input ceiling, so the estimate
is denied (`CONTEXT_PROJECTION_EXCEEDED`, exit 2) rather than clipped.
The **rotated** plan (C=25000, G=3000, same seven calls) expects
238000 input — a seventh of the RESUME expectation — with its
projection (43000) inside the ceiling; its selected hold is 152886
micro-EUR at reserve rates, against the 130353 tariff-rate comparison
and the 1623993 legacy fixed-reserve comparison. The worked example
reproduces the section-3 anchor numbers exactly (68169 / 56809 /
1159995).

## Not testable deterministically (deferred to later cards, with the reason)

- **N/G calibration.** Section 3's admission uses "p90 by
  task class/model/mode" from versioned numeric calibration supplied
  separately (no fleet-log access is assumed, and raw fleet logs are
  never imported). Card 3 validates the *use* of N/G (bounds, integer
  hygiene, the projection) over fixtures; producing the calibration
  values is outside this card's pure arithmetic and is deferred to
  whatever card owns the calibration input.
- **The wire statuses (400/403) and HTTP semantics.** Card 3 is pure —
  no wrapper, no HTTP. Section 7's 400 `INVALID_REQUEST` /
  `CONTEXT_PROJECTION_EXCEEDED` and 403 `POLICY_DENIED` are pinned here
  only as the static `{error:{code,message}}` shape and the exit-2 CLI
  mapping; the actual HTTP responses (with `server_at`,
  `policy_revision`, retry semantics) are card 5's.
- **Transactional hold accounting.** The admission rule
  (settled + unresolved holds + new hold <= cap) is pinned as pure
  arithmetic; the serialized, concurrent, per-period application of
  holds against the store (`leases`/`holds` rows, the one-slot rule,
  uncertain expiry) is card 4's lease store.
- **The fallback's "new estimate".** Section 6 says a fallback starts
  a new estimate/request/session. `select_tier` returns the tier; the
  re-estimate on the new session is the caller's (the lease cards')
  behavior, not the arithmetic's.
- **`ttl_ms` and `policy_revision` from the profiles row.** The
  columns exist (v3) but TTL expiry and policy-revision comparison are
  lease-lifecycle behaviors (cards 4-5); card 3's arithmetic has no
  clock.

## Fix round (two cold reads, tk-e44908311861)

Both cold reads (A and B) came back NOT READY; the lead's dispositions
are binding. New commits on top of d0a9edf (no rewrite), stdlib only.
Every new behavior test was red before its fix (evidence below); all 13
verified mutants are killed.

### Fixed (with tests)

| Finding (read) | Fix | Test(s) |
|---|---|---|
| A MAJOR 3 / B M1 — window_key dropped in composed_limits/check_admission | Cap identity is (metric, period, window_key); composed_limits keeps different windows separate; check_admission takes window_key (required for period "window", rejected otherwise) and only rows of the requested window apply | WindowSeparation (2 tests) |
| B M2 — admission compared one figure against the collapsed min across scopes | check_admission enforces EVERY applicable row against its own scope's (settled, holds); usage is keyed (scope, scope_id) and fails closed — a missing scope's usage is INVALID_REQUEST, not zero | AdmissionPerScope (4), incl. the lead's one-scope-passes/one-fails case |
| A MAJOR 1 / B m4 — rounding direction untested (the ceil mutants survived) | Tests pin both ceilings on non-exact multiples (the behavior was already ceil; the tests are the fix) | RoundingDirection (2) |
| A MAJOR 2 / B m2 — `^...$` + re.match accepts a trailing newline | fullmatch on the alias/UUID/hash checks | TrailingNewlineRejection (3) |
| B m1 / A MINOR 5 — metric/period/window_key pairing unvalidated | _validate_limit enforces: input/money use turn/day/month with no window; window_bps uses window with a window_key and hard <= 10000 bps; concurrency uses turn with no window | MetricPeriodPairing (2) |
| B m3 — reserve rates not required >= tariff rates | _validate_model rejects a reserve rate below the tariff rate (per component); the design's rates are 1.2x the tariff | ReserveAtLeastTariff (2) |
| B m5 — soft>hard normalization untested (X9 survived) | Test pins the drop: min soft above the tightest hard carries no information and becomes None | LimitRules.test_composed_soft_above_hard_is_dropped |
| A MINOR 4 — estimate() left O unbounded | O is the TOTAL output (section 3): bounded by M calls at the model output ceiling (section 7: the per-call cap <= model ceiling) | EstimateRules.test_output_total_bounded_by_model_ceiling |
| A MINOR 6 / B n5 — the config-import reference guard missed the v3 tables | The removal guard now also checks seats.machine_id, models.account_id and project_accounts.account_id: a referenced account/machine is INVALID_CONFIG (exit 2), not an FK fault STORE_ERROR (exit 3) | tests/test_card2.py ConfigImportUpsert.test_reimport_removed_alias_referenced_by_card3_tables |
| A NIT 9 — "park" passes as a model alias | 'park' is the fallback sentinel (model None): rejected as a candidate or a blocked entry | SweepRules.test_park_is_reserved_for_fallback |
| A NIT 8 — fixture reserve rates off the design's | Fixture rates are now the design's 480000/3240000 (both sets pin 68169; the design's is the traceable one) | (no behavior change: NamedChecks still pin 68169/56809/1159995) |
| B n1 — the v3 comment said the tables are "loaded by config-import" | Reworded: the tables stay EMPTY in this card (no policy config-import exists yet) | (comment) |
| B n3 — sweep_deferred takes one window | Docstring: section 5 defers when ANY selected window is >80%; the caller passes the max across the selected windows | (docstring) |
| B n4 — check_admission ignores soft | Docstring: soft bounds annotate the grant (card 4's job), admission consults hard only | (docstring) |

### Deferred (with reasons)

- **B m6 — seats.enforcement vocabulary.** The DDL CHECK mirrors the
  section-7 lease-enforcement enums exactly; "unmanaged" in section 5
  describes consumers outside coordinator coverage, not a seat
  enforcement value. The table is empty in this card; the vocabulary is
  settled by the card that loads seats (the lease enums are the current
  reading).
- **A MINOR 7 — profiles DDL CHECKs (N<=M, per_call_output_cap <=
  output_cap).** The profiles table is empty in this card (no loader);
  the loader card validates the document before any write (the
  card-1/2 validate()-before-write pattern). DDL CHECKs on an empty
  table would cost a schema-version bump for nothing.
- **B n2 — profiles.ttl_ms upper bound (section 6: 24h max) and
  expected_calls <= max_calls.** Same reason: empty table; the loader
  card owns document validation.
- **A NIT 9 — int64 intermediate products.** Every RETURNED quantity is
  bounded to INT64_MAX after the computation (E, I, the holds, the
  projection). Intermediate products (e.g. I * rate) are computed in
  arbitrary-precision Python and the result is checked; the rates come
  from the operator's models fixture (validated), not request input.
  The signed-64 contract lives at the boundary (section 7).
- **A NIT 9 — unknown utilization in sweep_deferred.** The pure
  function takes a known 0..10000 bps; what an UNKNOWN utilization
  means (never observed) is a readings-layer decision (section 5:
  "missing fields stay unknown"), not the arithmetic's. Documented in
  the docstring.

### Red-before evidence (at d0a9edf)

11 errors + 7 failures across the new and updated card-3 tests (the
contract changes and the behavior gaps), plus the new card-2 guard test
failing `3 != 2` (the FK fault surfaced as STORE_ERROR exit 3 instead
of INVALID_CONFIG exit 2 — exactly A MINOR 6).

### Mutation verification (all 13 killed, on the fixed code)

| Mutant | Killed by |
|---|---|
| margin ceil -> floor | RoundingDirection.test_input_margin_rounds_up |
| hold ceil -> floor | RoundingDirection.test_hold_rounds_up |
| fullmatch -> match (alias) | TrailingNewlineRejection (3) |
| composed key drops window_key | WindowSeparation.test_composed_keeps_windows_apart |
| admission filter drops window_key | WindowSeparation.test_admission_checks_the_requested_window |
| per-scope -> first scope vs collapsed min | AdmissionPerScope (2) |
| pairing rules removed | MetricPeriodPairing.test_bad_pairings_rejected |
| reserve >= tariff removed | ReserveAtLeastTariff.test_reserve_below_tariff_rejected |
| O bound removed | EstimateRules.test_output_total_bounded_by_model_ceiling |
| park sentinel removed (candidate) / (blocked) | SweepRules.test_park_is_reserved_for_fallback |
| store guard drops the models check | the new card-2 guard test |
| soft>hard normalization removed | LimitRules.test_composed_soft_above_hard_is_dropped |

### Tests after the fix round

65 -> 84 (18 new card-3 tests + 1 new card-2 guard test), all green:
`python -m unittest discover -s tests` — `Ran 84 tests ... OK`,
foreground, CPython 3.14.6 / SQLite 3.50.4.

## Commits on `card-3`

- 05c18af base (origin/main, cards 1 and 2 merged plus the card-2b design)
- 42cef7e test(card-2): same-epoch metadata ordering carry-over
- c7f9fba feat(card-3): schema v3 — policy tables (models/profiles/projects/teams/seats/project_accounts/limits)
- 57243e4 feat(card-3): pure policy arithmetic (estimates, holds, limits, sweep, fallback)
- 8031aec test(card-3): named checks and policy rules
- d0a9edf docs(card-3): step record and README section (head before the fix round)
- (fix round: the code+tests commit, then this updated step record)
