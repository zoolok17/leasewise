# Step record — Card 2b-1: provider billing ingestion

Work order: tk-815d6c4536fe (continuation of the dead-lettered original
order; the first session's work was kept on this branch). Seat:
qwen-quota-dev-1. Branch: `card-2b-1` (built on `main` @ 05c18af). Date:
2026-09-25. Interpreter: CPython 3.14.4 (standard library only:
`sqlite3`, `argparse`, `json`, `os`, `re`, `sys`, `time`, `uuid`,
`datetime`).

The card is docs/DESIGN.md section 8, row "2b-1: billing ingestion", plus
section 11's "Dependency and storage contract". Fixtures only: no live
provider calls, no HTTP, no network. The card's acceptance-matrix rows
(every row using injected time/fixtures, section 11) are the green bar.

## Commits (9 on `main` @ 05c18af, in order)

1. `4189ea5` test(card-2): gate test — re-pointed gateway must reset
   report state (X1 gate, assigned as 2b-1's first commit by the lead,
   section 10/X1)
2. `bc2c2a2` fix(card-2): reset a re-pointed gateway's report state
3. `86bfb9f` feat(card-2b-1): schema v3 — readings CHECK rebuild,
   billing tables
4. `f138130` test(card-2b-1): acceptance tests for billing ingestion
5. `ad2e21d` feat(card-2b-1): pure billing primitives
6. `485924d` feat(card-2b-1): reading document version 3
7. `e12ec29` feat(card-2b-1): provider-billing ingestion
8. `5f26cb5` feat(card-2b-1): billing-policy-import — closed validation,
   atomic upsert
9. `31ff993` test(card-2b-1): correct two fingerprint-snapshot placements

## What was built

- **X1 gate fix (commits 1-2).** A gateway re-pointed to a different
  machine/account had to reset its report state; the gate test pins the
  requirement and the store fix makes a re-pointed gateway's windows and
  report start clean for the new owner.
- **Schema v3, transactional (commit 3).** The `readings` CHECK is
  rebuilt to admit the `provider_billing` kind in one transaction that
  runs BEFORE `billing_totals`' source FKs exist (section 11: rebuild
  before FK creation); old rows, IDs, payloads and windows survive. New
  tables: `billing_policies` (PK `account_id` FK, the 17 closed policy
  fields, one per metered account) and `billing_totals` (PK
  `(account_id, scope_revision, period_start)`: `period_end`,
  `latest_micro_eur`, `observed_at`, `latest_event_id` FK,
  `high_water_micro_eur`, `high_water_event_id` FK, `conflict`).
  Interrupting after any single DDL step and restarting rolls back to a
  working store (FKs, IDs and windows survive) — `billing_migration_rollback`.
- **Pure billing primitives (commit 5, `reduce.py`).** `reduce_billing_total`
  implements the section-11 reducer: newer observations set `latest`
  (a newer total below the retained high-water latches `conflict`),
  equal-time unequal totals latch `conflict` (latest event chosen by
  UUID sort for deterministic display), and an older total above the
  latest latches the conflict too - raising the high-water where it
  beats it - WITHOUT refreshing the stored source time; the conflict is
  sticky, only a later audited resolution clears it.
  `convert_basis(amount, source_basis, target_basis, vat_bps)` is the
  shared pure VAT conversion: explicit bases, unknown defers (never
  approves, and nothing is converted from it - not even to itself),
  half-micros round upward. `billing_freshness` with the exact
  `age <= max_age` boundary and no idle exemption.
- **Reading document version 3 (commit 6, `validate_readings.py`).**
  Version-3 documents carry the `provider_billing` kind; version 2 (and
  earlier) documents remain importable, with missing amounts/periods
  meaning unusable money evidence, never zero. The closed payload
  (section 11): `{scope_alias, scope_revision, period_start,
  period_end, currency, total_micro_eur, non_ai_spend_present}` — exact
  key set, `EUR` only, nonnegative int64 micro-EUR, UTC-month period
  `[start, end)` with the source time inside it (final sample at end
  allowed), one exact boolean, aliases by the inventory regex, no
  provider IDs, no wire seat field, `>30 s` future source time rejected.
- **Provider-billing ingestion (commit 7, `store.py`).** Every accepted
  reading is recorded as a raw `readings` row (idempotency by `event_id`:
  identical replay = no-op, same event different content =
  `IDEMPOTENCY_CONFLICT` rollback). The projection into `billing_totals`
  is by SOURCE time through `reduce_billing_total`; the currency and
  money basis live in the policy, not the row. A provider bill is
  attributed money, so the account must be `metered`; where a policy
  exists, the reading must match its reporter machine, scope alias and
  scope revision (a replica copy never adds totals). Late
  prior-month arrivals update only that month; a new month starts
  unknown, never the prior month's floor.
