# Step record - Card L1d: replay before every policy check, and a stale-estimate error

Work item `card-l1d`, work order tk-d99e6c708c89. Branch `card-l1d`, built on the
card-l1c head `0b732c2`. A small, Leasewise-only card and a prerequisite of the
observe-only reporter of card 7a (`C:\lw\records\DESIGN-7A.md`, Part 5.1 and
Part 9, F6). Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only.

## In plain words

1. **A granted lease always comes back.** When a lease was granted but the answer
   got lost, the sender repeats the same request. Before this card, several
   policy checks ran first, so an operator's edit made in between (a model or an
   effort name removed, a project unlinked from its account, the account parked
   and so on) turned the repeat into an error although the lease existed. Now the
   stored lease is looked up right after the seat, and every policy check comes
   after. This also closes item N5 of card 4 (the project-account check before
   the replay).
2. **"Your estimate is too old" has its own name.** A stale or future-dated
   `observed_at` used to be a plain `INVALID_REQUEST`. It is now `ESTIMATE_STALE`
   (HTTP 400, exit 2, a fixed message). Because that check runs after the replay
   lookup, receiving it proves no lease exists for the request id, so the caller
   may safely build the request again once. Everything else that is wrong with a
   request is still `INVALID_REQUEST`.

Nothing else changes: a successful new grant, the replay answer, the token rules
of card L1a and the behaviour of L1b and L1c are untouched. No schema change.

## Files

- `quota_balancer/lease.py` - `_grant` order; the freshness failure.
- `quota_balancer/errors.py`, `quota_balancer/http_api.py` - the new code, its
  exit code, message and HTTP status.
- `tests/test_cardl1d.py` - 21 tests. Three older tests changed (below).
- `docs/STEP-CARD-L1D.md` (this record) and a README section.

## Grant order, before and after

| Step | Before (card-l1c head) | After (L1d) |
|---|---|---|
| 1 | closed shape check (no store reads) | same |
| 2 | HTTP wire-identity check | same |
| 3 | seat lookup (`NOT_FOUND`) | same |
| 4 | team, project (`NOT_FOUND`) | **replay lookup** (content hash, L1a legacy format; `IDEMPOTENCY_CONFLICT` on a changed body) |
| 5 | model (`NOT_FOUND`) | team, project (`NOT_FOUND`) |
| 6 | effort alias (`INVALID_REQUEST`) | model (`NOT_FOUND`) |
| 7 | account (`NOT_FOUND`) | effort alias (`INVALID_REQUEST`) |
| 8 | project-account link (`POLICY_DENIED`) | account (`NOT_FOUND`) |
| 9 | **replay lookup** | project-account link (`POLICY_DENIED`) |
| 10 | unknown context on a non-shadow seat (`CONTEXT_UNKNOWN`) | same |
| 11 | profile, candidates, ceilings (`NOT_FOUND`, `POLICY_DENIED`) | same |
| 12 | parked block (`ACCOUNT_PARKED`) | same |
| 13 | freshness (`INVALID_REQUEST`) | freshness (**`ESTIMATE_STALE`**) |
| 14 | arithmetic, enforcement, admission, insert | same |

The relative order of every check that moved is unchanged. No check sat between
the seat lookup and the replay lookup for a reason of its own: the team and
project reads only feed the policy checks, and the machine the replay is keyed by
comes from the seat row. The replay still reads no model, account or profile, so
an operator edit cannot reach it.

## Decisions

1. Replay after the seat lookup and the HTTP wire-identity check, before every
   other store read (the lead's order, exactly).
2. `ESTIMATE_STALE` covers "too old" and "too far in the future" and nothing else;
   the same rule applies to unknown-context requests.
3. A replay never sees a policy error or `ESTIMATE_STALE`; a changed body for the
   same request id stays `IDEMPOTENCY_CONFLICT` even after a policy edit or when
   the old `observed_at` is stale.
4. Static message "the admission estimate is stale"; no request text is echoed.
5. The edits in the tests are direct SQL on a store built by config-import v2:
   the importer's removal guards would refuse most of them, and an operator who
   edits the database by hand is the case the card must survive.

## Rows -> tests (`tests/test_cardl1d.py`)

| Row | Tests |
|---|---|
| replay_survives_policy_edits | `ReplaySurvivesPolicyEdits`: nine edits after a grant (model removed, effort removed, project link removed, account kind changed, profile deleted, candidates changed, profile ceiling lowered, seat reconfigured, account parked), each replays; a replay after minutes is not stale; `LegacyReplay` (L1a legacy format) after a model and profile removal |
| new_requests_keep_old_errors | the same nine edits, a NEW request gets the old error (`NOT_FOUND`, `INVALID_REQUEST`, `POLICY_DENIED`, `ACCOUNT_PARKED`); `OrderOfRefusals` (unknown seat, earlier of two failures) |
| estimate_stale_new_request | `EstimateStale`: both directions, the 120 s and 30 s boundaries still pass, unknown contexts, freshness stays after every policy check |
| estimate_stale_closed | code, exit 2, HTTP 400, static message |
| other_invalid_unchanged | `OtherInvalidRequestsUnchanged`: bad effort, mixed unknown context, bad type, missing key stay `INVALID_REQUEST`; the two codes differ |
| changed_replay_conflicts | `ChangedReplayConflicts`: after each edit and with a stale `observed_at` |
| HTTP | `HttpPath`: replay (200) after each shadow-seat edit, `400 ESTIMATE_STALE` both ways, a replay ignores staleness, `409` for a changed body after an edit, other bad requests stay `400 INVALID_REQUEST` |

## Pre-existing tests changed, and why

Three assertions now expect `ESTIMATE_STALE` where they expected
`INVALID_REQUEST` for the same stale or future `observed_at`: card 4 turn C
`test_stale_estimate_refused`, card 4 turn D `test_future_estimate_refused`
(both checks) and L1b `test_observed_at_must_still_be_fresh`. Nothing else moved;
no assertion was weakened (the inputs and the boundaries are unchanged).

## Judged not testable

Two requests racing for the same new request id: the existing `BEGIN IMMEDIATE`
serialises them and the second finds the first one's lease (unchanged).

## Residual risk

- A replay now succeeds for a lease whose model or account was removed by hand;
  the response is built from the stored lease only, so it needs nothing from them.
- Callers that treated a stale estimate as `INVALID_REQUEST` must now handle
  `ESTIMATE_STALE`; no caller in this repository did.
