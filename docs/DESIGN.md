# Quota balancer v3: an implementable service and gated integrations

Status: v3 - accepted for build. Lead decisions recorded 2026-09-24.
Audience: contributors building the separate `agenttalk-quota` repository, Python 3.11+
package `quota_balancer`, consumed by agenttalk. This is a design explanation/contract.
Increment 1 is independently buildable. The four lead choices are decided; implementation,
migration and acceptance-test gates still apply before enforcement is enabled.

## 1. Accepted measurements and scope

- Two fleets on one Codex account consumed three weekly allowances during 2026-09-18..21.
  Weeks-old threads held about 250K context; repeated tool calls averaged about 1.6M input
  tokens per dispatch. Limit errors retried about twenty times; guardian review added 1–33%.
- The 2026-09-22 OVH Qwen bill: 19.9M input, 0.3M output tokens; input approximately 90% of
  cost, at EUR 0.40/M input and 2.70/M output. These are supplied measurements, not retallied.
- Operator tiers: Sonnet for most Claude work, Opus for large tasks; Sol for most Codex
  work, Astra for the most important; Qwen for bounded work. Input matters at every tier.
- A Windows desktop and Ubuntu VM use gateways with fixed EUR 60/EUR 40 shares of one
  EUR 100 account budget. Keep these local ledgers authoritative; defer share transfers.

One coordinator serves a private tailnet; reporters and gateways remain local. No shared
network SQLite, automatic failover, dashboard scraping, web configuration or HTML in this
build plan. Status is JSON. Model/effort aliases are operator-configured, not a vendor catalog.
Money, input tokens and subscription utilization are distinct quantities; never convert
tokens into subscription entitlement or sum percentages from seats on the same account.

## 2. Existing code, rechecked at d24e72f

These are implementation facts, not capabilities attributed to the proposed service:

| Source, repository-relative line | Verified behavior and consequence |
|---|---|
| `src/agenttalk/capacity.py:9`, `:48`, `:98`, `:559` | Advisory, approximately one-turn lag; 600-second default age threshold; percentages/resets/context fields; observed/stale/unknown confidence. It never gates bus progress. |
| `src/agenttalk/capacity.py:143` | Claude statusline source defaults to one OS-user dump; observed_at is file mtime; no account identity. Cannot safely attribute that shared dump to two accounts. |
| `src/agenttalk/capacity.py:373`, `:518` | Codex rollout normalizer reads percentages, window lengths, resets and reached type, with source timestamp; read_local accepts a sessions directory/thread selector. |
| `src/agenttalk/wrapper/claude_adapter.py:65` | Allowed rate_limit_event is dropped; other statuses become retryable adapter errors. Fresh headless window ingestion needs a new parser path. |
| `src/agenttalk/wrapper/run.py:93`, `:124`, `:2393`; `src/agenttalk/wrapper/loop.py:546` | Usage/rate limits and overload share infrastructure classification; special pre-JSON text markers also exist. Infrastructure retry exhaustion is time/attempt based. Split structured account-limit evidence from overload. |
| `src/agenttalk/wrapper/loop.py:693` | Capacity refresh occurs on idle/successful turn boundaries, not as a mid-turn budget observer. |
| `src/agenttalk/wrapper/session.py:68`, `:119` | Fresh/resume spawning and failed-resume reset primitives already exist. A full per-dispatch context-mode policy, SUMMARY handoff and knowledge loader are separate prerequisites. |
| `src/agenttalk/wrapper/work_heartbeat.py:77`, `:293` | Default 900-second cap stops heartbeat stamping, not the child. It is not a turn checkpoint or lease timer. |
| `src/agenttalk/ovh_gateway.py:33`, `:119`, `:127` | Tariff rates 400000/2700000, reserve rates 480000/3240000 micro-EUR/M; input/output rounded separately. Full 262144-input/32768-output reservation = 231999 micro-EUR per attempt. |
| `src/agenttalk/ovh_gateway.py:1327`, `:1667` | open_child_turn persists message-bound max_calls/money/expiry; reserve_for_child atomically consumes a slot/reserves exposure. Defaults: 100000 calls, installed money cap (module default EUR 95), 24 hours. Per-lease parameterization is new work. |
| `src/agenttalk/ovh_gateway.py:1460`, `:1782`, `:1816`, `:1898` | close_child_turn is terminal; mark_uncertain, settle and reconcile preserve unresolved attempts. settle checks global token ceilings and actual-versus-reserved cost. |
| `src/agenttalk/ovh_gateway.py:2196`, `:2225` | status()["policy_hash"] and status()["child_cap_policy_hash"] directly map to price/child_cap report hashes; no new hash discovery API is needed. |
| `src/agenttalk/ovh_gateway_front.py:315` | Front validates max_tokens against the global ceiling, not a lease-specific output bound. |

The earlier context-policy draft is untracked, not a builder dependency. All rules needed
here are in section 3. Do not infer that its knowledge loader or SUMMARY workflow ships
today. The integration owner must demonstrate those prerequisites before enforcing them.

## 3. Context, estimates and honest input enforcement

Select context before admission; report numbers/provenance only, never prompt content.
FRESH is the default: a new CLI session with a self-contained context header in the dispatch
plus actual system/tool overhead. The header includes task, relevant state, constraints and
evidence pointers needed to start without thread history. Add curated independent
project knowledge only once its loader exists. SUMMARY means a new session with a bounded
handoff/context pack plus that same overhead; RESUME means the observed full previous
context plus dispatch/overhead. No unimplemented loader may contribute imaginary tokens.
Independent reviews exclude author handoff/private claims. Reviewer-owned memory is not
automatically independent: only curated material free of author claims may enter a cold read.
F20 selects this FRESH default. The service also accepts explicit SUMMARY/RESUME under
their prerequisites; author claims never enter cold reviews, including through the header.

