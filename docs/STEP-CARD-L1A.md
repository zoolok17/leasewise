# Step record — Card L1a: policy import, safe grants

Work item `card-l1a`, three turns on ONE branch (`card-l1a`, off `fbf5501`),
one cold read at the end. Approved by the desktop lead 2026-10-02
(re-baseline decision D-005). Interpreter: CPython 3.13 (`C:\lw\venv`),
standard library only. Turns 2 and 3 append to this record.

- turn 1 (this section): `config-import` version 2 (the seven policy
  tables) and the R-003 interim fix;
- turn 2: task_class carried into the store, profile applied at grant;
- turn 3: safe block clear/probe (N3), the expiry sweep, the test temp-dir
  leak.

## Turn 1 — work order tk-d3489e87536f

Nothing could load seats, models, profiles, projects, teams, limits and
project-account links except raw SQL in tests. Turn 1 gives the operator
that path through the existing `config-import` verb, and closes register
R-003 (a gateway's own `sized_ready` report could switch a metered seat to
ENFORCING metered leases). No new CLI verb, no HTTP route, no migration.

### Files

- `quota_balancer/validate_config_v2.py` — new. Pure validation of a
  version-2 document: closed shapes, alias references, value rules. Reuses
  `validate.validate` (the inventory part, unchanged) and
  `policy._validate_model` / `policy._validate_limit` (their
  INVALID_REQUEST is re-raised as INVALID_CONFIG). Owns `PLATFORM_ID`.
- `quota_balancer/policy_config.py` — new. Applies the validated policy to
  the seven tables inside `config-import`'s transaction (upsert discipline).
  No file, environment or network access (a test pins this).
- `quota_balancer/store.py` — `import_config` dispatches on `version == 2`;
  `_upsert` takes an optional policy document and applies it after the
  inventory upserts and before the inventory deletions.
- `quota_balancer/lease.py` — the R-003 gate (`METERED_ENFORCEMENT_ACTIVATED`)
  and `_PLATFORM_ID` now imported from `validate_config_v2` (one definition).
- `tests/test_cardl1a.py` — 35 tests. Changed older tests: below.
- `docs/STEP-CARD-L1A.md`, README section "Card L1a (turn 1)".

### Design as implemented

**Document version 2.** The version-1 keys (`accounts`, `machines`,
`gateways`, validated by the unchanged version-1 validator) plus `projects`,
`teams`, `project_accounts`, `seats`, `models`, `profiles`, `limits`. Every
key is required (an empty list is fine), no extra key at any level
(a duplicate key in the JSON is refused as before). Version 1 imports exactly
as today and never touches the seven tables; an unknown version (0, 3, "2",
2.0, true, null) is `SCHEMA_UNSUPPORTED`; a document without a version is
refused as before (`INVALID_CONFIG`).

**References are aliases.**
- team `{project, alias}`; project_account `{project, account}`;
- seat `{team: {project, team}, machine, alias, role, enforcement}`;
- model `{account, alias, ...}`;
- limit: flat object with `scope` and the path keys of that scope:
  `platform` (none), `project` (`project`), `team` (`project`, `team`),
  `seat` (`machine`, `seat`), plus `metric, period, window_key, soft, hard`.
  A seat is identified by `(machine, alias)`, the schema's own key.
  The platform scope stores `00000000-0000-0000-0000-000000000000`, the id
  grant's chain carries.
No UUID is ever read from the document; a UUID-shaped reference is just an
unknown alias.

**Values.** Seat `enforcement` is the schema's closed enum, `role` an alias.
A model is checked by `policy._validate_model` (ceilings, rates, reserve >=
tariff, `sized|fixed`, 64 lowercase hex hash); `effort_aliases` is a
non-empty list of distinct aliases stored comma-joined (the form grant
splits on). A limit is checked by `policy._validate_limit` (metric/period
pairing, `window_key` only for `window_bps`/`window`, soft <= hard, bps <=
10000, int64, bool-as-int refused). Profile numbers follow the schema CHECKs
(`expected_calls` 1..100, `margin_bps` 10000..20000, `max_calls` 1..100,
`ttl_ms` > 0, the rest >= 0, int64).

