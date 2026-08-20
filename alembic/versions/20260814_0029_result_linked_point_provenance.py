"""add Result-linked Circle Point transaction provenance

Revision ID: 20260814_0029
Revises: 20260814_0028
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0029"
down_revision: str | Sequence[str] | None = "20260814_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "room_point_transactions"
RESULT_TABLE = "race_results"
COLUMN_NAME = "related_race_result_id"
FOREIGN_KEY_NAME = "fk_room_point_transactions_related_race_result_id_race_results"
INDEX_NAME = "ix_room_point_transactions_related_race_result_id"
RESULT_ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    required_tables = {TABLE_NAME, RESULT_TABLE}
    if not required_tables <= tables:
        missing = ", ".join(sorted(required_tables - tables))
        raise RuntimeError(f"cannot add Result-linked Point provenance: missing table(s): {missing}")

    columns = {column["name"]: column for column in sa.inspect(bind).get_columns(TABLE_NAME)}
    existing_column = columns.get(COLUMN_NAME)
    if existing_column is not None:
        _validate_completed_schema(bind, existing_column)
        return

    with op.batch_alter_table(TABLE_NAME) as batch:
        batch.add_column(sa.Column(COLUMN_NAME, RESULT_ID_TYPE, nullable=True))
        batch.create_index(INDEX_NAME, [COLUMN_NAME], unique=False)
        batch.create_foreign_key(
            FOREIGN_KEY_NAME,
            RESULT_TABLE,
            [COLUMN_NAME],
            ["id"],
        )

    result_column = next(column for column in sa.inspect(bind).get_columns(TABLE_NAME) if column["name"] == COLUMN_NAME)
    _validate_completed_schema(bind, result_column)


def _validate_completed_schema(bind: sa.Connection, column: dict[str, object]) -> None:
    column_type = column.get("type")
    expected_type = sa.Integer if bind.dialect.name == "sqlite" else sa.BigInteger
    if (
        not isinstance(column_type, expected_type)
        or not bool(column.get("nullable"))
        or column.get("default") is not None
    ):
        raise RuntimeError(f"cannot add Result-linked Point provenance: conflicting {TABLE_NAME}.{COLUMN_NAME}")

    inspector = sa.inspect(bind)
    result_foreign_keys = [
        foreign_key
        for foreign_key in inspector.get_foreign_keys(TABLE_NAME)
        if COLUMN_NAME in tuple(foreign_key.get("constrained_columns") or ())
    ]
    if not (len(result_foreign_keys) == 1 and _is_canonical_foreign_key(result_foreign_keys[0])):
        raise RuntimeError("cannot add Result-linked Point provenance: noncanonical foreign key")

    result_indexes = [
        index for index in inspector.get_indexes(TABLE_NAME) if COLUMN_NAME in tuple(index.get("column_names") or ())
    ]
    if not (len(result_indexes) == 1 and _is_canonical_index(result_indexes[0])):
        raise RuntimeError("cannot add Result-linked Point provenance: noncanonical index")


def _is_canonical_foreign_key(foreign_key: dict[str, object]) -> bool:
    options = foreign_key.get("options")
    return (
        foreign_key.get("name") == FOREIGN_KEY_NAME
        and tuple(foreign_key.get("constrained_columns") or ()) == (COLUMN_NAME,)
        and foreign_key.get("referred_schema") is None
        and foreign_key.get("referred_table") == RESULT_TABLE
        and tuple(foreign_key.get("referred_columns") or ()) == ("id",)
        and (not isinstance(options, dict) or not any(value is not None for value in options.values()))
    )


def _is_canonical_index(index: dict[str, object]) -> bool:
    dialect_options = index.get("dialect_options")
    return (
        index.get("name") == INDEX_NAME
        and tuple(index.get("column_names") or ()) == (COLUMN_NAME,)
        and not bool(index.get("unique"))
        and (not isinstance(dialect_options, dict) or not any(value is not None for value in dialect_options.values()))
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if TABLE_NAME not in tables:
        raise RuntimeError(f"cannot remove Result-linked Point provenance: missing table {TABLE_NAME}")

    columns = {column["name"] for column in sa.inspect(bind).get_columns(TABLE_NAME)}
    if COLUMN_NAME not in columns:
        raise RuntimeError(f"cannot remove Result-linked Point provenance: missing {TABLE_NAME}.{COLUMN_NAME}")
    if bind.execute(sa.text(f"SELECT 1 FROM {TABLE_NAME} WHERE {COLUMN_NAME} IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("cannot remove Result-linked Point provenance while referenced transaction data exists")

    with op.batch_alter_table(TABLE_NAME) as batch:
        batch.drop_constraint(FOREIGN_KEY_NAME, type_="foreignkey")
        batch.drop_index(INDEX_NAME)
        batch.drop_column(COLUMN_NAME)
