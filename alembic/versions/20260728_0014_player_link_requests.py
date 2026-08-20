"""add durable player-link requests and operation audits

Revision ID: 20260728_0014
Revises: 20260726_0013
Create Date: 2026-07-28 15:00:00.000000

This is a forward-only MVP migration. The historical initial revision creates
live ORM metadata, so a fresh full-chain upgrade reaches this revision with
both target tables already present. That exact canonical target is accepted;
partial or malformed target tables are rejected. Recovery from a failed
deployment is the verified pre-deployment database backup, not manual repair
or Alembic downgrade.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260728_0014"
down_revision: str | Sequence[str] | None = "20260726_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PREVIOUS_REVISION = "20260726_0013"
REQUEST_TABLE = "player_link_requests"
AUDIT_TABLE = "player_link_operation_audits"
TARGET_TABLES = {REQUEST_TABLE, AUDIT_TABLE}
BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

REQUEST_STATUS_CHECK = "status IN ('pending', 'review_required', 'approved', 'rejected', 'cancelled')"
REQUEST_MARKER_CHECK = "active_request_marker IS NULL OR active_request_marker = 1"
REQUEST_STATE_CHECK = (
    "(status IN ('pending', 'review_required') AND active_request_marker = 1 "
    "AND selected_game_account_id IS NULL AND reviewed_by_discord_user_id IS NULL "
    "AND resolved_at IS NULL) OR "
    "(status = 'approved' AND active_request_marker IS NULL "
    "AND selected_game_account_id IS NOT NULL AND reviewed_by_discord_user_id IS NOT NULL "
    "AND resolved_at IS NOT NULL) OR "
    "(status = 'rejected' AND active_request_marker IS NULL "
    "AND selected_game_account_id IS NULL AND reviewed_by_discord_user_id IS NOT NULL "
    "AND resolved_at IS NOT NULL) OR "
    "(status = 'cancelled' AND active_request_marker IS NULL "
    "AND selected_game_account_id IS NULL AND reviewed_by_discord_user_id IS NULL "
    "AND resolved_at IS NULL)"
)
AUDIT_ACTION_CHECK = "action IN ('submit', 'cancel', 'review', 'approve', 'reject')"


def upgrade() -> None:
    connection = op.get_bind()
    _require_revision(connection, PREVIOUS_REVISION)
    tables = set(sa.inspect(connection).get_table_names())
    present = tables & TARGET_TABLES
    if present and present != TARGET_TABLES:
        raise RuntimeError("UNSUPPORTED_DATABASE_SHAPE: player-link target table already exists")
    required_predecessor_tables = {
        "alembic_version",
        "game_accounts",
        "discord_accounts",
        "identity_backfill_tasks",
        "room_point_accounts",
        "room_point_transactions",
        "sheet_import_runs",
        "sheet_import_records",
    }
    missing = sorted(required_predecessor_tables - tables)
    if missing:
        raise RuntimeError(
            "UNSUPPORTED_DATABASE_SHAPE: canonical predecessor tables are missing: " + ", ".join(missing)
        )

    if present == TARGET_TABLES:
        _verify_target(connection)
        return
    _create_target()
    _verify_target(connection)


def _create_target() -> None:
    op.create_table(
        REQUEST_TABLE,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.String(32), nullable=False),
        sa.Column("requester_discord_user_id", sa.String(32), nullable=False),
        sa.Column("discord_nickname_snapshot", sa.String(100), nullable=False),
        sa.Column("submitted_ingame_name", sa.String(100), nullable=False),
        sa.Column("submitted_uma_pid", sa.String(32), nullable=False),
        sa.Column("submitted_nickname_chunk", sa.String(100), nullable=True),
        sa.Column("submitted_participation_hint", sa.String(255), nullable=True),
        sa.Column("requester_note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("active_request_marker", sa.Integer(), server_default="1", nullable=True),
        sa.Column("selected_game_account_id", BIGINT, nullable=True),
        sa.Column("reviewed_by_discord_user_id", sa.String(32), nullable=True),
        sa.Column("review_note", sa.String(255), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["selected_game_account_id"],
            ["game_accounts.id"],
            name="fk_player_link_requests_selected_game_account",
        ),
        sa.UniqueConstraint(
            "guild_id",
            "requester_discord_user_id",
            "active_request_marker",
            name="uq_player_link_requests_active_requester",
        ),
        sa.UniqueConstraint(
            "selected_game_account_id",
            name="uq_player_link_requests_selected_game_account",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_player_link_requests_idempotency_key"),
        sa.CheckConstraint(REQUEST_STATUS_CHECK, name="status"),
        sa.CheckConstraint(REQUEST_MARKER_CHECK, name="active_request_marker"),
        sa.CheckConstraint(REQUEST_STATE_CHECK, name="state"),
    )
    op.create_table(
        AUDIT_TABLE,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("player_link_request_id", BIGINT, nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("actor_discord_user_id", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["player_link_request_id"],
            [f"{REQUEST_TABLE}.id"],
            name="fk_player_link_operation_audits_request",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_player_link_operation_audits_idempotency_key"),
        sa.CheckConstraint(AUDIT_ACTION_CHECK, name="action"),
    )


def downgrade() -> None:
    raise RuntimeError(
        "20260728_0014 downgrade is unsupported; restore the verified pre-deployment database backup instead"
    )


def _require_revision(connection: sa.Connection, expected: str) -> None:
    try:
        actual = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    except sa.exc.SQLAlchemyError as exc:
        raise RuntimeError("UNSUPPORTED_DATABASE_SHAPE: Alembic revision is unavailable") from exc
    if actual != expected:
        raise RuntimeError(f"UNSUPPORTED_DATABASE_SHAPE: expected Alembic revision {expected}, found {actual}")


def _verify_target(connection: sa.Connection) -> None:
    inspector = sa.inspect(connection)
    if not TARGET_TABLES.issubset(set(inspector.get_table_names())):
        raise RuntimeError("UNSUPPORTED_DATABASE_SHAPE: player-link target tables are missing")
    _verify_columns(
        inspector,
        REQUEST_TABLE,
        {
            "id",
            "guild_id",
            "requester_discord_user_id",
            "discord_nickname_snapshot",
            "submitted_ingame_name",
            "submitted_uma_pid",
            "submitted_nickname_chunk",
            "submitted_participation_hint",
            "requester_note",
            "status",
            "active_request_marker",
            "selected_game_account_id",
            "reviewed_by_discord_user_id",
            "review_note",
            "idempotency_key",
            "request_fingerprint",
            "resolved_at",
            "created_at",
            "updated_at",
        },
    )
    _verify_columns(
        inspector,
        AUDIT_TABLE,
        {
            "id",
            "player_link_request_id",
            "action",
            "actor_discord_user_id",
            "idempotency_key",
            "request_fingerprint",
            "before_json",
            "after_json",
            "reason",
            "created_at",
        },
    )
    _verify_foreign_key(
        inspector,
        REQUEST_TABLE,
        "fk_player_link_requests_selected_game_account",
        ("selected_game_account_id",),
        "game_accounts",
        ("id",),
    )
    _verify_foreign_key(
        inspector,
        AUDIT_TABLE,
        "fk_player_link_operation_audits_request",
        ("player_link_request_id",),
        REQUEST_TABLE,
        ("id",),
    )
    _verify_unique(
        inspector,
        REQUEST_TABLE,
        "uq_player_link_requests_active_requester",
        ("guild_id", "requester_discord_user_id", "active_request_marker"),
    )
    _verify_unique(
        inspector,
        REQUEST_TABLE,
        "uq_player_link_requests_selected_game_account",
        ("selected_game_account_id",),
    )
    _verify_unique(inspector, REQUEST_TABLE, "uq_player_link_requests_idempotency_key", ("idempotency_key",))
    _verify_unique(inspector, AUDIT_TABLE, "uq_player_link_operation_audits_idempotency_key", ("idempotency_key",))


def _verify_columns(inspector: sa.Inspector, table_name: str, expected: set[str]) -> None:
    actual = {str(column["name"]) for column in inspector.get_columns(table_name)}
    if actual != expected:
        raise RuntimeError(f"UNSUPPORTED_DATABASE_SHAPE: {table_name} columns do not match the canonical target")


def _verify_foreign_key(
    inspector: sa.Inspector,
    table_name: str,
    name: str,
    columns: tuple[str, ...],
    referred_table: str,
    referred_columns: tuple[str, ...],
) -> None:
    foreign_keys = {
        (
            str(foreign_key.get("name")),
            tuple(foreign_key["constrained_columns"]),
            foreign_key["referred_table"],
            tuple(foreign_key["referred_columns"]),
        )
        for foreign_key in inspector.get_foreign_keys(table_name)
    }
    if (name, columns, referred_table, referred_columns) not in foreign_keys:
        raise RuntimeError(f"UNSUPPORTED_DATABASE_SHAPE: {table_name} foreign keys do not match the canonical target")


def _verify_unique(inspector: sa.Inspector, table_name: str, name: str, columns: tuple[str, ...]) -> None:
    unique_constraints = {
        (str(constraint.get("name")), tuple(constraint["column_names"]))
        for constraint in inspector.get_unique_constraints(table_name)
    }
    if (name, columns) not in unique_constraints:
        raise RuntimeError(
            f"UNSUPPORTED_DATABASE_SHAPE: {table_name} unique constraints do not match the canonical target"
        )