**Profile JSON.** `candidates` is a non-empty ordered list of distinct model
aliases, optionally ending with `"park"`; each must name an imported model;
model aliases are unique across ALL accounts in version 2. `context_rules`
and `calibration` must be exactly `{}` (reserved for later cards) and are
stored as `{}`.

**Upsert discipline.** One transaction (the existing `BEGIN IMMEDIATE`):
inventory upserts -> policy upserts (projects, teams, project_accounts,
seats, models, profiles, limits) -> policy removals (limits, seats, teams,
project_accounts, profiles, models, projects) -> inventory removals. A
retained alias keeps its UUID and gets config-owned columns updated only
(`seats.last_seen_at` and every other operational column are never written).
A removed alias is deleted only when unreferenced, otherwise
`INVALID_CONFIG`, nothing changed (the whole transaction, inventory included,
rolls back):
- seat: referenced by any lease;
- model: any lease, block or limit_event;
- limit: any hold row (`holds.limit_id`);
- team: a remaining seat; project: a remaining team or project_account;
- account/machine removal keeps its existing guards, which now see the
  post-policy state (so dropping an account with its models and project links
  in one document works; dropping a machine whose seat a lease uses refuses).
Limits are identified by the section-7 uniqueness key
(scope, scope_id, metric, period, window_key) and the document replaces the
whole set. Profiles are keyed by `task_class`; the profile table has no
referencing rows.