Inline rotation contract: measured RESUME above the profile ceiling or five times dispatch
size is refused with CONTEXT_ROTATION_REQUIRED. Proposed ceilings: Codex 60000, Claude
200000 input tokens. Client builds SUMMARY, or FRESH if continuity is unnecessary; if the
required mode is unavailable, park with CONTEXT_UNAVAILABLE. Missing size => CONTEXT_UNKNOWN.
Context integration must prove deterministic mode selection, fresh session creation,
bounded handoff loading and preservation of unfinished-work pointers before enforcement.

For C initial input, N expected calls, G expected growth, M maximum calls, O total output:

```text
E = N*C + G*N*(N-1)/2
I = ceil(E*margin_bps/10000)                       # default margin_bps=12500
H_sized = ceil((I*reserve_input_rate + O*reserve_output_rate)/1000000) + 2*M - 1
H_fixed = M * 231999                             # legacy comparison, not a selected lease mode
```

Cached input is a subset of input, reserved at full rate. Money is integer micro-EUR;
the 2*M-1 allowance covers separate input/output ceilings on every attempt. With C=20000,
N=4, G=2000, M=5, O=4000: E=92000, I=115000, H_sized=68169 at reserve rates. At tariff
rates the analogous upper estimate is 56809, **not** the reservation. H_fixed=1159995.
Bind reserve rates/envelopes to the installed price hash; clients never choose their rates.

If C+G*(N-1) exceeds the model's input ceiling, deny CONTEXT_PROJECTION_EXCEEDED. Splitting
work or constructing SUMMARY/FRESH requires a new estimate; do not clip an oversized bound.
N/G come from versioned numeric-only calibration by task class/model/mode: calls per turn
p50/p90, first input, nonnegative per-call growth p50/p90, sample_count and source period.
Use p90 for admission; profile overrides record an owner/reason. No samples => conservative
operator caps, marked uncalibrated. Do not import raw fleet logs into the product database.
Calibration inputs, when separately supplied: Codex per-call last_token_usage.input_tokens
and Claude per-assistant-message usage from the measured fleet period, reduced locally to
numeric sequences. Aggregate totals (including the reported 890M input over nine days)
are cross-checks; they cannot uniquely determine N or G. No fleet-log access is assumed.

Fixture anchors: measured context=250000, mean dispatch input=1600000, retries≈20. Their
ratio 6.4 is inferred average calls, not a measured per-turn trace. Synthetic seven-call
RESUME at C=250000,G=0 => 1750000 input; rotated C=25000,G=3000 => 238000. Synthetic
C=30000,N=40,G=3000 => 3540000: FRESH alone cannot bound a long tool loop. Model these
separately from the measured 19900000/300000 metered-day totals and 1–33% guardian overhead.
Every instrumented billable child receives a separately held lease; parent estimates exclude
its separately counted calls. Uninstrumented automatic children remain an attribution gap.

Subscription I/M are admission plans and post-turn attribution limits in this scope.
There is **no mid-turn input kill guarantee**: an observer of Claude per-message usage and
Codex token_count would be a later increment with in-flight overshoot tests. Window admission,
context rotation and zero limit retries help prevent the measured pattern only after their
integrations ship; lease arithmetic alone would not have prevented the subscription burn.
Metered attempt enforcement requires section 4's request-sized gateway increment 6b.

## 4. Money authority, selected gateway migration and lifecycle

Keep gateway ledgers and fixed shares; absorption would migrate live reservation state
without demonstrated need. Coordinator holds enforce platform/project/team/seat budgets
in addition to the canonical account budget, but never replace the gateway's spend checks.
Account budget has one home: accounts.budget_micro_eur. There is no account/month money
limits row. Other limits compose by their tightest ceiling, never by adding duplicate caps.
Admission transaction: settled charges + unresolved holds + new hold <= each applicable cap.

F05 selects option B: request-sized reservation in gated increment 6b. Small holds cannot
use the existing fixed 231999-per-attempt path. Until 6b ships, metered quota leases are
advisory only: no enforceable token or service money hold, and no claim of lease protection.
Existing gateway spend checks remain authoritative; advisory never permits bypassing them.
The fixed-envelope lease alternative is rejected because it makes small leases impractical.

- **Required sized mode:** add persisted per-attempt input/output
  bounds, per-lease input/output counters and lease-selected caps on existing child rows;
  extend reserve_for_child, not a separate reservation authority. Compute H_sized with
  reserve rates. Normalize forwarded max_tokens to min(requested, lease per-call ceiling,
  remaining output); missing/invalid/zero values deny. Freeze/count the exact forwarded body.
  Use a model/template-specific validated complete token upper bound, including tools,
  wrappers and supported modalities; unavailable proof => INPUT_BOUND_UNAVAILABLE. No generic
  UTF-8 byte-length or min(bound,context_ceiling) shortcut is certified by this design.
  Update reservation logic, price/child hashes, schema/install verification, canary policy
  binding and settlement checks against stored bounds. Drain/reconcile, then re-initialize
  each gateway under a new price-policy hash and run its canary before enabling it. Preserve
  historical attempts/hashes during re-init; never reset liabilities. No metered lease
  enforcement before 6b ships and that gateway's re-init/canary succeeds.

Before transport, atomically reserve tokens/money/call under the child and local account
share. Gateway may deny a service grant for lower local headroom. A definitely untransmitted
oversized request is rejected without new spend; a reserved or possibly transmitted attempt
retains conservative liability and goes uncertain when usage cannot be proved. Observed
overrun records actual liability even above a hold, blocks further work and needs reconciliation.

