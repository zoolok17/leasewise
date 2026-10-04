# agenttalk-quota

A quota and token balancer that agenttalk consumes: accounts, subscriptions and pay-per-token endpoints, limits, per-turn leases, meter readings and routing policy.

Design (v3, accepted for build 2026-09-24): docs/DESIGN.md. Built card by
card (section 8); card 1 = standalone inventory, card 2 = the reading
reducer over it.

## Card 1 — standalone inventory, exactly three tables

A standalone CLI over one SQLite database. Standard library only
(`sqlite3`, `argparse`, `json`, `uuid`). No agenttalk, no models, no
scopes, no leases, no HTTP — those belong to later cards.

Requirements: Python 3.11+ (stdlib only). From the repository root:

```console
python -m quota_balancer --db quota.db init
python -m quota_balancer --db quota.db config-import --file docs/fixtures/card1-inventory.json
python -m quota_balancer --db quota.db status --json
```

(An installed copy exposes the same entry point as `quota`.)

Note: `--db` is a global option and must precede the verb
(`--db X init`, not `init --db X`). The store's SQL also requires
SQLite >= 3.37 (STRICT tables); `init` reports a store error on an
older SQLite instead of creating a half-specified schema.

- `init` — create the store at migration version 1 (idempotent; WAL,
  foreign keys on). Re-running it is a no-op.
- `config-import --file DOC` — validate a version-1 document (closed
  key shapes, alias regex `[a-z][a-z0-9_-]{0,63}`, duplicate-alias,
  reference, exact integer micro-EUR in the signed 64-bit range,
  metered-only shares, share SUM per account <= its budget) and replace
  the inventory atomically, retaining UUIDs for retained aliases.
- `status --json` — print the inventory projected to aliases only,
  sorted by alias:

  ```json
  {"schema_version":1,"accounts":[{"alias":"meter","kind":"metered","budget_micro_eur":100000000,"allocated_micro_eur":100000000}],"machines":["desktop","worker"],"gateways":[{"alias":"g1","account":"meter","machine":"desktop","share_micro_eur":60000000},{"alias":"g2","account":"meter","machine":"worker","share_micro_eur":40000000}]}
  ```

Exit codes: 0 success; 2 invalid config or unsupported schema version;
3 store I/O failure. On failure the tool prints one JSON object to
stderr — `{"error":{"code","message"}}` with code `INVALID_CONFIG`,
`SCHEMA_UNSUPPORTED` or `STORE_ERROR` — and no raw input or OS error
text. `status` success output is the only thing written to stdout.

Demo (import, restart, identical status): run `init`, then
`config-import --file docs/fixtures/card1-inventory.json`, then
`status --json`. Each invocation is a fresh process against the same
database file, so a second `status --json` after the import is the
restart case; the output is byte-identical and shows the account
budget and allocated amount of 100000000 micro-EUR. Fixtures:
`docs/fixtures/card1-inventory.json` (valid) and
`docs/fixtures/card1-inventory-invalid.json` (g2 share 40000001, which
pushes the share sum above the account budget).

Tests: `python -m unittest discover -s tests -v` (46 tests across both
cards — the card-1 suite, including the carry-over, plus the card-2 named
checks, reducer rules, and validation/connectivity checks; see
docs/STEP-CARD-1.md and docs/STEP-CARD-2.md).

## Card 2 — reading reducer

The same standalone CLI, extended. It ingests reporter fixtures and
reduces them into per-account windows and a per-gateway report, projected
to aliases only. Standard library only. No agenttalk, no models, no
scopes, no leases, no HTTP — still fixture only.

The database migrates from version 1 to version 2 (a v1 store is brought
up by `init`; a v0 store goes straight to 2). New tables `readings` and
`windows`; `gateways` gains the report columns and `accounts` the
coverage/idle-proof columns.

```console
python -m quota_balancer --db quota.db init
python -m quota_balancer --db quota.db config-import --file docs/fixtures/card1-inventory.json
python -m quota_balancer --db quota.db reading-import --file readings.json
python -m quota_balancer --db quota.db set-coverage --account meter --complete true
python -m quota_balancer --db quota.db reconcile --gateway g1
python -m quota_balancer --db quota.db status --json
```

- `reading-import --file DOC` — validate a version-2 document (closed key
  shapes, machine/account/window-key local aliases, `event_id` as a local
  UUID, exact integer `used_bps` in 0..10000, every integer bounded to the
  signed-64 range, policy hashes as fixed-length hex digests, kind
  `cli_window` or `gateway_snapshot`, no observations more than 30 seconds
  in the future) and, in one transaction, reduce it: dedup by `event_id`
  (a replay with matching content is a no-op, the same id with different
  content is `IDEMPOTENCY_CONFLICT`), keep the source observation time,
  hold the high-water utilization per reset epoch (a later epoch restarts
  from its first post-reset observation; an older epoch is a no-op), keep
  the explicit confidence states (`observed` requires a value and a reset;
  `unknown` carries nulls and never refreshes a window that already has a
  known value; a legacy `stale` sample arrives pre-mapped by the reporter
  as `observed` + `source_stale=true`, with its value and source time
  preserved), and advance the gateway report (the snapshot must bind to
  the gateway's own machine and account; monotonic `seq`; an epoch or
  policy-hash change needs reconciliation, which stays set until the
  operator's `reconcile` verb). An ambiguous source (no account) is
  recorded but reduced into no account.
- `set-coverage --account A --complete {true,false}` — operator-only; the
  only thing that ever writes `coverage_complete`. Telemetry never sets
  it.
- `reconcile --gateway G` — operator-only; clears a gateway's
  `reconciliation_required` state. Telemetry never clears it (an epoch or
  policy-hash change re-raises it on the next report).
- `status --json` — the card-1 inventory plus, per account,
  `coverage_complete` and its windows (window key, `used_bps`,
  `resets_at`, `confidence`, `source_stale`, `observed_at`, `age_ms`,
  `current`, `freshness_reason`), and, per gateway, the report
  (`state`, `last_report_at`, `ledger_epoch`, `last_seq`,
  `period_start`, `period_end`, `policy_hashes`, `sized_ready`).
  Freshness is fail-closed in this card: with coverage incomplete the
  600-second wall cap always applies (a sample older than 600 seconds is
  `current=false`, `freshness_reason=wall_cap`), and a gateway report
  older than 120 seconds reads `state=stale` (METER_STALE); the
  idle-current proof that would lift the cap is a later card's.

Exit codes: 0 success; 2 invalid config/schema or an idempotency
conflict; 3 store I/O failure. On failure the tool prints one JSON object
to stderr — `{"error":{"code","message"}}` with code `INVALID_CONFIG`,
`SCHEMA_UNSUPPORTED`, `STORE_ERROR` or `IDEMPOTENCY_CONFLICT` — with
static messages only.

Demo (reordered and idle samples): run `init`, `config-import`, then
`reading-import` a document where the later observation arrives before
the earlier one (the window takes the high-water usage and the latest
source time regardless of arrival order), and a second document with a
~700-second-old sample (the window reads `wall_cap`). A second `status
--json` after a process restart re-reads the same reduced state — only the
read-time `age_ms` advances. See docs/STEP-CARD-2.md for the transcript.

Tests: the five named checks (`reading_replay`, `stale_mapping`,
`coverage_fallback`, `account_isolation`, `privacy_2`) plus the reducer
rules; the named tests are shown red at the migration-only commit and
green with the reducer — see docs/STEP-CARD-2.md.

## Card 3 — policy arithmetic (pure)

No new CLI verb: this card is pure arithmetic over fixtures — no
wrapper, no HTTP, no store access in the arithmetic. Standard library
only (`re`). The database migrates from version 2 to version 3: the
seven policy tables `projects`, `teams`, `project_accounts`, `seats`,
`models`, `profiles`, `limits` and the `limits_scope` uniqueness index
(version-1/2 DDL byte for byte; same transactional discipline).

`quota_balancer/policy.py` implements docs/DESIGN.md sections 3, 4 and
6 as total functions over validated integer inputs:

- **Estimates and holds (section 3).** `E = N*C + G*N*(N-1)/2`, `I =
  ceil(E*margin_bps/10000)` (margin 10000..20000, default 12500), and
  the request-sized hold `H = ceil((I*input_rate + O*output_rate)/1e6)
  + 2*M - 1` at the model's reserve rates (rates: integer micro-EUR per
  1,000,000 tokens, bound to the model `policy_hash`). The tariff-rate
  figure and the fixed reserve `M*231999` are comparisons only — the
  fixed per-attempt path is never the selected lease mode.
- **Model ceiling.** `C + G*(N-1)` above the model input ceiling denies
  `CONTEXT_PROJECTION_EXCEEDED`; oversized bounds are never clipped.
  The per-call output cap must not exceed the model output ceiling, and
  O (the total output) must not exceed M calls at that ceiling.
- **Composed limits and admission (section 4).** `composed_limits` is a
  view only - the tightest hard ceiling per (metric, period,
  window_key) across platform/project/team/seat (never adding duplicate
  caps; limits for different windows stay separate), the account budget
  folded in as the one money home for (money, month, no window) - and
  must never feed admission. Admission is `check_admission`: each limit
  row applicable to the requester (matching metric/period/window AND
  scope_id on the requester's scope chain - foreign rows neither deny
  nor require usage) is enforced against its own scope's settled +
  unresolved holds + new hold, and the account budget (F04; there is no
  account/month limits row, so it is passed in) stands as the one
  mandatory (money_micro_eur, month) cap on the account's own settled +
  holds. Unknown usage fails closed; else `POLICY_DENIED`.
- **The 80% sweep and fallback (section 6).** a cold sweep defers
  strictly above 8000 bps; fallback tiers walk candidates then the
  ordered fallback (alias or terminal 'park') — a fallback hit is a new
  estimate/request/session, and cold_sweep never routes to a premium
  automatically.

Errors: `INVALID_REQUEST` / `CONTEXT_PROJECTION_EXCEEDED` /
`POLICY_DENIED`, all static `{"error":{"code","message"}}` with exit 2
(client-side request conditions). Closed shapes throughout: exact
integers (booleans rejected), signed-64 bounds, local aliases, local
UUIDs, 64-char hex policy hashes, enum checks.

Demo (synthetic 1750000 vs 238000 input): a seven-call RESUME at
C=250000, G=0 expects 1750000 input but its projection (250000) exceeds
the 200000 ceiling and is denied; the rotated plan (C=25000, G=3000,
same seven calls) expects 238000 input and is admitted with its
request-sized hold. See docs/STEP-CARD-3.md for the transcript.

Tests: the four named checks (`round_components`, `projection_denied`,
`fallback_tier`, `privacy_3`) plus the arithmetic rules; the named tests
are shown red at the migration-only commit and green with the
arithmetic — see docs/STEP-CARD-3.md.

## Card 2b-3 — pure OVH billing adapter

No new CLI verb, no daemon, no credential file, no network: one pure
function, `quota_balancer/ovh_adapter.py::poll_once`, turns ONE OVH
"project usage, current" HTTP answer into a version-3 reading-import
document with exactly one `provider_billing` reading, or into a closed
failure `{auth, transport, rate_limit, schema, currency, period, tls}`.
Credential, transport, clock and jitter are injected; exactly one
transport call per invocation, never a retry. Standard library only.

- **Output.** Success `{"document", "next_attempt_at"}`, failure
  `{"failure", "next_attempt_at"}`; nothing else (no messages, no bodies,
  no URL, no key). The document feeds the existing `reading-import`.
- **Money.** `total_micro_eur = ceil(priceInUcents / 100)` in integer
  arithmetic; JSON numbers parse as `Decimal`, never binary floats;
  `currencyCode` must be `EUR`.
- **non_ai_spend_present** is an exact boolean: true iff a category other
  than `ai-endpoints` bills more than zero; an unseen `hourlyUsage` /
  `monthlyUsage` element makes the answer unknown, i.e. failure `schema`,
  never false.
- **Schedule.** `next_attempt_at = max(now + poll + jitter, Retry-After)`,
  jitter an injected integer in [0, 90]; Retry-After counts on a 429 only,
  and any valid value (seconds or HTTP-date) wins over the poll schedule, however
  far out (clamped to int64).
