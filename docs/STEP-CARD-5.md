# Step record — Card 5: private HTTP

Work order: tk-671932fd4e85 (build, 2026-09-26); fix round tk-7d7fd9023f8c
(2026-09-27, HOLD: 1 blocker, 4 major, 3 minor - see the "Fix round"
section below). Branch `card-5` off `1821a4e` (cards 1-4, 2b-1, 2b-2,
2b-2b merged). Interpreter: CPython 3.14, standard library only
(`http.server`, `sqlite3`, `secrets`, `hashlib`, `ipaddress`).

The card is docs/DESIGN.md section 8, row "5: private HTTP": thin `/v1`
HTTP handlers over the tested store (cards 1-4) and policy (card 3), an
identity allowlist and the host ingress adapter. **Out of scope** (the
design says so): a custom TLS stack, an admin API, live provider calls,
the wrapper/gateway integration (cards 6a/6b/7).

## Files

- `quota_balancer/http_api.py` — the HTTP layer (new): route handlers
  separated from the socket/protocol glue (`RequestHandler`), the
  error-code -> wire-status table, the identity allowlist, lease-token
  minting, `check_bind_host`/`make_server`.
- `quota_balancer/migrate.py` — migration version 6: `machines` gets
  `identity_ref` (unique among non-null values, a partial index),
  `enabled`, `last_seen_at` — the columns section 7 already promised
  ("machines add identity_ref UNIQUE,enabled,last_seen_at") but no
  earlier card added.
- `quota_balancer/store.py` — `set_machine_identity` (new, operator-only,
  same pattern as `set_coverage`/`reconcile`): enrolls/updates a
  machine's allowlist entry. Never exposed over HTTP (no admin API).
- `quota_balancer/lease.py` — the m4 carry-over fix, renaming a discarded
  `_kind` local to `account_kind` (used by the fix), and (fix round
  tk-7d7fd9023f8c finding 5) `record_limit_event`: the new store function
  behind `POST /v1/limit-events`.
- `quota_balancer/errors.py` — `CODE_UNAUTHENTICATED`/`CODE_FORBIDDEN`
  (HTTP-transport-only; not in `EXIT_CODES`, there is no CLI request to
  authenticate).
- `quota_balancer/cli.py` — `identity-set` (wraps `set_machine_identity`)
  and `serve` (blocking; wraps `make_server(...).serve_forever()`).
- `tests/test_card5.py`, `tests/test_card4_seat_kind_fix.py` (5 tests,
  the m4 fix, store-level only) — test counts are in Evidence below (the
  fix round substantially rewrote `test_card5.py`'s fixtures to match the
  corrected wire contract, not only added a `FixRound*` section).
- Three pre-existing tests had a hardcoded literal schema version (5,
  and one "newer than current" sentinel 6) that the version-6 migration
  makes stale; bumped to 6/7 respectively (`tests/test_card1.py`,
  `tests/test_card2b1.py`, `tests/test_card4.py`) — no behavior changed,
  confirmed by re-running the full suite (below).

Reused, not forked: `lease.grant/check/settle`, `store.reading_import/
status`, `store._reject_duplicate_keys`, `store._connect` (never raises
raw `sqlite3.Error`; a missing db file is `STORE_ERROR` — exactly the
503 the card asks for), `migrate.rollback_quietly`. The HTTP layer adds
**zero** admission, freshness or reconciliation logic; every grant/
check/settle/readings/status call is the exact function cards 3/4/1/2b-1
already test.

## Design as implemented

