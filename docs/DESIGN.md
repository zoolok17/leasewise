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
build plan. Section 11 proposes signed billing-API readings, not dashboard scraping.
Status is JSON. Model/effort aliases are operator-configured, not a vendor catalog.
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
After billing integration, section 11 uses E only for comparable billing; otherwise admission uses
L_now+R_now plus its gross-excess hold check. R counts disjoint unresolved exposure once; other scope
ceilings retain their accounting. Unknown/stale billing alone never holds work or halves the budget.

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
Section 11 adds billing error mappings for the later HTTP integration; 2b is fixture-only.
No automatic retries for policy/schema errors. 429 parks until retry_at or explicit recovery;
null means no timed retry. Gateway reports older than 120 seconds fail closed. Service
outage policy cannot override a known block before its reset; offline recovery uses the
persisted local probe guard defined in section 5.

## 8. Build cards and prerequisites

Seven ordered groups, explicitly split into small cards where necessary. Each **card** is
one developer seat for one or two bounded turns; groups 6/7 are not single-seat turns.
No live provider calls in these fixture cards; later reporter canary needs separate approval.
Every card has `privacy_<card>`: Given injected secret/path/provider-ID
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
| 2b-1: billing ingestion (PLANNED) | After the reviewed card-2 merge is pinned: transactional CHECK migration, monetary gateway payloads, billing totals/policy import, source-time reducer and pure basis conversion shared with later cards. Fixtures only; section 11 defines closed shapes/errors. | Demo import/reorder/rollover without window changes; billing_snapshot_schema, billing_migration_rollback, billing_retention, billing_validation, billing_policy_enums, billing_owner_scope_revision, billing_scope, billing_replay, billing_same_time_conflict, billing_older_high_water, billing_time, billing_rollover, billing_tax_basis, billing_output_errors, privacy_2b. |
| 2b-2: billing evaluation (PLANNED) | Depends on 2b-1; evaluate_billing over explicit immutable context/request, historical and gross-excess reconciliation, read-only lag timer, conditional advisory headroom. No opening planner, writes, leases, holds or resolutions. | Demo uncertified 30 EUR excess => HOLD recommendation; normal P=45/L=50 => fallback headroom 50. Tests: billing_eval_input, billing_overlap_precedence, billing_gross_allowance, billing_non_ai_alert, billing_precedence and all reconciliation/headroom rows owned by 2b-2 in section 11; billing_output_errors, privacy_2b. |
| 2b-2b: opening planner (PLANNED) | Depends on 2b-2; plan_opening with closed input and deterministic proposal. No application, drain execution, financial writes or operator resolution. | Demo uncertain overlap refuses allocation; verified drain/provenance yields stable proposal. billing_opening_input, billing_opening_uncertain_overlap, billing_opening_catchup, billing_opening_allocations, billing_proposal_only, billing_output_errors, privacy_2b. |
| 2b-3: pure OVH adapter (PLANNED) | Depends on 2b-1 contract AND lead-supplied sanitized captured responses/mapping before dispatch; injected credential/transport/clock/jitter, one request -> reading-import document. No installation, real keys/network, credential store or loop. | Demo both captured hourly responses and non-AI/failure fixtures; billing_adapter, billing_non_ai, billing_poll_schedule, billing_tls, billing_output_errors, privacy_2b. Real reporter canary separately approved; application/enforced holds get a separate integration card; 6b remains gated. |
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

## 11. Card 2b addendum v4: provider billing evidence

Status: PLANNED implementation; B01–B06 below are DECIDED. Audience: Leasewise builders/operators.
Provider credentials remain reporter-side; the coordinator stores numeric evidence and local aliases only.
B01: whole-project cap only for an operator-designated dedicated AI Endpoints project; non-AI allowance
is explicit, and sum(gateway shares)+allowance<=cap on the same tax basis. Never infer an AI subtotal.
B02: provisional configurable poll=900s, source max age=7200s, expected economic lag=14400s (four hours).
B03: unknown/stale billing => ALERT, retained floors/local ceilings, no availability hold. Comparable
unexplained excess => HOLD after integration; in 2b these are advisory recommendations only.
B04: fixtures only, one pure OVH adapter; no reporter daemon, credential-store integration, delivery,
live calls, gateway writes, operator resolution or enforced holds. Four bounded cards include 2b-2b (§8).
B05: provisional configurable tolerance=max(50000 micro-EUR,100 bps of the relevant ledger figure).
B06 (operator, 2026-09-24): cap=100 EUR before VAT; cap_basis=provider_basis=ledger_basis=ex_vat.
Keep these explicit policy fields; verify the declared basis against the first real invoice in the
later reporter runbook, recording a numeric result or mismatch for operator review, never storing it raw.
Supplied observations: period [2026-09-01T00:00:00Z,2026-10-01T00:00:00Z); totalPrice 14.304598 at
19:00:26Z then 14.318837 at 20:00:49Z. Amount/time changed together in these two samples only.
Ledger 14.50 by 15:40Z still exceeded provider 14.30 at 19:00Z; later VM charges were absent. Lag is
multi-hour, not bounded by source freshness. Duplicated openings are unproven; provenance still matters.

### Dependency and storage contract