- **TLS.** A non-https base URL or a transport that does not report
  verification on is refused before any call.
- **Fixtures.** `docs/fixtures/ovh/` (two REAL sanitized captures, the rest
  labelled SYNTHETIC); tests: `tests/test_card2b3.py`. Decisions, the
  acceptance-row map and residual risk: docs/STEP-CARD-2B-3.md.

## Card L1a (turn 1) — policy import, config-import version 2

`config-import --file` now also accepts **version 2**: the version-1 inventory
(`accounts`, `machines`, `gateways`, unchanged) plus the seven policy tables
`projects`, `teams`, `project_accounts`, `seats`, `models`, `profiles`,
`limits`. Every key is required (an empty list is fine), no key may be added at
any level, and every reference is an **alias**, never a UUID. A version-1
document still imports exactly as before and leaves the policy tables alone;
any other version is `SCHEMA_UNSUPPORTED`.

The whole document applies in one transaction or not at all. Retained aliases
keep their UUID and update config-owned columns only (never `seats.last_seen_at`);
a removed alias is deleted only if nothing references it (leases, blocks,
holds, readings, limits), otherwise `INVALID_CONFIG` (exit 2) and nothing
changes. `context_rules` and `calibration` must be `{}` for now; a trailing
`"park"` in `candidates` is accepted (it is implicit and not stored); model
aliases are unique across all accounts.

```json
{
  "version": 2,
  "accounts": [{"alias": "meter", "vendor": "ovh", "kind": "metered",
                "budget_micro_eur": 100000000}],
  "machines": [{"alias": "desktop"}],
  "gateways": [{"alias": "g1", "account": "meter", "machine": "desktop",
                "share_micro_eur": 100000000, "period": "utc_month"}],
  "projects": [{"alias": "org"}],
  "teams": [{"project": "org", "alias": "dev"}],
  "project_accounts": [{"project": "org", "account": "meter"}],
  "seats": [{"team": {"project": "org", "team": "dev"}, "machine": "desktop",
             "alias": "s1", "role": "main", "enforcement": "metered_advisory"}],
  "models": [{"account": "meter", "alias": "opus", "context_ceiling": 180000,
              "output_ceiling": 18000, "tariff_input_rate": 1,
              "tariff_output_rate": 1, "reserve_input_rate": 1000000,
              "reserve_output_rate": 1000000, "reservation_mode": "sized",
              "policy_hash": "abababababababababababababababababababababababababababababababab",
              "effort_aliases": ["opus"]}],
  "profiles": [{"task_class": "bounded_read", "candidates": ["opus", "park"],
                "context_rules": {}, "calibration": {}, "expected_calls": 1,
                "growth_tokens": 0, "margin_bps": 12500, "max_calls": 2,
                "output_cap": 1000, "per_call_output_cap": 500,
                "ttl_ms": 1800000, "policy_revision": 1}],
  "limits": [
    {"scope": "platform", "metric": "concurrency", "period": "turn",
     "window_key": null, "soft": null, "hard": 4},
    {"scope": "team", "project": "org", "team": "dev",
     "metric": "money_micro_eur", "period": "month", "window_key": null,
     "soft": 50000000, "hard": 100000000},
    {"scope": "seat", "machine": "desktop", "seat": "s1",
     "metric": "window_bps", "period": "window", "window_key": "five_hour",
     "soft": null, "hard": 9000}]
}
```

**R-003 interim fix.** A seat whose `enforcement` is `metered` is always
granted `metered_advisory` (no money hold, no token, cannot pass `/check`),
whatever a gateway reports as `sized_ready` (still stored as telemetry). Card
R1 replaces the interim constant `lease.METERED_ENFORCEMENT_ACTIVATED` with an
explicit activation gate. See docs/STEP-CARD-L1A.md.

**Turn 2: the profile at grant.** The wire's `task_class` now reaches the
store: it keys the profile (imported by config-import v2), is part of the
replay content hash and is stored in `leases.task_class` (the effort alias stays
in `policy_snapshot`, which also records `task_class` and the profile's
`policy_revision`). Grant refuses an unknown class like an unknown model
(`NOT_FOUND`), denies a model outside the profile's `candidates` and any
`expected_calls`, `max_calls`, `output_cap`, `per_call_output_cap` or `ttl_ms`
above the profile (`POLICY_DENIED`), and uses the profile's `margin_bps` for the
estimate. A replay returns the stored lease before any of this, so a later
profile edit never changes a granted lease. See docs/STEP-CARD-L1A.md.

**Turn 3: two operator verbs.**
- `block-clear --account A [--model M] --reason R` clears the live block of an
  account (or of one model) and writes an audit row (actor `operator`, action
  `block_cleared`, only the reason and the previous state). `R` is one of
  `reset_observed`, `false_positive`, `operator_override`. It refuses with
  `ACCOUNT_PARKED` (exit 2, nothing changed) while ANY adjustment of the account
  is unresolved, so a block caused by a money overrun cannot be cleared until
  the reconciliation card can resolve it; a block from a provider usage limit
  can. No live block is `NOT_FOUND`; a repeat of a clear reports
  `{"state":"cleared","replayed":true}` and adds no audit row. Output:
  `{"state":"cleared","replayed":false}`.
- `expire-sweep` moves every active lease whose time has run out to
  `uncertain` (holds and slot kept, never a refund) and prints
  `{"expired":N}`; a second run prints `{"expired":0}`.
- An overrun now turns a `probing` block back to `parked` and audits every
  overrun's event id. See docs/STEP-CARD-L1A.md for the card summary.

## Card L1b - the shadow-request contract

A wrapper that only observes cannot know the size of the agent's context before
a turn, so it may now say so: `context.mode` = `"UNKNOWN"` with
`initial_tokens`, `dispatch_tokens` and `growth_tokens_per_call` all `null` and
`source` = `"unknown"` (`observed_at` stays required and fresh). Any mixture of
unknown and known fields is `INVALID_REQUEST`. An unknown context is granted to
**shadow seats only**; every other seat gets the new error `CONTEXT_UNKNOWN`
(HTTP 400, exit 2). The stored lease says so explicitly (`context_mode`
`UNKNOWN`, estimate `{"context":"unknown"}`) and its input cap is the model's own
bound (context ceiling x calls), recorded as `input_cap_basis` `model_ceiling`;
nothing is invented. A replay is matched first, as for every grant.

Leases that reserved no money (shadow, subscription, advisory) report on the
existing `/v1/leases/{id}/settle`: any number of non-final receipts, then a final
one with `actual_micro_eur` null; their response now carries `null`, not 0, for
`released_micro_eur` and `retained_micro_eur`. See docs/STEP-CARD-L1B.md.

## Card L1c - unknown usage for shadow leases, and `policy-ids`

A report (`/settle` receipt) about a **shadow** lease may now say "unknown" for
its token counts: `input_tokens`, `cached_input_tokens` and `output_tokens` may each
be `null` (stored as NULL, not 0); `calls` stays a required whole number. Every
other kind of lease (metered, advisory, subscription) must still report whole
numbers; a `null` there is `INVALID_REQUEST`, except for the one case that card
L6a-0 adds below. Shadow reports are observe-only:
whether known or unknown, they never count toward any token or money limit. This
needs schema version 9 or later (`init` upgrades a version-8 store in one transaction,
keeping every receipt).

`policy-ids --json` is a read-only command that prints, on one line, the ids a lease
request has to name: every seat (project, team, machine, seat aliases and the seat
id), every model (account alias, model alias, id) and the profile task classes,
sorted. It prints nothing else (no identity references, vendors, budgets or rates)
and changes nothing. The observe-only reporter of card 7a takes these ids as its
configuration in v1, copied by the operator; an HTTP lookup is deferred. See
docs/STEP-CARD-L1C.md.

## Card L1d - a granted lease always replays, and `ESTIMATE_STALE`

If a lease was granted but the answer was lost, sending the **same** request again
now always returns that lease, even when the operator has since removed the model,
removed an effort name, unlinked the project's account, changed the account, the
seat or the profile, or parked the account. Only the shape check and the seat
lookup happen before the replay lookup; every policy check comes after it. A
request with the same id but different content is still `IDEMPOTENCY_CONFLICT`.

A request whose `observed_at` is more than 120 seconds old, or more than 30 seconds
in the future, now gets its own error, `ESTIMATE_STALE` (HTTP 400, exit 2, the fixed
text "the admission estimate is stale"), instead of `INVALID_REQUEST`. It is checked
last among the policy checks and never for a replay, so receiving it proves that no
lease exists for that request id: the caller may rebuild the request with a
fresh estimate. Every other bad request is still `INVALID_REQUEST`. See
docs/STEP-CARD-L1D.md.

## Card L6a-0 - unknown token counts on a gateway's final receipt

**Status: built.** When a paid-model gateway "charges the reserve" for an attempt it could not prove,
it knows the money but not how many tokens were used. Until now Leasewise refused any final receipt
with an unknown token count for a lease that is not shadow, so such a lease could never be settled.
Now it can, in exactly one case, and the lease is marked so that nobody mistakes "unknown" for "zero".
Audience: the operator and anyone who reads Leasewise's receipts later. See docs/STEP-CARD-L6A-0.md.

**The rule.** A receipt may leave `input_tokens` and/or `output_tokens` as `null` only when all of
these are true:
1. it is a **final** receipt;
2. it names the lease's **own gateway** (no gateway, or another one, is not enough; another gateway
   is still `RECEIPT_CONFLICT` and parks the account);
3. it carries the money, `actual_micro_eur`;
4. `cached_input_tokens` is a whole number (the gateway sends 0);
5. the lease is `metered` or `metered_advisory`.

Anything else with a null count is still `INVALID_REQUEST`; subscription and shadow leases behave as
before. Replays work as usual: the same receipt again is a harmless `200`, and a changed one under the
same event id is `IDEMPOTENCY_CONFLICT`.

**What happens to the lease.** It settles. The money is settled and any overrun recorded exactly as
before. When input is unknown, the **token hold is kept whole** (nothing is refunded, no token overrun is
computed), because an unknown number is not a zero.

**How it is marked.** There is no new column. A lease is `usage_unknown` when its final receipt has a
null input or output count (`lease.usage_unknown(conn, lease_id)`), and the audit row written when the
lease settles says `"usage_unknown": 1`.

