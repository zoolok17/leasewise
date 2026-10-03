# Step record - Card R1: coordinator admission and reconciliation

Work item `card-r1` (this record grows one step at a time; each step is its own work item and branch). Design:
`C:\lw\records\DESIGN-R1.md`, with its **section 11** (the lead's decisions), which wins where it differs from
sections 1 to 10. Interpreter: CPython 3.13 (`C:\lw\venv`), standard library only. Audience: the lead and the
reviewers.

## In plain words

Before Leasewise may ever enforce limits on paid ("metered") work, its admission has to be right. Today it decides
from its own books only, ignores what the gateway says it spent, ignores the merged billing check, and does not
check how old its readings are. Card R1 fixes that, in two phases:

- **Phase 1, now** (about 9.5 turns): the parts that need nothing from agenttalk. These are the missing refusal
  words (step R1-0, below), honest window checks that cannot lock an idle seat out (R1-1), two rulings left over from
  card 4 (R1-2), keeping the gateway's money figures (R1-3), and a way to clear an overrun (R1-4). Each step has its own
  branch and is merged on its own.
- **Phase 2, later:** the evidence-based money admission, the billing gate and the status object. They wait for one
  field from the agenttalk side and one gateway build fact. The activation gate moves to card 6b.

**Paid enforcement stays impossible throughout.** `METERED_ENFORCEMENT_ACTIVATED` stays false until card 6b.

**Three items of the old R1 list are already closed and are not part of this card:** N5 (the project-account check
ran before the replay; closed by card L1d), N3 (the overrun park and block clearing; closed by card L1a) and m4 (a
subscription seat on a paid account; closed by card 5). DESIGN-R1.md section 1 has the lines. R1-2 only adds
regression tests so that the closures stay pinned.

## Step R1-0: the missing refusal words

Work item `card-r1-0`. Branch `card-r1-0`, from main `79322319c44f3fe21fd23c77343e70aca0f346d0` (card L6a-0 merged).
Spec: DESIGN-R1.md section 6.4 as amended by section 11; `docs/DESIGN.md` section 7 (the error list) and section 11
("Later HTTP mapping"). **No behaviour change: nothing emits the new words yet.**

### What changed

The design names refusal words that Leasewise did not define. They are now defined together, each with its HTTP status,
exit code and a fixed plain message, so that later steps and cards only have to emit them.

| Word | Status | Exit | Who will emit it |
| --- | --- | --- | --- |
| `METER_STALE` | 503 | 2 | R1-1 (window warnings), Phase 2 (evidence) |
| `RECONCILIATION_REQUIRED` | 503 | 2 | Phase 2 |
| `SERVICE_UNAVAILABLE` | 503 | 3 | the outage work (7c-3); exit 3 like a store fault, because the service itself cannot answer |
| `POLICY_MISMATCH` | 409 | 2 | card 6b |
| `INPUT_BOUND_UNAVAILABLE` | 403 | 2 | card 6b |
| `ENVELOPE_INCOMPATIBLE` | 403 | 2 | card 6b |
| `CONTEXT_UNAVAILABLE` | 403 | 2 | card 7d |
| `CONTEXT_ROTATION_REQUIRED` | 400 | 2 | card 7d |
| `UNSUPPORTED_EFFORT` | 400 | 2 | nobody: **defined and mapped only.** An unknown effort stays `INVALID_REQUEST` on purpose (privacy rule `privacy_4`, `lease.py` ~:659-663) |

The six billing words that already existed for the command line now also have their wire status, from the design's
"Later HTTP mapping": `BILLING_SCOPE_MISMATCH` 409, `BILLING_BASIS_UNKNOWN` 409, `BILLING_UNKNOWN` 503, `BILLING_STALE`
503, `BILLING_DIVERGENCE` 409, `OPENING_UNSAFE` 409.

Exit codes follow the existing scheme: a condition of the request or of the evidence behind it exits 2, a service that
cannot answer exits 3.

`errors.MESSAGES` is new: one table of every code and its fixed message, so that a test can say that no code is
missing a message and that no two codes say the same thing. Every message says what happened and what to do, and carries
no account, gateway, amount, hash or id.

**The `usage` reading kind.** `docs/DESIGN.md` listed a `usage` reading. It is marked removed there, with its old text
struck through so the history reads, and the note "Removed 2026-10-03 (lead decision D-116): usage travels in /settle
receipts; the kind stays refused." `http_api.py` still refuses the kind, unchanged; a test pins it.

### Tests (`tests/test_cardr1_codes.py`, 16)

Red first: before the change 16 of 16 failed or errored (the file reads the design's own error list, so a word that is
named there but not defined fails by name). After: all pass.

- the design's error list is read from `docs/DESIGN.md` itself; every word in it exists in `errors.py` with exactly the
  design's status, and the nine new words are exactly the ones that were missing;
- the six billing words exist and have the design's status;
- every code has an exit code (2 or 3; the two transport-only codes of card 5 have none, as before) and a fixed,
  non-empty message; the registry holds exactly the defined codes; a store fault or an unavailable service exits 3 and
  everything else 2;
- no two codes share a message; no message carries a placeholder, a line break or a non-ASCII character, and none is
  longer than 120 characters; each new message is at least five words;
- the reporter's closed list of Leasewise words (`decisions.CLOSED_CODES`, built from `errors.CODE_*`) contains the new
  words with no change to the reporter;
- nothing in `lease.py` or `store.py` emits a new word yet, and `UNSUPPORTED_EFFORT` appears nowhere in `lease.py`;
- the `usage` kind is still refused over the wire path with `INVALID_REQUEST` and its fixed text, and the design note is
  present with the old line struck through.

### Limits

Defining a word does not use it: until R1-1 and Phase 2 emit them, a caller can never see any of the new words.

## Step R1-1: honest window checks and warn mode

Work item `card-r1-1`, branch `card-r1-1`, from main `363c87e` (R1-0 merged). Spec: DESIGN-R1.md **section 12.2**
(which replaces 5.1, 5.2, 11.4 and the O-R1-5 ruling) and 12.1. Commit 1 of two.

### In plain words

A grant used to look only at the last usage number of a window. It now decides from everything it knows: the number,
how old it is, whether the window has reset since, and whether it is known at all. A window known to be used up
refuses. An old or never-seen window is allowed with a warning. A window the source says it does not know refuses an
admission seat and only warns a warn-mode seat. The answer shows the warnings and the window facts.

### The table (12.2) and the tests that pin each cell

Module `tests/test_cardr1_windows.py`; each cell runs in both modes unless it differs.

| Window evidence | Admission | Warn | Test |
| --- | --- | --- | --- |
| known, current, below | admit | admit | `TheTable.test_known_current_and_below_the_ceiling_is_admitted_without_a_warning` |
| known, at or above (equality, above, 100%) | `WINDOW_LIMIT` | `WINDOW_LIMIT` | `test_known_at_the_ceiling_refuses_and_so_does_known_above_it` |
| known, at or above, old | `WINDOW_LIMIT` | `WINDOW_LIMIT` | `test_known_at_the_ceiling_refuses_even_when_the_reading_is_old` |
| exhausted, reset time passed, no new observation | `WINDOW_LIMIT` | `WINDOW_LIMIT` | `test_exhaustion_is_not_forgotten_after_the_reset_time_passes` |
| known, below, not current | `METER_STALE` warning | same | `test_known_below_the_ceiling_but_old_is_admitted_with_a_stale_warning` |
| explicit unknown (NULL) | `WINDOW_LIMIT` | `WINDOW_LIMIT` warning | `test_an_explicit_unknown_refuses_an_admission_seat_and_warns_a_warn_seat` |
| never observed (no row) | `METER_STALE` warning, `confidence: missing` | same | `test_a_window_never_observed_is_admitted_with_a_stale_warning_and_is_never_an_observed_zero` |
| shadow request | never refused | | `test_a_shadow_request_is_never_refused_by_a_window` |
| metered or metered-advisory lease | like admission mode | | `test_a_metered_advisory_lease_follows_the_admission_rules` |

Freshness (`Freshness`): `test_600_seconds_is_current_and_600_seconds_and_a_millisecond_is_not`,
`test_a_recent_sample_whose_reset_has_passed_is_not_current`,
`test_a_sample_taken_after_the_reset_is_current_even_though_the_reset_time_has_passed`,
`test_an_observation_up_to_30_seconds_in_the_future_counts_as_age_zero`,
`test_an_observation_further_in_the_future_is_not_trusted_as_current`.
Refusals in both modes (`Refusals`): `test_a_parked_block_refuses_in_both_modes`,
`test_the_concurrency_limit_refuses_in_both_modes`.
Annotations (`Annotations`): one record and one warning per window with two limits, the tightest ceiling decides,
several windows by key, closed record keys and no source identity (privacy), empty lists without a window limit, only
the two closed codes. Replay (`Replay`): current annotations, grant untouched, never refused, shadow empty.

### What changed, and what a reader may notice

- **Inclusive comparison (F5).** `used_bps >= ceiling` refuses. The old generic check admitted a reading exactly at the
  limit; that changed on purpose.
- **Freshness** is `reduce.window_current`: the observation time (the table holds nothing else), 600 s, a reset passed
  with no newer observation, 30 s of future slop counting as age 0.
- **The answer** (`lease.grant`, and `http_api.handle_grant` on the wire) carries `warnings` and `windows`, built from
  the same read as the decision. They were always empty before.
- **Shadow** requests used to be run through the window check like everything else; they are now never refused by it.
  No existing test relied on the old behaviour.
- **One earlier test changed:** `test_cardr1_codes.test_nothing_in_the_product_emits_a_new_word_yet` now allows
  `METER_STALE` in `lease.py` as a warning and checks that it is never raised as a refusal.
- **Comment fix (M-010 nit):** the `http_api.py` note on `usage` now says the kind is retired (D-116) and stays refused.
- **A window limit can apply to a metered or metered-advisory lease** (limits are by scope, not by seat kind). Such a
  lease is treated like admission mode, as the brief said.

### Limits (12.2, F7)

- Admitting an old or never-observed reading below the ceiling is an **availability exception**. It does not bound
  what other consumers used since the reading; the ceiling is not a safety margin for that.
- A window known at or above its ceiling, or explicitly unknown, can lock an admission seat out until a fresh
  observation arrives. Nothing clears it on a timer. The recovery probe is the planned way to get one.
- The 600-second wall cap is the only freshness rule; the idle-seat allowance needs turn tracking that is not built.
