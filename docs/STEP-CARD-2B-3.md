# Step record — Card 2b-3: pure OVH billing adapter

Work order: tk-e6e08f2b6945. Branch `card-2b-3` off `fbf5501` (card 5
merged). Date: 2026-10-02. Interpreter: CPython 3.13 (`C:\lw\venv`),
standard library only.

The card is docs/DESIGN.md section 8, row "2b-3: pure OVH adapter", and
section 11 "Pure OVH adapter, output and errors". One call with an injected
credential, transport, clock and jitter integer turns one OVH "project
usage, current" answer into a version-3 reading-import document or a closed
failure. **Out of scope:** any loop, daemon, credential file or store,
outbox, delivery, real network or key, new CLI verb, store write.

## Files

- `quota_balancer/ovh_adapter.py` — the adapter (new). Place and name: one
  module, `ovh_*` after the provider, flat like `billing_eval.py` /
  `billing_open.py`; no package split for ~350 lines.
- `tests/test_card2b3.py` — 79 tests (below).
- `docs/fixtures/ovh/` — two REAL sanitized captures (byte for byte),
  six SYNTHETIC files, `README.md` (which is which) and a `.gitattributes`
  (`* -text`) that keeps the real files byte-exact under `autocrlf`.
- `docs/STEP-CARD-2B-3.md` (this record) and a README section.

Reused, not forked: `ALIAS_RE`, `INT64_MAX`, `CURRENCY_EUR`,
`DOC_VERSION_BILLING`, `KIND_PROVIDER_BILLING`, `_is_one_utc_month_period`
(from `validate_readings.py`) and `FUTURE_SLOP_MS` (`reduce.py`), so the
adapter cannot drift from what ingest accepts. The module imports no
`os`, `socket`, `urllib.request`, `time`, `random` or `sqlite3`.

## Design as implemented

`poll_once(*, credential, transport, clock, jitter_seconds, config,
poll_seconds=900, base_url=OVH_EU_BASE_URL)`.

1. **Caller bugs raise** `QuotaError(INVALID_CONFIG)` (static message, before
   any transport call, no value echoed): jitter not an int in [0, 90],
   `poll_seconds` not an int in [1, 7 days], aliases not matching the
   inventory regex, `scope_revision` not a positive int64, a credential
   part that is empty / not printable ASCII, a project id outside
   `[A-Za-z0-9_-]{1,64}` (keeps it a single path segment), a non-int or
   negative clock.
2. **TLS refusal before the call.** A base URL whose scheme is not `https`
   (or has no host), or a transport whose `verifies_tls` is not exactly
   `True` (an absent attribute counts as off), returns failure `tls` and
   the transport is never called.
3. **One signed GET.** `GET <base>/cloud/project/<project>/usage/current`;
   headers `X-Ovh-Application`, `X-Ovh-Consumer`, `X-Ovh-Timestamp`
   (`now_ms // 1000`), `X-Ovh-Signature = "$1$" + sha1(secret+consumer+GET+url+""+timestamp)`
   (parts joined by `+`; `sign_request` is public so a known input is
   pinned), plus `Accept`. Transport: `get(url, headers, timeout_seconds)`
   returning `TransportResponse(status, headers, body)`. `TlsError` or
   `ssl.SSLError` -> `tls`; any other `Exception` -> `transport`
   (`KeyboardInterrupt` and friends are not swallowed). A malformed answer
   object (not a 3-tuple, headers not a mapping, body not bytes/str) ->
   `transport`.
4. **Status map.** 200 -> parse. 400 whose body is exactly class
   `Client::BadRequest` + message `Invalid signature` (the real capture), and
   401/403 -> `auth`. Any other 400, other 4xx except 429, 1xx/2xx/3xx
   other than 200, 6xx and a non-int status -> `schema`. 429 -> `rate_limit`.
   5xx -> `transport`.