At 876458a, validate_readings.py accepts gateway metadata only, nullable periods, no money fields;
migrate.py admitted only cli_window/gateway_snapshot. Card 2 is now merged at cf2476b and the design at
8a54bce. X1's re-pointed-alias fix is assigned as the first commit of the dispatched 2b-1 build; validate
that fix before using its money evidence. It must reset/reconcile report state when machine/account changes.
2b-1 extends versioned reading-import (next document/schema versions fixed against that merge): old
documents remain importable, but missing amounts/periods mean unusable money evidence, never zero.
New gateway payload = existing metadata plus settled_micro_eur,reserved_micro_eur,uncertain_micro_eur,
unattributed_micro_eur; periods become required UTC-month bounds. First three amounts are disjoint;
unattributed is a subset of settled. Preserve per-gateway history, source time, seq and epoch/hash state.
Rebuild readings' CHECK transactionally before creating billing_totals' source FKs; preserve old rows,
IDs, payloads and windows. Do not accept the future usage kind until its closed validator/consumer ships.
provider_billing uses existing {event_id,machine,account,kind,observed_at,payload}; no wire seat field,
DB seat_id=null; received_at is ingest-owned. Account is an attributed metered alias, event_id local UUID.
Closed payload: {scope_alias,scope_revision,period_start,period_end,currency,total_micro_eur,
non_ai_spend_present}. Exact boolean for the last field; currency=EUR, money nonnegative int64 micro-EUR,
UTC-ms timestamps, positive int scope_revision; aliases follow inventory regex. Reject extra keys,
bool-as-int, floats, overflow, wrong owner/type and >30s future source time. No provider IDs on the wire.
billing_policies PK(account_id FK): reporter_machine_id FK,scope_alias,scope_revision,dedicated_ai,
non_ai_allowance_micro_eur,cap_basis,provider_basis,ledger_basis,vat_bps,poll_seconds,max_age_seconds,
lag_seconds,abs_tolerance_micro_eur,relative_tolerance_bps,mode,stale_action,divergence_action.
Closed enums: mode={advisory}; stale_action={alert}; divergence_action={hold}; each basis={unknown,
ex_vat,invoice_total}. dedicated_ai must be true; durations positive int seconds, VAT/tolerance bps
integers 0..10000; money nonnegative int64. Unknown basis is permitted configuration, not permission
to compare. vat_bps may be null only when no conversion is needed. Config is operator-owned.
2b-1 exposes billing-policy-import --file: {version,policies:[all fields above, account/machine aliases
instead of FKs]}, closed/atomic. After any billing reading, owner/scope/basis changes are refused until
the later resolution integration can reconcile them. Telemetry never changes policy or coverage flags.
billing_totals PK(account_id,scope_revision,period_start): period_end,latest_micro_eur,observed_at,
latest_event_id FK,high_water_micro_eur,high_water_event_id FK,conflict boolean. Currency/scope in policy.
Same event/content replays; same event/different content rolls back. Newer observations set latest;
equal-time unequal totals latch conflict (latest event selected by UUID sort for deterministic display).
Older higher totals raise high-water/conflict without refreshing time. A newer downward correction
retains high-water/conflict; retain each amount's own source time. Only later audited resolution clears it.
lastUpdate supplies observed_at, not poll receipt time. Unchanged success/failure never refreshes it;
empty, missing-time or malformed responses produce no reading. Fresh iff age<=max_age; no idle exemption.
Period=[start,end), source time within bounds (final sample at end allowed); old-month arrivals update
only that month. New month unknown, not zero; never carry the prior month's floor into the new period.
Keep financial history 13 calendar months after period end and unresolved evidence indefinitely.
2b does no deletion; a pure retention-eligibility predicate pins this rule for the later pruning owner.

### Tax basis and comparable evidence