**The next lease waits (input budget).** Accepting the receipt is not enough: the admission check adds
up the input tokens of settled leases, an addition skips unknown values, and a settled lease holds
nothing any more (an advisory lease never held anything). So unknown input would silently vanish from
the next decision. Instead: **while any final receipt of a lease that is not shadow has unknown input
tokens inside a scope's window, a new lease that needs that scope's input limit is refused with
`INPUT_USAGE_UNKNOWN`** (HTTP 429, exit code 2, the text "input usage in this window is unknown until
the window rolls over"). Details:
- it applies to every scope (platform, project, team, seat) that has an input-token limit in that
  window, and only to the scopes the receipt's lease belongs to;
- the window is the lease's grant day or grant month, as for every other settled usage, so **the refusal
  lasts until the window rolls over** (the next day for a daily limit, the next month for a monthly one);
  nothing clears it earlier;
- a receipt with **known** input and unknown **output** does not trigger it: its known input stays in
  the sum;
- shadow requests and shadow receipts are not affected (they never count toward any limit);
- money is not affected: the account budget only adds up money, which a final receipt always carries.

**For anyone who reads receipts later (reports, analytics): a null token count means "unknown", never
0.** Do not add it up as zero and do not average over it. The admission check above is the one reader
of the input column today, and it treats unknown as a reason to wait, not as zero.

**Limit.** The gateway side that produces such receipts is not built yet (card 6a); today only a test
or an operator can send one.

## Card R1 - honest window checks, the expiry clamp and the advisory budget

**Status: R1-1 and R1-2 built.** Leasewise's grant now says what it knows and what
it does not about a usage window, and a seat in warn mode is told instead of refused where the evidence is only old
or missing. Audience: the operator of a Leasewise store. Paid enforcement stays impossible (see the limits).

### R1-1: what a grant does with a usage window

A window limit says "stop above N% of this usage window". The grant used to look only at the last number. It now
looks at everything it has: the number, how old the observation is, whether the window has reset since, and whether
the number is known at all.

| What Leasewise knows about the window | Admission seat (`subscription_admission`) | Warn seat (`subscription_warn`) |
| --- | --- | --- |
| a known number, recent, below the limit | admitted | admitted |
| a known number **at or above** the limit (old or recent) | refused: `WINDOW_LIMIT` | refused: `WINDOW_LIMIT` |
| a known number below the limit, but **old** (see below) | admitted, with a `METER_STALE` warning | the same |
| **explicitly unknown** (the source said it does not know) | refused: `WINDOW_LIMIT` | admitted, with a `WINDOW_LIMIT` warning |
| **never observed** (no record at all) | admitted, with a `METER_STALE` warning | the same |

- **At the limit is over the limit.** The check is inclusive. Before this step a reading exactly at the limit was let
  through; that changed on purpose.
- **Old** means the observation is more than 600 seconds old, or the window's reset time has passed and nothing has
  been observed since. An observation up to 30 seconds in the future (clock difference) counts as brand new. Age is
  measured from when the source observed it, never from when it arrived.
- **A window that was never observed is not "0%".** Its record says `confidence: missing`.
- A metered or advisory lease on a paid account is treated like an admission seat. A shadow request (observe only)
  is never refused by a window. A concurrency limit and a parked account (a provider usage limit) refuse in both
  modes.

**What the answer shows.** The grant answer carries two lists. `warnings` has entries `{code, window_key}` with only
the codes `WINDOW_LIMIT` and `METER_STALE`, each once. `windows` has one record per window the lease is limited by:
`window_key`, `used_bps`, `resets_at`, `confidence` (`observed`, `unknown` or `missing`), `source_stale`, `age_ms` and
`current`. Neither list names where a reading came from, and neither carries any raw text. Both come from the same read
as the decision.

**A replay.** Asking again for the same grant returns the same lease with the *current* lists. It never runs the
admission again, never changes the stored lease, and never refuses because the window changed since.

**Limits of this step (please read).**

- Admitting an old or never-observed reading below the limit is an **availability exception**, not a safety margin. It
  says nothing about what other users consumed since the reading was taken.
- A window known to be used up, or explicitly unknown, can keep an admission seat locked out **until a fresh
  observation arrives**. Nothing clears it by timer; only a new observation does. The recovery probe (a later card) is
  the planned way to get one.
- The freshness rule uses the 600-second cap only. The longer allowance for an idle seat needs turn tracking that is
  not built.

### Technical detail

`lease._admit_window` decides each window group from one read of the `windows` row (`_window_record`); `_window_verdict`
holds the table; `reduce.window_current` is the freshness rule. `lease.grant` returns `windows` and `warnings`, and
`http_api.handle_grant` puts them on the wire. A replay builds them with `_annotations` from the stored policy snapshot's
scope chain. Tests: `tests/test_cardr1_windows.py`.

### R1-2: the month boundary and the advisory budget

**A paid lease never crosses into the next month.** A lease on a gateway (a paid lease) that is not observe-only now
gets a deadline of at most the start of the next UTC month: `min(grant time + time to live, start of next month)`. A
lease granted at 23:50 on the last day of a month therefore ends at midnight. This only matters in the last day of a
month (a lease lives at most 24 hours). Asking again for the same grant returns the stored deadline. Expiry releases
nothing by itself, and a final receipt that arrives after the rollover is still accepted and belongs to the month the
lease was granted in. Subscription leases and observe-only (shadow) requests are not changed, even when a shadow
request names a gateway.

**An advisory paid lease stops when the account's known spend has reached its cap.** An advisory lease reserves no
money, so the account budget never saw it. Now a new `metered_advisory` grant is refused `MONEY_LIMIT` when the final
receipts already received for this account in the current month add up to **at or above** its monthly budget.

- Only final receipts with a **known** cost count. A receipt whose cost is unknown counts as nothing, so this rule
  stops work on known spend only; it does not claim complete accounting.
- Reservations of older enforcing leases are not added.
- No money limit row is needed: the account's own budget is the cap.
- It runs after the "same grant again" lookup (a replay is never refused for it) and before the unknown-input wait, so a
  full budget is reported as `MONEY_LIMIT`, and the unknown-input wait is unchanged below the cap.

**Paid enforcement is still impossible.** While `METERED_ENFORCEMENT_ACTIVATED` is false, a grant for a metered seat on
a ready gateway is always `metered_advisory`: no money hold, no holds rows and no lease token. Tests:
`tests/test_cardr1_expiry.py` (`EnforcementProof`) and, from earlier cards, `test_cardl1a.R003MeteredNeverEnforcing`.

**What this card still does not claim.** After R1 Phase 1, Leasewise's window checks are explicit about missing, stale
and unknown evidence, and a window known to be used up blocks. Advisory paid leases stop when known spend has reached
the cap. It does **not** claim that admission uses the gateway's money figures or the billing check, that old readings
bound anyone's use, or that money accounting is complete. Enforcement stays off until card 6b.

Three older notes are closed and pinned by tests, not open: a replay still returns its lease after the project loses
the account (N5), an overrun turns a probing block back to parked (N3), and a subscription seat on a paid account is
refused (m4).

### Technical detail (R1-2)

`lease._next_month_start_ms` and the clamp sit just before the lease row is written; `lease._advisory_budget_check`
runs right before the admission groups, only for `metered_advisory` on an account with a budget. Tests:
`tests/test_cardr1_expiry.py`.

### R1-M: money is added exactly (steps R1-M1 to R1-M3)

In plain words: before one account's spending can decide anything, every amount that is added or compared has to mean the same
thing, and the adding must never go wrong silently. Reading the code found two problems that this part fixes, and one that a later
part will:

- **Fixed now: an absurd total is "budget used up", not "store failed".** The database's own `SUM` stops with an error when the
  amounts together pass the largest number it keeps, and that error came out as `STORE_ERROR` (residual risk R-013). Admission
  and the gateway shares now add the amounts exactly, outside the database: each number is split into a high and a low half, the
  database adds the halves (which cannot overflow), and Python puts them together. A total larger than any cap is refused as the
  limit it is (`MONEY_LIMIT`, `INPUT_LIMIT`); a gateway allocation that is too large is `INVALID_CONFIG` with the existing sentence
  **when the account's tax basis is known**; when the ledger basis is `unknown` that check is postponed, as it always was (accepting
  the evidence is not approving the allocation), so such an allocation is accepted;
  `status` shows such an allocation exactly, with `allocated_above_int64: true` (the key is absent for every ordinary account).
  A store that breaks its own rules (a negative amount, more than 2^31 - 1 rows in one sum, a hold settled above what it
  reserved) is still `STORE_ERROR`: that is a real fault.
- **Fixed now: one place for money arithmetic.** `quota_balancer/money.py` holds the rules: whole micro-EUR, a range check where an
  amount enters or is stored (2^63 - 1 in storage; on the reporter's wires, money in a receipt accepts up to 10^12 and money in an admission answer stops at 10^12 - 1), a sum compared with a limit stops as soon as it
  decides, and an **unknown amount is never 0**: a sum with an unknown term is unknown, though a known part that already passes
  the limit still refuses.
- **Fixed now (part 2, R1-M4): one declared unit on the lease side.** An account that has a billing policy has declared its tax
  bases (`ex_vat` or `invoice_total`). Before admission compares money, the lease books are converted into the account's **cap
  basis** (the basis of its budget and caps): the receipts, the open holds and the new hold, each **rounded up** (an amount that
  is spent never counts for less than it is). The rule: **the receipts and the model rates that size a hold are in the policy's
  ledger basis.** The gateway's receipts and reports are euros without VAT (agenttalk's answer C1), so a policy should declare
  `ledger_basis = "ex_vat"`; that is a convention of the gateway, not a field Leasewise can check (residual risk R-016). A gateway
  share used as its own ceiling rounds **down** (a limit never allows more than it is); used as an allocation against the cap it
  still rounds up. Tokens and slots are never converted.
  - **A cap shared by several accounts** (a team or project cap) holds the spending of accounts that may have different rates.
    Each contributing account's receipts and holds are read separately, converted with **that account's own policy** (rounded
    up), and only then added. The asking account's policy is never applied to another account's spending, and no account is left
    out. A shared cap has no tax basis of its own, so **every account that shares it must declare the same cap basis**: if the
    accounts that take part (the one asking, even with no spending yet, and every account that has a lease in that scope) declare
    more than one known cap basis, the grant is refused with `CAP_BASIS_CONFLICT` ("accounts that share a spending cap declare
    different cap bases; give them all the same cap basis"). This holds in advisory mode too, for team, project and platform caps,
    and for money limits only. An account with no billing policy, or whose cap basis is `unknown`, does not take part (the
    fallback below). **What to do:** give every account that shares the cap the same `cap_basis` in its billing policy.
    **The imports refuse a NEW conflict straight away:** `import_config` and `billing_policy_import` compare the conflicting
    scopes before and after the import and refuse, with the same word `CAP_BASIS_CONFLICT` and nothing written, an import that
    **adds** a scope in which accounts that share a money cap (a team, project, seat or platform cap; the accounts of a scope are
    the accounts of its project, and the platform spans every account) would declare different known cap bases. Accounts with no
    policy or an `unknown` basis do not take part. The lease check stays, for stores made before. **A store that already holds a
    mismatch stays importable:** an unrelated import is accepted, and so is one that repairs the mismatch (aligning the bases, or
    dropping the mismatched account from the scope). **Stated limit:** once an account has billing history its `cap_basis` is
    frozen (`INVALID_CONFIG`), so the conflict of an older store may not be repairable by import; such a store stays usable for
    imports, and leases in the conflicting scope are refused (`CAP_BASIS_CONFLICT`). An audited basis correction would be a later
    card, only if it is ever needed. A grant lists a scope's
    accounts and reads their policies once per request, so the number of queries grows with the accounts that have leases in a
    scope.
  - An account with **no billing policy** compares the lease books as they are.
  - A policy that **cannot convert** (a basis is `unknown`, or the bases differ and no VAT rate is given) leaves that account's
    lease books compared as they always were: there is nothing to convert with, and nothing is guessed. In a shared cap this
    applies to that account's part only. **This already matters for advisory leases today:** a missing VAT rate can admit a lease
    where a known rate would refuse it. It is also a gate item before enforcement is switched on.
  - With equal bases (every account today: `ex_vat` and `ex_vat`) and with no policy, every answer is the one it always was.
- **Fixed now (part 2, R1-M5): unknown stays unknown through the sums.** The evaluator and the opening planner add through the
  same module: a sum with an unknown term is unknown (never the known part, never 0), and the evaluator then shows no headroom.
  The terms of the admission inequality are built as known or unknown (`money.gateway_exposure`, `money.account_exposure`,
  `money.unreported_receipts`, `money.coordinator_headroom`): a gateway without usable evidence makes its account's exposure
  unknown, and **missing coverage is mode C**, the sum of all final receipts of the month, an upper bound that counts a settled
  lease twice (safe, stated), never unknown and never 0. The coordinator's headroom is never larger than the evaluator's. These
  terms are built and tested; the reducer that calls them is a later card (R1-3).

Results are identical before and after with equal bases and with no billing policy. With different bases the conversion decides,
by design; with a policy that cannot convert, the answer is the old one.

### Technical detail (R1-M)

`lease._money_sums` and `lease._hold_exposure` replace the four `SUM` queries of `_admit_group` (the scope and account totals of
settled receipts and of open holds; money and input tokens use the same query text, so both are fixed). The usage numbers reach the
pure rule `policy.check_admission` saturated at `policy.USAGE_MAX` (2^63, one above the largest cap). `_advisory_budget_check` uses
`money.reaches`, which stops at the first receipt that decides. `store._allocated` replaces the three `SUM(share_micro_eur)` queries.
Tests: `tests/test_cardr1_money.py`, `tests/test_cardr1_money_store.py`. Record: `docs/STEP-CARD-R1-M.md`.

Part 2: `lease._lease_basis` reads the account's policy (None when there is none or it cannot convert) and `lease._in_cap` converts
a lease-book amount with `money.convert_total` (exact above every bound, saturated at 2^63, rounded up). `reduce.convert_basis`
takes a rounding role (`"up"` by default, so every older caller is unchanged). `billing_eval._normalize_money` keeps
`share_micro_eur` (the allocation, up) and adds `share_limit_micro_eur` (the ceiling, down), which `billing_open` reads.
`lease._scope_total` reads a shared scope per account (`money.exact_sums` once per contributing account), converts each total with
that account's own `_lease_basis`, and adds with `money.add`.
Tests: `tests/test_cardr1_money_basis.py`, `tests/test_cardr1_money_unknown.py`, `tests/test_cardr1_money_shared.py`. Record:
`docs/STEP-CARD-R1-M2.md`.

## Card R1-3a - the tables that will keep a gateway's evidence

In plain words: Leasewise will one day let a paid gateway's own books help decide whether a lease is allowed. Before it can trust a
figure from the gateway it needs somewhere to keep every report, to record which report an operator adopted as the trusted baseline,
and to write down changes that need a decision (a new ledger, a new policy fingerprint, a total that fell, a new reader). **This first
step only creates those places, empty.** No report ingest and no admission code uses them yet (that is cards R1-3b and R1-3c); only the
configuration import writes to them, when a gateway moves (below), and the removal guard reads them. So nothing changes for any seat,
lease or report. Design note: `DESIGN-R1-3.md` in the team's records, revision 2 (sections 3 and 7).

What is new:

- **Store version 10.** `init` upgrades a version-9 store in one step. The seven new tables are `gateway_epochs`, `gateway_totals`,
  `gateway_approvals`, `gateway_adoptions`, `gateway_readers`, `gateway_transitions` and `gateway_chain_state`. The program that reads the
  gateway and sends its reports keeps its memory under a random identity (a *reader incarnation*): the tables keep the numbering of each
  identity separately, record which identity an operator chose (`gateway_readers`: waiting, current or retired), and keep one "new reader"
  note per identity. The upgrade only adds tables: it changes no
  existing table, and it **creates no row**, not even from the reports already stored. Every gateway's evidence stays unusable until
  an operator approves and adopts it, in a later card.
- **An older Leasewise refuses a version-10 store** (`SCHEMA_UNSUPPORTED`) before it touches it, instead of misreading it.
- **Moving a gateway forgets its trust.** When the configuration import re-points a kept gateway to another machine or another account,
  its approvals are deleted, its chain state stops pointing at an adoption (the adoption and reader rows stay, as history), and one
  `owner_changed` transition is written. It must be approved and adopted again. Changing only its share changes none of this.
- **A gateway that has evidence rows cannot be removed** by a configuration import, like a gateway with receipts today.

Not built yet in this card: reading document version 4, ingest into these tables (card R1-3b), the rules that decide what is a change, the operator commands
`gateway-approve` and `gateway-adopt`, and the reading source (cards R1-3b to R1-3e, and the separate card R1-READER).

### Technical detail (R1-3a)

The table definitions are in one place, `migrate._DDL_V10` (with the list `migrate.EVIDENCE_TABLES`), as `STRICT` tables with every
money column a whole number >= 0, a hash 64 lowercase hex characters, a generation and a reader incarnation 32, `unattributed <= settled`,
and closed words for a transition's kind, an adoption's kind and a reader's state (at most one `current` reader per gateway, and one
`reader_changed` row per incarnation). `store._forget_evidence` is the `import_config` effect; `store.REMOVAL_GUARDS["gateways"]`
lists the seven new tables. Tests: `tests/test_cardr13a_schema.py`. Record: `docs/STEP-CARD-R1-3A.md`.