active -> settled only on gateway final receipt after terminal child close and resolution
of all attempts. For zero spend, close/fence and verify no reserved/uncertain attempts in
one transaction, including a tombstone for a never-opened binding. Expiry -> uncertain,
never a refund. All retries reuse the durable child/message binding; terminal rows cannot
be reopened. mark_uncertain/reconcile remain authoritative for provider uncertainty.
Gateway snapshots reconcile, not add to per-lease charges. Import only increases in
unattributed settled totals once per gateway/period; block on unexplained totals or unbound
reservations. Never silently release holds after lost responses, restarts or mismatched hashes.
UTC monthly gateway periods/shares must match; shares sum <= account budget. Late receipts
retain their original period; scope accounting uses grant-day/month. Clamp expiry at period
boundaries; reconcile rollover mismatches before new metered admission. No online share transfer.

## 5. Subscription readings, freshness, retries and topology

Account identity comes from an operator-maintained reporter-local map of CLI configuration
home/session source to an installation-local account alias. Do not upload configuration
paths or provider IDs. Reject ambiguous shared statusline attribution; no guessing by OS user.
Add a headless Claude rate_limit_event parser, including allowed status; normalize utilization,
rateLimitType, reset and source timestamp from validated fixtures. Missing fields stay unknown.
Codex uses isolated per-seat rollout sources. Both sources bind seat/machine/account explicitly.
Map legacy stale => observed value with source_stale=true and its original timestamp;
unknown stays unknown, observed stays observed. Ingest time never refreshes observation time.

Check every configured window; retain the high-water utilization for its reset epoch. A
new epoch needs a new source observation after reset; time passage alone proves no recovery.
Known utilization >= the ceiling remains blocking even when stale. Default candidate policies:
one account-wide slot while coordinated, 90% ceiling, no cold_sweep if any selected window >80%.
F07 selects activity-based freshness with explicit accounts.coverage_complete, set only by
the operator, default false and never inferred from traffic. Complete means every machine
and every team using the account reports to this coordinator; any unmanaged consumer makes
coverage incomplete. With coverage incomplete, the 600-second wall cap always applies.
An older idle reading remains current only when coverage_complete=true and the coordinator
can prove no turn ran since the sample: no subsequent usage/admission, no active/uncertain
turn and uninterrupted coverage of every registered machine/team. A grant increments account
activity_seq; a post-turn reading certifies its
covered_seq only after all usage for that turn is observed. Missing proof uses the wall cap.
Unmanaged use, a missed required reporter heartbeat (>120 seconds) or reset invalidates
idle-current proof without silently changing the operator's flag; use the wall cap instead.
Report coverage_complete, observed coverage gaps, age and freshness_reason separately.

Structured account-limit evidence only: Codex reached_type/100% configured window; Claude
validated exhausted status plus rateLimitType; provider 429 with structured usage-limit body.
Unknown Claude statuses, generic 429, 529/overload and unstructured prose do not park peers.
An ambiguous limit-looking error stops local immediate retry pending a new signal/operator;
overload permits at most two backoff retries within the same lease. Persist classifier evidence
as enums/numbers, not raw bodies. Account limits park locally immediately and service-wide
on the reporter event, even if the event must queue. Never resume-loop a usage-limit error.
retry_at is latest exhausted-window reset +30 seconds; unknown reset means null, no timed
retry. After a known reset, allow one coordinator-authorized FRESH recovery probe while
connected. During an outage, the local guard permits one probe per machine/account after
the same reset margin, counted against local slots and marked uncoordinated. Persist probe
ownership to prevent parallel retry loops; absent fresh recovery evidence re-park. Unknown
reset remains parked. Local operator clear is audited, never inferred from timeout.

Report on events and every 30 seconds through a durable outbox. One coordinator prevents
cross-machine races; per-machine services would fragment shared windows. Metered service
unreachable => no new provider attempt, even with an earlier grant; in-flight attempts finish
locally and queue receipts. F19 selects warn-only during coordinator outages for subscription
accounts, including shared accounts, to preserve availability. Local guards require persisted
park-until-reset on provider-limit errors, zero limit retries and a per-machine concurrency
cap per account, configurable and default 2. A machine-local atomic slot store shared by all
wrappers/teams enforces the cap; retained live/uncertain children count across restart.
Cache the operator's per-account cap locally; without an override the local default is 2.
Use bounded FRESH profiles, preserve known blocks and forbid cold sweeps. Every degraded turn
and local status records uncoordinated=true; queue that marker for central status on recovery.
This cannot guarantee cross-machine concurrency. Metered accounts remain strictly fail-closed;
subscription availability is not permission to spend metered money offline. Reconnect uploads
blocks/usage before new leases. Revisit this default when the coordinator has a standby.
Bus delivery, replies, escalation and settlement never depend on lease admission.

## 6. Profiles, lifetime and trust

| Task | Preferred model | Effort alias | Context | Fallback |
|---|---|---|---|---|
| bounded_read | Qwen | bounded | FRESH | Sonnet, then park; quota-blocked Codex reads may route here |
| implement | Sonnet or configured Sol worker | standard | FRESH with self-contained header; SUMMARY for explicit continuity | Other worker family; Qwen only after bounded re-scope |
| large_task | Opus | deep | FRESH or SUMMARY | Split into worker tasks or park |
| critical_review | Astra | deep | FRESH without author memory | Opus independent reviewer or park |
| cold_sweep | Astra | deep | FRESH | Defer above 80%; no automatic premium bypass |
| own_thread_reply | Existing worker | standard | RESUME under rotation limits | SUMMARY or park |

Profiles are stable role choices; fallback starts a new estimate/request/session, not per-tool
model hopping. Validate effort support, never silently discard it. Selected model/effective
effort changes reset the session; fallback must preserve task independence/acceptance criteria.
TTL is per profile: bounded 30 minutes, standard 2 hours, deep 6 hours; absolute maximum
24 hours and installed gateway expiry. ttl_ms requests can shorten but not exceed the profile.
No renewal in this scope. At actual expiry, meter refuses new calls; wrapper requests child
termination and retains its slot until death is confirmed. Long processes need an adequate
profile at admission; no claim that heartbeat expiry/checkpoints magically stop or split work.

