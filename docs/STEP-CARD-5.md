# Step record — Card 5: private HTTP

Work order: tk-671932fd4e85. Branch `card-5` off `1821a4e` (cards 1-4,
2b-1, 2b-2, 2b-2b merged). Date: 2026-09-26. Interpreter: CPython 3.14,
standard library only (`http.server`, `sqlite3`, `secrets`, `hashlib`,
`ipaddress`).

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
- `quota_balancer/lease.py` — the m4 carry-over fix (below), plus
  renaming a discarded `_kind` local to `account_kind` (used by the fix).
- `quota_balancer/errors.py` — `CODE_UNAUTHENTICATED`/`CODE_FORBIDDEN`
  (HTTP-transport-only; not in `EXIT_CODES`, there is no CLI request to
  authenticate).
- `quota_balancer/cli.py` — `identity-set` (wraps `set_machine_identity`)
  and `serve` (blocking; wraps `make_server(...).serve_forever()`).
- `tests/test_card5.py` (49 tests), `tests/test_card4_seat_kind_fix.py`
  (5 tests, the m4 fix, store-level only).
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
lease for someone else's seat. The HTTP layer adds exactly one rule per
route: grant — the seat's own machine; check — the lease's own machine
**and** its current lease token; settle — the lease's own machine (no
token: "Reporter identity, not a wrapper token alone, authorizes money
settlement", section 6); readings — every declared reading's `machine`
equals the caller's own alias ("owner reporter").

**Lease tokens.** `leases.token_hash` exists in the schema but no store
function minted it (STEP-CARD-4.md: "token_hash is card 5"). This module
mints a fresh 256-bit token (`secrets.token_hex(32)`, sha256-hashed) on
every grant response whose state is not already `settled` — fresh or
replayed — and stores only the hash. A terminal replay (an
already-settled lease) returns no token.

**`public_bind`.** `check_bind_host` parses the host as a literal IP and
requires `.is_loopback`; a bare hostname (including `"localhost"`) is
refused too — trusting it would add a DNS/hosts-file dependency this
product does not have. `make_server` calls it before constructing
`ThreadingHTTPServer`, so a public/wildcard bind never opens a socket.

**Wire shapes.** `/v1/leases` 201 (fresh) / 200 (replay) drops the
store's internal `replayed` flag and adds `lease_token`; `windows`/
`warnings` are emitted empty (not fabricated — no card built so far
computes them, decision 3). `/check` is sliced to exactly
`{state,expires_at,caps}`; `/settle` to exactly
`{state,released_micro_eur,retained_micro_eur}`. Every response also
carries `server_at`/`policy_revision` (decision 2). Errors are
`{error:{code,message,retry_at,details:{}}}` plus the same envelope
fields — `details` is always `{}`; nothing computed here ever reaches
the wire unfiltered.

## Acceptance rows -> tests