## Card R1-3b - gateway report version 4, kept at ingest

In plain words: a gateway report can now say who is sending it and what ledger it is about, and Leasewise keeps each such report as its own
row. **A new report format, "version 4", adds five facts:** `generation` (which ledger the figures come from), `receipts_seq` (how far the
receipts go, or null when that is not claimed), `quiet` (whether nothing was in flight), `held_over_reservation` (how many settled calls went
over their reservation) and `reader_incarnation` (the random identity of the program that sent it). A version-4 report must carry **both**
policy fingerprints (a version-3 report may still carry none), and its event id is **derived** from the incarnation, the generation and the
report number, so a new sender can never collide with an old report. Versions 2 and 3 are accepted exactly as before.

What is new:

- **Each accepted version-4 report is copied into `gateway_totals`**, joined to its stored reading, and its (sender identity, epoch number) is
  recorded with its generation in `gateway_epochs`. If the same (sender identity, epoch) later arrives with a different generation, it is
  recorded as an `epoch_generation_mismatch` transition and the first mapping stays. Versions 2 and 3 never reach these tables.
- **A report that collides with one already stored leaves a permanent record.** "Collides" means the same event id with different content. It is
  refused as before (`IDEMPOTENCY_CONFLICT`, nothing of the refused report stored), but a `sequence_conflict` transition is written **inside the
  store's own write transaction**, on both collision paths (the HTTP identity check and the reading idempotency check). Only the gateway's real
  owner (re-checked under the lock) can create one, only when the stored report belongs to the same gateway, incarnation and epoch, and a repeat
  adds nothing. A collision from a reader marked `retired` is only counted. Nothing in this step resolves or overwrites a transition.

**No evidence consumer uses these rows for `status` or for any admission decision yet (ingest itself reads them, for its identity checks and
for the transitions card R1-3c writes), so no seat, lease or admission decision changes. No reader sends version 4 live until card
R1-READER closes its start-up and continuity check (D1).** Design note: `DESIGN-R1-3.md`, revision 2, sections 1.3 and 3.

### Technical detail (R1-3b)

`validate_readings`: `DOC_VERSION_V4`, `derive_event_id` (a UUID version 5 over `incarnation:generation:seq` in a fixed namespace), the five keys, the
required hashes. `http_api._wire_to_reading` marks a snapshot that names a `reader_incarnation` as version 4. `store._record_report_v4` writes the
totals and epoch rows; `store._record_collisions` (after a `ROLLBACK TO` the savepoint taken at the start of the import, inside the same
transaction) writes the record, and `store._after_collision_record` is the fault-injection point. Tests: `tests/test_cardr13b_ingest.py`.
Record: `docs/STEP-CARD-R1-3B.md`.

## Card R1-3c - what a gateway report means for trust: the rules, and the transitions written at ingest

In plain words: Leasewise now decides, for every version-4 gateway report that arrives, **what it means for trust**, and writes down any problem
it shows, once. The decisions are a set of small rules that look at no database (`quota_balancer/evidence.py`); ingest feeds them the facts and
writes what they return. **Only `status` will see the result (card R1-3d adds the block). No seat, lease or admission decision changes, and no
operator command exists yet.**

What the rules decide, in this order:

1. **A report from a retired reader** is kept as history and counted. It creates no transition of any kind, on every path.
2. **A clash of identity is a conflict, whatever arrived first.** The same (gateway, reader, epoch, number) with different content is a
   `sequence_conflict`; the same (reader, epoch) with another generation is an `epoch_generation_mismatch`.
3. **Then order.** The same number and content is a duplicate; an older epoch or older month of the current reader is old; a lower number is
   late. These are counted and ignored.
4. **Then what an in-order report shows:** a gateway that says it is on hold (`reported_by_gateway`), a new epoch, a changed fingerprint, a fall
   of `settled` (checked on every report), and, **on quiet reports only**, a fall of `unattributed` or a rise of it above the adopted report.
5. **The month roll.** The first quiet report of a new month rolls the chain on its own (a `period_roll` adoption with no operator) only if it is
   from the same reader, epoch and generation, carries the approved pair, and **nothing is open**. A report that says "on hold" is decided before
   any roll, so it blocks it.

Readers: the first reader seen for a gateway with no adoption is `pending` and raises nothing; a new reader after an adoption raises one
`reader_changed`. Whatever makes a reader `current` retires the old current one first.

Stated limits and choices:

- **A state kind is written once while it is open** (`reported_by_gateway`, `epoch_changed`, the two fingerprint changes, `unattributed_rose`);
  an event kind (a fall, a conflict, a new reader) is written for each report that shows it. Nothing here resolves or overwrites a transition.
- With **no adoption** there is no chain to differ from, so a gateway that has never been adopted raises only the identity kinds, which never
  need a chain; a later adoption names what is open.
- A busy gateway has an evidence gap at the start of each month until its first quiet report.

### Technical detail (R1-3c)

`evidence.assess(report, state)` returns the standing, the transitions, a counter, whether to create a reader row, whether the report becomes the
last quiet one, and whether to roll. `store._evidence_state` reads the facts before the report is stored; `store._apply_assessment` writes the
result; `store._roll_period`, `store._promote_reader` (retire first, then promote) and `store._reader_state` / `store._count_retired_report` (the
one retired-reader classification, shared with the collision record) are the writers. `billing_eval._gateway_usable` stays a second line of
defence: a version-4 chain must be one reader, generation and month. Tests: `tests/test_cardr13c_evidence.py`. Record: `docs/STEP-CARD-R1-3C.md`.

## Card R1-READER, step 1 - the reader's reading and computing parts (nothing live yet)

In plain words: Leasewise will one day take a paid gateway's books from a small reader on the gateway's machine. This step builds only the
parts of that reader that **read and compute**. **Nothing here reads a live gateway, keeps any state, or sends anything**, and nothing changes for
any seat, lease or admission decision. The reader is not used until the later steps are built, the gateway is upgraded in the operator's maintenance
window, and Codex has checked the reader against agenttalk's merged command.

What exists now:

- **The runner can run the report command.** The contained Windows runner (card L6a-1b) has a second, closed mode, `report`, that adds **no**
  arguments. The command it runs must end with the fixed words `gateway report --json`. The receipt mode is unchanged and is still the default.
- **A strict reader of the report.** It requires every field it uses, with exact types and bounds, refuses a version it does not know, ignores the
  keys it does not use, and keeps no name or path. A gateway with no child-turn feature, or without quota lease binding, is refused with its own
  word (`child_cap_not_ready`).
- **A reader of the receipt page with the month.** The page keeps its version (1). A caller says which row shape it expects: the existing rows, or
  month rows with `charge_period` (a month or null). The keys stay closed.