5. **200 body**, checked in this order (first failure wins):
   - JSON: UTF-8, <= 1 MiB, `parse_float=Decimal`, NaN/Infinity and
     duplicate keys rejected, nesting depth bomb caught -> `schema`.
   - `totalPrice` object, `currencyCode` a string; not `EUR` -> `currency`.
   - `priceInUcents` exact int (bool and `673728600.0` rejected), 0..int64;
     `total_micro_eur = -(-ucents // 100)` (ceil).
   - `totalPrice.value` a finite non-negative number, within 1 micro-EUR of
     `priceInUcents / 100`; `resourcesUsage[]` each `{type: str,
     totalPrice: number >= 0}`, sum within 1 micro-EUR **per entry** of
     `value` (zero entries: exact); `hourlyUsage` / `monthlyUsage` present
     objects whose every array (any depth, including `quantum`) is empty;
     else `schema`.
   - `lastUpdate`, `period.from`, `period.to`: strict `YYYY-MM-DDThh:mm:ss[.f{1,9}]Z`;
     missing/unparsable -> `schema`; `lastUpdate` > now + 30 s -> `schema`.
   - `[from, to)` not exactly one UTC month, or `lastUpdate` outside
     `[from, to]` (end instant allowed) -> `period`.
   - `non_ai_spend_present` = some entry with `type != "ai-endpoints"` and
     `totalPrice > 0`.
   Component data (`resources[].components[]`) and unknown keys are ignored;
   no token counts leave the module.
6. **Schedule.** `next_attempt_at = max(now + poll*1000 + jitter*1000,
   retry_after_ms)` on every result. `Retry-After` is read (case-insensitive
   name) on a 429 only: all-ASCII-digit delta-seconds or an HTTP-date that
   names its zone; anything else, and anything more than 24 h ahead, is
   ignored (decision 11).
7. **Document.** `{"version": 3, "readings": [one reading]}`; `observed_at` =
   `lastUpdate` ms (not the poll time); `event_id` = lowercase uuid5 under
   `EVENT_NAMESPACE` (uuid5 of the URL name `urn:leasewise:ovh-adapter:event-id:v1`
   in the URL namespace) over the compact JSON array
   `[machine, account, scope_alias, scope_revision, period_start,
   observed_at, total_micro_eur, non_ai_spend_present]`. An unchanged poll
   is byte-identical, so ingest treats it as a same-ID/same-content replay.

## Acceptance rows -> tests (`tests/test_card2b3.py`)

| Row | Tests |
|---|---|
| billing_adapter | `BillingAdapter`: exact document for the real capture (total 6737286, observed_at from lastUpdate, period bounds, event id pinned); passes the existing validator; not the poll time; unchanged poll byte-identical; event id lowercase uuid5 and every identity field moves it; fractional ucents ceil (5 cases); zero total; Decimal not float; `priceInUcents` exact non-negative int64 (bool/float/string/overflow/`.0`/exponent form); currency `USD`/`eur`/blank -> `currency`, non-string/missing -> `schema`; `value` vs ucents boundary (+-1 micro-EUR pass, beyond fail); category-sum boundary (1 and 2 entries, no entries); `lastUpdate` missing/unparsable/fraction forms; period not one month (6 cases), missing/unparsable -> `schema`; December rollover; `lastUpdate` outside the period and exactly at either bound; +30 s / +30.001 s boundary; unknown keys ignored |
| billing_non_ai | `BillingNonAi`: ai-only false; positive synthetic category true (fixture, total 8237286); zero (int and float) false; smallest positive (1 micro-EUR) true; non-empty `hourlyUsage` fixture -> `schema`; non-empty array at five places (incl. nested `quantum`, an unseen key, a deep dict) -> `schema`; missing/misshapen sections -> `schema`; bad category entries (no type, bad type, no/str/bool/null/negative price, non-dict) -> `schema`; components not needed; no token counts in the document |
| billing_poll_schedule | `BillingPollSchedule`: jitter 0/1/89/90; out-of-range, bool, float, string, None jitter -> typed error with zero transport calls; `poll_seconds` validation and scaling; Retry-After seconds, header-name case, HTTP-date, shorter than the poll, in the past, invalid forms, 429 only, beyond 24 h respected, int64 clamp; exactly one transport call on every outcome and every exception kind; failure schedule; the signed request (URL, headers, timeout); the signature for one known input; default OVH EU base; bad injected parts raise with no call and no secret in the error |
| billing_tls | `BillingTls`: `http`, `ftp`, scheme-relative, bare host, empty, uppercase `HTTP` refused before any call; `HTTPS` uppercase accepted; verification off/None/0/"yes"/1 and an attribute-less transport refused before any call; `TlsError`, `ssl.SSLError`, `ssl.SSLCertVerificationError` -> `tls`; other errors -> `transport`; `KeyboardInterrupt` propagates |
| billing_output_errors | `BillingOutputErrors`: the failure enum is exactly the seven; the REAL 400 `Invalid signature` -> `auth`; other 400 shapes -> `schema`; 401/403 -> `auth`; other 4xx -> `schema`; synthetic 429 fixture -> `rate_limit`; 5xx (HTML fixture) -> `transport`; connection errors/timeouts -> `transport`; unexpected statuses -> `schema`; malformed 200s (HTML page, truncated JSON, missing `totalPrice`, empty, NaN, duplicate key, depth bombs, trailing text, oversize) -> `schema`; a duplicate key cannot hide a field; malformed transport answers -> `transport`; every result is closed, int `next_attempt_at`, JSON-serialisable, all seven enums seen |
| privacy_2b (this card) | `Privacy2b`: sentinel strings in the credential, project id, URL, response body, error text of raised exceptions and Retry-After never appear in any of 12 result kinds nor in `repr`/`str` of the credential or the typed error; the signature and timestamp are not in any result; the module source has no file, environment, socket, `urllib.request`, `time`, `random` or SQLite use |
| end to end | `EndToEndIngest`: the real-capture document goes through the existing `reading-import` CLI against a temp store (synthetic inventory + billing policy with matching aliases): accepted, total 6737286 at `observed_at`; a poll an hour later yields the same document, accepted again, `readings` and `billing_totals` rows unchanged |