The pure convert_basis(amount_micro_eur,source_basis,target_basis,vat_bps) helper belongs to 2b-1.
Convert P and each gateway's amounts/shares to cap_basis before arithmetic; cap and allowance are
configured in that basis. Unknown basis defers the share-allocation check with ALERT, not approval. Source
bases are operator-declared, never guessed. Ex-VAT -> invoice: ceil(x*(10000+vat_bps)/10000);
inverse: ceil(x*10000/(10000+vat_bps)); equal bases unchanged. No implicit VAT default is permitted.
Convert each cumulative component once, not every imported delta. Use checked integers; unknown basis
or missing required rate yields BILLING_BASIS_UNKNOWN, null comparison/headroom, advisory ALERT only.
Tolerance tau(L)=max(abs_tolerance_micro_eur,ceil(L*relative_tolerance_bps/10000)); approved pair
50000 micro-EUR and 100 bps. Use L_now for gross/idle checks and L_asof for historical checks.
Never add tolerance to headroom. Calibrate on a provenance-checked idle
comparison after provider catch-up; four hours is an investigation delay, not proof of completeness.
Latest means highest seq after restricting to the store-reconciled current epoch chain; observed_at is
only freshness, not ordering. Other-chain rows cannot replace it; a reconciled switch selects a new chain.
L_now=sum latest settled per gateway; R_now=sum latest reserved+uncertain, never add unattributed again.
Unresolved transitions are exactly unresolved_event_ids from the store's reconcile state, not inferred
anew from historical transitions. History checks chain continuity only; an operator-cleared transition
does not re-latch, and an ordinary ok snapshot cannot clear a listed transition. Every selected report
needs exact period, age<=120s and nondecreasing totals within its chain; missing/ambiguous chains are unusable.
Pure evaluation accepts per-reading coverage evidence {event_id,upper_at,complete_at,
overlap_floor_micro_eur,matched_provenance}, with nullable times/overlap and an exact boolean. It is
fixture/operator-owned, never billing telemetry. upper_at certifies no included charge occurred later;
complete_at certifies all in-scope charges through that time are included. matched_provenance attests
matched monetary basis, prices and opening provenance. Reject complete_at>upper_at, complete without
upper, unknown/duplicate event references or times after now. The input contract below fixes overlap use.
OVH lastUpdate proves neither completeness nor an exact economic cutoff: the supplied evidence gives
no such certificate. Small excesses hidden by later spending still require that evidence; gross excess
does not. Time passage alone cannot fabricate a coverage certificate. No new certificate API.
Before looking for certificates, test fresh provider P_e against complete current gateway exposure:
P_e>L_now+R_now+B+allowance+tau(L_now) => unexplained_excess, hold, comparable. When conflicted,
P_e is latest_micro_eur with latest_event_id's time/receipt, never the retained high-water; run this
gross check before the conflict ALERT row, without clearing the conflict or changing its stored floor.
B is previous-period reserved+uncertain exposure at midnight that could be billed in this period;
add it during the first lag_seconds window. Freeze that boundary amount (do not shrink it when old
attempts settle), and retain it beyond that window until period attribution is reconciled; a timer alone
cannot remove charges still present in cumulative P. Unknown boundary evidence disables excess checks
as snapshot_skew (not B=0). A proved empty boundary gives B=0. B never increases admission headroom.
Every gateway report used here must cover at least the provider event's received_at (allow the existing
30s clock-skew margin by requiring observed_at>=received_at+30000), still be <=120s old, and pass
epoch/hash/history checks. Otherwise refresh reports and classify snapshot_skew; freshness alone is not
as-of coverage. Managed charges observed by the provider must already be settled or still reserved/
uncertain in these later reports. A fresh P=L_now+30 with zero R/allowance therefore triggers the hold
recommendation without upper_at. Nonmonotonic/incomplete accounting invalidates this sufficient test.
For each certified upper_at T, bracket each gateway with last snapshot <=T and first snapshot >=T;
require both within 120s of T, continuous epoch/hash and nondecreasing totals. L_asof is the sum of
settled amounts in the upper snapshots; U_asof adds their reserved+uncertain. Use these historical
amounts, not L_now, so subsequent spending cannot conceal excess. Treat allowance as an upper bound
on permitted non-AI exposure: P>U_asof+B+allowance+tau(L_asof) is unexplained excess; it proves no cause.
If P>L_asof+tau(L_asof) but <=U_asof+B+allowance+tau(L_asof), classify reserve_or_scope_covered.
Within tolerance classify matched only with complete, conflict-free evidence and no uncertain overlap;
otherwise temporally_unresolved. A material negative difference is provider_behind, never a refund.
Persistent missing spend: from continuous history compute settled at/before T-lag_seconds; when P is
materially lower, ALERT missing_old_spend even during ongoing activity. Without certified T, also use
an idle diagnostic: now-last_financial_change>=lag_seconds and P<L_now-tau(L_now) => ALERT lag_investigate.
Derive last_financial_change read-only from stored amount/epoch changes on each evaluation; never write
a timer or use report arrival. Identical stored history gives the same timer across restart. Reserve activity
breaks idle. Equality never starts this timer. Missing history cannot establish elapsed idle time.
Unknown/stale billing => ALERT, never a new HOLD recommendation; fresh conflicted billing first runs
the latest-value gross check and otherwise ALERTs. Missing bounds alone
is a normal provisional observation with action=none. Keep
an existing comparable excess latched for the later integration to resolve, not for a poll to clear.

### Advisory headroom and opening plan

