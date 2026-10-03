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
numbers; a `null` there is `INVALID_REQUEST`. Shadow reports are observe-only:
whether known or unknown, they never count toward any token or money limit. This
needs schema version 9 (`init` upgrades a version-8 store in one transaction,
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
put it in an agenttalk bus root. It is TOML, with one table per agent:

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
