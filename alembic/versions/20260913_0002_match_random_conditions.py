"""Allow random configured weather and track condition.

Revision ID: 20260913_0002
Revises: 20260824_0001
Create Date: 2026-09-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260913_0002"
down_revision: str | None = "20260824_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_WEATHER = sa.Enum("sunny", "cloudy", "rain", "snow", name="match_weather")
_NEW_WEATHER = sa.Enum("sunny", "cloudy", "rain", "snow", "random", name="match_weather")
_OLD_TRACK_CONDITION = sa.Enum("firm", "good", "soft", "heavy", name="match_track_condition")
_NEW_TRACK_CONDITION = sa.Enum("firm", "good", "soft", "heavy", "random", name="match_track_condition")


def upgrade() -> None:
    """Append random to the two configured Match condition enums."""
    if op.get_bind().dialect.name == "sqlite":
        return
    op.alter_column(
        "match_conditions",
        "weather",
        existing_type=_OLD_WEATHER,
        type_=_NEW_WEATHER,
        existing_nullable=False,
    )
    op.alter_column(
        "match_conditions",
        "track_condition",
        existing_type=_OLD_TRACK_CONDITION,
        type_=_NEW_TRACK_CONDITION,
        existing_nullable=False,
    )


def downgrade() -> None:
    """Restore the original enums when no stored row uses random."""
    if op.get_bind().dialect.name == "sqlite":
        return
    op.alter_column(
        "match_conditions",
        "track_condition",
        existing_type=_NEW_TRACK_CONDITION,
        type_=_OLD_TRACK_CONDITION,
        existing_nullable=False,
    )
    op.alter_column(
        "match_conditions",
        "weather",
        existing_type=_NEW_WEATHER,
        type_=_OLD_WEATHER,
        existing_nullable=False,
    )