For each retained provider event e, O_e is a proved lower bound on managed settled money already in P_e.
Let B_e be the sum of the last continuous ledger snapshots at/before complete_at (each within 120s).
Explicit overlap_floor_micro_eur wins when present: require complete coverage, matched_provenance,
usable B_e and 0<=O_e<=min(P_e,L_now,B_e); violation is INVALID_CONFIG, never clipped. If absent, derive
O_e=B_e only under those proofs and B_e<=P_e; otherwise O_e=0 and overlap is unproved. Never subtract
an upper snapshot. Zero without proof remains a conservative diagnostic, not comparable admission.
Spend estimate E=max(L_now,max_e(P_e+L_now-O_e)); diagnostic E may over-count when overlap is unproved.
Only with complete overlap proofs for every contributing provider event and fresh conflict-free inputs
is ordinary quality comparable. Excess is also comparable for HOLD purposes, even without overlap proof.
For comparable billing, headroom=cap-E-R_now and admission_basis=billing_estimate; otherwise use
headroom=cap-allowance-L_now-R_now and admission_basis=ledger_fallback, with usable local ledger inputs.
This managed-work headroom preserves the separate non-AI allocation; allowance is not available for H.
Keep E and known_floor for diagnostics; do not enforce them in fallback. This conditions §4's account
inequality; fallback still runs the gross-excess hold predicate before admission. With cap=100,L=50,
P=45,R=0,allowance=0 and no coverage, E=95 is diagnostic, headroom=50, action=none; allowance=10 gives 40.
Every hold recommendation denies requested H regardless of headroom; known integrated holds also deny.
Example with certified overlap: P=80, O=50, L_now=60, cap=100, R=0 => E=90, headroom=10.
This synthetic case includes allowance=30 and total gateway shares<=70, covering its known non-AI portion.
With P=14.30,O=14.30,L_now=18.43,R=0.50,cap=100 => 81.07. These are proof-bearing fixtures.
Missing P means estimate=null, not P=0; missing/unusable ledger means headroom=null and admission_basis=
unavailable. Unknown/stale/conflicted P preserves known_floor=max(known P,L_now), with unknown operands
omitted and null if neither is known. It uses ledger_fallback when available; a fresh latest-value gross
excess may still recommend HOLD despite a latched conflict.
R here is only disjoint gateway aggregates. Service-lease overlap belongs to cards 4/6a; 30K output
reservation/enforcement belongs to 6b. Lagging invisible bypass remains unbounded by this estimate.
Opening planner takes verified drain completion, snapshot/coverage references, per-gateway baselines,
proven external allocations and a separate non-AI allocation. It never drains or writes anything.
Require no reserved/uncertain attempts, fresh matching reports, no correction/epoch conflict, known
provenance and lastUpdate>=drain_completed+lag_seconds PLUS verified complete_at>=drain_completed.
No expiry or elapsed wait proves catch-up. Missing proof => OPENING_UNSAFE; no allocatable delta.
P-L is not evidence of an external charge; never subtract R to manufacture one. A plan requires
independently proven, disjoint external charges exactly covering max(0,P-L). Allocations must be
nonnegative, sum to that amount, charge only the owning gateway, and keep each opening within its
share; non-AI allocation stays in the allowance bucket, not in both gateway ledgers. Missing ownership
or an exceeded allowance => OPENING_UNSAFE. With P<L, preserve every baseline, never lower to P.
Output deterministic proposal ID from canonical numeric inputs/references; repeated calls have identical
contents and zero ledger writes. Application, revalidation/fencing, partial-write recovery, accept-correction
audit and operator resolutions require a separately sized integration work order; 6b activation remains.

### Pure input surfaces (2b-2 and 2b-2b)

Pin evaluate_billing(context,request)->Evaluation and plan_opening(context,request)->Proposal. No
new evaluator/planner CLI verbs or coverage-file discovery: fixtures call these pure functions directly.
Closed JSON-compatible context={account,period_start,period_end,cap_micro_eur,gateways,policy,readings,
unresolved_event_ids,boundary_exposure_micro_eur,boundary_event_ids}; gateways=[{gateway,share_micro_eur,
current_chain_event_ids}]. Current-chain IDs select store-accepted readings for that gateway's active ledger
identity; epoch-number reuse after an alias move does not join two identities. No caller-selected chain.
policy is the full billing-policy-import
record with account and reporter_machine aliases replacing the two FKs. readings are validated reading
envelopes plus received_at, all for this account; unresolved_event_ids are local UUID references to
unresolved transitions copied from store reconcile state. Include retained same-period history and the
previous-period boundary evidence referenced by boundary_event_ids; never add old-period totals to L_now.
boundary_exposure_micro_eur is nullable nonnegative int64, derived from complete store boundary evidence,
not user telemetry; null means unknown. References/chain membership are validated; contradictory same-seq
amounts in one chain are unusable, while identical replays count once. Evaluator never mutates reconcile state.
Evaluation request={version:1,account,now_ms,requested_micro_eur,coverage}; coverage is an array of the
closed per-event evidence records above, [] when absent. requested_micro_eur is nonnegative int64;
now_ms is injected UTC-ms, never read from a clock inside evaluation. Account must match context.
Opening request={version:1,account,now_ms,drain_completed_at,drain_verified,billing_event_id,coverage,
baselines,allocations,non_ai_allocation,proven_external}; drain_verified is an exact boolean attestation.
baselines=[{gateway,snapshot_event_id,baseline_micro_eur}] covers every configured gateway exactly
once and must equal its referenced settled snapshot. allocations=[{gateway,external_micro_eur,
evidence_ids}]; non_ai_allocation={amount_micro_eur,evidence_ids}; evidence_ids are arrays of local UUIDs.
proven_external=[{evidence_id,gateway,amount_micro_eur,disjoint_from_managed}]; gateway=null only for
non-AI charges, disjoint_from_managed must be true. These operator/fixture assertions are not provider
telemetry. Each proof must be consumed exactly once by its owner; sum of its amounts must equal the
allocation. Missing gateway allocations mean zero; duplicate gateways/proof IDs are invalid.
All fields required, no extra keys; reuse alias/int64/UTC-ms/boolean validation. Unknown references,
bad ordering, invalid explicit overlap, account mismatch or unsupported version => INVALID_CONFIG.
Validly shaped but unsafe opening evidence returns unsafe/empty allocations; evaluation always sets
proposal=null. plan_opening returns the Proposal shape below and neither function reads files or writes.

### Pure OVH adapter, output and errors

