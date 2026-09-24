"""Schema migration for the quota inventory store.

Card 1 is migration version 1. The DDL is normative: it is copied byte for
byte from docs/DESIGN.md section 8 ("Card 1") and creates exactly the three
tables of this card.
"""

from .errors import (
    CODE_SCHEMA_UNSUPPORTED,
    MSG_SCHEMA_UNSUPPORTED,
    QuotaError,
)

SCHEMA_VERSION = 1

MIGRATION_V1 = """\
CREATE TABLE accounts(id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE, vendor TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('metered','subscription')), budget_micro_eur INTEGER,
 CHECK((kind='metered' AND typeof(budget_micro_eur)='integer' AND budget_micro_eur>0)
 OR (kind='subscription' AND budget_micro_eur IS NULL))) STRICT;
CREATE TABLE machines(id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE) STRICT;
CREATE TABLE gateways(id TEXT PRIMARY KEY, alias TEXT NOT NULL UNIQUE,
 account_id TEXT NOT NULL REFERENCES accounts(id), machine_id TEXT NOT NULL REFERENCES machines(id),
 share_micro_eur INTEGER NOT NULL CHECK(share_micro_eur>0), period TEXT NOT NULL CHECK(period='utc_month')) STRICT;
PRAGMA user_version=1;
"""


def migrate(conn):
    """Bring the database to schema version 1. Idempotent.

    Version 0 means "not initialized" (fresh or empty file) and runs the
    migration. Version 1 is a no-op. Any other version is unknown or newer
    and is rejected.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version != 0:
        raise QuotaError(CODE_SCHEMA_UNSUPPORTED, MSG_SCHEMA_UNSUPPORTED)
    conn.executescript(MIGRATION_V1)
    conn.commit()
