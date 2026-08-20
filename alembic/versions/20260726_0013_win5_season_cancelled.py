"""allow draft WIN5 seasons to be cancelled

Revision ID: 20260726_0013
Revises: 20260726_0012
Create Date: 2026-07-26 04:30:00.000000

Season cancellation is a soft delete. The row and its operation audits remain
available, and database-generated IDs are never reused. The separate operator
season number may be reclaimed after cancelling the latest draft.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260726_0013"
down_revision: str | Sequence[str] | None = "20260726_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("win5_seasons") as batch:
        batch.drop_constraint("win5_season_status", type_="check")
        batch.add_column(
            sa.Column(
                "season_number",
                sa.Integer(),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "season_number_marker",
                sa.Integer(),
                nullable=True,
            )
        )
    connection = op.get_bind()
    connection.execute(sa.text("UPDATE win5_seasons SET season_number = id, season_number_marker = id"))
    with op.batch_alter_table("win5_seasons") as batch:
        batch.alter_column(
            "season_number",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch.create_unique_constraint(
            "uq_win5_seasons_season_number_marker",
            ["season_number_marker"],
        )
        batch.create_check_constraint(
            "win5_season_number_claim",
            "season_number > 0 AND "
            "((status = 'cancelled' AND season_number_marker IS NULL) OR "
            "(status <> 'cancelled' AND season_number_marker = season_number))",
        )
        batch.create_check_constraint(
            "win5_season_status",
            "status IN ('draft', 'active', 'closed', 'cancelled')",
        )


def downgrade() -> None:
    connection = op.get_bind()
    cancelled_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM win5_seasons WHERE status = 'cancelled'")
    ).scalar_one()
    if cancelled_count:
        raise RuntimeError(
            "20260726_0013 downgrade refused: cancelled WIN5 season history "
            "requires the verified pre-deployment database backup"
        )
    with op.batch_alter_table("win5_seasons") as batch:
        batch.drop_constraint("win5_season_status", type_="check")
        batch.drop_constraint(
            "uq_win5_seasons_season_number_marker",
            type_="unique",
        )
        batch.drop_constraint("win5_season_number_claim", type_="check")
        batch.drop_column("season_number_marker")
        batch.drop_column("season_number")
        batch.create_check_constraint(
            "win5_season_status",
            "status IN ('draft', 'active', 'closed')",
        )