- **The money, as the lead's worked example:** the four amounts, the count of settled-over-reservation attempts, the hold flag, and
  "spend nobody owns" = the settled total, less the receipts of that month (up to the same snapshot's number, in the same generation), less the
  settled cost of leased turns that have no receipt yet. A result below zero is refused, never rounded up. Every report counts as quiet, derived from
  the report version.

**Where the names come from:** agenttalk's merged head `921c82d4` (its guide `docs/QWEN-OVH-TRIAL.md`, "Receipts" and "The ledger report"), in
one table, `reporter/gwcontract.py`. The tests include agenttalk's own documented report and page, exactly as it prints them.

**The unowned figure is not exact monthly lease attribution.** A receipt that spans two months is counted in neither, so each month's share of it
stays in "spend nobody owns" although a lease owns it. Wherever Leasewise shows the figure, it keeps that qualification.

**One thing to decide before the gateway is upgraded:** the older receipt path (`reporter/run.py`, seat-lease receipts) reads existing rows and would
refuse every row of a gateway at `921c82d4` or later. This step does not change it. Details: `docs/STEP-CARD-R1-READER-1.md`. **Closed by card R1-RC below.**

Design: `DESIGN-R1-READER.md` (revision 3). Record: `docs/STEP-CARD-R1-READER-1.md`.

### Technical detail (R1-READER step 1)

`runner.run(..., mode=runner.MODE_REPORT)` appends nothing and starts nothing unless the command ends with `runner.REPORT_TAIL`; `MODE_RECEIPTS`
(default) appends `--after N --limit M --json`; an unknown mode, or a receipts run without whole numbers, starts nothing (`cannot_run`). Failures of
the report command (exit code 2) arrive as the runner's `exit_code`. `receipts.parse_page(..., month_rows=False)` and `read_page(..., month_rows=False)`:
the envelope is always version 1; `month_rows=True` returns `MonthReceipt` rows. `gwreport.parse_snapshot`, `fold_page`, `compute` and the `Totals`,
`Snapshot` and `Figures` values are pure. Tests: `tests/test_cardr1reader1_report.py`.

## Card R1-READER, step 2 - the money reader's memory (nothing live yet)

In plain words: the money reader must never skip or replace a report number. It moves past number n only after Leasewise has accepted exactly the bytes
it saved for n. This step builds the **memory** that makes that rule hold. **Nothing here reads a gateway or sends anything**; it is state and the rules
over it, with tests, and no seat, lease or admission decision changes.

What exists now (the reporter's own database, schema version 5; `reporter/gwstate.py`):

- **An identity** (a random value) that never changes, with a halt that is empty or one of three words (`continuity_conflict`, `send_refused`,
  `state_unavailable`).
- **Two progress numbers per generation:** `minted_seq` (the highest number saved) and `acked_seq` (the highest accepted), the ledger epochs (a new epoch
  whenever the generation changes, a return to an earlier one included), **at most one pending report** (its exact bytes), the receipts already read, and an
  **archive** that `reset` fills and never erases.
- **The start-up check:** the state is accepted only as `minted == acked` with no pending report, or `minted == acked + 1` with a pending report of exactly
  that number, and the pending bytes, their event id, the identity and the epoch mapping must agree. Anything else halts the reader as `state_unavailable`.
- **A complete absence of state is a first start** (a fresh identity). An absent identity with any surviving row is not: it halts. Every relationship of the state
  is checked in both directions (an epoch without progress, or progress without its receipts, halts too), and a saved report's identity facts must have exactly
  the types Leasewise accepts. The largest report or epoch number cannot be incremented: it halts, and `reset` recovers.
- **Mint** (number, save and keep the next report, together with the receipts read, in one transaction) and **acceptance** (only for HTTP 202 with
  `accepted` exactly `true` and the same event id; it deletes the pending report and moves `acked_seq`, in one transaction). **Reset** archives the old
  state and starts a fresh one in one transaction.

What is not here yet (step 3): the cycle, sending, the client's answers, counting refusals, the `resume` and `reset` commands and the status words.

### Technical detail (R1-READER step 2)

`gwstate.start`, `mint(state, generation, build, totals, now_ms)`, `accept(state, status, answer)`, `reset`, `inconsistency` (reads only), `report_facts` (the one
place that knows where a report states its own number, incarnation, generation and epoch). Tables: `gwr_identity`, `gwr_epoch`, `gwr_seq`, `gwr_pending`,
`gwr_receipts`, `gwr_receipt_months`, `gwr_archive`. The reporter state moves from schema version 4 to 5 (older files are migrated; a reporter that knows less
refuses a newer file). Tests: `tests/test_cardr1reader2_state.py`. Record: `docs/STEP-CARD-R1-READER-2.md`.

## Card R1-READER, step 3a - the money reader's send side (nothing reads a gateway yet)

In plain words: the money reader keeps at most one report waiting, saved as exact bytes. This step sends it and reads Leasewise's answer, and gives the
operator two commands to recover it. **Nothing reads a gateway yet** (step 3b builds the cycle and the report itself); the sender simply sends whatever is
pending, and with nothing pending it does nothing.

- **Only an exact acceptance clears the report:** HTTP 202, `accepted` exactly `true`, and the same event id. Anything else keeps the same saved bytes.
- **A collision** (409 `IDEMPOTENCY_CONFLICT`) stops the reader for good (`continuity_conflict`) until the operator runs `reset`.
- **A definitive refusal** (400 with `INVALID_CONFIG` or `SCHEMA_UNSUPPORTED`) is counted on the pending report. **Five in a row** (the named constant
  `REFUSAL_THRESHOLD`) stop the reader (`send_refused`). A retry in between does not reset the count; an acceptance clears it with the report.
- **Everything else is a retry:** 401, 429, 5xx, no answer, a timeout, an unreadable answer, even an unreadable success. The report is kept and tried again.
- **The report has its own lane:** its retry never stops the other uploads of the cycle, and their retries never stop it.
- **`reporter resume --confirm`** (local, audited, never talks to Leasewise) works only after `send_refused`: it clears the stop and gives the report a new
  retry budget, keeping the cumulative counts. **`reporter reset --confirm`** archives the reader's whole state and starts a fresh identity (which Leasewise
  must then be told to adopt). Without `--confirm` nothing is opened or changed.
- **If the old state is damaged** so that it cannot be archived normally (for example a report id that is not an id), `reset` keeps the whole old state as raw
  evidence in a bounded quarantine column (**256 KiB**) and discards nothing. A state too large for that is refused (`RESET_TOO_LARGE`) with nothing
  changed; then stop the reporter, **move the reporter's state file aside and keep it**, and start again: a complete absence of state is a first start.
- **`status`** gains a `money_reader` block (status version 4): whether a report is pending, how many refusals in a row, the stop word, and counts of
  `send_failed`, `send_refused`, `continuity_conflict`, `reader_halted`, `state_unavailable` and the totals. Numbers and closed words only.

### Technical detail (R1-READER step 3a)

`gwsend.classify` / `gwsend.send_pending` (the four answer classes; `client.post_reading` posts the stored bytes to `/v1/readings` and never raises);
`gwstate.note_refusal`, `note_conflict`, `resume`, `reset` (with `_quarantine`), `REFUSAL_THRESHOLD`, `QUARANTINE_MAX_BYTES`; the lane is `Reporter._send_report`, run
first in `_send`. New tables part of the same schema version 5: the archive's `quarantine` column and `gwr_audit`. Tests: `tests/test_cardr1reader3a_send.py`.
Record: `docs/STEP-CARD-R1-READER-3A.md`.

## Card R1-RC - the receipt path reads pages with or without the month

In plain words: when the operator upgrades the live gateway (agenttalk `921c82d4` or later), every receipt on a page carries a new field, the month
(`charge_period`). The reporter's receipt path, which settles seat leases from the gateway's receipts, only knew the old shape and would have refused
every row from that day on. That is safe, but it would have gone blind. **Now it reads both shapes** and keeps working across the upgrade.

- **What the operator sees:** nothing today. The receipt path keeps working after the gateway upgrade. This closes the residual risk R-017.
- **The month is read and not kept.** What the reporter stores and sends for a receipt is exactly what it was; a test shows that the same receipt read from
  an old page and from a month page gives identical stored rows and identical receipts at Leasewise.
- **One gateway, one shape.** A page whose rows all have the month, or none has it, is read. A page that mixes the two is refused, and nothing of it is
  stored. A receipt with no calls and a month is refused too, as agenttalk's own checker requires.
- **A fix of one more instance of a known mistake:** the close-time check used a pattern that accepts the digits of other scripts. It now accepts only the
  digits 0 to 9, and so do the park-marker time, the Windows security-identifier text and the journal segment number. A sweep of the whole package found no
  other pattern of this kind.

### Technical detail (R1-RC)

`receipts.EITHER` is a third value of `month_rows` (with `False` and `True`); `receipts._shape` decides the shape of a page from its rows. `run.py`
`_take_pages` passes `month_rows=receipts.EITHER`; `fold_page` (the R1 reader) still needs month rows. `_ISO`, `parkreader._PARKED_AT` and
`config._SID_TEXT` use `[0-9]`, and `events.take_snapshot` takes a segment number only if it is ASCII digits. Tests: `tests/test_cardr1rc_either.py`.
Record: `docs/STEP-CARD-R1-RC.md`.

## Card 7a-2 - the reporter core

`quota_balancer/reporter/` is the part of the observe-only reporter that decides
what to tell Leasewise about the turn journal agenttalk writes. It reads the
journal in snapshot rounds, keeps its own database (one owner at a time), opens a
shadow lease per message, prepares one report per launched run and one final
receipt, and says honestly when its view may be incomplete (then the final's token
numbers are "unknown", never 0). A message whose result is not known gets no
final. It sends nothing itself: the HTTP client, the `run` / `drain` / `status`
commands and the configuration are card 7a-3. See docs/STEP-CARD-7A-2.md.


## Card 7a-3 - the reporter program

**Status: built.** The reporter is now a program you can run. It reads the
turn journal that agenttalk writes, and tells Leasewise over its private HTTP
interface what each agent did: one observe-only lease per message, one report
per launched run, one final receipt. It never reserves money and never decides
anything; Leasewise does not use its numbers for admission. Audience: the
operator of a Leasewise store. See docs/STEP-CARD-7A-3.md.

### Quick start

1. Save the ids once: `python -m quota_balancer --db inventory.db policy-ids --json > policy-ids.json`.
2. Write the configuration file (a TOML file, see below) and make it private.
   On Windows: `icacls reporter.toml /reset && icacls reporter.toml /inheritance:r /grant:r "%USERDOMAIN%\%USERNAME%:F"`
   (the first part drops any grant to someone else, the second removes what the
   folder passes down and gives the file to you alone).
   On Linux or macOS: `chmod 600 reporter.toml`.
3. Read the journal once and send what is due:

```
python -m quota_balancer reporter drain --config reporter.toml
```

Expected output: one line such as
`{"blocked": null, "journal": "ok", "new_events": 3, "sent": 3}`.

### The configuration file

The file holds a secret (the identity value Leasewise's ingress expects), so the
reporter refuses to start if other users can read it. Never commit it and never
put it in an agenttalk bus root. The identity must not be, or be part of, any of
Leasewise's public error words; use a long generated value. It is TOML, with one table per agent:

```toml
version = 1
base_url = "http://127.0.0.1:8443"   # plain http only for loopback; else https
identity = "the-identity-value"       # SECRET
machine = "desktop"                   # this machine's alias
journal_root = "C:/Users/me/AppData/Local/agenttalk/turn-events/<project>"
state_path = "C:/Users/me/AppData/Local/leasewise/reporter/state.db"
policy_ids = "C:/Users/me/leasewise/policy-ids.json"
# poll_seconds = 5               # `run` only
# silent_after_seconds = 86400   # a configured agent quiet this long is "silent"

[agents.alpha]            # the agenttalk agent name
seat = "s6"               # with `machine`, the seat key
account = "meter"         # with `model`, the model key
model = "opus"
task_class = "observed"   # must exist in the snapshot
effort = "opus"
expected_calls = 1        # the ceilings to request, within the profile
max_calls = 2
output_cap = 1000
per_call_output_cap = 500
ttl_ms = 1800000
# team = "dev"            # only to tell apart two seats with one alias
```

Keys are compound, as `policy-ids --json` returns them: a seat is
(this machine, seat alias), a model is (account, model alias). A bare alias that
matches more than one row is refused at start-up. An agent that appears in the
journal but has no table is "unconfigured": its events are read and counted and
never sent. **Who may read the file.** On Windows the reporter reads the file's
access list as security identifiers (not names, which depend on the language of
the system) and allows only three: you (the current user), SYSTEM (`S-1-5-18`)
and the Administrators group (`S-1-5-32-544`). Any other identity, an entry it
cannot classify, an entry inherited from the folder, or a list it cannot read
stops the start with a fixed message. On other systems no group or other
permission bit may be set.

### Commands

| Command | What it does |
|---|---|
| `reporter drain` | One pass: read the journal, send what is due, exit. The default operator action. |
| `reporter run` | Repeat until stopped (Ctrl-C or SIGTERM); safe to stop at any moment. |
| `reporter status [--json]` | What the reporter knows, per agent. |

`run` and `drain` cannot overlap: the second one stops at once with the fixed
message "the state is owned by another reporter" (exit 2). While `run` is
active, `status` shows a copy that `run` refreshes every cycle.

Exit codes: 0 done; 1 internal fault; 2 refused to start (configuration, or the
state is owned); 3 Leasewise did not answer or refused the identity, so items are
still waiting; 4 the journal is refused (see below).

### Runbook

- **Leasewise is down.** Nothing is lost: everything is stored first and sent
  later with the same bytes and the same `Idempotency-Key`. One request is in
  flight at a time, with 5-second timeouts on each network step (connecting,
  sending, reading), which is not a total limit on one request; failures back off
  from 5 seconds to 15 minutes with jitter. An answer that cannot be used (garbled, too
  deeply nested, not text, empty, larger than the size limit, or a lease id that
  repeats the secret identity) is handled by its status. On a success (2xx), a
  401, a 429 or a 5xx it counts as no answer: it is retried with the same bytes and
  nothing from it is stored. On any other 4xx it is a permanent refusal with
  no retry loop (the item is listed under `permanent_failures`). A `401` is retried too. `status` keeps the last one as history
  (`last_401`: code, time, and `active`). While it is active the advice is to
  check the identity value in the file; the first request Leasewise accepts
  afterwards makes it history and the advice goes away.
- **Error words.** Only Leasewise's own error codes (the ones in
  `quota_balancer/errors.py`) are ever saved or shown. Any other text an answer
  carries becomes `UNKNOWN_CODE`; the reporter adds `BAD_ANSWER` (a success
  without the lease) and `MALFORMED_RESPONSE` (an unreadable answer).
- **Items that failed for good** (acquisitions, reports and finals) are listed in
  `status` under `permanent_failures`, each with its code, and one warning line
  appears. A final also says whether the observation behind it was complete: a
  complete observation whose final was never delivered is shown as that.
- **After an outage, or when `status` shows open or incomplete messages,** run
  Leasewise's `expire-sweep` yourself:
  `python -m quota_balancer --db inventory.db expire-sweep`. It moves leases
  whose time has passed to `uncertain`; a shadow lease holds no money. It is an
  explicit step, never a timer. Scheduling it, or the reporter, needs the
  desktop lead's approval (a task named `lw-...`).
- **`journal_refused` in status, exit 4.** The journal has a schema version this
  reporter does not know (or a file is shorter than the position already read).
  The reporter reads no new input until you fix it; what it already stored is
  still sent. Upgrade the reporter, then run it again.
- **An agent "stopped by a configuration fault".** Leasewise answered
  `POLICY_DENIED`, `NOT_FOUND`, `FORBIDDEN` or `CONTEXT_UNKNOWN` for it. The code
  is in `status`. Fix the cause (seat, model, profile ceilings, identity) and
  start the reporter again: a start forgives the stop. The message that got the
  refusal stays failed.
- **Never delete the state file** while Leasewise still holds its leases: the
  state remembers the exact bytes of each request, and a rebuilt one would be
  refused as a changed replay. Leave old leases to `expire-sweep`.
- **The observed task class.** Import a profile whose ceilings match the
  `[agents.*]` tables, with a TTL long enough for your outages: a report that
  arrives after its lease expired is dropped (counted), and the final is still
  accepted.

### What `status --json` shows

At the top: `rounds_completed`, `journal_refused`, `last_401`,
`silent_configured_seats`, `operator_actions` (plain sentences); `last_401`
carries `active`. Per agent, also `permanent_failures` and
`permanent_failures_total`:
`configured`, `silent`, `stopped_by_configuration_fault`, `streams` (seen,
missing, with gaps, with torn tails), `events_read`, `dropped_total`,
`damaged_lines`, `messages` (known, open, complete, incomplete with a reason,
`outcome_unknown`), `finals` (pending, sent, failed), `late_observations`,
`acquisitions` (pending, acquired, failed with codes), `reports` (pending, sent,
dropped, failed, over the cap), `last_successful_call`. "Complete" means "no loss
the journal can see"; it is not a guarantee. `status` never shows the URL, the
identity or any message text.

## Card 7c-2 - limit events

**Status: built.** When a Claude seat runs out of its allowance, agenttalk stops retrying and keeps a
small "parked" file for that seat. The reporter now notices that file and tells Leasewise: "this
account is out until about this time". Leasewise marks the account parked and holds it until **30
seconds after** that time, so the provider has really reopened. Audience: the operator of a Leasewise
store. See docs/STEP-CARD-7C-2.md.

### What it does, in plain words

1. **The reporter looks** at the park files in the agenttalk folders you name, about every 5 seconds.
   While this is on, the wait between two finished cycles is at most 60 seconds; reading and sending
   can make the time between two looks longer than that, so 60 seconds is a cap on the wait, not a
   promise about the gap between looks.
2. **It reads each file strictly.** Anything unexpected gives no event, and a file that may only be
   half written is looked at again on the next look.
3. **It builds one event** for the account you declare and the limit that was hit. Two seats on one
   account that hit the same limit make one event; a later reset time makes a new one. The event is
   stored with its body frozen before anything is sent, so a restart or an outage loses nothing, and
   every retry resends exactly the stored bytes.
4. **It sends the event first** in every cycle, to Leasewise only (`POST /v1/limit-events`), before any
   lease message: a lease answer of "retry" or 429 (a parked account refuses every lease request)
   never delays it, and a limit event that Leasewise refuses fails only itself, never an agent and
   never the reporter.
5. **Leasewise** holds the account until the reset plus 30 seconds. It adds the 30 seconds itself,
   once, to the new event's time only; the time the reporter sent is kept as sent, and a repeated
   delivery of an event is still a pure read.
6. **`reporter status`** shows what limit events are doing, including the sentence under "Limits".

### How to set it up

1. **Save the ids again.** The reporter needs each account's id. `policy-ids` now lists
   `accounts` (alias and local id), so save its output again after upgrading:
   `python -m quota_balancer --db inventory.db policy-ids --json > policy-ids.json`. An older
   snapshot has no accounts, and the reporter then refuses to start if limit sources are configured.
2. **Add the account map** to the reporter configuration (every key is required, `provider` is
   `claude` for now, at most 8 sources):

```toml
[[limit_events.sources]]
bus_root = "C:/path/to/the/agenttalk/folder"   # read only; never the folder holding this file
os_user  = "operator-desktop"                  # your own label for the OS user the seats run under
provider = "claude"
account  = "claude-main"                       # a Leasewise account alias
```

   The park files prove nothing about which account a seat uses, and a seat's name never decides it:
   **this map is a setting you are responsible for.** The folder rules:
   - two sources may not name the same folder with the same provider (the folder is compared as it
     really is, so two spellings of one folder count as one);
   - one OS user and provider must name one account;
   - the folder may not contain the reporter's state file or this configuration file;
   - the reporter never writes into the folder.
3. **Start the reporter** as before (`reporter run` or `reporter drain`). It refuses to start, with a
   fixed sentence, when a configured account is missing from the snapshot or appears twice.
4. **Look at `reporter status --json`.** With limit events on it shows the account map in use (so you can
   check it), the counts of the last look, how many events were captured, are waiting, were delivered
   or failed, and any capture fault. The plain text view shows only the gap sentence and the lines that
   ask for action. A configuration without the `limit_events` block behaves exactly as before. While an
   older reporter (from before this card) is still running during an upgrade, `status` shows its
   snapshot and says that it has no limit-event section; it never says that limit capture is off.

### What is read, and what is not

The reporter reads the documented contract in agenttalk's README ("Reading the park marker from
another program") and nothing more. A missing file, one that cannot be opened for a moment (on
Windows), half a file, or one with a documented field missing is read again on the next look. A file
with a field that is not documented, a wrong type, a value out of range or another version, and any
provider other than `claude`, gives no event. A stale file (not refreshed for 300 seconds), a file
without a window or a park time, and a reset that has already passed give no event either. Only five
values are kept from a file; the seat's name, the message id and the wrapper generation are checked and
dropped, and never leave the machine.