**R-003.** `lease._grant`: a seat whose `enforcement` is `metered` is granted
`metered_advisory` unless `sized_ready AND METERED_ENFORCEMENT_ACTIVATED`; the
constant is `False` and nothing in the product sets it. `sized_ready` is still
stored as telemetry; the reading validator is untouched. The code comment says
the gate is replaced by card R1's explicit activation gate and that metered
leases stay advisory until then (desktop lead's decision). A metered seat with
no gateway is still `INVALID_REQUEST`, as before.

### Rows -> tests (`tests/test_cardl1a.py`)

| Row | Tests |
|---|---|
| policy_import_roundtrip | `PolicyImportRoundtrip`: every table populated and stored the way grant/plan read it; re-import (and a new process) is a byte-identical no-op; changed config columns update in place with the same UUIDs while `last_seen_at` survives; inventory state (coverage) survives; empty lists valid; a seat moves team keeping its id |
| policy_import_rollback | `PolicyImportRollback`: 82 single-fault documents (bad references of every kind, extra/missing keys at each level, bool-as-int, floats, ranges and overflow, unknown enums, list/dict where an alias goes, bad hashes/aliases/efforts, non-empty `context_rules`/`calibration`, bad `candidates`, ambiguous and duplicate aliases, every duplicate, inventory rules) each exit 2 with the static error and an unchanged store; an empty store stays empty; versions 0/3/99/"2"/2.0/true/null -> `SCHEMA_UNSUPPORTED`; a failure during apply rolls the inventory additions of the same document back; malformed files |
| policy_import_referenced_removal | `PolicyImportReferencedRemoval`: referenced seat / model (lease), model (block), limit (hold, needs the gate patched), machine-with-referenced-seat refuse and change nothing; unreferenced seat, model, limit, project_account, then the whole policy, are deleted with other UUIDs retained; dropping an account takes its models and links |
| version1_still_imports | `Version1StillImports`: v1 on a fresh store leaves the seven tables empty; v1 after v2 leaves them byte-identical; v1 still refuses to strand a policy row; v1 with a policy key is refused; no version refused as before |
| grant_after_import | `GrantAfterImport`: a lease is granted through `lease.grant` with rows that came only from the import (no SQL insert); an imported platform limit is read through `PLATFORM_ID` and a re-import changes the outcome; the project-account link and effort aliases matter; a shadow seat on another machine |
| r003_metered_never_enforcing | `R003MeteredNeverEnforcing`: after a `sized_ready=true` snapshot a metered seat gets `metered_advisory`, null `money_hold`, no token, no hold rows; HTTP: 201 advisory with `lease_token` null and `/check` 403; the gate is the only switch (patched open -> old enforcing result, so the test fails without the fix); gate open but not sized -> advisory |
| privacy_l1a | `PrivacyL1a`: four sentinels (secret, Windows path, POSIX path, key-like) in every string slot, alone and together, never in stdout/stderr or the store; a missing file's path is not echoed; the new modules do no file/env/network access |

### Pre-existing tests I changed, and why (decision 6)

No assertion was weakened or removed; every changed test keeps its intent.
All tests below drive enforcing metered leases (holds, tokens, settlement,
expiry), which after R-003 are reachable only through card R1's future gate.
They now open the interim gate for their own duration with
`mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True)` in
`setUp` (stopped by `addCleanup`), i.e. they exercise the grant's real
post-activation arithmetic through the real `lease.grant`. Their fixtures
still set `sized_ready` by SQL, as before, and still need it (gate AND
sized-ready both required).
- `tests/test_card4_ops.py` (also `_race_worker`, which sets the module
  constant itself because a spawned process does not inherit the patch),
  `test_card4_expiry.py`, `test_card4_settle_fix.py`,
  `test_card4_turn_c_fix.py`, `test_card4_turn_d_fix.py`,
  `test_card4_enforce_fix.py`: store-level hold/settle/expiry/limit tests.
- `tests/test_card5.py` (`_ServerCase.setUp`, so every HTTP test): the
  in-process server shares the patched module. Its negative tests
  (`gateway_sized=0` -> advisory, shadow, non-enforcing leases cannot
  /check) are unaffected: they pass with the gate open or closed.
- `tests/test_card4_seat_kind_fix.py` also sets `sized_ready` by SQL but
  passes unchanged (its assertions are about subscription seats on a metered
  account), so it is not patched.
- `tests/test_card1.py::test_schema_version_rejected`: it used `version: 2`
  as its example of an unsupported version. Version 2 now exists, so a
  v1-shaped document claiming 2 is a bad document (`INVALID_CONFIG`,
  asserted) and the first unsupported version is 3 (`SCHEMA_UNSUPPORTED`,
  asserted); the `"1"` and `true` cases are unchanged.

### Evidence

- **Red first.** Commit `42d9f8a` (tests only), run in a detached worktree at
  that commit: `python -m unittest tests.test_cardl1a
  tests.test_card1.Card1ContractTests.test_schema_version_rejected` ->
  `Ran 36 tests ... FAILED (failures=29, errors=1)`. Failing: every roundtrip,
  rollback, referenced-removal, privacy, R-003 and grant-after-import test,
  the two v1-after-v2 tests and `test_schema_version_rejected`; the one error
  is the source-scan test, which cannot open the not-yet-existing
  `validate_config_v2.py`. Of the 36, 6 pass on main (assertions that hold trivially there, e.g. a
  v1 document with a policy key is refused, a missing file is refused). Output kept in
  the seat scratch only until this reply.
- **Green.** `tests.test_cardl1a`: 35 tests OK. Full suite `python -m unittest discover -s tests`: `Ran 555 tests in 250.6s ... OK` (520 on main + 35 new; an earlier full run without the test setUp patches had 27 failures and 1 error, the list that drove the changes above). Mutation spot-check of the new code: 11 single-point mutations (R-003 gate, seat/limit/model reference guards, apply skipped, ambiguous-model-alias check, `context_rules` check, retained team UUID, `park` stripping, platform id) all killed by `test_cardl1a`. The README example document imports (exit 0).

### Decisions (the lead's seven, then mine)

1. Version 2 as specified (all keys required, closed shapes, v1 unchanged,
   unknown version `SCHEMA_UNSUPPORTED`).
2. Alias references; a limit's scope is `{scope, ...alias path}` as above.
3. Closed value checks reuse the card-3 validators.
4. `candidates` / `context_rules` / `calibration` as specified.
5. Upsert discipline as specified (one transaction, retained UUIDs,
   referenced removals refuse).
6. R-003 interim fix as specified, via `METERED_ENFORCEMENT_ACTIVATED`.
7. No HTTP route, no CLI verb. **`status --json` is left alone**: a closed
   alias-only projection of seven tables is not small, and nothing in this turn
   needs it; say if you want it in a later turn.

Mine:

8. **A trailing `"park"` is accepted but not stored.** `http_api`'s plan reads
   `profiles.candidates` as a JSON list of model aliases and hands it to
   `policy.select_tier`, which rejects `"park"` inside the candidates (it is the
   fallback sentinel). Storing it would break `/v1/plan`. The terminal park is
   implicit (every candidate blocked parks), so only model aliases are stored,
   e.g. `["opus","fast"]`. A re-import is therefore a no-op. The migration
   comment ("last tier is 'park'") is stale for the same reason. Flag if you
   want `"park"` kept in the column and `select_tier`/`plan` changed instead.
9. **Seat identity is `(machine, alias)`**, the schema's UNIQUE key, so a seat
   changing machine is a removal plus an addition; changing team, role or
   enforcement is an in-place update. A `seat` limit path is therefore
   `{machine, seat}`.
10. **`PLATFORM_ID` moved** to `validate_config_v2` and `lease._PLATFORM_ID`
    imports it (`store` cannot import `lease`: `lease` imports `store`).
11. **Limit deletion** refuses when ANY hold row names the limit (holds of
    settled leases are history a settle may still need), so a limit that was
    ever used is soft-removed only by setting its `hard`; this is the safe
    reading of "referenced by ... holds ... limits".
12. **`limit_events`** (a table the brief did not list) also references
    models; a model one points at cannot be removed.
13. **Gate as a module constant, patched by tests.** The alternative (a new
    argument on `grant`) would change the store API for a gate card R1 will
    redesign; a constant is the smallest honest placeholder, with one
    definition and one comment.
14. **A document with a policy section replaces the whole policy set**: absent
    from the document means removed (when unreferenced). That is the version-1
    semantics extended, not a merge.

### Judged not testable

- Concurrent imports racing (the existing `BEGIN IMMEDIATE` serialises them;
  not new here).
- Card R1's real gate: only the interim constant exists, and its closed state
  is what is tested.

### Residual risk

- The imported `limits` replace the whole set: an operator who forgets a limit
  in the document silently loses that cap (unless a hold references it, then
  the import refuses). A later `status` projection or a dry-run verb would
  help; not in this turn.
- `candidates` without the stored `"park"` (decision 8) is a deliberate
  departure from the migration's comment; the lead owns that call.
- Metered seats are advisory in production until R1: no money hold, no token.
  That is the requested behaviour, but any deployment relying on enforcing
  metered leases stops getting them.
- Retained-alias semantics for models: effort aliases and rates update in
  place, so an in-flight lease keeps the policy snapshot it was granted under
  (its stored `policy_snapshot`), not the new numbers.

## Turn 2 — work order tk-63885b367128

The wire accepted `task_class` and dropped it, the store put the effort alias
in the `task_class` column, and grant never read the profile. Turn 2 makes the
profile the server-side policy of every grant (DESIGN.md sections 6-7). Same
branch, on top of the turn-1 head `6096030`.

### Files

- `quota_balancer/lease.py` — `task_class` joins the closed grant request;
  the profile is read inside the grant transaction after the replay check;
  candidate and ceiling checks; `margin_bps` from the profile; `task_class`
  stored; `policy_snapshot` also records `task_class` and `policy_revision`.
- `quota_balancer/http_api.py` — `_wire_to_grant` keeps `task_class` (it was
  validated and dropped).
- `quota_balancer/migrate.py` — comment only (the stale "last tier is 'park'");
  the DDL strings are byte-identical (`MIGRATION_V3` unchanged).
- `tests/test_cardl1a_t2.py` — 22 tests. Changed older tests: below.
- `docs/STEP-CARD-L1A.md`, README section.

### Design as implemented

Order inside `_grant`, after the existing seat/team/project/model/account
lookups and the project-account check, and AFTER the replay check:
1. profile by `request["task_class"]`; none -> `_not_found()` (the same error
   as an unknown model id);
2. the requested model's alias must be in the profile's `candidates` (JSON list
   of aliases; `park` is implicit and never stored) else `POLICY_DENIED`;
3. each of `expected_calls`, `max_calls`, `output_cap`, `per_call_output_cap`,
   `ttl_ms` must be <= the profile's value else `POLICY_DENIED`;
4. `policy.estimate(..., margin_bps)` with the profile's `margin_bps`; the
   request has no way to set it (the closed request has no such key).
The absolute bounds are untouched and still run first for the request's own
shape (N<=M<=100, the 24 h TTL maximum, the model ceilings). The replay path is
unchanged and returns the stored lease before any profile access, so a profile
edited or deleted after a grant never changes or breaks a granted lease.
`task_class` is part of the server-side content hash (it hashes every request
field but `request_hash`), so the same `request_id` with another class is
`IDEMPOTENCY_CONFLICT`. `leases.task_class` holds the class; the effort stays
in `policy_snapshot`.

### Decisions (the lead's eight, then mine)

1. task_class carried end to end, hashed, stored; snapshot gains `task_class`
   and `policy_revision`; store callers pass it (closed request shape).
2. Unknown class: `_not_found()` after the replay check.
3. Candidate check -> `POLICY_DENIED`.
4. Profile margin replaces the default.
5. Five ceilings, request <= profile, else `POLICY_DENIED`.
6. Replay first; pinned by a store and an HTTP test.
7. `migrate.py` comment fixed, DDL untouched (checked).
8. Fixtures grown a profile (list below).

Mine:
9. **Failure order** when several rules break: unknown class (404) -> candidate
   -> ceilings (both 403/POLICY_DENIED) -> parked block -> estimate freshness
   -> estimate arithmetic. A test pins that an unknown class beats a ceiling
   breach.
10. **A request's own absolute bound beats the profile**: a request above the
    24 h TTL maximum or with `max_calls` > 100 is `INVALID_REQUEST` even when
    the profile (config-import accepts up to `ttl_ms` int64, calls <= 100) would
    allow it; this is the existing static validation, unchanged.
11. **No profile lookup on a replay** also means a replay of a lease whose class
    was later deleted still succeeds; only new requests meet the new profile.
12. **A corrupted `candidates` value** (not a JSON list) is a store fault
    (`STORE_ERROR`), not a policy denial: config-import v2 can never write one.

### Rows -> tests (`tests/test_cardl1a_t2.py`)

| Row | Tests |
|---|---|
| task_class_stored | `TaskClassStored`: the class (not the effort) is in `leases.task_class`; the snapshot has effort, task_class and the profile's `policy_revision` (and follows a bump); `task_class` is required and an alias in the closed request |
| unknown_task_class | `UnknownTaskClass`: identical code and message to an unknown model id; no lease row; an empty profile table refuses every class |
| candidate_denied | `CandidateDenied`: a model outside the candidates -> `POLICY_DENIED`, a listed one is granted; every listed candidate works; a model that belongs to another class's profile is denied here |
| profile_margin_applied | `ProfileMarginApplied`: pinned numbers from the card-3 arithmetic (E=1000: margin 12500 -> I=1250, H=2253; 20000 -> I=2000, H=3003; 10000 -> I=1000, H=2003) equal the lease caps, the stored estimate and the snapshot; the request cannot carry a margin |
| profile_ceilings | `ProfileCeilings`: each of the five fields at the profile value is granted and at +1 is `POLICY_DENIED`; less than the profile is granted; raising the profile raises the ceiling; absolute bounds still apply above a generous profile; unknown class beats a ceiling breach |
| replay_after_profile_edit | `ReplayAfterProfileEdit`: after tightening the profile (candidates, margin, ceilings, revision) and after deleting it, the identical request replays the identical stored lease, rows unchanged; a new request meets the new profile |
| content_hash_includes_task_class | `ContentHashIncludesTaskClass`: same request_id, other class -> `IDEMPOTENCY_CONFLICT`, one lease row; the identical request replays |
| HTTP path | `HttpPath`: `/v1/leases` on rows imported by config-import v2 returns 201 and stores the class; 404 unknown class, `POLICY_DENIED` for candidate and TTL; replay after the profile was deleted returns 200 with the same lease |

### Pre-existing tests changed, and why (decision 8)

No assertion weakened or removed.
- `tests/test_card4_ops.py`, `test_card4_expiry.py`, `test_card4_settle_fix.py`,
  `test_card4_turn_c_fix.py`, `test_card4_enforce_fix.py`,
  `test_card4_seat_kind_fix.py`: their `_grant_request` helper now carries
  `"task_class": "bounded_read"` (the request shape is closed) and their
  `_build_store` fixture inserts one SQL profile row with that class,
  candidates = every model alias of the fixture and the values the requests
  already used (1 call, 2 max, 1000 / 500 output, 30 min, margin 12500), so
  every number they assert (holds, caps) is unchanged. They are SQL because
  these fixtures build a store at a fixed schema version with raw rows (they
  predate config-import v2 and test hand-built states such as a v4 store).
  `test_card4_turn_d_fix.py` shares turn-c's helper.
- `tests/test_card4_turn_d_fix.py::test_ttl_24h_boundary` and
  `tests/test_card4_enforce_fix.py::test_ttl_ms_bounded`: they test the 24 h
  ABSOLUTE TTL maximum, which the 30-minute profile now caps first. Each now
  raises its fixture profile's `ttl_ms` to 86_400_000 (one `UPDATE` after the
  store is built) so the same boundary is exercised with the same
  assertions.
