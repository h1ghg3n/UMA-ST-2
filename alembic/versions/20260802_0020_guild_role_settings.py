"""store guild-scoped Discord authorization Roles

Revision ID: 20260802_0020
Revises: 20260801_0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260802_0020"
down_revision: str | Sequence[str] | None = "20260801_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("guild_discord_settings") as batch:
        batch.add_column(sa.Column("operator_role_id", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("bot_manager_role_id", sa.String(length=32), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    has_configured_roles = connection.execute(
        sa.text(
            "SELECT 1 FROM guild_discord_settings "
            "WHERE operator_role_id IS NOT NULL OR bot_manager_role_id IS NOT NULL LIMIT 1"
        )
    ).first()
    if has_configured_roles is not None:
        raise RuntimeError("role settings downgrade refused after operational role configuration")
    with op.batch_alter_table("guild_discord_settings") as batch:
        batch.drop_column("bot_manager_role_id")
        batch.drop_column("operator_role_id")