Old wrappers keep working in observe-only deployments and appear unmanaged/unmetered.
Enabling a strict gateway explicitly rejects calls without the new lease binding. A seat
skipping leases can use subscriptions; direct provider credentials bypass gateway/service.
Normal agent shells often share the wrapper/reporter OS owner: they can read credentials,
forge readings/receipts or invoke the admin CLI. V3 is cooperative against those agents,
not an adversarial budget sandbox. Separate OS/service identities would be another product scope.
Unreported usage can be invisible; show coverage, discrepancies and unknown attribution.

Default loopback; optional private authenticated TLS tailnet ingress with machine allowlist.
No public/wildcard listener, remote admin API or arbitrary CORS. Ingress replaces identity
headers; loopback writes also require client identity. Provider secrets stay outside this
product. Host credential facilities hold machine auth; service stores identity references.
Opaque random 256-bit lease tokens are hashed, scoped to machine/seat/account/turn, redacted
and revocable. Reporter identity, not a wrapper token alone, authorizes money settlement.
No prompts, local paths, external account IDs or secret values in stored/output metadata.

## 7. Data and HTTP contract

SQLite foreign keys/WAL, integer micro-EUR/tokens/basis points (10000=100%), UTC epoch
milliseconds. Checked signed-64-bit arithmetic; reject booleans-as-integers, nonfinite/negative
values, unknown fields/enums. IDs are local UUIDs except operator aliases; alias regex is
`[a-z][a-z0-9_-]{0,63}`. HTTP wire api_version=1 is independent of numbered database migrations.
`PRAGMA user_version` starts at 1; ordered transactional migrations increment it; reject a
newer unknown version. All migrations create only the tables required by their card.

| Table (PK=id unless stated) | Required fields / constraints beyond PK |
|---|---|
| accounts, machines, gateways | Exact increment-1 DDL below; later gateways add ledger_epoch,last_seq,period_start/end,policy_hashes,last_report_at,state,sized_ready; accounts add window_keys,activity_seq,coverage_complete(operator-only boolean,default false),required_reporters(machine/team pairs),outage_local_concurrency_cap(positive integer,default 2); machines add identity_ref UNIQUE,enabled,last_seen_at |
| models | account_id FK,alias,context_ceiling,output_ceiling,tariff_input/output_rate,reserve_input/output_rate,reservation_mode,policy_hash,effort_aliases; UNIQUE(account_id,alias) |
| projects; teams; project_accounts | projects(alias UNIQUE); teams(project_id FK,alias, UNIQUE(project_id,alias)); project_accounts PK(project_id,account_id), both FK |
| seats | team_id FK,machine_id FK,alias,role,enforcement,last_seen_at; UNIQUE(machine_id,alias) |
| limits | scope(platform/project/team/seat),scope_id,metric(input_tokens/money_micro_eur/window_bps/concurrency),period(turn/day/month/window),window_key nullable,soft nullable,hard |
| profiles | task_class UNIQUE,candidates JSON,context_rules JSON,calibration JSON,expected_calls,growth_tokens,margin_bps,max_calls,output_cap,per_call_output_cap,ttl_ms,policy_revision |
| readings | event_id PK,machine_id FK,account_id FK,seat_id nullable FK,kind,observed_at,received_at,payload JSON |
| windows | PK(account_id,window_key),used_bps,resets_at,observed_at,confidence(observed/unknown),source_stale,covered_seq,source_event_id |
| blocks | account_id FK,model_id nullable FK,reason,retry_at nullable,state(parked/probing/cleared),source_event_id,updated_at |
| leases | machine_id/seat_id/account_id/model_id FK,gateway_id/parent_lease_id nullable FK,turn_id,request_id,request_hash,token_hash nullable,task_class,context_mode,enforcement,estimate JSON,policy_snapshot JSON,input_cap,output_cap,max_calls,per_call_output_cap,money_hold nullable,granted_at,expires_at,activity_seq,period_start,state(active/uncertain/settled),outcome; UNIQUE(machine_id,request_id) |
| holds | PK(lease_id,limit_id,period_start),reserved,settled_actual nullable; account holds use a reserved synthetic account-limit identifier, not a duplicate limits row |
| receipts | event_id PK,lease_id FK,gateway_id nullable FK,seq nullable,calls,input_tokens,cached_input_tokens,output_tokens,actual_micro_eur nullable,outcome,final,observed_at,received_at |
| adjustments | account_id FK,gateway_id FK,source_event_id UNIQUE,period_start,unattributed_micro_eur,reason,resolved_at nullable |
| audit; daily_totals | audit(actor_alias,action,object_id,at,redacted_details); daily_totals PK(scope,scope_id,utc_day),tokens/calls/money/unmanaged_turns, derived once from final records |

Exact uniqueness indexes for later migrations (empty string is not a valid UUID/window key):

```sql
CREATE UNIQUE INDEX limits_scope ON limits(scope,scope_id,metric,period,COALESCE(window_key,''));
CREATE UNIQUE INDEX blocks_live ON blocks(account_id,COALESCE(model_id,'')) WHERE state IN ('parked','probing');
CREATE UNIQUE INDEX receipt_final ON receipts(lease_id) WHERE final=1;
CREATE UNIQUE INDEX lease_live ON leases(seat_id,turn_id) WHERE state IN ('active','uncertain');
```

Transactions serialize grants, receipts and events. policy_snapshot freezes rates/hashes,
mode, scope memberships and caps; reductions block new work, never release existing holds.
Limits: input/money use turn/day/month, window_bps uses window, concurrency counts live and
uncertain leases. soft<=hard. Retain unresolved rows indefinitely, financial rows 13 months,
normalized readings 30 days then numeric daily totals. Children share project authorization,
not necessarily model/account; parent tokens cannot mint child leases.