- `tests/test_card5.py::test_grant_failed_identity_write_rolls_back_the_whole_mutation`:
  its "changed body" used `task_class="an_alternate_task_class"`, which now
  (correctly) names no profile and is a 404. The changed body now differs in
  `output_cap` (one lower, within the profile); the idempotency intent (a
  different body under the same key is a fresh first request, the original then
  conflicts) and every assertion are unchanged. The card-5 fixture already had a
  `bounded_read` profile. The second card-5 edit is
  `test_concurrent_grant_same_key_different_body_gets_one_conflict` (same
  reason, same fix). A card-5 wire-identity test that replays a key with a
  nonexistent class still gets its 409 from the wire layer and is unchanged.
- `tests/test_cardl1a.py`: its request helper carries `task_class`
  (turn-1 tests; its imported base document already has the profile).

### Evidence

- **Red first.** Commit `5552ad0` (tests only, on the turn-1 production code),
  run in a detached worktree: `python -m unittest tests.test_cardl1a_t2` ->
  `Ran 22 tests ... FAILED (failures=7, errors=11)`: 18 of 22 fail (the closed
  request has no `task_class`, no profile is consulted).
- **Green.** `tests.test_cardl1a_t2`: 22 tests OK. Full suite `python -m unittest discover -s tests`: `Ran 577 tests in 234.3s ... OK` (555 after turn 1 + 22). Before the fixture edits the older files had 13 failures and 54 errors; that list drove the changed-tests section above. A racy card-5 test (`test_concurrent_grant_same_key_different_body_gets_one_conflict`) whose second body used an unknown task_class now differs in `output_cap` (same intent: one 201, one 409).
- Mutation spot-check: 8 single-point mutations of the lease change (unknown
  class check, candidate check, ceiling comparison, the TTL ceiling, margin,
  stored task_class, snapshot revision, hash excluding task_class) all killed.

### Judged not testable

Concurrent profile edits against a grant (both run under BEGIN IMMEDIATE, so
they serialise; not new).

### Residual risk

- The wire body still has no way to name a profile revision: a client that
  planned under revision N and is granted under N+1 sees only the new ceilings
  (a denial is `POLICY_DENIED` with no detail). `/v1/plan` returns the same
  caps, so a client can re-plan.
- Seven older fixtures grew a SQL profile row; if the profile columns change
  (turn 3 or R1), those fixtures need the same edit.
