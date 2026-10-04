"""The names of agenttalk's `gateway report` command and receipt pages, as one table (card R1-READER step 1).

Where they come from: agenttalk's merged head `921c82d4d5b94d133dd954f223c55e663c132cc1` (agenttalk PR #316).
* `docs/QWEN-OVH-TRIAL.md`, section "Receipts" (about :685-734) and section "The ledger report (report version 1)" (about :736-885);
* `src/agenttalk/ovh_gateway.py`: `GATEWAY_REPORT_VERSION` (:75), `CHILD_RECEIPT_REPORT_VERSION` (:73), `HOLD_REASON_WORDS` (:100), the
  receipt page keys `_RECEIPT_PAGE_KEYS` and `_RECEIPT_KEYS` (about :3957-3972) and the page version check (:4003).

Every name the reader uses sits here and nowhere else. If agenttalk changes one, this table and the check against its new head change; the
logic of `gwreport.py` and `receipts.py` does not.
"""

# --- the report: one JSON object, printed by `agenttalk gateway report --json` -----------------------------------------------------
KEY_VERSION = "gateway_report_version"          # the report's own version; the value below is the only one the reader accepts
ACCEPTED_VERSION = 1                            # the version that always carries `unreceipted_bound_actual`; any other is refused
KEY_RECEIPT_VERSION = "child_receipt_report_version"   # 2 = every receipt row carries `charge_period`; null = lease binding not installed
ACCEPTED_RECEIPT_VERSION = 2
KEY_GENERATION = "generation"                   # 32 lowercase hex characters: the ledger's own identity; every receipt page names it
KEY_PRICE_HASH = "policy_hash"                  # 64 lowercase hex characters
KEY_CAP_HASH = "child_cap_policy_hash"          # 64 lowercase hex characters; null when `child_cap_ready` is false
KEY_CAP_READY = "child_cap_ready"               # must be true: false means no child-turn feature, so no lease can own money
KEY_PERIODS = "periods"                         # a list of {KEY_PERIOD, KEY_COMMITTED}, in month order
KEY_PERIOD = "period"                           # "YYYY-MM" (UTC), the same value in every list that names a month
KEY_COMMITTED = "committed_micro_eur"
KEY_UNRESOLVED = "unresolved"                   # a list of {KEY_STATE, KEY_RESERVED, KEY_ACTUAL}; each row also has a `period`, not used
KEY_STATE = "state"                             # "reserved" or "uncertain"
KEY_RESERVED = "reserved_micro_eur"
KEY_ACTUAL = "actual_micro_eur"                 # a whole number, or null until a cost is recorded
KEY_THROUGH = "child_receipts_through_seq"      # `N`: the highest receipt number in this snapshot; null without lease binding
KEY_UNRECEIPTED = "unreceipted_bound_actual"    # a list of {KEY_PERIOD, KEY_UNRECEIPTED_AMOUNT}
KEY_UNRECEIPTED_AMOUNT = "micro_eur"
KEY_HOLD = "service_hold"                       # a flag
KEY_HOLD_REASON = "service_hold_reason"         # null without a hold, else one of HOLD_REASONS; checked, then dropped
HOLD_REASONS = ("attempt_over_reservation", "dashboard_canary_mismatch", "manual", "other")

STATES = ("reserved", "uncertain")

# Keys the report carries that the reader does not use (it ignores them, never keeps them): `observed_at`, `child_receipts_pending`,
# `open_child_turns`, `open_child_turns_expired`, `earliest_open_expiry`, and the `period` of each `unresolved` row. Every unresolved
# attempt counts, whatever its month.

# --- the receipt page ---------------------------------------------------------------------------------------------------------------
# The page keeps `envelope_version` 1 (there is no page version 2). The month arrives as one more key on every row. A report that says
# `child_receipt_report_version` 2 promises that its gateway's rows carry it.
KEY_CHARGE_PERIOD = "charge_period"             # on each receipt row: "YYYY-MM", or null (the attempts span months, or there was no charge)
