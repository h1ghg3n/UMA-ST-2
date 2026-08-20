"""add immutable Room Match odds snapshots

Revision ID: 20260731_0018
Revises: 20260731_0017
Create Date: 2026-07-31 11:00:00.000000

The snapshot persists operator-confirmed odds together with the final result
revision and settlement-eligible participant multiplier. Later settlement must
read this immutable contract rather than mutable operator input.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260731_0018"
down_revision: str | Sequence[str] | None = "20260731_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def upgrade() -> None:
    op.create_table(
        "room_match_odds_snapshots",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column("race_id", BIGINT_PK, sa.ForeignKey("races.id"), nullable=False),
        sa.Column(
            "match_result_submission_id",
            BIGINT_PK,
            sa.ForeignKey("match_result_submissions.id"),
            nullable=False,
        ),
        sa.Column("settlement_participant_count", sa.Integer(), nullable=False),
        sa.Column("payout_multiplier", sa.Numeric(3, 2), nullable=False),
        sa.Column("confirmed_by_discord_user_id", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "confirmed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "settlement_participant_count > 0",
            name="ck_room_match_odds_snapshots_positive_settlement_participant_count",
        ),
        sa.CheckConstraint(
            "payout_multiplier IN (0.50, 1.00)",
            name="ck_room_match_odds_snapshots_supported_payout_multiplier",
        ),
        sa.UniqueConstraint("race_id", name="uq_room_match_odds_snapshots_race"),
        sa.UniqueConstraint("match_result_submission_id", name="uq_room_match_odds_snapshots_submission"),
    )
    op.create_table(
        "room_match_odds_snapshot_entries",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column(
            "odds_snapshot_id",
            BIGINT_PK,
            sa.ForeignKey("room_match_odds_snapshots.id"),
            nullable=False,
        ),
        sa.Column("bet_type", sa.String(length=32), nullable=False),
        sa.Column("winning_numbers", sa.JSON(), nullable=False),
        sa.Column("declared_payout_rate", sa.Numeric(10, 2), nullable=False),
        sa.Column("effective_payout_rate", sa.Numeric(10, 2), nullable=False),
        sa.CheckConstraint(
            "declared_payout_rate >= 0",
            name="ck_room_match_odds_snapshot_entries_nonnegative_declared_payout_rate",
        ),
        sa.CheckConstraint(
            "effective_payout_rate >= 0",
            name="ck_room_match_odds_snapshot_entries_nonnegative_effective_payout_rate",
        ),
        sa.CheckConstraint(
            "bet_type IN ('win', 'quinella', 'trio')",
            name="ck_room_match_odds_snapshot_entries_supported_bet_type",
        ),
        sa.UniqueConstraint(
            "odds_snapshot_id",
            "bet_type",
            name="uq_room_match_odds_snapshot_entries_bet_type",
        ),
    )


def downgrade() -> None:
    op.drop_table("room_match_odds_snapshot_entries")
    op.drop_table("room_match_odds_snapshots")
