"""store Room Match rating values as decimals

Revision ID: 20260730_0016
Revises: 20260730_0015
Create Date: 2026-07-30 18:30:00.000000

The source workbook carries full decimal Rating values between races and uses
one decimal place only as a display format. This revision removes the
integer-only persistence mismatch without changing rating formulas or creating
RatingEvents.
"""

from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "20260730_0016"
down_revision: str | Sequence[str] | None = "20260730_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TARGET_PRECISION = 30
TARGET_SCALE = 18
TARGET_TYPE = sa.Numeric(TARGET_PRECISION, TARGET_SCALE)

LEGACY_TYPES = {
    "rating_rules": {
        "base_delta": sa.Integer(),
    },
    "race_rating_contexts": {
        "average_rating_before": sa.Numeric(10, 2),
    },
    "rating_events": {
        "rating_before": sa.Integer(),
        "base_delta": sa.Integer(),
        "adjustment_delta": sa.Integer(),
        "total_delta": sa.Integer(),
        "rating_after": sa.Integer(),
    },
}


def upgrade() -> None:
    _validate_supported_shape()
    for table_name, legacy_columns in LEGACY_TYPES.items():
        current = _columns(table_name)
        pending = [column_name for column_name in legacy_columns if not _is_target_type(current[column_name]["type"])]
        if not pending:
            continue
        with op.batch_alter_table(table_name) as batch:
            for column_name in pending:
                column = current[column_name]
                batch.alter_column(
                    column_name,
                    existing_type=column["type"],
                    type_=TARGET_TYPE,
                    existing_nullable=column["nullable"],
                )
    _validate_target()


def downgrade() -> None:
    _validate_supported_shape()
    _validate_lossless_downgrade()
    for table_name, legacy_columns in LEGACY_TYPES.items():
        current = _columns(table_name)
        pending = [column_name for column_name in legacy_columns if _is_target_type(current[column_name]["type"])]
        if not pending:
            continue
        with op.batch_alter_table(table_name) as batch:
            for column_name in pending:
                column = current[column_name]
                batch.alter_column(
                    column_name,
                    existing_type=column["type"],
                    type_=legacy_columns[column_name],
                    existing_nullable=column["nullable"],
                )
    _validate_legacy()


def _validate_supported_shape() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    missing_tables = sorted(set(LEGACY_TYPES) - tables)
    if missing_tables:
        raise RuntimeError(f"UNSUPPORTED_DATABASE_SHAPE: missing Rating tables: {missing_tables}")

    unsupported: list[str] = []
    for table_name, legacy_columns in LEGACY_TYPES.items():
        columns = _columns(table_name)
        for column_name, legacy_type in legacy_columns.items():
            column = columns.get(column_name)
            if column is None:
                unsupported.append(f"{table_name}.{column_name}=missing")
                continue
            actual_type = column["type"]
            if not (_is_target_type(actual_type) or _is_same_legacy_type(actual_type, legacy_type)):
                unsupported.append(f"{table_name}.{column_name}={actual_type}")
    if unsupported:
        raise RuntimeError(f"UNSUPPORTED_DATABASE_SHAPE: incompatible Rating columns: {sorted(unsupported)}")


def _validate_target() -> None:
    incomplete = [
        f"{table_name}.{column_name}"
        for table_name, legacy_columns in LEGACY_TYPES.items()
        for column_name in legacy_columns
        if not _is_target_type(_columns(table_name)[column_name]["type"])
    ]
    if incomplete:
        raise RuntimeError(f"UNSUPPORTED_DATABASE_SHAPE: incomplete decimal Rating schema: {sorted(incomplete)}")


def _validate_legacy() -> None:
    incomplete = [
        f"{table_name}.{column_name}"
        for table_name, legacy_columns in LEGACY_TYPES.items()
        for column_name, legacy_type in legacy_columns.items()
        if not _is_same_legacy_type(_columns(table_name)[column_name]["type"], legacy_type)
    ]
    if incomplete:
        raise RuntimeError(f"UNSUPPORTED_DATABASE_SHAPE: incomplete legacy Rating schema: {sorted(incomplete)}")


def _validate_lossless_downgrade() -> None:
    connection = op.get_bind()
    metadata = sa.MetaData()
    for table_name, legacy_columns in LEGACY_TYPES.items():
        table = sa.Table(table_name, metadata, autoload_with=connection)
        current = _columns(table_name)
        for column_name, legacy_type in legacy_columns.items():
            if not _is_target_type(current[column_name]["type"]):
                continue
            column = table.c[column_name]
            if isinstance(legacy_type, sa.Integer):
                lossy = sa.or_(
                    column != sa.cast(column, sa.BigInteger()),
                    column < -2_147_483_648,
                    column > 2_147_483_647,
                )
            else:
                lossy = sa.or_(
                    column != sa.func.round(column, 2),
                    column < sa.literal(Decimal("-99999999.99"), type_=TARGET_TYPE),
                    column > sa.literal(Decimal("99999999.99"), type_=TARGET_TYPE),
                )
            if (
                connection.execute(sa.select(sa.literal(1)).select_from(table).where(lossy).limit(1)).first()
                is not None
            ):
                raise RuntimeError(
                    "UNSUPPORTED_DATABASE_SHAPE: decimal Rating data cannot be represented "
                    f"by legacy column {table_name}.{column_name}; restore the pre-upgrade backup to roll back"
                )


def _columns(table_name: str) -> dict[str, dict[str, object]]:
    return {str(column["name"]): column for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _is_target_type(value: object) -> bool:
    return isinstance(value, sa.Numeric) and value.precision == TARGET_PRECISION and value.scale == TARGET_SCALE


def _is_same_legacy_type(value: object, expected: sa.types.TypeEngine) -> bool:
    if isinstance(expected, sa.Integer):
        return isinstance(value, sa.Integer)
    return (
        isinstance(value, sa.Numeric)
        and isinstance(expected, sa.Numeric)
        and value.precision == expected.precision
        and value.scale == expected.scale
    )