Dispatch prerequisite for 2b-3: the lead supplies sanitized captures of the 19:00:26Z and 20:00:49Z
responses, including resource-category monetary fields, with the approved field mapping. Without them
do not dispatch or invent fixtures; canary approval does not replace this build prerequisite.
2b-3: one call with injected credential, transport, clock and jitter integer -> versioned reading-import
document or closed failure; no loop, file credential access or outbox. Signed read-only GET of project
usage/current uses totalPrice, lastUpdate and UTC period. No account-wide empty-result fallback.
Return non_ai_spend_present=true iff any non-ai-endpoints category has nonzero billed amount; missing
or unverified category-price shape fails schema rather than emitting false. Monetary field mapping must
be pinned to those captures before implementation; category names alone are insufficient.
Decimal parsing/ceil to micro-EUR, no binary floats. Failure={auth,transport,rate_limit,schema,currency,
period,tls}; failure output {failure,next_attempt_at}, success {document,next_attempt_at}. Compute next
attempt=max(now+poll+jitter,valid Retry-After), jitter in [0,90]s injected; no immediate transport retry.
Transport contract refuses disabled TLS verification/non-HTTPS; fixtures test that refusal, not actual
certificate verification. Later deployment checklist: tested TLS, exact GET-only grants, local secret
ACLs, authenticated account binding (card 5), signed-header/URL redaction and separately approved canary.
Runbook: set allowance to verified measured non-AI spend (normally zero for a dedicated project when
non_ai_spend_present=false and no outstanding non-AI liability is known). Unused allowance masks bypass
up to that unused amount. Recommend/review reductions; never let a lagged false flag auto-change policy.
No raw exceptions, IDs, keys or bodies in documents/status/errors; only registered aliases, local UUIDs,
numbers and closed enums. Other provider adapters/deployment plumbing need separate work orders.
Evaluation output (all required): {state,action,reason,quality,admission_basis,known_floor_micro_eur,
estimate_micro_eur,headroom_micro_eur,request_fits,proposal}; nullable arithmetic when unknown.
admission_basis={billing_estimate,ledger_fallback,unavailable}; no enforcement capability in this result.
state={matched,provider_behind,reserve_or_scope_covered,temporally_unresolved,unexplained_excess,
unknown,stale,conflict}; action={none,alert,hold}; quality={comparable,provisional,unknown}.
reason={matched,lag_expected,lag_investigate,missing_old_spend,reserve_or_scope_covered,snapshot_skew,
coverage_unknown,provider_excess,billing_unknown,billing_stale,source_conflict,gateway_unusable,
basis_unknown,scope_mismatch,non_ai_present}; non_ai_present requires ALERT at minimum.
Decision table is ordered, first matching base row wins; every gross check includes its time/ledger guards.
Q=comparable only with complete overlap proof and fresh conflict-free inputs, otherwise provisional.

| Condition | state / reason | action | quality |
|---|---|---|---|
| Invalid basis/scope or unusable ledger | unknown / basis_unknown,scope_mismatch,gateway_unusable (in that order) | alert | unknown |
| No provider sample; stale sample (in that order) | unknown,stale / billing_unknown,billing_stale | alert | provisional |
| Fresh gross excess beyond exposure+B+allowance+tolerance (latest value if conflicted) | unexplained_excess / provider_excess | hold | comparable |
| Remaining latched conflict | conflict / source_conflict | alert | provisional |
| Unknown midnight boundary exposure | temporally_unresolved / snapshot_skew | none | provisional |
| Certified historical excess beyond exposure+B+allowance+tolerance | unexplained_excess / provider_excess | hold | comparable |
| Certified P below old settled by material amount | provider_behind / missing_old_spend | alert | Q |
| Idle material negative gap at >=lag_seconds | provider_behind / lag_investigate | alert | provisional |
| No certificate, gross time guards absent | temporally_unresolved / snapshot_skew | none | provisional |
| No certificate, gross test passed without excess | temporally_unresolved / coverage_unknown | none | provisional |
| Certified positive difference covered by reserves/allowance | reserve_or_scope_covered / reserve_or_scope_covered | none | Q |
| Matched within tolerance and complete evidence, no uncertain overlap | matched / matched | none | Q |
| Certified material negative gap without lag alarm | provider_behind / lag_expected | none | Q |
| Otherwise incomplete certificate/brackets | temporally_unresolved / coverage_unknown | none | provisional |

Overlay: any retained non_ai_spend_present in the current period raises none to alert with reason=
non_ai_present; it never downgrades hold or replaces an earlier alert reason. A missing certificate alone
is not anomalous. Existing holds are owned by later integration and never cleared by this pure output.
request_fits=false for action=hold; else H<=effective headroom when non-null, else null. Both comparable
and provisional results can therefore report a local fallback fit; this is advisory, never lease authority.
proposal=null or {proposal_id,state,reason,source_events,allocations,non_ai_allocation_micro_eur};
state={ready,unsafe}; reason={verified,opening_unsafe}; allocations=[{gateway,baseline_micro_eur,
external_micro_eur,opening_micro_eur}]. Unsafe proposals have empty allocations; no inferred authority.
Existing reading/status CLI exits 0 on valid advisory output/import, even action=hold. Pure functions
return results or typed errors; future thin CLI wrappers use the same mapping, not new verbs in 2b-2.
Closed validation errors INVALID_CONFIG,SCHEMA_UNSUPPORTED,IDEMPOTENCY_CONFLICT exit 2;
BILLING_SCOPE_MISMATCH,BILLING_BASIS_UNKNOWN,BILLING_UNKNOWN,BILLING_STALE,BILLING_DIVERGENCE,
OPENING_UNSAFE also exit 2 if an explicit usable-plan request fails; STORE_ERROR exits 3. Successful
status retains advisory reasons and exit 0. Adapter failure enums return exit 2; numeric overflow uses
INVALID_CONFIG. Later HTTP mapping: scope=409,basis=409,unknown/stale=503,divergence/opening=409;
config/schema=400,idempotency=409,store=503. These add to §7; no HTTP implementation in 2b.

