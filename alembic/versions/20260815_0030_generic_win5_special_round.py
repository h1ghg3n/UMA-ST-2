"""rename the WIN5 special Round persistence value

Revision ID: 20260815_0030
Revises: 20260814_0029
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260815_0030"
down_revision: str | Sequence[str] | None = "20260814_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "win5_rounds"
CONSTRAINT_NAME = "win5_round_type_race"
LEGACY_SPECIAL_TYPE = "breeders_cup_day"
SPECIAL_TYPE = "special"


def upgrade() -> None:
    _replace_special_type(
        source=LEGACY_SPECIAL_TYPE,
        target=SPECIAL_TYPE,
    )


def downgrade() -> None:
    _replace_special_type(
        source=SPECIAL_TYPE,
        target=LEGACY_SPECIAL_TYPE,
    )


def _replace_special_type(*, source: str, target: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE_NAME not in inspector.get_table_names():
        raise RuntimeError(f"cannot rename WIN5 special Round type: missing table {TABLE_NAME}")

    columns = {column["name"] for column in inspector.get_columns(TABLE_NAME)}
    if not {"round_type", "race_id"} <= columns:
        raise RuntimeError("cannot rename WIN5 special Round type: required columns are missing")

    invalid_row = bind.execute(
        sa.text(
            f"SELECT id FROM {TABLE_NAME} "
            "WHERE round_type NOT IN ('normal', :source, :target) "
            "OR (round_type = 'normal' AND race_id IS NULL) "
            "OR (round_type IN (:source, :target) AND race_id IS NOT NULL) "
            "LIMIT 1"
        ),
        {"source": source, "target": target},
    ).first()
    if invalid_row is not None:
        raise RuntimeError("cannot rename WIN5 special Round type: conflicting Round data")

    with op.batch_alter_table(TABLE_NAME) as batch:
        batch.drop_constraint(CONSTRAINT_NAME, type_="check")

    bind.execute(
        sa.text(f"UPDATE {TABLE_NAME} SET round_type = :target WHERE round_type = :source"),
        {"source": source, "target": target},
    )

    check_sql = f"(round_type = 'normal' AND race_id IS NOT NULL) OR (round_type = '{target}' AND race_id IS NULL)"
    with op.batch_alter_table(TABLE_NAME) as batch:
        batch.create_check_constraint(CONSTRAINT_NAME, check_sql)
