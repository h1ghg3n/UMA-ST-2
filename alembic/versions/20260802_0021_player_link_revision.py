"""audit player-link request revisions

Revision ID: 20260802_0021
Revises: 20260802_0020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260802_0021"
down_revision: str | Sequence[str] | None = "20260802_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "player_link_operation_audits"
# The project naming convention expands this logical name to the physical
# MariaDB constraint name. Passing the expanded name would apply it twice.
_CONSTRAINT = "action"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_CONSTRAINT, type_="check")
        batch.create_check_constraint(
            "action",
            "action IN ('submit', 'revise', 'cancel', 'review', 'approve', 'reject')",
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT 1 FROM player_link_operation_audits WHERE action = 'revise' LIMIT 1")
    ).first():
        raise RuntimeError("player-link revision downgrade refused after revision audit exists")
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_CONSTRAINT, type_="check")
        batch.create_check_constraint(
            "action",
            "action IN ('submit', 'cancel', 'review', 'approve', 'reject')",
        )