### Acceptance matrix (future tests; every row uses injected time/fixtures)

| Named tests / owner | Given / When / Then |
|---|---|
| billing_snapshot_schema / 2b-1 | Given legacy metadata-only snapshots, When importing/evaluating money evidence, Then amounts remain unknown; new closed amounts/periods validate. Merged-SHA and captured-fixture checks are dispatch prerequisites, not product tests. |
| billing_migration_rollback; billing_retention / 2b-1 | Given v2 rows and interruption after each DDL, When migrate/restart, Then rollback/FKs/IDs/windows survive; eligibility keeps 30-day/13-month/unresolved evidence correctly, no pruning occurs. |
| billing_validation; billing_policy_enums / 2b-1 | Given bool/float/negative/overflow/extra keys/wire seat/wrong account or unsupported enum, When importing, Then exit 2 and unchanged DB; telemetry cannot change any policy. |
| billing_owner_scope_revision; billing_scope / 2b-1 | Given foreign machine, inactive revision, bad alias, nondedicated project or shares+allowance>cap, When importing, Then reject; replica copies never add totals, policy changes with history refused. |
| billing_replay; billing_same_time_conflict; billing_older_high_water / 2b-1 | Given same-ID replay/collision, equal-time different totals or older higher amount, When reordered, Then no double counting, collision rollback, deterministic conflict/high-water without time refresh. |
| billing_time; billing_rollover / 2b-1 | Given failure/unchanged polls, source age=7200s/+1ms, future=30s/+1ms, midnight or late prior-month reading, When reduced, Then exact freshness/future boundaries, new month unknown/ALERT and independent periods; no idle exemption. |
| billing_gateway_uncertainty / 2b-2 | Given absent/null/stale (>120s), unresolved epoch/hash state or decreased in-chain ledger, When evaluated, Then admission_basis=unavailable, headroom=null and request_fits=null; 120s valid, unattributed subset counted once. |
| billing_conflicted_latest_gross / 2b-2 | Given latched high-water=100, fresh latest=50 and L=50, When gross-tested, Then conflict ALERT only; latest=80 with L=50 and R/B/allowance=0 yields comparable HOLD without a certificate, using latest receipt guards and preserving conflict. |
| billing_midnight_exposure / 2b-2 | Given previous-period boundary exposure=0.20 and new-period P=0.20/L=R=0, When evaluated before/at/after lag_seconds, Then no false excess; B stays after settlement/timer expiry until attribution reconciliation, unknown B cannot become zero, and P>R+L+B+allowance+tau still holds. |
| billing_reconcile_state_precedence / 2b-2 | Given an old transition in history, When its ID is absent after store reconciliation, Then it does not re-latch; while listed, repeated ok reports leave the gateway unusable. No evaluator writes. |
| billing_latest_chain_seq / 2b-2 | Given shuffled times, lower-seq replay, late old-epoch rows and a moved alias reusing its epoch number, When selecting latest, Then highest seq in the store-selected current chain wins; other identities/chains cannot supply current amounts without reconciliation. |
| billing_fallback_allowance / 2b-2 | Given cap=100,L=50,R=0,allowance=10 and provisional billing, When evaluating H=40 and H=40.000001, Then headroom=40, fits=true/false; allowance remains separate from managed work. |
| billing_allowance_runbook / later reporter | Given verified non-AI spend or a lagged false flag, When recommending allowance, Then use measured liability (zero only when none is known), disclose the blind band, and never mutate policy from telemetry. |
| billing_tax_basis / 2b-1 | Given B06 ex_vat policy, unknown/mismatched bases and half-micro conversions, When importing/evaluating shared conversion, Then 100 EUR stays 100 before VAT, allocation checks use one basis, unknown alerts and conversion rounds upward. |
| billing_tolerance / 2b-2 | Given approved configurable 50000 micro-EUR/100 bps and ledger 20 EUR, When calculating tau, Then 0.20 EUR; absolute floor at low totals, never budget credit. |
| billing_eval_input; billing_overlap_precedence / 2b-2 | Given closed context/request and complete_at>upper_at, unknown IDs or O outside min(P,L_now,B_e), When evaluated, Then invalid input rejected; explicit valid overlap wins, absent overlap derives only with proof; injected now controls all time. |
| billing_reconcile_gross_excess_uncertified; billing_gross_allowance / 2b-2 | Given P=L_now+30 without certificates, When later complete gateway reports are fresh, Then comparable HOLD; older reports cannot hold. Given P at L+R+allowance+tau and +1 micro-EUR, Then no gross hold at equality, hold above; reserves/allowance prevent false excess. |
| billing_non_ai_alert; billing_precedence / 2b-2 | Given normal missing bounds, non-AI flag, stale/conflict, or gross excess, When selecting ordered table/overlay, Then none for coverage alone, alert for non-AI, anomalies preserve earlier reason, and hold is never downgraded; historical excess precedes unresolved bounds. |
| billing_reconcile_snapshot_skew; billing_reconcile_busy / 2b-2 | Given old current report or as-of L=10/P=13 then L_now=14 with valid brackets, When evaluated, Then skew unresolved or true 3 EUR excess despite continued spend. |
| billing_reconcile_uncertain_overlap; billing_coverage / 2b-2 | Given L=18.43/P=19/R_asof=0.57 or uncertified lastUpdate, When evaluated, Then covered or unresolved, no excess HOLD; verified P>U+allowance+tau yields advisory HOLD only. |
| billing_reconcile_equal_idle; billing_reconcile_lag_timer / 2b-2 | Given equality or fresh-source negative gap, When time reaches 4h across restart/heartbeats, Then equality stays matched and material gap alerts at >=4h; activity resets idle, historical missing-old still alerts in busy periods. |
| billing_headroom; billing_headroom_boundaries / 2b-2 | Given both proof-bearing examples and cap=100/P=45/L=50/R=0 without proofs, When computing, Then comparable 81.07/10 and provisional fallback 50 (diagnostic E=95); no upper subtraction, R once, H=headroom fits/+1 does not, hold always false-fit, overflow rejected, missing provider never zeroed. |
| billing_opening_input / 2b-2b | Given wrong baseline references, duplicate proof consumption, mismatched account or extra request keys, When planning, Then INVALID_CONFIG; well-shaped insufficient proof gives unsafe with empty allocations and no I/O. |
| billing_opening_uncertain_overlap; billing_opening_catchup / 2b-2b | Given 18.43/19/0.57 or early drain, When proposing, Then unsafe/no allocation until resolved and catch-up proved; eventual settled 19 produces no duplicate 0.57. |
| billing_opening_allocations; billing_proposal_only / 2b-2b | Given negative amounts, When validating, Then INVALID_CONFIG; Given well-shaped missing provenance, wrong sums/owner or share overflow, When planning, Then unsafe; valid plans preserve baselines, stable IDs, non-AI bucket once and no financial writes. |
| billing_adapter; billing_non_ai; billing_poll_schedule; billing_tls / 2b-3 | Given signed-GET fixtures, fractional totals/category errors and all failure enums, When called once, Then ceil money, exact flag/no unknown-as-false, bounded jitter/Retry-After precedence and TLS-option refusal; no retry or credential storage. |
| billing_output_errors; privacy_2b / all | Given every result/error branch and keys/IDs/paths/signed headers/URLs/raw failures, When serialized, Then closed shape/exit mappings and only approved aliases/numbers/enums; compare before/after snapshots for zero financial side effects. |
| billing_accept_correction; billing_apply_fence; billing_no_poll_clear / later integration | Given overreport correction, uncertain overlap or interrupted multi-gateway apply, When audited resolution runs, Then no double liability, fence persists until complete, poll never clears holds or creates receipts. |
| billing_invoice_basis / later reporter | Given the first real invoice and explicit ex_vat fields, When the operator verifies provider/ledger/cap basis, Then record a numeric pass or mismatch for review; do not store the raw invoice in coordinator metadata. |
| billing_lease_overlap; billing_output_30k; billing_reporter_auth; billing_deployment / cards 4/6a,6b,5,later reporter | Given mirrored lease attempts, 30K output, foreign identity or real TLS/grants, When integrated, Then single exposure, bounded output, denied identity and verified deployment; no such claim in 2b. |

