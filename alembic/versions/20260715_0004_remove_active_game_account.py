"""remove the retired active game account selection table

Revision ID: 20260715_0004
Revises: 20260714_0003
Create Date: 2026-07-15 00:04:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260715_0004"
down_revision: str | Sequence[str] | None = "20260714_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ARCHIVE_TABLE = "wu6b_active_game_account_archive"


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "active_game_accounts" not in tables:
        return
    _reject_active_owner_mismatch()
    _create_archive_if_missing(tables)
    _copy_active_rows(source_table="active_game_accounts", destination_table=ARCHIVE_TABLE)
    op.drop_table("active_game_accounts")


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "active_game_accounts" not in tables:
        op.create_table(
            "active_game_accounts",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("discord_account_id", sa.BigInteger(), nullable=False),
            sa.Column("game_account_id", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["discord_account_id"], ["discord_accounts.id"]),
            sa.ForeignKeyConstraint(["game_account_id"], ["game_accounts.id"]),
            sa.UniqueConstraint("discord_account_id"),
            mysql_engine="InnoDB",
            mysql_charset="utf8mb4",
            mysql_collate="utf8mb4_unicode_ci",
        )
    if ARCHIVE_TABLE not in tables:
        return
    _copy_active_rows(source_table=ARCHIVE_TABLE, destination_table="active_game_accounts")
    op.drop_table(ARCHIVE_TABLE)


def _create_archive_if_missing(tables: set[str]) -> None:
    if ARCHIVE_TABLE in tables:
        return
    op.create_table(
        ARCHIVE_TABLE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("discord_account_id", sa.BigInteger(), nullable=False),
        sa.Column("game_account_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def _copy_active_rows(*, source_table: str, destination_table: str) -> None:
    columns = "id, discord_account_id, game_account_id, created_at, updated_at"
    dialect_name = op.get_bind().dialect.name
    if dialect_name == "mysql":
        statement = (
            f"INSERT INTO {destination_table} ({columns}) "
            f"SELECT {columns} FROM {source_table} "
            "ON DUPLICATE KEY UPDATE "
            "discord_account_id = VALUES(discord_account_id), "
            "game_account_id = VALUES(game_account_id), "
            "created_at = VALUES(created_at), "
            "updated_at = VALUES(updated_at)"
        )
    else:
        statement = f"INSERT OR REPLACE INTO {destination_table} ({columns}) SELECT {columns} FROM {source_table}"
    op.execute(sa.text(statement))


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
