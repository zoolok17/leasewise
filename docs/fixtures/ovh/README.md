# OVH fixtures (card 2b-3)

No real OVH API is ever called from code or tests. These files are the only inputs the pure OVH adapter (`quota_balancer/ovh_adapter.py`) is tested against.
`.gitattributes` here turns line-ending conversion off so the REAL files stay byte for byte as supplied.

## REAL (captured 2026-10-02 by the desktop lead, sanitized; do not edit)
| File | sha256 | What |
|---|---|---|
| `usage-current-2026-10-02T192336Z.sanitized.json` | 97e38d048ebcd1d5fce6cde9aae775abeede0a407c719a580672f72132ae5b07 | `GET /cloud/project/<project>/usage/current`, HTTP 200, October 2026 period. A `_capture` header object was added by the capturer; it is not part of the OVH body (tests strip it). |
| `auth-refused-invalid-signature.sanitized.json` | 3bf1e6b326942e535653a3b4259f3f276739cf647209186f434246ae573cf32c | OVH's answer to a deliberately invalid signature: HTTP 400, `Client::BadRequest` / `Invalid signature`. |

Field mapping and the non-AI rule: the capturer's README (approved mapping) is summarised in `docs/STEP-CARD-2B-3.md`.

## SYNTHETIC (invented for tests; each carries "synthetic" in its name and says so inside where the format allows)
| File | Used for |
|---|---|
| `usage-current-synthetic-non-ai-spend.json` | the real body plus one invented non-AI category with a positive price (totals adjusted to agree): `non_ai_spend_present=true`. |
| `usage-current-synthetic-hourly-nonempty.json` | the real body with one invented element in `hourlyUsage.instance`: the answer is unknown, so failure `schema`. |
| `usage-current-synthetic-missing-total-price.json` | the real body without `totalPrice`: failure `schema`. |
| `usage-current-synthetic-truncated.json.txt` | half of a JSON body (not valid JSON): failure `schema`. |
| `error-synthetic-html-page.html` | a non-JSON HTML error page: failure `schema` (on a 200) / `transport` (on a 5xx). |
| `error-synthetic-rate-limited.json` | an invented HTTP 429 with a `Retry-After` header: failure `rate_limit`. A real 429 was never captured. |

Other synthetic variants (boundary amounts, wrong currency, shifted periods) are built in the tests from the real body and are not stored as files.