- **billing-policy-import (commit 8).** New closed CLI verb
  `billing-policy-import --file {version, policies:[...]}`:
  `validate_policies.py` validates the self-contained shape (17 exact
  keys, closed enums `mode={advisory}`, `stale_action={alert}`,
  `divergence_action={hold}`, each basis in `{unknown, ex_vat,
  invoice_total}`, `dedicated_ai` true, positive durations, VAT/tolerance
  bps 0..10000, `vat_bps` null only when no conversion is needed) and the
  B01 allocation invariant (gateway shares + non-AI allowance <= account
  cap, compared on one tax basis — an unknown basis defers the check,
  never approves it, no implicit VAT default). Reference resolution
  (account/machine exist and are metered) happens inside the store's
  `BEGIN IMMEDIATE` upsert, which is atomic. Once the account has a
  billing reading, owner/scope/basis changes are refused (only the later
  audited resolution clears them); the operator-tunable fields still
  update. Telemetry never changes a policy.
- **Acceptance tests (commits 4, 9, `tests/test_card2b1.py`).** 17 test
  methods in 15 classes, one class per acceptance-matrix row tagged
  `/ 2b-1` (plus the two `/ all` rows this card runs:
  `billing_output_errors`, `privacy_2b`). Commit 9 corrects two
  snapshot placements in the tests (not the store): the
  `billing_older_high_water` no-op assertion must compare the
  `billing_totals` projection and the non-`readings` tables, because an
  older LOWER total is new raw evidence (recorded with its own source
  time, section 11) even though it moves nothing — and the status
  projection carries wall-clock age, so it is not a stable equality.
  `billing_output_errors` must re-snapshot after its two ACCEPTED
  imports so the final equality covers only rejected operations.

## Commands and counts

Full suite, from the repo root:

    python3 -m unittest discover -s tests

    Ran 66 tests in ~32 s — OK

Breakdown: `tests/test_card1.py` 17, `tests/test_card2.py` 32,
`tests/test_card2b1.py` 17. The card file alone:

    python3 -m unittest tests.test_card2b1

    Ran 17 tests in ~9.5 s — OK

Acceptance-matrix rows (section 11) to classes, all green:
`billing_snapshot_schema` → BillingSnapshotSchema (2 methods),
`billing_migration_rollback` → BillingMigrationRollback,
`billing_retention` → BillingRetention (2), `billing_validation` →
BillingValidation, `billing_policy_enums` → BillingPolicyEnums,
`billing_owner_scope_revision` → BillingOwnerScopeRevision,
`billing_scope` → BillingScope, `billing_replay` → BillingReplay,
`billing_same_time_conflict` → BillingSameTimeConflict,
`billing_older_high_water` → BillingOlderHighWater, `billing_time` →
BillingTime, `billing_rollover` → BillingRollover,
`billing_tax_basis` → BillingTaxBasis, `billing_output_errors` →
BillingOutputErrors, `privacy_2b` → Privacy2b.

## Fixture demo

All files under the scratch dir; `PYTHON` is `python3`.

    python3 -m quota_balancer --db $S/demo.db init
    python3 -m quota_balancer --db $S/demo.db config-import --file $S/config.json
    python3 -m quota_balancer --db $S/demo.db billing-policy-import --file $S/policy.json
    python3 -m quota_balancer --db $S/demo.db reading-import --file $S/readings.json
    python3 -m quota_balancer --db $S/demo.db status --json

`config.json` is the standard inventory fixture (metered account
`meter`, 100 EUR cap, machines `desktop`/`worker`, gateways `g1`/`g2`
splitting the cap 60/40); `policy.json` is one version-1 policy for
`meter` (reporter `desktop`, scope `proj-ai` revision 1, ex-VAT bases
throughout, `vat_bps` null because no conversion is needed);
`readings.json` is a version-3 document with one `provider_billing`
reading: 42 EUR (42000000 micro-EUR) for the 2026-09 period,
`observed_at` = now-1s. All four commands exit 0, and the status
billing projection for `meter` reads:

    {"scope_revision": 1, "period_start": 1788220800000,
     "period_end": 1790812800000, "latest_micro_eur": 42000000,
     "high_water_micro_eur": 42000000, "conflict": false,
     "fresh": true, "freshness_reason": "fresh"}

(replayed: identical document is a no-op; the same event with a
different total exits 2 with the static `IDEMPOTENCY_CONFLICT` error
and leaves the store untouched; a `-5` total or a foreign scope alias
exits 2 with the static `INVALID_CONFIG` error, likewise untouched.)

## Judged not testable on this card (and why)

- **Live provider behavior (OVH).** Owned by 2b-3, which needs
  lead-supplied sanitized captured responses before dispatch; this card
  is fixtures only (section 8 row 2b-3: "Real reporter canary
  separately approved"). Nothing here touches a network or a credential.
- **Actual retention deletion.** Section 11: "2b does no deletion". The
  retention-eligibility predicate (30-day/13-month/unresolved-evidence
  rule) is pure and tested (`billing_retention`); executing prunes is a
  later pruning owner's card.
- **Audited conflict resolution.** "Only later audited resolution
  clears it" — the latch and its retention across newer downward
  corrections are tested here; the clearing path is a later
  integration's surface.
- **Wall-clock elapsed time.** Every time-sensitive row uses injected
  time/fixtures (the section-11 matrix rule); exact boundaries are
  tested at the millisecond (freshness 7200 s/+1 ms, future 30 s/+1 ms,
  midnight rollover), so real multi-day elapsing adds nothing this card
  can assert.