| Row | Tests |
|---|---|
| `token_scope` | `TokenScope` (`tests/test_card5.py`): check denies a different machine even with the right lease id; check denies the right machine with a stale/missing/wrong token; readings denies a foreign machine (and accepts its own); settle denies a machine that owns neither the lease nor the request; settle accepted from the lease's own machine |
| `replay_grant` | `ReplayGrant`: replay rotates the token and adds no new hold row; a terminal (already-settled) replay returns the settled state with no token; different content under the same ids is `IDEMPOTENCY_CONFLICT` |
| `public_bind` | `PublicBind`: wildcard/public/hostname/malformed hosts refused (pure `check_bind_host`, table-driven); loopback hosts accepted; `make_server` refuses a public bind before any socket exists; the default host is loopback and actually serves |
| `privacy_5` | `Privacy5`: injected secret/path/provider-ID sentinels never appear in any error body across every route; `/v1/status` is JSON-only and never echoes a sentinel identity; the error envelope is a closed shape (no stray debug field); a genuine un-typed internal exception is still caught and never leaks; a corrupt (non-sqlite) store file leaks nothing |
| Also cover: 401 vs 403 | `IdentityAuth`: missing/empty header -> 401; unknown/disabled machine -> 403; a grant for a seat owned by a different machine -> 403 |
| Also cover: 503 | `StoreUnavailable`: a missing db file -> 503 `STORE_ERROR` |
| Also cover: body bounds, malformed/duplicate JSON | `BodyBounds`: oversized body (both a naturally-oversized body and one isolated from any other 400 reason via a lowered bound); missing/empty Content-Length (both the empty-string and the genuinely-absent-header cases, isolated with a raw socket); malformed JSON; duplicate JSON key; a non-object top-level JSON value |
| Demo: two machines share one block | `Demo`: two authenticated machines on two seats of the same account both get `ACCOUNT_PARKED`; clearing the block unblocks both |
| (supplemental) wire contract | `WireContract`: closed response shapes; the wire's JSON `true` for `final` converts to the store's `1` (and a plain `int` on the wire is refused, not silently accepted); path/body `lease_id` must agree; `INPUT_LIMIT`/`MONEY_LIMIT`/`NOT_FOUND`/unrouted-path mappings; the `/check` body shape is enforced even though `lease.check` itself never reads it |
| (supplemental) enrollment | `IdentityEnrollment`: `store.set_machine_identity` enrolls/updates/disables; rejects a duplicate `identity_ref`; rejects an unknown machine; a machine may reclaim its own value; the CLI `identity-set` and `serve` verbs, end to end (`serve --host 0.0.0.0` still refuses over a real subprocess) |
| m4 carry-over | `tests/test_card4_seat_kind_fix.py`: `subscription_admission`/`subscription_warn` on a metered account -> `POLICY_DENIED`; the same on a subscription account still grants; `metered`/`metered_advisory`/`shadow` on a metered account still grant (the negative control for the narrowed fix, see decision 1) |

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
4. **The section-7 flat `/readings` body vs. the tested batch
   document.** Section 7 shows `{event_id,account_id,seat_id?,
   observed_at,kind,payload}` (implying the machine is implicit from the
   caller's identity and never on the wire at all); the only tested,
   store-backed path is the card-2/2b-1 **batch** document
   (`{version,readings:[{event_id,machine,account,kind,observed_at,
   payload}]}`), which names its own `machine` per reading and has no
   `seat_id` concept. Reusing the tested path rather than forking new
   validation, `/v1/readings` accepts that batch document unchanged,
   with one addition: every declared `machine` must equal the caller's
   own alias. The flat single-reading shape and `seat_id` attribution
   are not reachable through this build.
5. **`/check`'s `request_id`.** `lease.check()` takes no body at all;
   `request_id` is section 7's documented shape, not something the
   store consumes for deduplication (check is idempotent by
   construction — no write, no cache). Validated for shape (a closed
   `{"request_id": <str>}`) and otherwise ignored, rather than invented
   a use for it.
6. **`/settle`'s path `{id}` vs. the body's own `lease_id`.** Section 7
   shows both. A mismatch is `INVALID_REQUEST` (400) rather than
   silently preferring one.
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

## Residual risk

- `windows`/`warnings` in the grant response are structurally present
  but always empty (decision 3): a client that expects real window data
  there will not get it from this build.
- The flat single-reading `/readings` wire shape and `seat_id`
  attribution (decision 4) are not implemented; only the batch document
  is reachable.
- `policy_revision` (decision 2) is a placeholder, not the design's
  intended value (which no card has defined yet).
- The identity allowlist's threat model is intentionally cooperative
  (section 6's own scoping), not adversarial: any co-resident process
  that can reach the loopback port can present any enrolled machine's
  `identity_ref` header. A real deployment's actual security boundary is
  the tailnet ingress this card does not implement (no custom TLS
  stack, out of scope).
- No HTTP integration exists for `/limit-events` or `GET /plan`: no
  store function backs either yet in the built cards.
