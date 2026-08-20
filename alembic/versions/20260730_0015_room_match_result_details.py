"""add Room Match result detail fields

Revision ID: 20260730_0015
Revises: 20260728_0014
Create Date: 2026-07-30 12:00:00.000000

The four nullable columns extend the shared RaceResult contract. Existing
legacy rows remain valid, and future OCR adapters must use the same application
DTO as manual input rather than an OCR-specific table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260730_0015"
down_revision: str | Sequence[str] | None = "20260728_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "race_results"
COLUMNS = {
    "character_evaluation_rank": sa.String(32),
    "popularity_rank": sa.Integer(),
    "finish_time_ms": sa.Integer(),
    "finish_margin_text": sa.String(64),
}
CHECKS = {
    "ck_race_results_positive_popularity_rank": "popularity_rank IS NULL OR popularity_rank > 0",
    "ck_race_results_positive_finish_time_ms": "finish_time_ms IS NULL OR finish_time_ms > 0",
}


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if TABLE not in inspector.get_table_names():
        raise RuntimeError("UNSUPPORTED_DATABASE_SHAPE: race_results table is missing")

    existing_columns = {column["name"] for column in inspector.get_columns(TABLE)}
    missing_columns = [name for name in COLUMNS if name not in existing_columns]
    if missing_columns:
        with op.batch_alter_table(TABLE) as batch:
            for name in missing_columns:
                batch.add_column(sa.Column(name, COLUMNS[name], nullable=True))

    existing_checks = _check_names()
    missing_checks = [name for name in CHECKS if name not in existing_checks]
    if missing_checks:
        with op.batch_alter_table(TABLE) as batch:
            for name in missing_checks:
                batch.create_check_constraint(op.f(name), CHECKS[name])

    _validate_target()


def downgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if TABLE not in inspector.get_table_names():
        raise RuntimeError("UNSUPPORTED_DATABASE_SHAPE: race_results table is missing")
    existing_columns = {column["name"] for column in inspector.get_columns(TABLE)}
    present_columns = [name for name in COLUMNS if name in existing_columns]
    if present_columns:
        detail_table = sa.table(TABLE, *(sa.column(name) for name in present_columns))
        populated = connection.execute(
            sa.select(sa.literal(1))
            .select_from(detail_table)
            .where(sa.or_(*(detail_table.c[name].is_not(None) for name in present_columns)))
            .limit(1)
        ).first()
        if populated is not None:
            raise RuntimeError(
                "UNSUPPORTED_DATABASE_SHAPE: result detail data exists; restore the pre-upgrade backup to roll back"
            )

    present_checks = [name for name in CHECKS if name in _check_names()]
    with op.batch_alter_table(TABLE) as batch:
        for name in present_checks:
            batch.drop_constraint(op.f(name), type_="check")
        for name in present_columns:
            batch.drop_column(name)


def _check_names() -> set[str]:
    return {
        str(constraint["name"])
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(TABLE)
        if constraint.get("name")
    }


def _validate_target() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"]: column for column in inspector.get_columns(TABLE)}
    missing_columns = sorted(set(COLUMNS) - set(columns))
    missing_checks = sorted(set(CHECKS) - _check_names())
    wrong_nullability = sorted(name for name in COLUMNS if name in columns and not columns[name]["nullable"])
    if missing_columns or missing_checks or wrong_nullability:
        detail = (
            f"missing_columns={missing_columns}, missing_checks={missing_checks}, "
            f"nonnullable_columns={wrong_nullability}"
        )
        raise RuntimeError(f"UNSUPPORTED_DATABASE_SHAPE: incomplete Room Match result detail schema: {detail}")
