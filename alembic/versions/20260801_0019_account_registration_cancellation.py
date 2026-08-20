"""allow cancelled self-registration identity state

Revision ID: 20260801_0019
Revises: 20260731_0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260801_0019"
down_revision: str | Sequence[str] | None = "20260731_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("game_accounts") as batch:
        batch.drop_constraint(op.f("ck_game_accounts_identity_state"), type_="check")
        batch.create_check_constraint(
            "identity_state",
            "(identity_status = 'confirmed_identity' AND uma_pid IS NOT NULL) OR "
            "(identity_status IN ('pending_identity', 'identity_conflict', 'registration_cancelled') "
            "AND uma_pid IS NULL)",
        )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM game_accounts WHERE identity_status = 'registration_cancelled' LIMIT 1"))
        .first()
    ):
        raise RuntimeError("UNSUPPORTED_DATABASE_SHAPE: cancelled registrations exist; restore the pre-upgrade backup")
    with op.batch_alter_table("game_accounts") as batch:
        batch.drop_constraint(op.f("ck_game_accounts_identity_state"), type_="check")
        batch.create_check_constraint(
            "identity_state",
            "(identity_status = 'confirmed_identity' AND uma_pid IS NOT NULL) OR "
            "(identity_status IN ('pending_identity', 'identity_conflict') AND uma_pid IS NULL)",
        )