### Findings disposition and remaining choices

| Finding (A/B identify readers) | Disposition | Resolution |
|---|---|---|
| A1 opening uncertain overlap (BLOCKER) | ACCEPTED | Planner refuses any unresolved attempt and requires disjoint provenance; overlap test. |
| A2; B-B1 as-of comparability (BLOCKERS) | ACCEPTED | Historical brackets, certified economic coverage, reserve/non-AI exposure and unresolved state; busy/skew tests. lastUpdate alone is insufficient. |
| A3 equality/lag timer | ACCEPTED | Matched equality; financial-activity timer and historical lag threshold tested at boundary/restart. |
| A4; B-B3 scope/input/dependency (BLOCKERS) | ACCEPTED | Bounded fixture cards, explicit money-payload extension, merged-SHA gate; future integration tests moved. v3 further splits opening work. |
| A5; B-S4 lag/tolerance | ACCEPTED | Four-hour lag separate from 7200s age; B05 now fixes provisional absolute-plus-relative values. |
| A6; B-S1 missing named tests | ACCEPTED | Acceptance matrix covers validation, ownership, corrections, migration, retention eligibility, arithmetic, plans, scheduling and future ownership. |
| B-B2 headroom (BLOCKER) | ACCEPTED | Add post-coverage spend using proved lower overlap; never subtract upper snapshot; update §4 account inequality. Missing proof uses conservative zero overlap. |
| B-S2 owner column; B-C1 typed revision | ACCEPTED | reporter_machine_id FK, integer revision/registered aliases, closed local owner policy. |
| B-S3 enums; B-S8 exits/HTTP | ACCEPTED | Closed policy/results/errors, exit 0 advisory versus exit 2 unusable request, explicit future HTTP mappings. |
| B-S5 premature opening | ACCEPTED | Source time after drain+lag is necessary but not sufficient; require confirmed completeness and no uncertainty. |
| B-S6 rollover outage | ACCEPTED | Unknown/stale alerts only; retain same-period floors and local ceilings. |
| B-S7 migration order | ACCEPTED | CHECK rebuild precedes billing source FKs, transactional rollback tests. |
| B-S7 pre-add usage kind | REJECTED | Keep schema support with its owning consumer card, accepting one later rebuild. Widening CHECK alone would not bypass the closed validator; the earlier validation rationale was incorrect. |
| B-C2 fixture TLS versus deployment | ACCEPTED | Fixture refusal/signed-request checks separated from later actual TLS/grants/ACL checklist. |
| B-C3 non-AI flag | ACCEPTED | Boolean flag, dedicated designation and allowance constraint; unknown category-price shape fails schema. |
| B-C4 correction action | ACCEPTED | Audited accept-correction belongs to the separately sized resolution integration, not polling. |
| B-Q1/Q2 live time/period | ACCEPTED | Exact supplied UTC period/two samples recorded; equal-time total changes remain possible/conflicted. |
| B-Q3 provenance; B-Q4 VAT | ACCEPTED | Multi-hour lag accepted without declaring duplicated openings disproved; B06 declares ex-VAT with first-invoice verification. |