All `/v1` routes require authenticated machine/observer scope; responses contain server_at,
policy_revision. Mutations use Idempotency-Key=request_id/event_id; same ID/content replays,
different content =>409. IDs below are placeholders, not runnable commands.

```json
{"request_id":"<uuid>","turn_id":"<uuid>","seat_id":"<uuid>","task_class":"bounded_read",
 "model_id":"<uuid>","effort":"bounded","gateway_id":"<uuid>","parent_lease_id":null,
 "context":{"mode":"FRESH","initial_tokens":20000,"dispatch_tokens":1000,"source":"tokenizer",
 "observed_at":1790208000000,"growth_tokens_per_call":2000},"expected_calls":4,"max_calls":5,
 "output_cap":4000,"per_call_output_cap":1000,"ttl_ms":1800000}
```

POST /leases accepts that body; gateway is null only for subscriptions. Server derives
membership/account/rates, validates candidate/context/profile and computes caps. N<=M<=100,
margin_bps=10000..20000; estimates must be <=120 seconds old. source enum is
tokenizer/cli_observed/conservative_bound. Output cap is total, per-call cap <= model ceiling.
201: {lease_id,lease_token,granted_at,expires_at,state,enforcement,caps,money_hold,windows,warnings};
caps={expected_input,input_cap,output_cap,max_calls,per_call_output_cap}, windows are section-7
window records without source identity. enforcement=metered/subscription_admission/
subscription_warn/shadow/metered_advisory, derived from seat policy and gateway readiness.
Before 6b/per-gateway re-init/canary, only metered_advisory is permitted on a metered account;
it has null token/money_hold and cannot pass /check. Subscription money_hold=null. Shadow
records have null token/money_hold, consume no slot and cannot pass /check. Replay returns
200 and rotates a non-null token hash, never adds a hold;
finish acquisition before dispatch. Terminal replay returns terminal state without a token.

POST /leases/{id}/check {request_id}: bound token/machine; 200 {state,expires_at,caps} only if
active/unblocked; re-evaluate even on retry, do not cache permission or extend TTL. Gateway
checks before each attempt, then atomically enforces its local remaining envelope.
POST /readings {event_id,account_id,seat_id?,observed_at,kind,payload}: owner reporter;
202 {event_id,accepted}. Closed payload schemas:
- cli_window: {windows:[{key,used_bps,resets_at}],confidence,source_stale,covered_seq};
  null percentage/reset only when unknown; reject future observations beyond 30 seconds.
- gateway_snapshot: {gateway_id,ledger_epoch,seq,period_start,period_end,settled_micro_eur,
  reserved_micro_eur,uncertain_micro_eur,unattributed_micro_eur,reconciliation_required,
  sized_ready,policy_hashes:{price,child_cap}}; first three amounts disjoint, unattributed subset of
  settled. Sequence increases; duplicate events replay; epoch/hash change needs reconciliation.
  sized_ready requires deployed 6b plus re-init/canary evidence for the approved new hash;
  a lease request cannot set readiness or the operator-owned coverage flag.
- usage: {turn_id,lease_id?,input_tokens,cached_input_tokens,output_tokens,calls,final,uncoordinated};
  cumulative, use maxima rather than adding samples; never settles money.
POST /limit-events {event_id,account_id,model_id?,turn_id,observed_at,evidence_kind,
resets_at,exhausted_windows}: reporter authorized for account; evidence_kind is one of
codex_exhausted/claude_exhausted/provider_usage_429. 202 {block_id,retry_at,state}.
POST /leases/{id}/settle {event_id,outcome,calls,input_tokens,cached_input_tokens,output_tokens,
actual_micro_eur,gateway_id?,seq?,final:true}: bound gateway's closed-child receipt for money;
bound wrapper's termination confirmation for subscriptions, money=null. Outcomes completed/
cancelled/failed/provider_limit. Accept late receipts; identical duplicates no-op, conflicting
receipts block/reconcile and preserve conservative liability. Unknown spend is not a zero
final receipt. 200 {state,released_micro_eur,retained_micro_eur}, money fields null for subscription.
GET /plan?project_id=... returns ordered candidates/reasons/context/caps/retry_at, no hold.
GET /status returns account windows/age/freshness_reason/coverage_complete and coverage gaps,
gateway shares/balances/readiness, service holds, spend, discrepancies, enforcement coverage,
uncoordinated turns/machines and recommendations; local status remains available offline.
The status presentation labels degraded work "uncoordinated"; never conflate balances.

Errors: {error:{code,message,retry_at:null|timestamp,details:{...}}}. 400 INVALID_REQUEST,
CONTEXT_UNKNOWN,CONTEXT_ROTATION_REQUIRED,CONTEXT_PROJECTION_EXCEEDED,UNSUPPORTED_EFFORT;
401 UNAUTHENTICATED; 403 FORBIDDEN,POLICY_DENIED,CONTEXT_UNAVAILABLE,ENVELOPE_INCOMPATIBLE,
INPUT_BOUND_UNAVAILABLE; 404 NOT_FOUND; 409 IDEMPOTENCY_CONFLICT,RECEIPT_CONFLICT,POLICY_MISMATCH;
410 LEASE_EXPIRED,LEASE_CLOSED; 429 INPUT_LIMIT,MONEY_LIMIT,WINDOW_LIMIT,ACCOUNT_PARKED,
CONCURRENCY_LIMIT; 503 METER_STALE,RECONCILIATION_REQUIRED,SERVICE_UNAVAILABLE.
No automatic retries for policy/schema errors. 429 parks until retry_at or explicit recovery;
null means no timed retry. Gateway reports older than 120 seconds fail closed. Service
outage policy cannot override a known block before its reset; offline recovery uses the
persisted local probe guard defined in section 5.

## 8. Build cards and prerequisites