## Evidence

- **Red first.** Commit `92b3e6c` (tests and fixtures only):
  `python -m unittest tests.test_card2b3` ->
  `ImportError: cannot import name 'ovh_adapter' from 'quota_balancer'`,
  `Ran 1 test ... FAILED (errors=1)`: the module does not exist, so no named
  test can run (the file fails at import, as the brief expected).
- **Green.** After the implementation commit, `tests.test_card2b3`:
  `Ran 79 tests ... OK`.
- **Mutation spot-check.** 37 single-point mutations of `ovh_adapter.py` (ceil, jitter bounds, both tolerance boundaries, +30 s, period bounds, non-AI predicate, empty-array rule, Retry-After rules and cap, `max()`, TLS flag/scheme/exception, 400 signature, 403, 5xx, duplicate keys, NaN, Decimal parsing, currency, one-month period, bool ucents, event-id inputs, signed body, signature prefix, observed_at, usage sections, size limit, 429). The first sweep left 3 survivors: the naive-HTTP-date check (redundant with the TypeError path; the explicit check was removed), NaN read as 0 and the body size limit (each masked by a later check); two tests were added (`test_non_finite_numbers_are_never_read_as_numbers`, `test_body_size_limit_is_enforced_on_valid_json`). Re-run: 0 survivors. Script kept in the seat scratch only.
- **Full suite**, once, foreground: `python -m unittest discover -s tests` -> `Ran 599 tests in 191.2s ... OK` (520 before this card + 79 new; an earlier run of the same tree before the two mutation-driven tests: 597 tests, 186.8s, OK).
- Fixture provenance: the two REAL files copied from `C:\lw\incoming\2b-3\`,
  sha256 verified after copying:
  `usage-current-2026-10-02T192336Z.sanitized.json` =
  `97e38d048ebcd1d5fce6cde9aae775abeede0a407c719a580672f72132ae5b07`,
  `auth-refused-invalid-signature.sanitized.json` =
  `3bf1e6b326942e535653a3b4259f3f276739cf647209186f434246ae573cf32c`.
  SYNTHETIC: `usage-current-synthetic-non-ai-spend.json`,
  `usage-current-synthetic-hourly-nonempty.json`,
  `usage-current-synthetic-missing-total-price.json`,
  `usage-current-synthetic-truncated.json.txt`,
  `error-synthetic-html-page.html`, `error-synthetic-rate-limited.json`.
  The rate-limit (429 with `Retry-After`), HTML page, truncated JSON and
  missing-`totalPrice` answers are SYNTHETIC: none was ever captured.
- Fixtures were not edited: the real body's `_capture` header is stripped in
  the tests (it is the capturer's, not OVH's); the adapter itself ignores
  unknown keys anyway.

## Decisions on ambiguities

The ten from the work order (followed as written):

1. Money: `ceil(priceInUcents/100)` in integers; `parse_float=Decimal`;
   `priceInUcents` exact non-negative int64 (bool rejected) else `schema`;
   currency other than `EUR` -> `currency`.
2. Consistency: `value` within 1 micro-EUR of `priceInUcents`; category sum
   within 1 micro-EUR per `resourcesUsage` entry of `value`; else `schema`;
   both boundaries pinned.
3. non-AI flag exact; any non-empty array under `hourlyUsage` /
   `monthlyUsage` (nested `quantum` too) -> `schema`; bad `type` /
   `totalPrice` -> `schema`; components ignored, no token counts.
4. `observed_at` = `lastUpdate`; one-UTC-month period and `lastUpdate` in
   `[from, to]` else `period`; > 30 s ahead -> `schema`.
5. HTTP mapping as listed in "Design" item 4; TLS refusal before the call.
6. Schedule `max(now+poll+jitter, Retry-After)`; jitter in [0, 90] else the
   typed error; one call, no retry.
7. `event_id` uuid5 over the canonical values (fixed namespace constant).
8. Signing as OVH documents it; credential injected; nothing read from
   files or environment; the credential's `repr` is redacted.
9. Output shapes closed; aliases and revision injected and validated with
   the inventory rules.
10. One module, `ovh_adapter.py`.

Added by this card (the brief left them open):

11. **Retry-After** (REVISED by the lead after review, fix round 1
    finding 4; the first build capped it at 24 h and silently overrode a
    valid header). Any syntactically valid Retry-After on a 429 counts:
    ASCII-digit delta-seconds or an HTTP-date that names its zone; anything
    else is ignored. There is no upper limit; `next_attempt_at` is clamped
    to the int64 maximum (including digit strings too long to convert), so
    the adapter never schedules earlier than the provider asked. A date in
    the past is valid but loses the `max`. Downstream a stalled billing
    source becomes stale billing, which the evaluator reports as an ALERT.
12. **Order of failures** when a body breaks several rules: structure ->
    currency -> money -> usage sections -> timestamps -> period. Only the
    first is reported (a single enum).
13. **Missing or unparsable `period` fields -> `schema`;** only a parsed,
    non-one-month period is `period`.
14. **`hourlyUsage` / `monthlyUsage` must be present objects.** A body
    without them is `schema` (unverified shape), not "empty".
15. **Transport contract.** `verifies_tls` must be `is True`; a transport
    without it is refused. A 30 s timeout is passed to the transport.
    Statuses outside the closed map (1xx, 2xx other than 200, 3xx, 6xx,
    non-int) are `schema`: a redirect is never followed.
16. **Body limits.** Over 1 MiB, non-UTF-8, NaN/Infinity, duplicate keys and
    deeply nested JSON are `schema`; `value` and entry prices are bounded
    below 10^13 EUR before arithmetic.
17. **A 400 other than the signature case is `schema`** (the README says
    "request error"; there is no `request` enum), consistent with decision
    5's other-4xx rule.
18. **Timestamps** accept only the `...Z` form with a 1-9 digit fraction;
    sub-millisecond digits are truncated. A numeric offset is `schema`.
19. **Clock** returns epoch milliseconds; the signature timestamp is its
    floor in seconds (OVH wants server time; a skew-corrected clock can be
    injected by the later reporter).

## Judged not testable here

- Real certificate verification and a real transport: the contract is the
  refusal and the error mapping (design: "fixtures test that refusal, not
  actual certificate verification").
- Acceptance of this signature by OVH: only the known-input digest is
  pinned; the one real 400 shows OVH rejects a bad one, and a good one needs
  a real key (forbidden).
- A real 429 and its `Retry-After`: never captured; synthetic only.

## Residual risk

- A non-AI category whose real element shape differs (non-empty
  `hourlyUsage` / `monthlyUsage`) always fails `schema`; a project with
  routine non-AI usage would never report until the shape is captured and
  mapped (by design).
- OVH's `value` vs `priceInUcents` rounding rule is inferred from one
  capture (equal there); the 1 micro-EUR tolerance is the lead's decision.
- A category sum can legitimately differ from `value` in ways one capture
  cannot show (e.g. discounts); that would surface as `schema`, never as a
  wrong number.
- `X-Ovh-Timestamp` uses the injected local clock; a skewed host clock makes
  OVH refuse (a 400 `Invalid signature` maps to `auth`). The later reporter
  card owns time sync.
- No end-to-end test against a real transport; the deployment checklist in
  section 11 (tested TLS, grants, secret ACLs, redaction, canary) stays open.

## Fix round 1 (tk-76e822bde522)

Cold read (codex-lw-reviewer-1) of head `1a761e6`: verdict FIX, four
reproduced defects; the mapping, ceil, non-AI rule and replay were judged
sound. Tests first at `03f35d9`, fixes after.

| # | Finding | Fix | Tests (`tests/test_card2b3.py`) |
|---|---|---|---|
| 1 | Money arithmetic depended on the caller's Decimal context (prec 6 turned success into `schema`, an Inexact trap escaped, the addition rounded 1.00000100000000000000000000001 - 1 onto the tolerance) | Every money number is bounded in representation (<= 15 digits before, <= 12 after the point, judged on the parsed sign/digits/exponent), else `schema`. All money arithmetic runs in ONE module-level `decimal.Context` (prec 60; Inexact, InvalidOperation, Overflow, DivisionByZero trapped, Rounded not) via `localcontext(_MONEY_CONTEXT)`, including the category sum; a trapped signal -> `schema`. The integer path is unchanged | `FixRound1MoneyContext`: caller prec 6 / 1, caller traps (Inexact, Rounded, all) give the identical document; a schema failure is also context-independent; the 29-digit case and the 12-decimal case (1 vs 1.000001000001) -> `schema`; 1.000001 / 0.999999 / 1.0000009 accepted, 1.0000011 / 0.9999989 rejected; 12 vs 13 decimals (incl. trailing zeros, `1E-13`); integer-digit bound |
| 2 | A number literal such as 1e9999999999999999999 made `json.loads(parse_float=Decimal)` raise `InvalidOperation` out of `poll_once` | `parse_float` / `parse_int` reject literals longer than 64 characters and build the Decimal with `Decimal(text, _MONEY_CONTEXT)` (explicit trapped context, so the caller's traps cannot turn it into NaN); `ArithmeticError` is caught at the parser boundary. The 400 "Invalid signature" recogniser uses the same parser: an unparsable 400 body is not the signature case -> `schema` | `FixRound1ExtremeNumbers`: 200 and 400 bodies with four extreme literals (ignored field) -> closed `schema` with `next_attempt_at`; one in a known field; caller context with the traps off; the 64/65-character boundary for floats and ints |
| 3 | A malformed `base_url` raised a raw `ValueError` quoting the URL out of `urlsplit` | `_check_base_url` in `_validate_inputs`, before any call: printable ASCII only, parses (port included), no userinfo, query or fragment, non-empty host for https; failures raise the static `QuotaError(INVALID_CONFIG)` raised outside any `except` and `from None`, so the adapter's own URL-parser exception never appears (a caller that invokes `poll_once` while handling its own exception may see that outer exception in `__context__`, with `__suppress_context__` True, and the formatted traceback omits it). A well-formed non-https URL still fails `tls` before the transport | `FixRound1MalformedBaseUrl`: the reviewer's sentinel URL and 13 more malformed forms -> static error, zero transport calls, sentinel absent from `str`, `repr`, `args`, `__cause__`, `__context__` and the formatted traceback; non-https still `tls`; a URL with port and trailing slash works |
| 4 | Decision 11 capped Retry-After at 24 h and silently scheduled the poll instead (172800 -> now+900000) | Cap removed (revised decision 11 above); result clamped to int64 | `BillingPollSchedule`: 86400, 172800, a date two days ahead and a year-end date respected; `9*40`, `9*5000`, int64 max and the first overflowing second clamp to int64 max, the last fitting second is exact; the invalid-header list lost its cap-based entries |

**Red before.** At `03f35d9` (tests only, production unchanged):
`python -m unittest tests.test_card2b3` -> `Ran 97 tests ... FAILED
(failures=8, errors=5)`: ERROR `test_200_body_with_an_ignored_extreme_exponent_is_schema`,
`test_400_body_with_an_extreme_exponent_is_schema`,
`test_extreme_exponent_in_a_known_field_is_schema`,
`test_the_reviewers_sentinel_url`, `test_caller_traps_do_not_change_the_result`;
FAIL `test_absurd_retry_after_clamps_to_int64_never_earlier`,
`test_valid_retry_after_beyond_a_day_is_respected`,
`test_number_literals_are_bounded_to_64_characters`,
`test_untrapped_caller_context_cannot_turn_it_into_nan`,
`test_other_malformed_urls`, `test_caller_precision_does_not_change_the_result`,
`test_representation_bound_on_money_numbers`,
`test_subtolerance_noise_cannot_round_onto_the_tolerance`. (Boundary guards
such as the 12-decimal over-tolerance case already passed: they pin behaviour
that must survive the fix.)

**Green after.** `tests.test_card2b3`: `Ran 97 tests ... OK` (79 + 18 net new; the cap tests were replaced). Full suite `python -m unittest discover -s tests`: `Ran 617 tests in 188.3s ... OK` (520 on main + 97).

**Mutation spot-check of the fix.** 17 single-point mutations (explicit vs
ambient context, the trap list, both representation bounds, both literal
bounds, explicit-context parse, the ArithmeticError catch, each base-URL
rule, the clamp, the date branch): 14 killed. Two survivors were equivalent and kept as defence: the
15-integer-digit bound (any consistent amount is already limited by the int64
ucents total, so it is unobservable) and `from None` (with the adapter's own
URL-parser exception raised outside any `except` there is nothing of its own to
hide; the suppression matters only when a caller is already handling an
exception, see finding 3). The third survivor, a shortcut that clamped
Retry-After by digit-string LENGTH, was NOT equivalent: it was the defect found
by the delta read (fix round 2, finding F5 below).

New decisions:

20. **Representation bound.** Money numbers (`value`, every category
    `totalPrice`): finite, non-negative, <= 15 integer digits and <= 12
    decimals as parsed (`1E-13` and `0.0000000000000` are out; the check is on
    the exponent, not the numeric value). Lead decision.
21. **One explicit money context** (prec 60, Inexact / InvalidOperation /
    Overflow / DivisionByZero trapped, Rounded not); the ambient context is
    never used; a trapped signal -> `schema`. Lead decision.
22. **Base URL.** Printable ASCII, parseable (port included), no userinfo /
    query / fragment, host required for https; else the static invalid-input
    error. Anything else well-formed but not https (including a scheme-less
    string, which carries no scheme to trust) stays `tls`, refused before the
    transport, as in the first build.
23. **Number literals** are bounded to 64 characters (floats and ints)
    before conversion; the 64/65 boundary is pinned.

## Fix round 2 (tk-8cfe35ffe95f)

Delta read of head `c367e96`: the four original findings closed; one new
defect and one documentation inaccuracy. Tests first at `5364f6d`.

| # | Finding | Fix | Tests |
|---|---|---|---|
| F5 (major) | `_retry_after_ms` judged magnitude by the digit string's LENGTH: 19 zeros, or 20 zeros + 1200, scheduled the int64 maximum instead of now+poll / now+1200 s (a zero delay stalled polling) | leading zeros are stripped before judging magnitude (all zeros = 0 seconds); only more than 18 significant digits clamps to int64; `int()` is only called on at most 18 digits; `max(now+poll+jitter, retry_after)` unchanged | `FixRound2RetryAfterLeadingZeros`: `0`, `00`, 19/20/5000 zeros -> now+poll; 20 or 5000 zeros + `1200` -> now+1200 s; `0001200`, 19 zeros + 172800, 40 zeros + 86400; the last fitting second (with and without 30 leading zeros) exact, one more, `9`*19/40/5000, zeros + 40 nines and `1` + 5000 zeros clamp to int64 max; invalid headers (`-0`, `+0`, `0.5`, `0x0`, `1_0`, ...) still ignored |
| F6 (minor, docs) | The record claimed `from None` leaves no `__context__` and was therefore equivalent | text narrowed (finding 3 row and the mutation note): the adapter's own URL-parser exception never appears; a caller's outer exception may sit in `__context__` with `__suppress_context__` True, and the formatted traceback omits it. The code (`from None`) is unchanged. The "18-digit shortcut is equivalent" note is corrected: F5 disproves it | n/a (documentation) |

**Red before.** At `5364f6d`: `python -m unittest tests.test_card2b3` -> `Ran
101 tests ... FAILED (failures=3)`: `test_zeros_mean_zero_seconds_and_never_stall_the_poll`,
`test_leading_zeros_do_not_change_the_value`,
`test_genuine_overflow_still_clamps_and_the_edges_hold` (the last because its
zero-prefixed edge case also hit the length shortcut).

**Green after.** `tests.test_card2b3`: `Ran 101 tests ... OK` (97 + 4). Full suite `python -m unittest discover -s tests`: `Ran 621 tests in 188.3s ... OK` (520 on main + 101).