| ID | State | Recorded choice |
|---|---|---|
| B05 Tolerance pair | DECIDED | max(50000 micro-EUR,100 bps of the relevant ledger figure), provisional/configurable; no headroom credit. |
| B06 Tax basis | DECIDED | 100 EUR before VAT; explicit cap/provider/ledger basis=ex_vat; verify against first real invoice in the later reporter runbook. |

### Reader B delta disposition (v3)

| Finding | Disposition | Resolution |
|---|---|---|
| N1 gross excess without certificate (BLOCKER) | ACCEPTED | Fresh P>L_now+R_now+allowance+tau holds without a certificate, provided complete gateway reports cover provider receipt time; gross/skew/allowance tests. |
| N2 missing evaluation/planner inputs (BLOCKER) | ACCEPTED | Pure function signatures, closed context/request shapes, reference validation and explicit-overlap precedence; no invented CLI verbs. |
| S1 permanent coverage alert / mapping | ACCEPTED | Ordered state/reason/action/quality table; missing bounds alone is none/provisional; anomaly overlay and precedence tests. |
| S2 unconditional E admission | ACCEPTED | Sections 4/11 use E for comparable billing; otherwise ledger fallback plus gross-excess check. E=95 is diagnostic for P=45/L=50, effective headroom remains 50. |
| S3 card size / conversion dependency | ACCEPTED | Opening planner moved to 2b-2b; shared pure conversion owned by 2b-1. |
| S4 adapter captured input | ACCEPTED | Sanitized captures and approved category-price mapping required before 2b-3 dispatch, not merely before canary. |
| C1 B05 values | ACCEPTED | 50000 micro-EUR/100 bps in policy decision, formula and fixture expectations. |
| C2 process gate as test | ACCEPTED | Merged-SHA gate remains a dispatch prerequisite; billing_snapshot_schema tests only executable legacy/new snapshot behavior. |
| C3 timer writes | ACCEPTED | last_financial_change derived read-only from retained history on each evaluation; restart needs no new state. |
| C4 uncovered rules | ACCEPTED | Named input/overlap, gross-allowance, non-AI overlay and precedence fixtures cover each listed boundary. |

No remaining lead choices in this addendum. Card 2 is merged; X1 is assigned to 2b-1's first commit.
Captured OVH fixtures, first-invoice verification and real canary retain their respective build/deployment gates.

### Reader B v4 clarification disposition

| Item | Disposition | Resolution |
|---|---|---|
| S1 conflicted gross check | ACCEPTED | Fresh latest value is tested before conflict ALERT; high-water cannot trigger that check; billing_conflicted_latest_gross. |
| S2 midnight exposure | ACCEPTED | Add frozen previous-period boundary exposure to excess comparisons; retain until attribution resolves, not merely until lag expires; billing_midnight_exposure. |
| S3 reconciliation precedence | ACCEPTED | Store reconcile state alone supplies unresolved_event_ids; history checks continuity without re-latching cleared transitions; billing_reconcile_state_precedence. |
| S4 latest snapshot | ACCEPTED | Highest seq in the store-selected current epoch chain, never another chain or simply newest arrival; billing_latest_chain_seq. |
| C1 fallback allowance | ACCEPTED | Managed fallback subtracts non-AI allowance from headroom/request_fits; billing_fallback_allowance. |
| C2 allowance blind band | ACCEPTED | Runbook recommends verified measured non-AI liability, normally zero on a dedicated project; operator-only changes; billing_allowance_runbook. |
| X1 alias identity gate | ASSIGNED | Lead placed alias report reset/reconciliation in 2b-1's first commit; this design records the dependency, not an implementation claim. |
| Missing unavailable case | ACCEPTED | billing_gateway_uncertainty explicitly requires unavailable basis, null headroom and null request_fits. |