**Identity allowlist.** `X-Machine-Identity: <identity_ref>` names the
caller; missing/empty -> 401 `UNAUTHENTICATED`; unknown or disabled ->
403 `FORBIDDEN` (never distinguished from an ownership failure on the
wire — "cross-machine denial", the row's own words). Section 6's own
threat model is COOPERATIVE, not adversarial, against a co-resident
process; this header is trusted as presented on loopback, the same as
any other same-host signal. A real tailnet ingress terminates TLS and
overwrites the header with its own verified value before forwarding —
out of this card's scope ("no custom TLS stack").

**Ownership (the part the store does not check).** The store derives
`machine_id` from a seat/lease/gateway row, never from a caller — so
nothing before this card stopped a foreign machine from granting a
lease for someone else's seat. The HTTP layer adds one rule per route:
grant — the seat's own machine; check — the lease's own machine, its
current lease token, **and** an enforcing mode (fix round
tk-7d7fd9023f8c finding 1); settle — the lease's own machine (no token:
"Reporter identity, not a wrapper token alone, authorizes money
settlement", section 6); readings and limit-events — the reporting
machine is *implicit* (the authenticated caller, never a client-supplied
field — see finding 3) **and**, for a `cli_window` reading or a
limit-event, must be an authorized reporter for the named account via
the same project_accounts chain `grant()` uses (finding 2). *(Corrected
by the fix round: this paragraph originally said readings' ownership
check was "every declared reading's `machine` equals the caller's own
alias" against a client-supplied `machine` field on a batch document -
that field no longer exists on the wire at all; see finding 3.)*

**Lease tokens.** `leases.token_hash` exists in the schema but no store
function minted it (STEP-CARD-4.md: "token_hash is card 5"). This module
mints a fresh 256-bit token (`secrets.token_hex(32)`, sha256-hashed) on
every grant response whose lease is both non-terminal AND an enforcing
mode — fresh or replayed — and stores only the hash. A terminal replay,
or a `metered_advisory`/`shadow` grant, returns no token. *(Corrected:
this paragraph originally said "whose state is not already settled" -
the blind read's BLOCKER finding 1 showed `metered_advisory`/`shadow`
grants got a token too, and could then pass /check with it; both token
issuance and /check are now gated by enforcement mode, not merely by
state.)*

**`public_bind`.** `check_bind_host` parses the host as a literal AF_INET
IPv4 address and requires `.is_loopback`; a bare hostname (including
`"localhost"`) and any IPv6 literal (including `"::1"`) are refused too
(fix round finding 8: the server is AF_INET-only, so accepting `::1`
here just meant failing later, with a raw `gaierror`, at construction).
`make_server` calls it before constructing `ThreadingHTTPServer`, so a
public/wildcard/IPv6 bind never opens a socket. *(Corrected: this
paragraph originally said IPv6 loopback was accepted; it also said
nothing about a distinct protocol-error path - a raw request with an
invalid HTTP method name is refused by `BaseHTTPRequestHandler`'s own
`send_error`, BEFORE `_handle` or authentication ever run, and its
default implementation embeds the raw method text, unescaped, in an
HTML body; `RequestHandler.send_error` is now overridden to emit the
same closed JSON envelope, reading neither `message` nor `explain` -
finding 6.)*

**Wire shapes (rebuilt by the fix round - see finding 3 below for the
full adapter).** `/v1/leases` accepts section 7's own documented body
(`task_class`, no `request_hash`); `/v1/leases/{id}/settle` accepts
section 7's own documented body (no `lease_id`/`observed_at`/
`received_at` - the id comes from the URL, the timing from the server's
receipt instant); `/v1/readings` accepts section 7's own flat,
single-reading body (`account_id`, not an alias; no `machine` field at
all). Every mutation additionally requires an `Idempotency-Key` header
agreeing with the body's own id (finding 4). `/v1/leases` responses drop
the store's internal `replayed` flag and add `lease_token`; `windows`/
`warnings` are emitted empty (not fabricated — no card built so far
computes them, decision 3). `/check` is sliced to exactly
`{state,expires_at,caps}`; `/settle` to exactly
`{state,released_micro_eur,retained_micro_eur}`. Every response also
carries `server_at`/`policy_revision` (decision 2). Errors are
`{error:{code,message,retry_at,details:{}}}` plus the same envelope
fields — `details` is always `{}`; nothing computed here ever reaches
the wire unfiltered - including from `BaseHTTPRequestHandler`'s own
protocol-error path (finding 6).

## Acceptance rows -> tests

