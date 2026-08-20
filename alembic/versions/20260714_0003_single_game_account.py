"""enforce one game account per Discord account and registration idempotency

Revision ID: 20260714_0003
Revises: 20260711_0002
Create Date: 2026-07-14 00:03:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260714_0003"
down_revision: str | Sequence[str] | None = "20260711_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    _preflight(inspector, tables)
    if "game_accounts" in tables:
        columns = {column["name"] for column in inspector.get_columns("game_accounts")}
        if "discord_account_id" not in columns:
            raise RuntimeError("cannot upgrade game_accounts: discord_account_id is missing")
        if not _has_unique_constraint(inspector, "game_accounts", "discord_account_id"):
            with op.batch_alter_table("game_accounts") as batch:
                batch.create_unique_constraint("uq_game_accounts_discord_account_id", ["discord_account_id"])
        column = next(
            column for column in inspector.get_columns("game_accounts") if column["name"] == "discord_account_id"
        )
        if column["nullable"]:
            with op.batch_alter_table("game_accounts") as batch:
                batch.alter_column("discord_account_id", existing_type=column["type"], nullable=False)

    if "room_point_transactions" in tables:
        transaction_columns = {column["name"] for column in inspector.get_columns("room_point_transactions")}
        if "idempotency_key" not in transaction_columns:
            with op.batch_alter_table("room_point_transactions") as batch:
                batch.add_column(sa.Column("idempotency_key", sa.String(length=128), nullable=True))
        inspector = sa.inspect(op.get_bind())
        if not _has_unique_constraint(inspector, "room_point_transactions", "idempotency_key"):
            with op.batch_alter_table("room_point_transactions") as batch:
                batch.create_unique_constraint("uq_room_point_transactions_idempotency_key", ["idempotency_key"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "room_point_transactions" in tables:
        transaction_columns = {column["name"] for column in inspector.get_columns("room_point_transactions")}
        if "idempotency_key" in transaction_columns:
            constraint = _find_unique_constraint(inspector, "room_point_transactions", "idempotency_key")
            with op.batch_alter_table("room_point_transactions") as batch:
                if constraint is not None:
                    batch.drop_constraint(_constraint_name(constraint), type_="unique")
                batch.drop_column("idempotency_key")

    inspector = sa.inspect(op.get_bind())
    if "game_accounts" not in set(inspector.get_table_names()):
        return
    columns = {column["name"]: column for column in inspector.get_columns("game_accounts")}
    if "discord_account_id" not in columns:
        return
    constraint = _find_unique_constraint(inspector, "game_accounts", "discord_account_id")
    needs_nullable_restore = not columns["discord_account_id"]["nullable"]
    if constraint is None and not needs_nullable_restore:
        return
    with op.batch_alter_table("game_accounts") as batch:
        # MariaDB may use the WU6 unique index to support this column's FK.
        # Restore an ordinary FK-supporting index before removing the unique one.
        if constraint is not None and not _has_non_unique_index(inspector, "game_accounts", "discord_account_id"):
            batch.create_index(
                "fk_game_accounts_discord_account_id_discord_accounts",
                ["discord_account_id"],
                unique=False,
            )
        if constraint is not None:
            batch.drop_constraint(_constraint_name(constraint), type_="unique")
        if needs_nullable_restore:
            batch.alter_column(
                "discord_account_id",
                existing_type=columns["discord_account_id"]["type"],
                nullable=True,
            )


def _preflight(inspector: sa.Inspector, tables: set[str]) -> None:
    if "game_accounts" in tables:
        columns = {column["name"] for column in inspector.get_columns("game_accounts")}
        if "discord_account_id" not in columns:
            raise RuntimeError("cannot upgrade game_accounts: discord_account_id is missing")
        _reject_multiple_or_unowned_game_accounts()
    if "active_game_accounts" in tables:
        _reject_active_owner_mismatch()
    if "room_point_transactions" in tables:
        columns = {column["name"] for column in inspector.get_columns("room_point_transactions")}
        if "idempotency_key" in columns:
            duplicates = (
                op.get_bind()
                .execute(
                    sa.text(
                        "SELECT idempotency_key FROM room_point_transactions "
                        "WHERE idempotency_key IS NOT NULL GROUP BY idempotency_key HAVING COUNT(*) > 1"
                    )
                )
                .first()
            )
            if duplicates is not None:
                raise RuntimeError("cannot upgrade: duplicate room point transaction idempotency key")


def _reject_multiple_or_unowned_game_accounts() -> None:
    bind = op.get_bind()
    multiple = bind.execute(
        sa.text(
            "SELECT discord_account_id FROM game_accounts "
            "WHERE discord_account_id IS NOT NULL "
            "GROUP BY discord_account_id HAVING COUNT(*) > 1"
        )
    ).first()
    if multiple is not None:
        raise RuntimeError("cannot upgrade: a Discord account has multiple game accounts")
    unowned = bind.execute(sa.text("SELECT id FROM game_accounts WHERE discord_account_id IS NULL LIMIT 1")).first()
    if unowned is not None:
        raise RuntimeError("cannot upgrade: a game account has no Discord owner")


def _reject_active_owner_mismatch() -> None:
    mismatch = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT aga.id FROM active_game_accounts AS aga "
                "LEFT JOIN game_accounts AS ga ON ga.id = aga.game_account_id "
                "WHERE ga.id IS NULL OR ga.discord_account_id IS NULL "
                "OR ga.discord_account_id <> aga.discord_account_id LIMIT 1"
            )
        )
        .first()
    )
    if mismatch is not None:
        raise RuntimeError("cannot upgrade: active game account ownership mismatch")


def _has_unique_constraint(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    return _find_unique_constraint(inspector, table_name, column_name) is not None


def _find_unique_constraint(
    inspector: sa.Inspector,
    table_name: str,
    column_name: str,
) -> dict[str, object] | None:
    return next(
        (
            constraint
            for constraint in inspector.get_unique_constraints(table_name)
            if tuple(constraint["column_names"]) == (column_name,)
        ),
        None,
    )


def _constraint_name(constraint: dict[str, object]) -> str:
    name = constraint.get("name")
    if not isinstance(name, str) or not name:
        raise RuntimeError("cannot safely downgrade an unnamed WU6A unique constraint")
    return name


def _has_non_unique_index(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    return any(
        tuple(index["column_names"]) == (column_name,) and not index["unique"]
        for index in inspector.get_indexes(table_name)
    )
