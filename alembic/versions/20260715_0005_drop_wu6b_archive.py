"""drop the migration-only WU6B active-account archive

Revision ID: 20260715_0005
Revises: 20260715_0004
Create Date: 2026-07-15 00:05:00.000000

This corrective revision also runs for databases that applied the original
20260715_0004 implementation. Deployments must take an authoritative database
snapshot before upgrading because archived active-selection rows are retired
and intentionally not retained in the head schema.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260715_0005"
down_revision: str | Sequence[str] | None = "20260715_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ARCHIVE_TABLE = "wu6b_active_game_account_archive"


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "active_game_accounts" in tables:
        raise RuntimeError("cannot finalize WU6B while active_game_accounts still exists")
    if ARCHIVE_TABLE in tables:
        op.drop_table(ARCHIVE_TABLE)


def downgrade() -> None:
    # The retired selection rows cannot be reconstructed after archive cleanup.
    # Restore the required pre-upgrade database snapshot for a data rollback.
    pass