| Row | Tests |
|---|---|
| `token_scope` | `TokenScope` (`tests/test_card5.py`): check denies a different machine even with the right lease id; check denies the right machine with a stale/missing/wrong token; readings denies an unauthorized reporter (and accepts its own); settle denies a machine that owns neither the lease nor the request; settle accepted from the lease's own machine |
| `replay_grant` | `ReplayGrant`: replay rotates the token and adds no new hold row; a terminal (already-settled) replay returns the settled state with no token; different content under the same ids is `IDEMPOTENCY_CONFLICT` |
| `public_bind` | `PublicBind`, `FixRoundIPv6Bind`: wildcard/public/hostname/IPv6/malformed hosts refused (pure `check_bind_host`, table-driven); loopback hosts accepted; `make_server` refuses a public or IPv6 bind before any socket exists (not a `gaierror`); the default host is loopback and actually serves |
| `privacy_5` | `Privacy5`: injected secret/path/provider-ID sentinels never appear in any error body across every route; `/v1/status` is JSON-only and never echoes a sentinel identity; the error envelope is a closed shape (no stray debug field); a genuine un-typed internal exception is still caught and never leaks; a corrupt (non-sqlite) store file leaks nothing; a protocol-level error (an invalid HTTP method) still emits closed JSON, never the raw method text |
| Also cover: 401 vs 403 | `IdentityAuth`: missing/empty header -> 401; unknown/disabled machine -> 403; a grant for a seat owned by a different machine -> 403 |
| Also cover: 503 | `StoreUnavailable`: a missing db file -> 503 `STORE_ERROR` |
| Also cover: body bounds, malformed/duplicate JSON | `BodyBounds`: oversized body (both a naturally-oversized body and one isolated from any other 400 reason via a lowered bound); missing/empty Content-Length (both the empty-string and the genuinely-absent-header cases, isolated with a raw socket); malformed JSON; duplicate JSON key (both a naturally-malformed body and one isolated from any other 400 reason); a non-object top-level JSON value |
| Demo: two machines share one block | `Demo`: two authenticated machines on two seats of the same account both get `ACCOUNT_PARKED`; clearing the block unblocks both; a `POST /v1/limit-events` from EITHER machine parks both, joining the same block (not a duplicate) |
| (supplemental) wire contract | `WireContract`: closed response shapes; the wire's JSON `true` for `final` converts to the store's `1` (and a plain `int` on the wire is refused, not silently accepted); the settle body carries no `lease_id`/`observed_at`/`received_at`; `INPUT_LIMIT`/`MONEY_LIMIT`/`NOT_FOUND`/unrouted-path mappings; the `/check` body shape is enforced even though `lease.check` itself never reads it |
| (supplemental) enrollment | `IdentityEnrollment`: `store.set_machine_identity` enrolls/updates/disables; rejects a duplicate `identity_ref`; rejects an unknown machine; a machine may reclaim its own value; the CLI `identity-set` and `serve` verbs, end to end (`serve --host 0.0.0.0` still refuses over a real, timeout-bounded subprocess) |
| m4 carry-over | `tests/test_card4_seat_kind_fix.py`: `subscription_admission`/`subscription_warn` on a metered account -> `POLICY_DENIED`; the same on a subscription account still grants; `metered`/`metered_advisory`/`shadow` on a metered account still grant (the negative control for the narrowed fix, see decision 1) |
| Fix round tk-7d7fd9023f8c | `FixRoundNonEnforcingLeases`, `FixRoundReadingAccountAuthorization`, `FixRoundWireAdapter`, `FixRoundIdempotencyKey`, `FixRoundPlanAndLimitEvents`, `FixRoundCheckRequestIdUuid`, `FixRoundIPv6Bind` (`tests/test_card5.py`) - one class per finding; see the Fix round section below for the finding -> fix -> test -> commit table |

## Evidence

- **Red first.** Against the pre-card-5 tree (implementation files
  stashed, new test files kept in place): `test_card5.py` fails at
  import (`AttributeError: module 'quota_balancer.errors' has no
  attribute 'CODE_UNAUTHENTICATED'`); `test_card4_seat_kind_fix.py`
  fails at fixture build (`AttributeError: module
  'quota_balancer.migrate' has no attribute 'MIGRATION_V6'`), 6 errors.
  Kept: `card-5/evidence/red-card5.txt`, `red-m4.txt`.
- **Green.** `test_card5.py`: 49 tests OK. `test_card4_seat_kind_fix.py`:
  5 tests OK.