Seven ordered groups, explicitly split into small cards where necessary. Each **card** is
one developer seat for one or two bounded turns; groups 6/7 are not single-seat turns.
No live provider calls. Every card has `privacy_<card>`: Given injected secret/path/provider-ID
sentinels, When status/error output is rendered, Then none appear. Only local aliases survive.
Future acceptance tests below are requirements, not a claim that the product has passed them.

### Card 1: standalone inventory, exactly three tables

New repo `agenttalk-quota`; stdlib sqlite3/argparse/json only; package `quota_balancer`.
Proposed CLI `python -m quota_balancer` (installed alias `quota`), explicit --db argument;
verbs init, config-import --file, status --json. No agenttalk/models/scopes/leases/HTTP.
init is idempotent; migration version 1 executes this DDL with foreign_keys=ON and WAL:

```sql
CREATE TABLE accounts(id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE, vendor TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('metered','subscription')), budget_micro_eur INTEGER,
 CHECK((kind='metered' AND typeof(budget_micro_eur)='integer' AND budget_micro_eur>0)
 OR (kind='subscription' AND budget_micro_eur IS NULL))) STRICT;
CREATE TABLE machines(id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE) STRICT;
CREATE TABLE gateways(id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE,
 account_id TEXT NOT NULL REFERENCES accounts(id), machine_id TEXT NOT NULL REFERENCES machines(id),
 share_micro_eur INTEGER NOT NULL CHECK(share_micro_eur>0), period TEXT NOT NULL CHECK(period='utc_month')) STRICT;
PRAGMA user_version=1;
```

Exact import version-1 shape/example, all keys required, no additional keys at any level:

```json
{"version":1,"accounts":[{"alias":"meter","vendor":"ovh","kind":"metered","budget_micro_eur":100000000}],
 "machines":[{"alias":"desktop"},{"alias":"worker"}],"gateways":[
 {"alias":"g1","account":"meter","machine":"desktop","share_micro_eur":60000000,"period":"utc_month"},
 {"alias":"g2","account":"meter","machine":"worker","share_micro_eur":40000000,"period":"utc_month"}]}
```

Invalid fixture: same document with g2.share_micro_eur=40000001. vendor enum=ovh/openai/anthropic.
Import validates alias
regex, duplicate aliases, references, exact integer types/range and metered-only shares;
then replaces inventory atomically, retaining UUIDs for retained aliases. Share SUM per
account <= its budget. Reject unknown/newer schema versions; future active leases prohibit
replacement. Fixed UTC-month shares only; no network/credential fields. Account budget is
canonical, no limits table in this card. Status projects aliases only, sorted by alias:
`{"schema_version":1,"accounts":[{"alias":"meter","kind":"metered","budget_micro_eur":100000000,"allocated_micro_eur":100000000}],"machines":["desktop","worker"],"gateways":[{"alias":"g1","account":"meter","machine":"desktop","share_micro_eur":60000000},{"alias":"g2","account":"meter","machine":"worker","share_micro_eur":40000000}]}`.
Exit 0 success; 2 invalid config/schema; 3 store IO. Errors {error:{code,message}} with codes
INVALID_CONFIG,SCHEMA_UNSUPPORTED,STORE_ERROR; no raw input/OS error text. Demo: import,
restart, identical status showing budget and allocated amount of 100000000 micro-EUR.
- `init_is_idempotent`: Given new DB, When init twice/reopen, Then version=1, exactly three tables.
- `shares_cannot_exceed_account`: Given valid import, When invalid fixture imported, Then exit 2, prior data intact.
- `invalid_config_rolls_back`: Given inventory, When bad alias/reference/unknown key/period is imported, Then unchanged.
- `money_is_integer`: Given float/bool/negative/overflow money, When imported, Then exit 2; integer fixture succeeds.
- `privacy_1`: Given private sentinels, When status/error, Then only approved aliases/static error text appear.

### Remaining cards (each work order includes its row and section 7's shared contract)

