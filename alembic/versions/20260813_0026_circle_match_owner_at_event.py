"""add immutable Circle Match owner-at-event attribution fields

Revision ID: 20260813_0026
Revises: 20260806_0025
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_0026"
down_revision: str | Sequence[str] | None = "20260806_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OWNER_COLUMN = "owner_at_event_persona_id"
ATTRIBUTION_TABLES = ("race_entries", "race_results")


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    required_tables = {*ATTRIBUTION_TABLES, "personas"}
    if not required_tables <= tables:
        missing = ", ".join(sorted(required_tables - tables))
        raise RuntimeError(f"cannot upgrade owner-at-event schema: missing table(s): {missing}")

    for table_name in ATTRIBUTION_TABLES:
        inspector = sa.inspect(bind)
        columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        owner_column = columns.get(OWNER_COLUMN)
        if owner_column is None:
            with op.batch_alter_table(table_name) as batch:
                batch.add_column(sa.Column(OWNER_COLUMN, sa.String(length=36), nullable=True))
        elif (
            not isinstance(owner_column["type"], sa.String)
            or owner_column["type"].length != 36
            or not owner_column["nullable"]
        ):
            raise RuntimeError(f"cannot upgrade owner-at-event schema: conflicting {table_name}.{OWNER_COLUMN}")

        inspector = sa.inspect(bind)
        foreign_key_name = f"fk_{table_name}_{OWNER_COLUMN}_personas"
        owner_foreign_keys = _owner_foreign_keys(inspector, table_name)
        if owner_foreign_keys and not (
            len(owner_foreign_keys) == 1
            and _is_canonical_foreign_key(owner_foreign_keys[0], expected_name=foreign_key_name)
        ):
            raise RuntimeError(f"cannot upgrade owner-at-event schema: conflicting foreign key on {table_name}")

        index_name = f"ix_{table_name}_{OWNER_COLUMN}_race_id"
        owner_indexes = _owner_indexes(inspector, table_name)
        canonical_indexes = [index for index in owner_indexes if _is_canonical_index(index, expected_name=index_name)]
        implicit_indexes = [index for index in owner_indexes if _is_repairable_implicit_foreign_key_index(index)]
        repairable_implicit_state = (
            len(owner_foreign_keys) == 1
            and len(canonical_indexes) <= 1
            and len(implicit_indexes) == 1
            and len(owner_indexes) == len(canonical_indexes) + len(implicit_indexes)
        )
        if (
            owner_indexes
            and not (len(owner_indexes) == 1 and len(canonical_indexes) == 1)
            and not repairable_implicit_state
        ):
            raise RuntimeError(f"cannot upgrade owner-at-event schema: conflicting attribution index on {table_name}")
        if not canonical_indexes:
            with op.batch_alter_table(table_name) as batch:
                batch.create_index(
                    index_name,
                    [OWNER_COLUMN, "race_id"],
                    unique=False,
                )

        if repairable_implicit_state:
            inspector = sa.inspect(bind)
            remaining_implicit_indexes = [
                index
                for index in _owner_indexes(inspector, table_name)
                if not _is_canonical_index(index, expected_name=index_name)
            ]
            for implicit_index in remaining_implicit_indexes:
                with op.batch_alter_table(table_name) as batch:
                    batch.drop_index(str(implicit_index["name"]))

        if not owner_foreign_keys:
            with op.batch_alter_table(table_name) as batch:
                batch.create_foreign_key(
                    foreign_key_name,
                    "personas",
                    [OWNER_COLUMN],
                    ["id"],
                )

        _validate_canonical_owner_schema(bind, table_name)


def _owner_foreign_keys(inspector: sa.Inspector, table_name: str) -> list[dict[str, object]]:
    return [
        foreign_key
        for foreign_key in inspector.get_foreign_keys(table_name)
        if OWNER_COLUMN in tuple(foreign_key.get("constrained_columns", ()))
    ]


def _is_canonical_foreign_key(foreign_key: dict[str, object], *, expected_name: str) -> bool:
    options = foreign_key.get("options")
    return (
        foreign_key.get("name") == expected_name
        and tuple(foreign_key.get("constrained_columns", ())) == (OWNER_COLUMN,)
        and foreign_key.get("referred_schema") is None
        and foreign_key.get("referred_table") == "personas"
        and tuple(foreign_key.get("referred_columns", ())) == ("id",)
        and (not isinstance(options, dict) or not any(value is not None for value in options.values()))
    )


def _owner_indexes(inspector: sa.Inspector, table_name: str) -> list[dict[str, object]]:
    return [
        index for index in inspector.get_indexes(table_name) if OWNER_COLUMN in tuple(index.get("column_names", ()))
    ]


def _is_canonical_index(index: dict[str, object], *, expected_name: str) -> bool:
    dialect_options = index.get("dialect_options")
    return (
        index.get("name") == expected_name
        and tuple(index.get("column_names", ())) == (OWNER_COLUMN, "race_id")
        and not bool(index.get("unique"))
        and (not isinstance(dialect_options, dict) or not any(value is not None for value in dialect_options.values()))
    )


def _is_repairable_implicit_foreign_key_index(index: dict[str, object]) -> bool:
    return (
        isinstance(index.get("name"), str)
        and tuple(index.get("column_names", ())) == (OWNER_COLUMN,)
        and not bool(index.get("unique"))
    )


def _validate_canonical_owner_schema(bind: sa.Connection, table_name: str) -> None:
    inspector = sa.inspect(bind)
    foreign_key_name = f"fk_{table_name}_{OWNER_COLUMN}_personas"
    owner_foreign_keys = _owner_foreign_keys(inspector, table_name)
    if not (
        len(owner_foreign_keys) == 1
        and _is_canonical_foreign_key(owner_foreign_keys[0], expected_name=foreign_key_name)
    ):
        raise RuntimeError(f"cannot upgrade owner-at-event schema: noncanonical foreign key on {table_name}")

    index_name = f"ix_{table_name}_{OWNER_COLUMN}_race_id"
    owner_indexes = _owner_indexes(inspector, table_name)
    if not (len(owner_indexes) == 1 and _is_canonical_index(owner_indexes[0], expected_name=index_name)):
        raise RuntimeError(f"cannot upgrade owner-at-event schema: noncanonical attribution index on {table_name}")


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    missing = set(ATTRIBUTION_TABLES) - tables
    if missing:
        raise RuntimeError("cannot downgrade owner-at-event schema: missing table(s): " + ", ".join(sorted(missing)))

    for table_name in ATTRIBUTION_TABLES:
        inspector = sa.inspect(bind)
        if OWNER_COLUMN not in {column["name"] for column in inspector.get_columns(table_name)}:
            continue
        attributed_row = bind.execute(
            sa.text(f"SELECT 1 FROM {table_name} WHERE {OWNER_COLUMN} IS NOT NULL LIMIT 1")
        ).first()
        if attributed_row is not None:
            raise RuntimeError(f"cannot downgrade owner-at-event schema while {table_name} attribution data exists")

    for table_name in reversed(ATTRIBUTION_TABLES):
        inspector = sa.inspect(bind)
        if OWNER_COLUMN not in {column["name"] for column in inspector.get_columns(table_name)}:
            continue
        index_name = f"ix_{table_name}_{OWNER_COLUMN}_race_id"
        foreign_key_name = f"fk_{table_name}_{OWNER_COLUMN}_personas"
        with op.batch_alter_table(table_name) as batch:
            if any(
                foreign_key.get("name") == foreign_key_name for foreign_key in inspector.get_foreign_keys(table_name)
            ):
                batch.drop_constraint(foreign_key_name, type_="foreignkey")
            if any(index.get("name") == index_name for index in inspector.get_indexes(table_name)):
                batch.drop_index(index_name)
            batch.drop_column(OWNER_COLUMN)