- **Targeted regression.** `test_card4*.py` (the whole card-4 family,
  including the new m4 file): 59 tests OK — confirms the m4 fix and the
  v5->v6 migration bump broke nothing card 4 already pinned.
- **Mutation verification** (`card-5/mutate5.py`, two targets):
  `http_api.py` — 22 mutations (every auth/ownership/token/bind/body/
  error-shape branch), all killed. `lease.py`'s m4 guard — 4 mutations
  (dropped entirely; drops only `subscription_admission`; drops only
  `subscription_warn`; over-broadly also catches `shadow`), all killed.
  First sweep had 6 survivors (missing-Content-Length, non-dict body,
  oversized body, an added "debug" field in the error envelope, an
  uncaught non-`QuotaError` exception, and the `/check` body's own
  closed-shape check): one (`non-dict body allowed`) was a genuinely
  redundant check — every caller already narrows or defers to a store
  validator that rejects a non-dict document — and was deleted rather
  than defended; the other five got isolating tests (a raw-socket
  request with NO Content-Length header at all; a body lowered under a
  monkeypatched `MAX_BODY_BYTES` with no other reason to be a 400; an
  exact error-envelope key-set assertion; a monkeypatched handler
  raising a plain `RuntimeError`; a `/check` body with an extra key or a
  wrong-typed `request_id`). Final sweep: 0 survivors. Kept:
  `card-5/evidence/mutation-run.txt`.
- **Full suite**, once, foreground (`PYTHONPATH=<clone>`, `TEMP`/`TMP`
  under scratch): `Ran 447 tests in 146.7s ... OK` (393 before this
  card + 49 + 5). `git diff --check` clean.

## Decisions on ambiguities (design silent or ambiguous; fail-closed reading)

