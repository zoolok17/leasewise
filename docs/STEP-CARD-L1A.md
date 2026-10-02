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

## Turn 3 — work order tk-aa1b48b41f77

In plain words: two operator tools were missing. A block that stops an
account (after a provider usage limit or a money overrun) could only be removed
by editing the database, and leases whose time ran out stayed "active" for
ever because nothing called the expiry rule. Turn 3 adds `block-clear`, which
is audited and refuses to clear an account that still has an unexplained
overrun, and `expire-sweep`, which moves expired leases to "uncertain" without
refunding or freeing anything. It also stops the card-5 tests leaving temporary
folders behind. There is no "probe" command: setting a block to `probing` by
hand is not the single-probe protocol the design needs, so that is a later card.

### Files

- `quota_balancer/lease.py` — `block_clear`, `expire_sweep` (plus a small
  shared `_mark_uncertain` that `expire` now uses too), and the overrun path.
- `quota_balancer/cli.py` — the verbs `block-clear` and `expire-sweep`.
- `tests/test_cardl1a_t3.py` — 30 tests. `tests/test_card5.py` — temp-dir clean-up.
- `docs/STEP-CARD-L1A.md`, README.

### Design as implemented

**`block-clear --account A [--model M] --reason R`** (CLI only, no HTTP route).
`R` is one of `reset_observed`, `false_positive`, `operator_override`; it is
checked in the function (not by argparse `choices`, which would echo a rejected
value back). One `BEGIN IMMEDIATE` transaction:
1. unknown account or model alias -> `NOT_FOUND`;
2. find the live (`parked` or `probing`) block of exactly that scope (the
   account-wide block when no model is named; a model's own block when one is);
3. none live: if a cleared block exists for the scope, report
   `{"state":"cleared","replayed":true}` and change nothing (no second audit
   row); if none ever existed, `NOT_FOUND`;
4. **the N3 rule:** scan EVERY adjustment of the account with
   `resolved_at IS NULL` (not only the block's `source_event_id`); any one
   refuses with `ACCOUNT_PARKED` and changes nothing;
5. otherwise set the block `cleared`, `updated_at` = now, and write one audit
   row: actor `operator`, action `block_cleared`, object the block id, details
   exactly `{"reason": ..., "previous_state": "parked"|"probing"}`.
Output `{"state":"cleared","replayed":false}`; no ids or aliases.

**The overrun path.** When a final receipt overruns a money hold, the account
is parked as before, but the old `INSERT OR IGNORE` is replaced: if a live
account-wide block exists, a `parked` one stays parked (and keeps its source
event), a `probing` one becomes `parked` again (retry time cleared,
`updated_at` = the settle time); if none exists a new parked block is created.
Every overrun writes one audit row `overrun_block` (object the block id,
details `{"event_id": ..., "previous_state": null|"parked"|"probing"}`), so
overruns that share one block stay traceable. Model-scoped blocks are separate
(the overrun is account-wide). The limit-event block path is unchanged.

**`expire-sweep`.** One transaction: every `active` lease with
`expires_at <= now` becomes `uncertain` (the existing `expire` rule, one audit
row each, holds and live slot kept); a second run finds nothing. The CLI uses
the wall clock; `lease.expire_sweep(path, now_ms)` takes the time as an
argument. Output `{"expired": N}`. It never settles, refunds or deletes. A late
final receipt can still settle an uncertain lease (tested).

**N-001.** The card-5 helpers that used `tempfile.mkdtemp` (the two v6-store
builders) never removed their folders; each now registers
`shutil.rmtree` as a test cleanup.

### Decisions (the lead's five, then mine)

1. `block-clear`: CLI-only, closed reason enum, audit row as specified.
2. The N3 rule as specified; refusal code **`ACCOUNT_PARKED`** (exit 2, static
   message "the account is parked"). Why this existing code: it is exactly the
   state of the account (it stays parked, and a new grant gets the same code),
   it is already in the closed set and exit mapping, and no other code says
   "an operator action is refused because the account still carries an
   unresolved overrun" without echoing anything. The replayed clear and the
   missing block follow the brief (`replayed:true`, `NOT_FOUND`).
3. The overrun path as specified (probing -> parked, audit of every overrun).
4. `expire-sweep` as specified.
5. N-001 as specified.

Mine:
6. **Scope exactness.** `--model` clears only that model's block; no `--model`
   clears only the account-wide block. A clear of an account-wide scope never
   touches a model block and the reverse.
7. **A replay reports `cleared` for a scope that was cleared even if a
   long-ago cleared row is the only trace** (the table keeps cleared rows); a
   scope that never had a block is `NOT_FOUND`.
8. **A probing block's retry time is cleared when an overrun re-parks it**
   (an overrun is not a timed retry; the block waits for reconciliation).
9. **Argparse usage errors still echo an unrecognised option.** That is
   existing behaviour of every verb (a stray `--something` is printed back by
   argparse); the new verbs avoid it for their own values (reason, aliases) but
   I did not change the global parser. Residual risk below.
10. **`block-clear` of a block that an unresolved adjustment protects cannot be
    forced** (no flag): the intended consequence until card R1 can resolve
    adjustments.

### Rows -> tests (`tests/test_cardl1a_t3.py`)

| Row | Tests |
|---|---|
| block_clear_audited | `BlockClearAudited`: a usage-limit block clears with the exact output, `updated_at` set, the audit row (actor, object, only reason and previous state); the account can be granted again; every reason, from a `probing` block; a replay is a no-op with no second audit row; model block and account-wide block are separate; a new block after a clear clears again |
| block_clear_refuses_unresolved_adjustment | `BlockClearRefusesUnresolvedAdjustment`: an overrun block refuses (static `ACCOUNT_PARKED`, nothing changes); an adjustment whose event is NOT the block's source event refuses; two adjustments with one resolved still refuses; once resolved the clear succeeds; another account's adjustment does not block |
| block_clear_not_found | `BlockClearNotFound`: no block; exact scope (account-wide vs model); unknown account and model; reason outside the enum and malformed aliases are `INVALID_REQUEST`, nothing changes |
| overrun_reparks_probing | `OverrunReparksProbing`: a probing block becomes parked (`updated_at`, audit); a parked block stays parked and keeps its source; two overruns share one block and both are in the audit; a model block is not the account's overrun block |
| expire_sweep | `ExpireSweep`: expired active -> uncertain with holds, `money_hold`, receipts and lease rows unchanged and one audit row; the unexpired and the settled lease untouched; the same seat and turn still conflicts; `expires_at` exactly now is expired and one ms early is not; a second sweep changes nothing; a late final receipt still settles; advisory leases expire too; the CLI prints `{"expired":1}` then `{"expired":0}`; a missing store is exit 3 |
| privacy_l1a (extension) | `PrivacyL1aTurn3`: four sentinels in the account, model and reason never reach stdout, stderr or the audit; the audit vocabulary is closed; the sweep prints no ids or aliases; the lease module still has no file/env/network access |

### Pre-existing tests changed, and why

- `tests/test_card5.py`: the two `mkdtemp` helpers (`_build_v6_store_with_legacy_block`
  and `_build_upgraded_legacy_store`) gain a cleanup. No assertion touched.
- Nothing else. The overrun change keeps the old behaviour for a fresh account
  (`test_overrun_blocks_account` and the other settle tests pass unchanged).

### Evidence

- **Red first.** Commit `38df396` (tests only, turn-2 production code), run in a
  detached worktree: `python -m unittest tests.test_cardl1a_t3` -> `Ran 30
  tests ... FAILED (failures=20, errors=7)`: 27 of 30 fail (no verbs, no sweep,
  no re-park); 3 pass trivially (e.g. the lease module's no-file-access scan).
  After that commit I corrected three set-up mistakes in my own tests (order of
  a lease and a block, the seat of a conflict grant, one sweep boundary case).
- **Green.** `tests.test_cardl1a_t3`: 30 tests OK. Full suite `python -m
  unittest discover -s tests`: `Ran 607 tests in 271.2s ... OK` (577 + 30).
- **N-001 check.** With `TMP`/`TEMP` pointed at an empty folder, `test_card5`
  alone left 5 `quota-card5-v6*` folders before the fix; after the fix a full
  run of the whole suite (607 tests) leaves the folder empty (0 entries).
  Other test files already used `TemporaryDirectory` (they were clean in the
  same run).
- **Mutation spot-check.** 10 single-point mutations (the adjustment scan, its
  `resolved_at` filter, the re-park, the overrun audit, the `<=` boundary, the
  state filter of the sweep, the clear audit, the replay, the scope, the reason
  enum): all killed. The first run left one survivor (the sweep's state filter:
  the settled lease in my test was not yet due); the test now makes the settled
  lease due, and the mutant dies.

### Judged not testable

Concurrent `block-clear` against a settling overrun: both take `BEGIN
IMMEDIATE` and serialise; the refusal rule is checked inside the same
transaction as the state change.

### Residual risk

- Until card R1 can resolve adjustments, an account blocked by an overrun
  cannot be unblocked by any product path (intended, decision 10); an operator
  must resolve it in the database, which is audited nowhere.
- A stray unrecognised command-line option is echoed by argparse for every verb
  (decision 9).
- `expire-sweep` is a manual verb: nothing calls it on a timer yet, so leases
  stay "active" until an operator (or a later scheduler) runs it.

## Closing summary of card L1a (turns 1-3)

What L1a now provides:
- **Loading policy:** `config-import` version 2 loads projects, teams,
  project-account links, seats, models, profiles and limits from one document
  (aliases only, one transaction, retained ids, referenced removals refused).
- **Safer metered grants:** a metered seat is always granted advisory leases
  until card R1's activation gate (register R-003 closed as an interim).
- **Profiles at grant:** the task class keys a profile; grant checks the
  candidate model, the five ceilings and the margin, stores the class, and a
  profile edit never changes a lease already granted.
- **Operator tools:** an audited `block-clear` that refuses while an overrun is
  unresolved, an overrun path that re-parks a probing block and audits every
  overrun, and `expire-sweep`.
- **Hygiene:** card-5 tests no longer leak temporary folders (N-001).

What it deliberately does not do: no probe verb or probe protocol (a later
card, with 7d-1's local slots); no way to resolve adjustments or activate
enforcing metered leases (card R1); no status projection of the policy tables
and no scheduler for the sweep; the wire still cannot name a profile revision;
nothing for L1b.

## Fix round 1 (tk-80472a29a673)

In plain words: the cold read of this card found five problems. Three are about
data written by the version before L1a (Leasewise has never been deployed, but
the code must not rely on that): an old "enforcing" lease could still pass the
permission check while enforcement is switched off; a request that was already
granted could no longer be repeated safely; and a broken stored profile looked
like an ordinary refusal. Two are about deletions: the policy loader could
remove a seat that reading history still points at, and some removals of
accounts and machines failed as a database fault instead of a clear
"configuration refused". All five are fixed, each with a test that failed first.

| # | Finding | Fix | Tests (`tests/test_cardl1a_fix1.py`) |
|---|---|---|---|
| F1 (major) | A `metered` lease stored by the code before L1a still got 200 from `/check` and a rotated token on replay while `METERED_ENFORCEMENT_ACTIVATED` was False | one function, `lease.token_bearing(enforcement)`, decides whether a lease may carry a token or pass `/check`: a stored `metered` lease counts only when the gate is open; the other values are as before. `http_api` uses it for token issuance and for `/check`. R1 replaces that one condition. Holds, settle, late receipts, expiry and the sweep are untouched | `F1OldEnforcingLeaseCannotCheck`: `/check` -> `FORBIDDEN` with the gate closed and 200 when open; a replay returns no token and does not rotate the stored hash; settle (non-final then final) releases and retains as before and the holds stay; expiry and the sweep keep the hold; the condition's table |
| F2 (major) | A lease stored before L1a has a fingerprint without `task_class`; its identical request returned `IDEMPOTENCY_CONFLICT` | on replay the stored lease is compared by format: a lease whose `policy_snapshot` has a `task_class` key is compared with the current fingerprint (task_class included); one without it with the legacy fingerprint (computed exactly as the old code did: every field but `request_hash` and `task_class`). New grants always store the current hash | `F2UnchangedRequestReplaysAfterTheUpgrade`: a legacy-format lease plus its identical request replays (store and HTTP, with the wire-identity ledger); a changed body conflicts for five fields; a current-format lease with another task class conflicts; new grants store the current hash and the marker |
| F3 (major) | Removing a seat deleted a `readings.seat_id` reference (no foreign key) | the policy removal guard is now one table, `policy_config.REMOVAL_GUARDS`, covering declared foreign keys and logical references (audit below) | `F3LogicalReferencesGuardRemoval`: a reading, daily totals of a seat, a team and a project, a held limit, a lease each refuse (INVALID_CONFIG, whole store unchanged); an unreferenced seat is deleted; a profile may be removed while leases name its class; a test compares the table with the live schema's foreign keys |
| F4 (minor) | An account referenced only by a block or limit event hit the database foreign key: STORE_ERROR, exit 3 | the inventory removals use one table, `store.REMOVAL_GUARDS`, for accounts, machines and gateways, in both document versions; the foreign key remains the backstop | `F4InventoryRemovalsAreInvalidConfig`: block + limit event (the reviewer's case), block only, adjustment only (accounts); lease, receipt, adjustment (gateways); lease (machines); both document versions; never `STORE_ERROR`; a test compares the table with the live schema's foreign keys |
| F5 (minor) | A stored `candidates` that is valid JSON but not a list of strings (for example `{}`) read as `POLICY_DENIED` | a malformed stored shape is `STORE_ERROR` (exit 3, static message); a valid list that does not contain the model stays `POLICY_DENIED` | `F5BrokenStoredProfile`: eleven malformed values -> store error; the wire layer sees the same error; valid lists with and without the model; a replay still needs no profile |

**Marker for F2.** `"task_class" in policy_snapshot`. The snapshot is a
fixed set of keys built by grant (`policy_hash`, `reservation_mode`, `rates`,
`chain`, `effort`, `caps`, `period_start`, and since L1a `task_class`,
`policy_revision`); none is chosen by a client, so a snapshot written before
L1a can never contain `task_class`, and one written after always does. Known
limit: the old fingerprint never saw `task_class`, so for a legacy lease a
changed task class cannot be told apart (pinned by a test); on the HTTP path
the wire-identity ledger, which hashes the whole wire body, still catches it.

### Audit 1 - references into the seven policy tables (F3)

| Reference (table.column) | Target | Kind | Guarded? | Why |
|---|---|---|---|---|
| teams.project_id | projects | foreign key | yes | existing |
| project_accounts.project_id | projects | foreign key | yes | the project's links are deleted first; any left refuses |
| limits.scope_id (scope project) | projects | logical | yes (new) | limits are removed first; a held limit refuses; any left refuses |
| daily_totals.scope_id (scope project) | projects | logical | yes (new) | totals are history nothing may orphan |
| seats.team_id | teams | foreign key | yes | existing |
| limits.scope_id (scope team) | teams | logical | yes (new) | as above |
| daily_totals.scope_id (scope team) | teams | logical | yes (new) | as above |
| leases.seat_id | seats | foreign key | yes | existing |
| readings.seat_id | seats | logical (no FK) | yes (new) | the reviewer's case |
| limits.scope_id (scope seat) | seats | logical | yes (new) | as above |
| daily_totals.scope_id (scope seat) | seats | logical | yes (new) | as above |
| leases.model_id | models | foreign key | yes | existing |
| blocks.model_id | models | foreign key | yes | existing |
| limit_events.model_id | models | foreign key | yes | existing |
| holds.limit_id | limits | logical | yes | existing (turn 1): holds of settled leases are history |
| leases.task_class | profiles | logical (text) | NO, on purpose | history, not a live reference: a replay needs no profile (turn-2 decision 11), leases stored before L1a hold the effort alias in that column, and nothing joins on it. Removing a profile with leases is allowed (tested) |
| audit.object_id, blocks.source_event_id | various | logical (history) | no | event history, not a reference |
| project_accounts | - | nothing points at it | n/a | deleted when absent from the document |

### Audit 2 - references into the inventory (F4)

| Reference | Target | Before | Now |
|---|---|---|---|
| readings.account_id, windows.account_id, models.account_id, project_accounts.account_id, billing_policies.account_id, billing_totals.account_id | accounts | guarded | guarded |
| blocks.account_id, limit_events.account_id, adjustments.account_id, leases.account_id, gateways.account_id | accounts | NOT guarded (database fault) | guarded |
| readings.machine_id, seats.machine_id, billing_policies.reporter_machine_id | machines | guarded | guarded |
| leases.machine_id, gateways.machine_id | machines | NOT guarded | guarded |
| leases.gateway_id, receipts.gateway_id, adjustments.gateway_id | gateways | NOT guarded (database fault) | guarded |

Honest note: `leases.account_id` and `leases.machine_id` can only fire alone if
a lease row existed without its seat and model, which the foreign keys forbid
(the seat and model guards refuse first). They are guarded as a backstop and
covered by the two schema-completeness tests, not by a separate scenario.

### Evidence

- **Red first.** Commit `12ed41b` (tests only, production code unchanged):
  `python -m unittest tests.test_cardl1a_fix1` -> 33 tests, 20 red. Failing
  (before my test-set-up corrections, then re-run): F1
  `test_check_is_forbidden_while_the_gate_is_closed`,
  `test_a_replay_returns_no_token_and_never_rotates_one`,
  `test_the_condition_lives_in_one_place`; F2
  `test_the_identical_request_replays_the_stored_lease`,
  `test_over_http_the_identical_wire_body_replays`,
  `test_a_changed_task_class_cannot_be_told_apart_on_a_legacy_lease`,
  `test_a_changed_body_is_a_conflict`; F3
  `test_a_seat_with_a_reading_cannot_be_removed`,
  `test_a_scope_with_daily_totals_cannot_be_removed`,
  `test_the_policy_guard_list_covers_every_reference`; F4 the account block,
  limit-event, adjustment and gateway lease, receipt and adjustment tests,
  `test_a_referenced_removal_is_never_a_store_error`,
  `test_the_inventory_guard_lists_cover_every_foreign_key`; F5
  `test_malformed_shapes_are_store_errors`,
  `test_the_store_error_is_exit_3_and_reaches_the_wire_layer`.
  (Two tests of mine were corrected after the red commit: an effort value the
  model does not allow, and a block on the wrong account.)
- **Green.** `tests.test_cardl1a_fix1`: 33 tests OK. Full suite: `Ran 742 tests
  in 288.4s ... OK` (709 + 33); with TMP at an empty folder nothing was left.
- **Mutation spot-check.** 13 single-point mutations (the gate condition, token
  issuance, the check, both fingerprint branches, five guard entries, the gateway
  guard, the shape check): all killed.
- **Changed pre-existing tests:** none.

### Decisions

11. The gate is applied to permission in ONE function, `lease.token_bearing`.
12. The replay marker is `task_class` in `policy_snapshot` (above).
13. `leases.task_class -> profiles` is deliberately not guarded (audit 1).
14. The guard lists are plain tables compared with the live schema's foreign
    keys by tests, so a new reference cannot be forgotten silently.