| Card | Tables/module, rules and errors | Demo and Given/When/Then tests |
|---|---|---|
| 2: reading reducer | readings/windows + gateway report/coverage columns; fixture CLI only. IDs deduplicate, source time preserved; ambiguous source UNKNOWN; confidence mapping explicit. Operator sets coverage_complete; every machine/team must report for idle proof. Incomplete coverage uses 600s wall cap. Same epoch never lowers high-water; new epoch needs post-reset observation; old gateway report METER_STALE. | Demo reordered/idle samples. `reading_replay`: Given same ID, When replay/collision, Then no-op/409. `stale_mapping`: Given stale, Then value/time/flag retained. `coverage_fallback`: Given incomplete coverage or missing reporter, When sample age>600s, Then stale; telemetry cannot set coverage_complete. `account_isolation`: Given ambiguous source, Then neither account updated. privacy_2. |
| 3: policy arithmetic | models/profiles/projects/teams/seats/project_accounts/limits; pure fixtures, no wrapper. Calculate E/I/request-sized holds (fixed reserve only a comparison), composed limits, model ceiling, 80% sweep and fallback; INVALID_REQUEST/CONTEXT_PROJECTION_EXCEEDED/POLICY_DENIED. | Demo synthetic 1750000 vs 238000 input. `round_components`: Given five calls, When reserve formula, Then 68169. `projection_denied`: Given over-context projection, Then deny. `fallback_tier`: Given blocked Codex read/critical review, Then Qwen read/independent premium or park. privacy_3. |
| 4: lease store | leases/holds/receipts/blocks/adjustments/audit/daily_totals; exact partial indexes above. Transactional grant/check/settle, one slot, uncertain expiry retains liability, receipt conflict blocks, same ID replays. Fixture gateway only; 409/410/429 contracts above. | Demo restart with unresolved hold. `null_uniqueness`: Given null-window/model duplicates, When insert, Then reject; final-receipt/live-lease duplicates also reject. `grant_race`: Given one budget, When concurrent grants, Then no overspend. `late_receipt`: Given expiry, When final receipt, Then reconcile original period once. privacy_4. |
| 5: private HTTP | Thin /v1 handlers over tested store/policy, identity allowlist and host ingress adapter; no custom TLS stack/admin API. Loopback default; 401/403 cross-machine denial; 503 outage; status JSON only. | Demo two authenticated fixture machines share one block. `token_scope`: Given foreign identity, When check/report, Then deny. `replay_grant`: Given lost response, When retry, Then one hold/rotated token. `public_bind`: Given public binding, When start, Then refuse. privacy_5. |
| 6a: gateway child binding | Existing child_turns/attempts authority; add lease-selected immutable caps/hash reference and terminal receipt journal. Atomic cap assignment at open; retries reuse; no parallel cap ledger. Fixture-only until 6b; metered leases stay advisory. | Demo fake child restart. `child_reuse`: Given retry, When open, Then same caps/cumulative spend. `zero_fence`: Given no attempts, When terminal fence/receipt, Then no later reserve. `unresolved_retained`: Given uncertain attempt, When close, Then no refund. privacy_6a. |
| 6b: required sized-reservation gate | New per-attempt bounds/child token totals; new price-policy hash, reservation/schema/settle migration, per-gateway re-init plus canary. Complete bound/output clamp/uncertainty rules; INPUT_BOUND_UNAVAILABLE/ENVELOPE_INCOMPATIBLE. No wrapper changes. | Demo fixture request below old envelope. `output_clamped`: Given high max_tokens, Then lease bound. `activation_gate`: Given missing 6b/re-init/canary, When lease requested, Then advisory only/no enforcing token. `hash_migration`: Given old/unresolved ledger, Then refuse activation until reconciled. `overrun_hold`: Given transmitted overrun, Then actual liability/blocked. `pretransport_deny`: Given untransmitted oversize, Then no attempt. privacy_6b. |
| 7a: shadow client/outbox | One wrapper spawn/end seam, durable local outbox; request/report only, no claimed protection or override of existing spend guards. Existing raw context mode labeled unknown until integration. No parser/context policy/UI. | Demo lost response/restart. `outbox_replay`: Given durable event, When replay, Then one report. `shadow_no_spend_authority`: Given advisory result, Then no enforcing token or bypass of a gateway denial. privacy_7a. |
| 7b: source parser | Reporter-local source/account map; new Claude allowed/blocked rate-event normalization, Codex numeric mapping; no retry changes. Unsupported/missing fields unknown. Raw provider bodies remain local. | Demo two-account headless fixtures. `claude_allowed_reading`: Given valid allowed event, When parse, Then window emitted. `two_accounts`: Given distinct sources, When normalize, Then no cross-attribution. `missing_reset`: Given absent reset, Then unknown. privacy_7b. |
| 7c: retry split | Wrapper classifier/local persisted block; structured exhausted evidence => zero retries and queued account event, overload max two, text-only local caution not peer block. One coordinated reset probe, or one local machine/account probe during outage after reset; local slots still apply. No context loader or input kill. | Demo limit/529/text trio. `limit_zero_retry`: Given exhaustion, When handled/restarted, Then parked. `overload_bounded`: Given 529, Then <=2 retries. `text_not_fleet_block`: Given prose alone, Then no peer park. `single_probe`: Given known reset/outage/restart, Then one local probe; unknown reset stays parked. privacy_7c. |
| 7d: context/enforcement switch | Prove FRESH/reset/header handling and 7b/7c first; SUMMARY/knowledge need their own proven loaders only when used. Metered enforcement requires 6b/per-gateway activation. Choose/count actual context before lease; post-turn input attribution only. Persist local outage slots, cap default 2/account/machine across all teams; warn-only subscriptions, fail-closed money; every degraded turn uncoordinated. | Demo header-only FRESH, long turn and outage. `context_before_lease`: Given oversized resume, Then rotate/re-estimate or park. `long_turn`: Given deep profile/hour, Then valid. `outage_slots`: Given two local active turns/default cap, Then third denied across teams/restart. `outage_records`: Given unreachable coordinator, Then subscription record uncoordinated=true and money denied. `bus_ungated`: Given denied spawn, Then bus works. `expiry_slot`: Given live expired child, Then slot retained. privacy_7d. |

## 9. Lead decisions 2026-09-24

- **F05 — B:** request-sized reservation is mandatory gated card 6b: new price-policy hash,
  re-init and canary per gateway. Until it ships, metered quota leases are advisory only.
- **F07 — B, complete coverage:** idle-current proof requires every machine and team on
  the account to report. Operator-owned coverage_complete is explicit, never inferred;
  any unmanaged consumer means incomplete coverage and the 600-second wall cap applies.
- **F19 — B with guards, for availability:** subscription outages use warn-only with local
  park-until-reset, zero limit retries and a configurable machine/account cap (default 2).
  Status and every degraded turn say "uncoordinated"; money stays fail-closed. This reverses
  the author's strict-shared-account recommendation after the overnight fleet outage caused
  by a Windows update on 2026-09-23. Revisit when the coordinator has a standby.
- **F20 — A:** FRESH by default with a self-contained dispatch context header; independent
  curated knowledge only after loader proof, and no author claims in cold reviews.

## 10. Findings disposition and recorded decisions

A = Claude Opus reader; B = Qwen3.8-27B reader. B/S/C/Q refer to each reply's labeled
blocker/should/could/question, with numbered prose sections in parentheses. Duplicates are
merged. ACCEPTED means handled in the design, not implemented. Factual disagreements were
resolved against section 2's code; DECIDED records the lead's selections above.