### Limits

- **The gap (best effort).** A park that starts and ends while the reporter is not running, or between two checks, is never reported. The seat still parks on its own, and the next refusal after the reporter is back is reported. This closes when agenttalk keeps a permanent "parked" record in its turn journal (a later agenttalk change, after the turn-journal pull request #300 is merged, together with Codex limit events). Until then, limit events are best effort.
- **The map is trusted (R-011).** If someone signs a different account into the same OS user, the map
  is wrong until you edit it, and the reporter cannot see the switch.
- **One outbox, one memory.** An event's body is frozen only while the same reporter state file is
  kept. If the state file is recreated, or two reporters run with the same `machine` alias, the same
  `event_id` can be rebuilt with a different first-seen time (`observed_at`); Leasewise then answers
  `409` and that event fails for good, and `status` shows it. There is no deduplication across state
  files.
- **A folder with more than 256 entries.** The reporter reads the folder's FIRST 256 entries, in the
  order the system lists them (ignored names count too), and says so with a flag; it never counts the
  folder. That order is usually the same every time. So in a folder with more than 256 entries, a
  marker outside those first 256 can stay unseen on EVERY look, until earlier entries go away: a later
  look does not necessarily catch up. This is a stated best-effort limit. A folder of park files is
  small in practice (one per parked seat), far below 256.
- **No backfill.** Blocks that already exist keep the `retry_at` they have; only new events get the 30
  seconds.
- **No downgrade of the state file.** *(Historical: this is how it was when card 7c-2 landed. The state file is
  version 3 since card L6a-1, and `status_version` is 3; see that card below.)* The state file became version 2, and
  a version-1 file is upgraded the first time the new reporter opens it. A reporter built before this card meets a
  version-2 file with its existing "the state file version is not supported" refusal: it never opens
  or changes it. Keep a copy of the state file if you may need to go back.
- **Claude only.** Codex limit events wait for agenttalk's own Codex producer.
- **Leasewise does not un-park an account by itself.** An operator clears the block until card 7c-3
  adds recovery.
- **Advisory until R1.** Leasewise's refusals are advisory until card R1, so a missed event costs a
  missing warning, not a wrong refusal.

### Technical detail

State file version 2 (historical; version 3 since card L6a-1) adds the `limit_event` table (one row per limit fact
keyed by `event_id`; the frozen canonical body; identity and body cannot be changed or deleted). The limit pass runs
before the lease pass and the two stay separate; `status_version` was 2 with a top-level `limit_events` object.
One limit fact is (installation, account alias, evidence kind, window, reset); with an unknown reset the
park time joins the key. `event_id` and `turn_id` are derived from that key; the body is built field by
field with the reset RAW. Leasewise's margin is `LIMIT_RETRY_MARGIN_MS` in `lease.py`.

## Card L6a-1 - gateway receipts

**Status: built, fixture-only.** A gateway keeps one final receipt for every quota lease it served: how
many calls, how much text went in and out, what it cost, how it ended. The reporter reads those receipts
and tells Leasewise, so the lease is settled with what really happened. Audience: the operator of a
Leasewise store. Until agenttalk ships its receipt command, receipts can only come from a file you write
yourself (that is what the tests do). Design: `C:\lw\records\DESIGN-6A.md` section 8; build record:
`docs/STEP-CARD-L6A-1.md`.

### What it does, in plain words

The reporter never loses a receipt and never sends one to the wrong place. It does that in two steps,
and a receipt is only released from the first to the second when the right paperwork exists.

1. **Keep.** Every receipt that arrives is first written down exactly as the gateway said it, together
   with the number of the last receipt taken, in one step. This happens even when the reporter does not
   yet know which lease the receipt belongs to. If anything goes wrong, nothing is written and the same
   receipts are read again.
2. **Release.** The reporter reads, but never changes, the **admission store**: the file where the
   admission command (card L6a-2) writes, before each turn starts, which lease it handed out and which
   gateway that lease expects. Before anything is copied from the store, each record is checked against
   itself: the reference must be the lease's own text and the hash must be the SHA-256 of it. One record
   that does not match refuses the whole read (nothing is copied), so a damaged store cannot send one
   lease's receipt to another. A kept receipt is released for sending only when the store has a record
   for its lease **and** that record names the very gateway the receipt came from. At that moment the
   settle message is made once and never changes; it is the only thing ever sent for that receipt.
3. **Send.** The reporter sends the stored message to Leasewise (`POST /v1/leases/{id}/settle`), after
   limit events and before lease messages. A restart, a lost answer or a retry sends the same bytes
   again, so Leasewise recognises them and answers with the first answer. Only the answer `200` means
   "done"; any other success status is treated as an unreadable answer and the same bytes are sent again,
   with the word `MALFORMED_RESPONSE` showing.

**What waits, and what is set aside.**

- A receipt whose lease has no record yet is **quarantined: `unknown_reference`**. Nothing is sent.
  Each cycle considers captures whose binding exists, oldest first, whether newly captured or previously
  quarantined, up to 500, so no number of waiting receipts can keep another from being released.
- A receipt whose record names **another gateway** is quarantined: `wrong_gateway`. Nothing is ever sent
  for it. This (and `duplicate_reference`) is permanent: a record never changes, so it is never looked at again
  and stays visible in `status`. The same holds if a source's name is later pointed at a different gateway: the receipts already
  kept stay tied to the gateway they came from, and the new mapping starts a separate source with its
  own counters.
- A second receipt for a lease and ledger that already has one is quarantined: `duplicate_reference`.
- A **missing or busy admission store** is never read as "this lease is unknown". The reporter reads it
  again later; receipts are still kept.

**A rebuilt ledger.** Each gateway ledger has its own identity (its *generation*). When a page arrives
from a different generation, the reporter keeps the old counter, starts a counter at 0 for the new one
(or takes back the kept counter of one it saw before: generation A, then B, then A again continues where
A stopped), and never mixes the two. Message ids include the generation, so two ledgers cannot collide.

**Unknown token counts.** A gateway may report that it does not know a lease's input or output tokens
(a `null`). That is passed on as unknown and never turned into 0; Leasewise settles the lease and marks it
`usage_unknown` (card L6a-0). Cached input is always reported as 0 (see the limits).

### What Leasewise answers, and what the reporter does

| Leasewise answers | The reporter does |
| --- | --- |
| `200` (a first settle, or the same message again) | done |
| `RECEIPT_CONFLICT` (a different final for a settled lease, or a lease of another gateway; the account is parked) | that receipt fails for good; `status` shows it and asks you to look at the parked account |
| `IDEMPOTENCY_CONFLICT`, `INVALID_REQUEST`, `NOT_FOUND`, `FORBIDDEN` | that receipt fails for good, with its word |
| `LEASE_CLOSED` | not a "done" word: a settle never answers it, so it is a permanent failure of that receipt |
| no answer, `401`, `429`, `5xx`, an unreadable `2xx` | wait and send the same bytes again |

A failed receipt never erases what was kept, and never stops the other receipts or the other passes.

### What `status` shows

The `gateway_receipts` object (closed words and counts only; never a lease id, a reference, a gateway
id, an amount or a path):

- `rows`: waiting (`pending`), delivered (`sent`), `failed_permanent` and `quarantined`; `failed` counts
  the permanent failures by closed word;
- `quarantine`: how many are set aside for each reason, and `quarantine_action`: one fixed sentence per
  reason saying what to check (the sentences also appear in `operator_actions`);
- `generation_changes`: for each source, how many new ledger generations were seen and when the latest
  first appeared;
- `awaiting_receipt`: leases whose record the reporter has copied but for which no receipt has come, as a
  count and the age in seconds of the oldest; `overdue`: those among them whose own expiry has passed
  (`operator_actions` says so);
- `admission_store`: the current state of the admission store as the reporter last read it (`ok`, `missing`,
  `busy` or `unreadable`; empty before the first read) and how many times each trouble
  happened before. The state goes back to `ok` after a good read. `operator_actions` has a sentence for the
  ones that need you (a wait, `busy`, does not);
- `refused` and `read_later`: how often a read was refused or had to wait, by closed reason;
- `last_seq`, `stored`, `last_read`, `last_ok` and the note about cached input.

### A command source (card L6a-1b, Windows only)

In plain words: instead of a test file, the reporter can run agenttalk's receipt command and read the page it prints. The
page goes through exactly the same strict rules as a file. Running another program is the dangerous part, so the reporter
promises three things: the program and **everything it starts** end together (even a helper started in its first instant),
it never freezes the reporter, and every failure is a plain word in `status`.

**The reporter must run as the same Windows user as the gateway.** The receipt command reads that user's own ledger and
credential file, so a reporter that runs as another user reads nothing useful. For the same reason the operator's `env`
table below will need that user's application-data variable, so the program can find those files.

```toml
[gateway_receipts]
gateway = "g1"
admission_store = "C:/path/to/admission.db"
command = ["C:/path/to/agenttalk.exe", "gateway", "receipts"]   # an argument list, no shell; the first is an .exe
timeout_seconds = 20                                             # 1 to 60, default 20: one deadline for one read
# max_bytes = 1048576                                            # the most output read (1024 to 4194304)
# page_limit = 100                                               # receipts in one page, 1 to 1000

[gateway_receipts.env]                                           # at most 8; the only extra settings the program gets
AGENTTALK_ROOT = "C:/lw/team"
```

What the reporter does with it:

- It appends `--after N --limit M --json` **last**, where `N` is the last receipt number it took. Do not write
  them yourself: any argument that could be read as one of those names is refused at start-up (`--after`, `--after=5`,
  `--aft`, `--a`, `--l`, `--j` and a bare `--` are all refused; a plain word or `-m` is fine).
- **Start-up refusals** (one fixed sentence each, never repeating what the file said): not Windows; the command is not a
  list of 1 to 16 texts; the first element is not a fully qualified `X:\...` path (`C:foo` and `\foo` are refused; `/` is
  read as `\`); it starts from a network or device path (`\\server`, `//server`, `\\?\`, `\\.\`); it is not an `.exe`
  (Windows would run a `.bat` or `.cmd` through `cmd.exe`, which splits arguments by other rules); an argument has a double
  quote or a control character; a reserved option; `timeout_seconds` or `env` outside their bounds, or given with a fixture.
- **Containment.** The program is created frozen and already inside a Windows job that ends everything in it, checked to be
  inside, and only then allowed to run. If that cannot be done, nothing runs. When the read ends (or the reporter stops)
  the job is ended and its own count of live processes is polled to zero. If that cannot be confirmed, the read says so
  and its output is **not used**.
- **What the program gets.** Standard input that ends at once, standard error thrown away, only two handles (nothing else
  the reporter holds), the folder of the program file as its working directory, and an environment of only `SYSTEMROOT`,
  `SYSTEMDRIVE`, `PATH`, `TEMP` and `TMP` (from the reporter's own environment) plus your `env` table (yours wins, names
  matched without regard to case). Unlisted variables are excluded. This is lifetime containment, not a sandbox against your
  own command.
- **The words in `status`** (`gateway_receipts.read_later`, counts only; nothing is captured and the cursor does not move):
  `source_missing`, `not_json`, `cannot_run` (it could not be started), `cannot_contain` (a protective step failed: it
  never ran), `timeout`, `interrupted` (the reporter was stopping), `exit_code` (a non-zero exit: its output is ignored),
  `leftover_process` (it exited but a helper still held the output), `read_failed`. Too much output is a refused page
  (`refused.too_large`). `cleanup_incomplete` counts reads whose end was "termination requested but not confirmed";
  `status` also shows the source kind (`fixture` or `command`) and the last read's word. Advice sentences follow the
  **current** failure: they go away when a read succeeds.
- **The cycle budget.** A cycle reads receipts for at most `max(timeout_seconds, 30)` seconds, checked **before** each read
  starts; then it stops taking pages (the next cycle goes on from the cursor) and the send passes run. A read that starts
  just before the budget ends runs for its own deadline, so, **with normally returning Windows calls, receipt reading can
  occupy roughly `max(timeout_seconds, 30) + timeout_seconds + 2` seconds: about 52 s at the default, about 122 s at 60.**
  This bounds receipt reading, not the whole cycle.

Limits, plainly:

- **Before live use, the receipt command's own option parser must be checked.** The command must parse the appended
  `--after N --limit M --json` as options. Configured arguments must not introduce aliases of those options,
  argument-file expansion (`@file`), or a remainder/forwarding mode that consumes them. Other parsers are unsupported. The
  refusal of shortened spellings is a guard, not a proof for every parser.
- **The deadline is cooperative.** It holds while the Windows calls return normally. Not covered: creating the process, the
  setup before the clock starts, and a possible wait inside `PeekNamedPipe`. The reporter must be the only reader of the pipe.
- A drive letter mapped to remote storage is not detected as a network path; the operator is responsible for it.
- Helpers started through another Windows service (a scheduled task, a service) are outside any job. A helper that
  keeps stdout open beyond the post-exit grace causes `leftover_process`. Helpers that do not hold stdout are ended during
  job cleanup, and a successful page may still be accepted. Configure a program that waits for the work needed to produce
  its page.
- The job holds at most 32 processes and has no memory or processor limits.
- Windows only; Linux and macOS are a later card.

### How to set it up

1. **Save the ids again.** `policy-ids` also lists `gateways` (alias and local id). Save its output
   again; an older snapshot has none, and the reporter then refuses to start if `gateway_receipts` is set.
2. **Add the block** to the reporter configuration. Name `admission_store` and **exactly one** receipt source: a
   `fixture` file (tests) or a `command` (a program the reporter runs, **Windows only**; see "A command source" below):

```toml
[gateway_receipts]
gateway = "g1"                                  # a Leasewise gateway alias
admission_store = "C:/path/to/admission.db"     # the admission store, an absolute path (read only here)
fixture = "C:/path/to/receipts.json"            # a ledger file (tests)
# max_bytes = 1048576                           # 1024 to 4194304: the largest fixture read
# page_limit = 100                              # receipts in one page, 1 to 1000
```

   The fixture holds `{"generation": "<32 hex>", "receipts": [...]}` (the reporter makes the page after the
   last number it took, of at most `page_limit` receipts), or one finished page (version 1: `generation`,
   `after_seq`, `next_seq`, `has_more`, `receipts`), which is refused if it is longer than `page_limit`.
3. **Start the reporter** as before. It refuses to start, with a fixed sentence, when the gateway alias
   is missing from the snapshot, appears twice, has an id that is not a UUID, or when the `admission_store`
   setting is left out or is not an absolute path. A file that the setting names but that is not there yet
   is different: the reporter starts, keeps every receipt, shows `admission store: missing`, and reads it
   again each cycle. Without the block nothing changes.

### Limits

- **No history limit any more (card L6a-2).** Card L6a-1 read the granted rows of the admission store whole, up to
  100,000. The reporter now copies new grants in batches after a durable grant number (see card L6a-2 below), so no
  history size can stop it. Never delete admissions.
- **Command sources are for Windows only (card L6a-1b).** On any other system a configured `command` is refused at
  start-up with "Command receipt sources are only available on Windows in this version; use a fixture file." (see
  "A command source" below for what it does and what it does not promise).
- **The page format is still our reading of the design.** agenttalk does not have the receipt command yet (its card
  6a-1), so the page format lives in one file (`quota_balancer/reporter/receipts.py`); a fixture file is the only source
  that exists today.
- **Nothing is compacted.** Kept receipts and their messages are never edited or deleted. A page with a
  gap or a repeated number is refused whole.
- **A replaced ledger cannot rebuild old receipts that were never received.** The record of a lease does
  not say which generation it was in; a receipt missing from a replaced ledger stays missing.
- **A missing receipt is never a guessed zero.** The lease keeps its liability (an advisory lease holds no
  money, so there is nothing to release). Waiting work stays visible
  in `status` (`awaiting_receipt`, then `overdue`); nothing else settles that lease.
- **Cached input is reported as 0.** The gateway records none, so every settle says `0`. Zero is a gap in
  what is recorded, not a measurement. `status` says so too.
- **Journal reports for gateway leases are not suppressed yet.** That, and the list of gateway agents,
  come with card L6a-2 (the admission command).
- **Live gateways need the X3 upgrade** (the gateway program with the receipt API) before this can reach
  one. Nothing here touches a live gateway.
- **A refused source stays stuck** until it is fixed: no receipt after the last kept one is taken. `status`
  asks for attention in that case; a wait ("read later") is not a fault.
- **No downgrade of the state file.** It is now version 3 (1 and 2 are upgraded in one step). A reporter
  built before this card refuses a version-3 file with its existing "the state file version is not
  supported" sentence and never opens or changes it. Keep a copy if you may go back.
  *(Card L6a-2 moves the file to version 4, which only adds the `superseded` record; see its section. A version-3
  file is upgraded in one step, nothing is deleted, and the L6a-1 reporter refuses version 4 the same way.)*
- **Advisory until R1.** Metered leases stay advisory in Leasewise until card R1.

### Technical detail

State version 3: `receipt_source` (a name and the gateway it was bound to, immutable), `receipt_cursor`
(one per source and generation; its `last_seq` and `page_ord` are updated as pages are taken and as the
generation read last changes; `first_seen_ms` is when it first appeared), `receipt_binding` (copied from the admission store, immutable), `receipt_capture` (stage 1,
immutable evidence, the source's gateway frozen on the row), `receipt_delivery` (stage 2, immutable, the
settle text), and `receipt_state` (the mutable delivery-status table: `captured`, `quarantined`, `pending`, `sent`,
`failed_permanent`, with a closed reason). Stage 1 and stage 2 are each one `BEGIN IMMEDIATE`. Stage 2 labels
all new captures without a record in one statement, then works on the captures whose reference has a record, oldest
first, at most 500 per cycle, found by a direct join on the reference hash (`receipt_capture_ref` indexes it).
`event_id` is a UUIDv5 of the reference hash and the ledger generation. The pass order is limit, receipt,
lease; `status_version` is 3. The admission store is opened with `mode=ro` and `query_only`; its layout is
in `quota_balancer/reporter/admission_store.py`, the one module the admission command will reuse.

## Card L6a-2 - the turn admission program (`reporter admit`)

**Status: built, fixture-only.** Before a paid turn starts, the agent wrapper asks one question: *may this turn run,
and under what limits?* `reporter admit` is the local program that answers it. It asks Leasewise for a lease, keeps the
answer in a small database file (the **admission store**), and gives the **same answer** every time it is asked about
the same message, however often it is run, killed or run at the same time as itself. Audience: the operator who sets up
the wrapper's turn admission hook. Design: `C:\lw\records\DESIGN-6A.md` sections 8.5 to 8.7; the wire format is the
neutral hook specification, section 4. Build records: `docs/STEP-CARD-L6A-2.md`.

### What it does, in plain words

The hook runs the program once per question. One JSON request goes in on standard input; one JSON answer comes out on
standard output; nothing else is printed. There are four questions:

| Question | What the wrapper wants to know | Answer |
| --- | --- | --- |
| `admit` | May this message's turn run? | `admitted` with a **reference** (the lease id), the caps and the time left; or `held` with one of six words |
| `recall` | What was the answer for this message? | the stored admission, or `unknown`. It never creates anything |
| `pending` | Which admitted messages are not closed yet? | up to the limit asked for (1 to 64), each with its reference |
| `closed` | This message is closed | `ok`. A repeat changes nothing |

Only `admit` ever answers `held`. If another question cannot be answered now (the store is busy, or the message's lock is
not free in time), the program prints **nothing**, writes a fixed sentence on standard error and exits with code 3; the
wrapper keeps its work and asks again. A request that is not allowed (wrong key, wrong type, over a bound or over 512
bytes) exits with code 2. The reasons for `held` are only `coordinator_unreachable`, `refused`, `parked`,
`context_unprovable`, `store_busy` and `not_configured`.

**Why the same answer every time.**

1. A message that already has a saved grant is answered from the file: no lock, no call to Leasewise.
2. Otherwise one of 64 small lock files, chosen from the message's key, lets only one run work on that message at a time.
   The operating system frees the lock if the run dies, so nothing is ever left locked and nothing is cleaned up by hand.
3. The request, and every later body, is **written down before it is sent**. If an answer is lost, the same bytes go
   again and Leasewise recognises them and returns the lease it already made. A stale estimate is rebuilt (at most 3 times
   per recovery episode); if Leasewise then says "same request, different body", an earlier body was granted after all, and
   the saved bodies are replayed, newest first (at most 4), to find that grant.
4. The grant is saved **before** anything is printed.
5. The whole run has one 20-second deadline (below the hook's 25 s), and it is one real clock: the time left is
   handed to every step, including the connection, each piece of Leasewise's answer (a peer that answers a few bytes at
   a time cannot stretch it), every wait for the store file, and the retry that opens the store, which does not start
   an attempt it cannot finish. If the deadline comes first the connection is closed and the answer is `held` with
   `store_busy`; what was learned is kept, so the next run continues where it stopped. A transport failure just before the deadline can be answered `coordinator_unreachable` (with the normal back-off) instead of `store_busy`; which one it is depends on which side of the deadline the failure lands. Both answers keep the saved request. A grant that Leasewise made
   but that could not be saved in time is recovered by the next run, which sends the very same saved bytes and gets the
   same lease back. Cleaning up the exchange after the deadline measured at most a few milliseconds, **for plain HTTP and
   for HTTPS alike** (worst of ten runs: 2 ms; `test_card7l6a2_fix2` for a connection that uses up the whole budget and a
   peer that answers a few bytes at a time, `test_card7l6a1b_https` for the secure-connection setup). The one thing allowed
   past the deadline is a save that is already in hand, which may wait at most 0.5 s for the store file. So the run ends
   within the deadline plus well under a second, and the tests allow one second.
   **The connection obeys the deadline too, and so does the HTTPS handshake:** the connection is made with the time that is
   left, its socket belongs to the watchdog from the moment it exists (for HTTPS, from before the handshake, and the
   handshake is given only what is left of the budget, not the whole budget again), and nothing is sent or waited for
   after the deadline. **What is still outside the deadline, for HTTPS:** the local work of building the certificate
   settings and checking the certificate (about 50 ms of processor time, counted in the budget but not interruptible).
   Certificate checking itself is unchanged. For that reason, **with `[admission]`
   the address of Leasewise (`base_url`) must be an IP address or `localhost`**: a host name is refused at start-up
   ("base_url: with [admission], the host must be an IP address or localhost"), because looking a name up cannot be
   stopped by a deadline. `localhost` is connected as `127.0.0.1` without asking the resolver (Leasewise serves on IPv4
   loopback only). Without `[admission]`, the `run` path may still use a host name; its lookup is not bounded, and it
   has no hook promise.
6. Any other refusal or outage is `held` with a back-off kept in the file (10 s after an outage, 30 s when the account is
   parked, 60 s after a refusal), so a wrapper that asks again every few seconds does not call Leasewise every time.

A message may keep at most **16** saved bodies. At that point `admit` holds it as `refused`, `reporter status` shows how
many messages are at the cap, and nothing is ever deleted.

### How to set it up

1. **Save the ids again.** `policy-ids` lists `gateways`; the reporter configuration names the gateway by alias.
2. **Add the `[admission]` block** to the reporter configuration, and the same store to `[gateway_receipts]`:

```toml
[admission]
store = "C:/path/to/admission.db"    # an absolute path; the reporter's receipt reader must name the SAME file
gateway = "g1"                       # a Leasewise gateway alias
gateway_agents = ["alpha"]           # the agents whose turns the gateway admits (each needs its own [agents.alpha])
fresh_overhead_bound_tokens = 4000   # the operator's bounds: what a fresh turn adds before the message
resume_history_bound_tokens = 60000  # what a resumed turn carries
growth_bound_tokens = 2000           # growth per call
context_ceiling_tokens = 180000      # optional: the model's context ceiling, to refuse a sum that cannot fit
```

   The seat, model, task class and call limits come from the agent's own `[agents.alpha]` table, as for the journal
   pipeline. The reporter **refuses to start** (a fixed sentence) if `[admission]` and `[gateway_receipts]` name different
   store files, if a `gateway_agents` entry has no agent table, or if a value is not valid.
3. **Point the wrapper's hook at the program:** `python -m quota_balancer reporter admit --config <file>`. The
   configuration file is a secret (it holds the identity): keep it readable by its owner only.

Nothing is invented. A bound that is not written is simply missing, and a turn that needs it is `held` as
`context_unprovable`. An agent that is not in `gateway_agents` is `held` as `not_configured`. A request that was already
saved is the exception in one direction only: if the operator removes the agent while its request is saved, the next
`admit` still recovers the lease (it resends the same stored bytes), saves the binding so that nothing is lost, and then
answers `held` as `not_configured`, because today's settings no longer allow that agent's turn. Only a rebuild of a stale
estimate needs the agent's settings: in that case the answer is `held` as `not_configured` and the row stays
`requested`, with nothing deleted. The `profile` field of a
request is checked against its bound and not otherwise used.

### One authority per paid message

For a message bound to a paid lease, the **gateway's final receipt is the only thing that settles it.** The journal
pipeline (card 7a) never acquires a lease for it, never sends a non-final report and never sends a final. Its
observations are still read and kept, for diagnosis.

- An agent in `gateway_agents` is never acquired or reported for by the journal pipeline at all.
- A message that has a binding in the admission store creates no journal acquisition, report or final. `status` counts
  the events skipped (`suppressed`).
- Journal rows queued **before** the binding appeared are set aside with a `superseded` record: they are never sent and
  never deleted. If the journal acquisition had already been sent, it is counted as `superseded_after_send`; the
  observe-only lease it made holds nothing.
- A missing or busy admission store is never taken to mean "this message is unbound".

**Transition limit.** An agent that is not yet in `gateway_agents` can still be journalled before a binding is seen. List
an agent in `gateway_agents` **before** its first gateway admission to close that route. The observe-only acquisition that
may exist before then is not a second authority: it holds nothing, and the bound lease is settled only by the receipt.

### The receipt reader reads only what is new

The admission store numbers every grant in the order it was saved (`grant_seq`). The reporter copies grants in batches of
500 after the last number it took, keeps that number in its state, and never reads old grants again, so there is **no
lifetime limit** (the 100,000-grant limit of card L6a-1 is gone). Each batch is checked: a row whose reference is not its
lease id, or whose hash is not the SHA-256 of it, refuses the whole batch.

### What `status` adds

Under `gateway_receipts`: `admission_store` (the current state, counts of past failures, and `at_body_cap`, the number of
messages held at the body cap) and `journal_suppression` (`gateway_agents`, `suppressed`, `superseded`,
`superseded_after_send`). Counts and closed words only.

### Limits

- **Replaying a stored admission is not a per-attempt authority check**, and leases stay **advisory**: the program grants
  `metered_advisory` leases with null caps (the gateway uses its own ledger ceiling). Enforcement comes later.
- **The lock bounds waiting; it does not promise fairness.** Under heavy contention a run may answer `store_busy`.
- **A missing receipt is never a guessed zero.** The lease keeps its liability and the wait stays visible in `status`
  (`awaiting_receipt`, then `overdue`).
- **After a restart with no evidence** of how a turn ended, the wrapper's close may say `cancelled` although the work
  finished. `closed` is only an acknowledgement: it never proves that a receipt exists or that its cost is zero.
- **No compaction.** Admissions, attempts and captures are never deleted. A replaced gateway ledger cannot rebuild old
  missing receipts from a binding alone.
- **Fixture-only.** No live gateway is touched; a live one needs the gateway-side upgrade first.
- **A message at the body cap stays held** until an operator looks at why its grant never arrived.

### Technical detail

The store layout is one module, `quota_balancer/reporter/admission_store.py` (version 1): `admission` (one row per
message; the key, the request, the saved bodies' count, the grant written once, `grant_seq`, `return_seq`,
`replay_tried`, `closed_ms`), `admission_attempt` (every body, saved before it is sent, never changed), and
`admission_meta` (two counters that only grow: `grant_seq` and `return_seq`). Triggers keep the key, the grant and the
close acknowledgement fixed and forbid deletes. `pending` orders by never-returned first, then `return_seq`, then row,
and gives every returned entry the next `return_seq` in the same transaction, so a standing or backward clock changes
nothing. The wire format is `reporter/admit_wire.py`, the flow `reporter/admit.py`, the lock
`reporter/admission_lock.py` (`fcntl.flock` on POSIX; `msvcrt.locking` on one byte at offset 0 on Windows). The
journal side is `rounds.py` (`suppressed`) and `state.py` (`superseded`, `message_is_bound`, `supersede_bound`).
