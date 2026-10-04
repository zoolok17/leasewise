"""The money reader's report bytes (card R1-READER step 3b, DESIGN-R1-READER.md 6 and 8 step 8): the exact document Leasewise's
`POST /v1/readings` takes for a version-4 gateway snapshot.

One pure function: the computed figures of a snapshot (`gwreport.Figures`) plus the reader's own identity, epoch and number become the bytes
that are saved, and later sent unchanged, as the one pending report. Nothing here reads a clock, a file or a database. The wire document is the
flat reading `{event_id, account_id, observed_at, kind, payload}` (`http_api._wire_to_reading`), whose payload names the gateway by its store id
(`gateway_id`; the document names the gateway's own account by `account_id`: Leasewise takes a gateway's report only from its owning machine and
account) and carries the nine version-3 fields and the five version-4 keys. The identity facts sit where `gwstate.report_facts` reads
them: `event_id` at the top, `reader_incarnation`, `generation`, `seq` and `ledger_epoch` in the payload.

The bytes hold only numbers, flags, hex digests and the gateway's id. No name of an agent, a message, a lease or a path, and nothing that the
gateway's report printed beyond the figures.
"""

import json

from ..validate_readings import derive_event_id

KIND = "gateway_snapshot"


def build_report(figures, gateway_id, account_id, incarnation, epoch, seq, observed_at):
    """The report as bytes (UTF-8 JSON, keys sorted, no spaces): so the same inputs always give the same bytes."""
    payload = {
        "gateway_id": gateway_id,
        "ledger_epoch": epoch,
        "seq": seq,
        "period_start": figures.period_start,
        "period_end": figures.period_end,
        "policy_hashes": {"price": figures.price_hash, "child_cap": figures.child_cap_hash},
        "sized_ready": figures.sized_ready,
        "reconciliation_required": figures.reconciliation_required,
        "settled_micro_eur": figures.settled,
        "reserved_micro_eur": figures.reserved,
        "uncertain_micro_eur": figures.uncertain,
        "unattributed_micro_eur": figures.unattributed,
        "generation": figures.generation,
        "receipts_seq": figures.receipts_seq,
        "quiet": figures.quiet,
        "held_over_reservation": figures.held_over_reservation,
        "reader_incarnation": incarnation,
    }
    document = {
        "event_id": derive_event_id(incarnation, figures.generation, seq),
        "account_id": account_id,
        "observed_at": observed_at,
        "kind": KIND,
        "payload": payload,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