| ID / finding and reader | Disposition | Resolution or one-line options |
|---|---|---|
| F01 Missing context dependency — BLOCKING (A B1; B seams/Q5) | ACCEPTED | Sections 2–3 inline rules and distinguish existing fresh spawning from missing full context workflow; 7d is gated. |
| F02 Increment-1 ambiguity/buildability — BLOCKING (A B2; B (2)) | ACCEPTED | Card 1 fixes repo/language/verbs, exactly three tables/DDL, import examples, status, versions and Given/When/Then tests. |
| F03 Nullable/conditional uniqueness (A B2; B S1) | ACCEPTED | Section 7 gives exact expression/partial indexes and card 4 tests null duplicates; these tables are not in card 1. |
| F04 Two account-budget homes (A B2; B S2) | ACCEPTED | Section 4/card 1 make accounts.budget_micro_eur sole source; no account/month money limits row. |
| F05 Fixed gateway reserve conflicts with small leases (A B3/Q1/Q4; B C1/Q4) | DECIDED | B: mandatory request-sized card 6b, new price-policy hash, per-gateway re-init/canary; metered leases advisory until it ships and activation succeeds. Sections 4/7/9 and card 6b. |
| F06 Headless Claude source/account attribution (A S1) | ACCEPTED | Section 5 and 7b add explicit local source mapping and Claude event parsing; ambiguous/missing data remain unknown. |
| F07 Freshness after idle / reader disagreement (A S2/Q3; B Q3) | DECIDED | B: idle-current only with complete all-machine/all-team coverage and proof of no intervening turn. Operator sets coverage_complete; incomplete coverage uses 600 seconds. Sections 5/7/9 and card 2. |
| F08 Account limit versus overload classifier (A S3) | ACCEPTED | Section 5/7c specify structured signals, zero limit retries, bounded overload retries and no prose-only fleet park. |
| F09 Unbuilt mid-turn input enforcement (A S4/(3)/(4); B (3)) | ACCEPTED | Sections 3/6/7d state subscription planning/post-turn attribution only; future observer and in-flight tests are separate work. |
| F10 Short TTL versus real turns (A S5/Q3; B C3/Q3) | ACCEPTED | Section 6 uses 30m/2h/6h profiles, 24h maximum and explicit expiry/termination semantics; 7d tests hour-long work. |
| F11 Existing child cap/missing ledger methods (A S6; B S4/C1) | ACCEPTED | Sections 2/4 and 6a reuse child rows/reserve_for_child, list mark_uncertain/reconcile, and identify new per-row parameters. |
| F12 Separate-component rounding disagreement (A S7; B (3)) | ACCEPTED | Verified settlement code resolves this fact: section 3 uses +2*M-1 and reserve rates; 68169 reserve versus 56809 tariff estimate. |
| F13 N/G calibration, ceilings and burn fixtures (A S8/C2; B Q3) | ACCEPTED | Section 3 defines numeric calibration, projection denial, measured anchors and clearly labeled synthetic/inferred call counts. |
| F14 Oversized integration steps (A Q6; B Q6) | ACCEPTED | Cards 1–7d carry boundaries/rules/errors/demo/tests; 6/7 are multi-card groups, not one-turn promises. HTML deferred. |
| F15 Old-wrapper behavior (A C1) | ACCEPTED | Section 6 says old wrappers keep working as unmanaged in observe-only mode; strict gateway explicitly denies unleased traffic. |
| F16 Privacy tests on every increment (A C3) | ACCEPTED | Section 8 defines Given/When/Then privacy_<card>; every card includes its named check. |
| F17 Normal same-owner agents can bypass controls (A (4); B (4)) | ACCEPTED | Section 6 explicitly includes credential access, forged reports and admin invocation by ordinary agent shells in the cooperative boundary. |
| F18 Policy hashes already in status (B S3) | ACCEPTED | Section 2 verifies both exact existing keys; reports rename fields only, no new discovery API. |
| F19 Subscription outage default (A Q2; B Q2) | DECIDED | B with guards for availability: subscription warn-only, local park/reset and zero limit retries, machine/account concurrency default 2 (configurable); every degraded turn/status uncoordinated. Money fail-closed; revisit with standby. Sections 5/9 and card 7d. |
| F20 Context default and reviewer knowledge (A Q5; B Q5) | DECIDED | A: FRESH with self-contained dispatch header; curated independent knowledge only after loader proof; author claims excluded from cold reviews. Sections 3/6/9 and card 7d. |
| F21 Existing stale confidence mapping (B C2) | ACCEPTED | Section 5 preserves value/source timestamp with source_stale; section 7 schema and card 2 cover all three legacy states. |
| F22 Generic byte bound clipped to context ceiling (A Q4) | REJECTED | UTF-8 length plus an unspecified template allowance is not a proved complete bound; clipping can hide an oversized request. Section 4 requires validated full encoding or fixed envelope. |
| F23 Every over-envelope request must become uncertain (B Q4) | REJECTED | Definitely untransmitted rejection creates no spend. Section 4/6b preserve uncertainty for reserved/possibly transmitted work and actual overruns. |
| F24 Warn-only for independent metered accounts (B Q2) | REJECTED | Money admission remains fail-closed regardless of topology; section 5 permits warn-only only for subscriptions. |
| F25 Would-have-prevented-burn assurance (B (3); qualified by A (3)) | REJECTED | Required context/parser/classifier integrations are not implemented and subscription caps do not stop mid-turn calls. Section 3 limits the claim to conditional prevention of the pattern. |
| F26 Repeat full schema/DDL in every card (A S9) | REJECTED | Verbatim duplication of all contracts exceeds this single-note scope and creates drift. Card 1 is self-contained; later work orders must include section 7's shared contract alongside their bounded card. |

Build handoff: all four lead choices are resolved. Confirm integration owners and provide
numeric calibration fixtures; retain the named implementation/deployment gates. Coordinated
subscription concurrency remains one; the offline per-machine default of two is explicitly
uncoordinated. Acceptance for build does not claim deployed enforcement or passing product tests.
