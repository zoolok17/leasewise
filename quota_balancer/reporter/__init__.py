"""The observe-only reporter core (card 7a-2; docs/STEP-CARD-7A-2.md).

It reads the turn-event journal that agenttalk writes (schema version 1),
keeps its own durable state, and decides what to tell Leasewise: a shadow
lease per message, one report per launched dispatch, and one final receipt.
This package only DECIDES; the HTTP client, the verbs and the configuration
file belong to card 7a-3. Standard library only.
"""