1. **m4, narrowed to `subscription_admission`/`subscription_warn`.** The
   carry-over names `subscription_admission`; the schema's enforcement
   enum also has `subscription_warn` (the same billing-model claim) and
   `shadow` (a generic observe-only mode with **no** billing-model claim
   of its own — section 6: "Old wrappers keep working in observe-only
   deployments and appear unmanaged/unmetered"). `shadow` on a metered
   account is already deliberately exercised by
   `test_card4_turn_c_fix.test_shadow_consumes_no_concurrency_slot`;
   blanket-rejecting it would have broken a previously-tested,
   previously-intentional configuration. The fix rejects exactly
   `subscription_admission`/`subscription_warn` on a `kind='metered'`
   account (POLICY_DENIED, in `lease.py`'s `_grant`, so it is closed
   before card 5 ever exposes a grant route) and leaves `shadow` alone.
   `metered`/`metered_advisory` are unaffected either way.
2. **`server_at`/`policy_revision` on every response, success or
   error.** Section 7 states this once, in prose, before the endpoint
   list, without repeating it per response shape and without saying
   what `policy_revision` even measures - no card built so far has a
   policy-revision concept. `SCHEMA_VERSION` is the closest honest,
   stable integer available; it is **not** a claim that it is the
   design's intended `policy_revision` source. A later card owning that
   concept should replace this.
3. **`windows`/`warnings` in the grant response.** Listed in section 7's
   response shape; no card built so far computes either for a fresh
   grant. Emitted as empty arrays (a documented gap, not a fabricated
   value) rather than omitted (closed-shape) or invented.
4. **~~The section-7 flat `/readings` body vs. the tested batch
   document~~ SUPERSEDED by the fix round (finding 3).** The first build
   kept exposing the internal batch document, reasoning that the flat
   wire shape had no store-backed path; the blind read correctly called
   this a DTO violation ("do not expose the offline import/request
   shape"), not an acceptable substitution. The fix round adds a real
   adapter (`_wire_to_reading`): the wire's flat `{event_id,account_id,
   seat_id?,observed_at,kind,payload}` is accepted as documented,
   `account_id` (a store UUID) is resolved to the alias the batch
   document needs, and `machine` is supplied implicitly as the
   authenticated caller's own alias (never a client-suppliable field -
   this both satisfies "owner reporter" by construction and closes the
   spoofing surface the original machine-field check depended on).
   `seat_id` is validated (null, or an existing seat) but still not
   persisted - `store._apply_reading` hardcodes `readings.seat_id` to
   `NULL`; no card built so far threads a seat_id through the reduction
   pipeline, and forking that tested pipeline is out of this fix round's
   scope. This remains the one exact missing dependency reported (not
   silently dropped): a later card that wants seat-level reading
   attribution must extend `validate_readings`/`store._apply_reading`.
5. **`/check`'s `request_id`.** `lease.check()` takes no body at all;
   `request_id` is section 7's documented shape, not something the
   store consumes for deduplication (check is idempotent by
   construction — no write, no cache). Validated for shape (a closed
   `{"request_id": <str>}`, and now a strict UUID - finding 7) and
   otherwise ignored, rather than invented a use for it.
6. **`/settle`'s path `{id}` vs. a body `lease_id`.** The fix round
   removes `lease_id` from the wire body entirely (finding 3): the URL
   path is the only source now, so there is no longer a place for the
   two to disagree. *(Corrected: the first build accepted a body
   `lease_id` alongside the path and rejected a mismatch; the blind read
   showed this made the documented body - which never had one - a 400.)*
7. **The wire's `final` must be a JSON boolean, not `0`/`1`.** The store
   explicitly rejects a Python `bool` (booleans-as-integers, section 7)
   for `final`; STEP-CARD-4.md says the wire's `true` converts in this
   layer. Read strictly: the wire must send a JSON boolean (`type(...)
   is not bool` is refused, matching this codebase's own "reject
   booleans-as-integers" idiom, mirrored the other way); an integer
   `0`/`1` on the wire is refused, not silently accepted as if it were
   already the store's own form.
8. **Settle ownership: the lease's own machine only, not "or its
   gateway's machine."** `grant()` itself refuses a gateway foreign to
   the seat's machine (section 4), so a lease's bound gateway is
   *always* on the same machine as its seat — there is no reachable case
   where they differ. (An earlier draft of this fix checked both; it was
   simplified once a test proved the "foreign gateway machine" branch is
   unreachable through any legitimate grant.)
9. **Body size bound: 65536 bytes, `INVALID_REQUEST` (400).** Section 7
   never states a bound or its wire status; a bound is still needed to
   avoid reading an unbounded body from an authenticated-but-hostile
   sender. 400 keeps this inside the design's own closed status set
   (no undocumented 413).
10. **A missing `Content-Length` is `INVALID_REQUEST`, not a
    chunked-transfer read.** This server does not implement chunked
    transfer encoding (not named anywhere in scope); a request that
    cannot declare its length is refused rather than read until the
    connection closes (which would let a slow/never-ending body hang a
    handler thread).
11. **`GET /v1/status` has no per-account restriction.** Any
    authenticated, enabled machine may call it — the row's own shape
    ("account windows/age/...", "gateway shares/balances", etc.) is
    inventory-wide, matching every other card's `status()` output, which
    has never been account-scoped.

## Fix round tk-7d7fd9023f8c (HOLD: 1 blocker, 4 major, 3 minor)

Branch `card-5`, still on top of the commits above. Blind read (codex),
full text at the review's own path (not this repo); the lead relayed all
8 findings. Each got a failing-first reproduction built from the
reviewer's own description, in `tests/test_card5.py` under one
`FixRound*` class per finding (the m4-adjacent lease.py work already had
its own file from the previous round and needed no changes here).

| # | Sev | Finding | Fix | Test(s) | Commit |
|---|---|---|---|---|---|
| 1 | BLOCKER | `metered_advisory`/`shadow` grants got a non-null token and could pass `/check` | `http_api.py`: token issuance gated on `enforcement not in _NON_ENFORCING` (not just `state != "settled"`); `/check` additionally rejects `enforcement in _NON_ENFORCING`, explicitly - not merely by relying on the token being null | `FixRoundNonEnforcingLeases` (4 tests: `metered_advisory` and `shadow` grants have no token and cannot check; the enforcement gate holds even with a stray matching token forced onto the lease directly in the store; the ordinary `metered` positive control is unaffected) | (this fix round) |
| 2 | MAJOR | A reporter's own machine identity does not authorize the ACCOUNT a `cli_window` reading names | `http_api.py`: `_authorized_reporter_for_account` (the same `project_accounts` chain `grant()` uses), gating `cli_window` specifically - `gateway_snapshot` (its reducer binds to the gateway's own machine+account) and `provider_billing` (the account's configured `reporter_machine`) already had their own | `FixRoundReadingAccountAuthorization` (3 tests: an unrelated account is refused, and no window row is written; the project's own account is accepted; a regression control that `gateway_snapshot` is unaffected - still refused by its OWN check, not the new one) | (this fix round) |
| 3 | MAJOR | The public DTOs did not implement section 7 (grant needed a store-only `request_hash`; readings only accepted the internal batch document; settle needed a body `lease_id`/`observed_at`/`received_at`) | `http_api.py`: `_wire_to_grant`/`_wire_to_settle`/`_wire_to_reading` - real wire<->store adapters, deriving `request_hash` (synthesized, never compared - see decision on this below), `lease_id`/`observed_at`/`received_at` (from the URL/server clock), and `machine`/account alias (resolved, never client-supplied) instead of exposing store internals | `FixRoundWireAdapter` (5 tests: the documented grant body, without `request_hash`, is accepted; the documented flat reading is accepted; the documented settle body, with only the URL `lease_id`, is accepted; settle's and reading's OWN closed-shape checks, isolated with an extra/missing key each - added after the mutation re-run) plus every other test in the file rewritten to the corrected wire shapes | (this fix round) |
| 4 | MAJOR | `Idempotency-Key` was never inspected; the same key across different bodies created two leases/holds | `http_api.py`: `_check_idempotency_key` - required, UUID-shaped, must equal the body's own `request_id`/`event_id`, checked BEFORE the store's own replay/conflict logic, on all four mutations (leases, settle, readings, limit-events) | `FixRoundIdempotencyKey` (8 tests: missing, malformed, disagreeing key; the reviewer's own repro - same key, two different `request_id`s, refused rather than silently making two leases; settle/readings/limit-events each require agreement too; a direct call isolating the format check from the agreement check - added after the mutation re-run, decision 18) | (this fix round) |
| 5 | MAJOR | `GET /v1/plan` and `POST /v1/limit-events` were absent (404); the demo's own block-sharing had no reporter path | `lease.py`: new `record_limit_event` (idempotent by `event_id`, joins - not duplicates - a live block via `INSERT OR IGNORE`, scoped by `model_id`); `http_api.py`: `handle_limit_event` (project_accounts-authorized) and `handle_plan` (profile lookup + `policy.select_tier` over blocked/offered models; `context` and `fallback` tiers explicitly reported as gaps - see decisions below, not silently omitted) | `FixRoundPlanAndLimitEvents` (10 tests, including a model-scoped-vs-account-wide block join isolation added after the mutation re-run) plus `Demo.test_a_limit_event_from_either_machine_parks_both` (the row's own demo, now reachable end to end) | (this fix round) |
| 6 | MINOR | An invalid HTTP method got an HTML 501 echoing the raw method text, bypassing the closed JSON/privacy handler | `http_api.py`: `RequestHandler.send_error` overridden - reads neither `message` nor `explain`, emits the same static, typed, closed envelope every other error uses | `Privacy5.test_protocol_error_bypasses_bypass_is_closed_json` (a raw socket sending an invalid method line, since no HTTP client constructs one) | (this fix round) |
| 7 | MINOR | `/check` accepted a non-UUID `request_id` | `http_api.py`: reuse `validate_readings._is_uuid` at the `/check` body-shape check | `FixRoundCheckRequestIdUuid` (and `WireContract.test_check_body_shape_is_closed_even_though_check_ignores_it` gained a non-UUID case) | (this fix round) |
| 8 | MINOR | `check_bind_host` accepted `::1`; `make_server` then raised a raw `gaierror` (AF_INET-only server) | `http_api.py`: `check_bind_host` requires `isinstance(addr, ipaddress.IPv4Address)` in addition to `.is_loopback` - IPv6 is refused as `BindRefused`, not a driver exception | `FixRoundIPv6Bind` (2 tests); `PublicBind.test_wildcard_and_public_hosts_refused_before_any_socket` gained `"::1"`/`"::"` | (this fix round) |

**New decisions from this round** (numbered continuing from the list
above, for cross-reference; design silent or ambiguous, fail-closed
reading):

12. **`request_hash` is synthesized, not derived from the wire body.**
    The store computes its own content hash over every request field
    EXCEPT the client-supplied `request_hash` and uses THAT for
    idempotency; the field only needs to be a well-formed 64-hex-char
    string to pass the store's own shape check. The adapter fills a
    fixed placeholder (`"0" * 64`) rather than hashing the wire body
    itself - hashing would be pure theater (the value is never compared
    to anything), and a fixed placeholder makes that explicit rather
    than implying a real hash is computed.
13. **`task_class` is accepted, shape-validated, and dropped.** No card
    built gives it an independent store column (STEP-CARD-4.md:
    "task_class has no design taxonomy in card-4 scope - the column
    stores the effort alias"); silently forwarding it as `task_class`
    would collide with what `_grant` already derives from `effort`.
    Accepting and discarding it (after validating it is at least
    alias-shaped, so a garbage value is still refused) is the least
    surprising reading until a card defines the taxonomy.
14. **`Idempotency-Key` applies to all four mutations, not only grant and
    settle.** Section 7 states the rule once, generically ("Mutations
    use..."); `/v1/readings` and `/v1/limit-events` are mutations with
    their own natural id (`event_id`) and got no exemption named
    anywhere.
15. **`/v1/plan` requires a `task_class` query parameter card 5 adds.**
    Section 7's own shown URL (`GET /plan?project_id=...`) is captioned
    "a placeholder, not a runnable command"; a `profiles` row is keyed by
    `task_class` with no other way to select one. Missing either query
    parameter is `INVALID_REQUEST` (400); an unknown project or
    `task_class` is `NOT_FOUND` (404).
16. **`/v1/plan`'s `context` is always `null`; `fallback` tiers are
    always `[]`.** No card built computes a project-scoped, request-time
    context outside of an actual `POST /leases` call (this endpoint
    takes no per-call token/mode input at all), and the `profiles` table
    has no fallback-tier column (card 3 never added one). Both are
    reported here and in Residual risk, not fabricated or silently
    dropped - exactly the lead's own fallback instruction for "section 7
    needs something no built card provides."
17. **A limit-event's block is account-wide or model-scoped, matching
    `blocks_live`'s own partial index; a second, different report for an
    already-blocked account/model JOINS the existing live block rather
    than erroring.** This is what makes the row's own demo (two machines
    share one block) reachable through a real reporter path: `INSERT OR
    IGNORE` followed by re-selecting the live row, mirroring
    `lease.py`'s own pre-existing `_receipt_conflict_park` pattern
    exactly, rather than inventing a new one.
18. **`_check_idempotency_key` validates the header's UUID format
    itself, even though every route's downstream store call also
    rejects a non-UUID id.** The reviewer's own wording names both
    checks ("validate the header format and its agreement..."). Once
    agreement is required, a key that disagrees is already refused
    regardless of format, and a key that AGREES with a non-UUID id will
    also be caught later, with the identical 400, by the store's own
    UUID validation on that same id - so from the wire, the format check
    looks redundant. It is kept anyway (explicit validation of a
    client-controlled header, matching this codebase's everywhere-applied
    "validate every input field" habit) and is verified by calling
    `_check_idempotency_key` directly rather than through the socket,
    since no wire-level input can isolate it from the downstream check.

**Evidence.**
- Red: with `http_api.py` and `lease.py` stashed back to c5cd023 (the
  pre-fix-round head) and the new test file kept, every `FixRound*` class
  failed or errored (29 tests, 4 failures + 22 errors) matching each
  finding's description (e.g. finding 1's tests errored on
  `IDEMPOTENCY_HEADER` not existing yet; finding 8's IPv6 test failed
  because `BindRefused` was NOT raised - the exact bug). Kept:
  `card-5/evidence/red-fixround.txt`.
- Green: all `FixRound*` classes plus the rest of the (substantially
  rewritten - see Files) `test_card5.py`: 85 tests OK (49 at c5cd023, +36
  net this round - not "+5": every fixture and call site in the file was
  rewritten to the corrected wire contract, not merely appended to).
- Targeted regression: `test_card4*.py` (the whole card-4 family,
  unaffected by this round's `lease.py` addition): 59 tests OK.
- Mutation re-run (`card-5/mutate5.py`, expanded for this round). A first
  attempt crossed two INDEPENDENT background `mutate5.py` runs writing
  the SAME target files concurrently (a self-inflicted process-management
  mistake, not a design finding); its output was corrupted (interleaved,
  garbled lines) and discarded outright - the runaway processes and two
  stray hung `quota_balancer serve` children were killed by exact PID,
  `http_api.py` was verified against a pre-sweep backup and found
  mutated on disk (restored), and the suite was re-confirmed green
  before any further mutation testing. The clean, single re-run: 45
  mutations, first pass 38 killed / 7 survived - `duplicate-key
  rejection bypassed` (the first build's own isolating test had not been
  re-run since it landed mid-sweep; confirmed killed alone), two closed
  -shape checks the wire adapters had no dedicated extra/missing-key
  test for (settle, reading), the idempotency-key FORMAT check
  specifically (agreement alone was tested; format needed a direct
  function-level call, since a wire-level non-UUID id also fails the
  store's own downstream UUID check with the identical 400 - decision
  18), and two `record_limit_event` behaviors (`test_card5.py` was not
  in the mutation harness's own test pattern for `lease.py` mutations -
  a harness bug, fixed) - the model-scoped block join and the
  account/model conflict check. Five isolating tests were added and the
  harness fixed (pattern list); clean re-run: 44 mutations, 1 survivor -
  `settle wire key-set check dropped`. Applied and re-verified BY HAND
  (not through the harness): the test suite stayed green even with the
  check removed, confirming it genuinely unreachable - `_wire_to_settle`
  copies the body through unchanged, and `lease.settle()`'s own
  `_validate_settle_receipt` independently re-checks the exact same
  closed shape with the identical error code (the same reason the
  neighboring `receipt.setdefault(...)` mutation was already ruled
  unreachable). Removed from the harness rather than defended with a
  test for unreachable code, leaving 43 mutations for the next run. The
  suite was confirmed green again after manually reverting that check.
  Kept: `card-5/evidence/mutation-run-fixround.txt` (the first, routing
  -incomplete sweep), `mutation-run-fixround2.txt` (the clean 44
  -mutation re-run with its one now-explained, now-removed survivor).
- Full suite, once, foreground (`PYTHONPATH=<clone>`): `Ran 483 tests in
  161.8s ... OK` (447 at c5cd023 + 36 net new, all in `test_card5.py`;
  `test_card4_seat_kind_fix.py` unchanged at 5). `git diff --check`
  clean.

**Record corrections.** Corrected in place, marked inline, rather than
silently rewritten: the "Ownership", "Lease tokens", "`public_bind`" and
"Wire shapes" paragraphs of "Design as implemented", and decisions 4, 6
and 8 (superseded/corrected above). The first build's Evidence section
(51/0/1 mutation counts, 447-test full suite) is historically accurate
for that build and is left as originally written.

## Residual risk

- `windows`/`warnings` in the grant response are structurally present
  but always empty (decision 3): a client that expects real window data
  there will not get it from this build.
- `policy_revision` (decision 2) is a placeholder, not the design's
  intended value (which no card has defined yet).
- The identity allowlist's threat model is intentionally cooperative
  (section 6's own scoping), not adversarial: any co-resident process
  that can reach the loopback port can present any enrolled machine's
  `identity_ref` header. A real deployment's actual security boundary is
  the tailnet ingress this card does not implement (no custom TLS
  stack, out of scope).
- (Fix round tk-7d7fd9023f8c) `task_class` (grant) and `seat_id`
  (readings) are accepted on the wire for shape compliance but have no
  independent store slot to land in - see the fix round's finding-3
  entry below for the exact missing dependency in each case.
- (Fix round tk-7d7fd9023f8c) `GET /v1/plan`'s `context` is always null
  (no card built provides a project-scoped, request-time context
  outside of an actual `POST /leases` call) and its `fallback` tiers are
  always `[]` (the `profiles` table has no fallback-tier column) - both
  reported gaps, not silent omissions; see finding 5 below.
- IPv6 loopback (`::1`) is refused outright rather than served (finding
  8): a real IPv6 deployment needs a second, dual-stack server this
  build does not provide.
