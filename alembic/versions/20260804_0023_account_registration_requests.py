"""add Persona-safe account registration requests

Revision ID: 20260804_0023
Revises: 20260803_0022
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_0023"
down_revision: str | Sequence[str] | None = "20260803_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
_GAME_ACCOUNT_DISCORD_UNIQUE = "uq_game_accounts_discord_account_id"
_GAME_ACCOUNT_DISCORD_INDEX = "ix_game_accounts_discord_account_id"


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    request_tables = {"account_registration_requests", "account_registration_operation_audits"}
    if request_tables <= tables:
        return
    if request_tables & tables:
        raise RuntimeError("cannot upgrade account registration requests: target schema is incomplete")
    _make_game_account_discord_pointer_optional()
    op.create_table(
        "account_registration_requests",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.String(length=32), nullable=False),
        sa.Column("requester_discord_user_id", sa.String(length=32), nullable=False),
        sa.Column("discord_nickname_snapshot", sa.String(length=100), nullable=False),
        sa.Column("submitted_uma_pid", sa.String(length=32), nullable=False),
        sa.Column("submitted_nickname", sa.String(length=100), nullable=True),
        sa.Column("submitted_ingame_name", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("active_request_marker", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("reviewed_by_discord_user_id", sa.String(length=32), nullable=True),
        sa.Column("review_note", sa.String(length=255), nullable=True),
        sa.Column("accepted_persona_id", sa.String(length=36), nullable=True),
        sa.Column("accepted_game_account_id", BIGINT, nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["accepted_persona_id"], ["personas.id"], name="fk_account_registration_requests_accepted_persona"
        ),
        sa.ForeignKeyConstraint(
            ["accepted_game_account_id"],
            ["game_accounts.id"],
            name="fk_account_registration_requests_accepted_game_account",
        ),
        sa.UniqueConstraint(
            "guild_id",
            "requester_discord_user_id",
            "active_request_marker",
            name="uq_account_registration_requests_active_request",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_account_registration_requests_idempotency_key"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled')", name="ck_account_registration_requests_status"
        ),
        sa.CheckConstraint(
            "active_request_marker IS NULL OR active_request_marker = 1",
            name="ck_account_registration_requests_active_request_marker",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND active_request_marker = 1 AND reviewed_by_discord_user_id IS NULL "
            "AND accepted_persona_id IS NULL AND accepted_game_account_id IS NULL "
            "AND resolved_at IS NULL) OR "
            "(status = 'approved' AND active_request_marker IS NULL AND reviewed_by_discord_user_id IS NOT NULL "
            "AND accepted_persona_id IS NOT NULL AND accepted_game_account_id IS NOT NULL "
            "AND resolved_at IS NOT NULL) OR "
            "(status = 'rejected' AND active_request_marker IS NULL AND reviewed_by_discord_user_id IS NOT NULL "
            "AND accepted_persona_id IS NULL AND accepted_game_account_id IS NULL "
            "AND resolved_at IS NOT NULL) OR "
            "(status = 'cancelled' AND active_request_marker IS NULL AND reviewed_by_discord_user_id IS NULL "
            "AND accepted_persona_id IS NULL AND accepted_game_account_id IS NULL "
            "AND resolved_at IS NOT NULL)",
            name="ck_account_registration_requests_lifecycle",
        ),
    )
    op.create_table(
        "account_registration_operation_audits",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("account_registration_request_id", BIGINT, nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("actor_discord_user_id", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_registration_request_id"],
            ["account_registration_requests.id"],
            name="fk_account_registration_operation_audits_request",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_account_registration_operation_audits_idempotency_key"),
        sa.CheckConstraint(
            "action IN ('submit', 'cancel', 'approve', 'reject')",
            name="ck_account_registration_operation_audits_action",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM account_registration_requests LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade account registration requests after request data exists")
    if (
        bind.execute(sa.text("SELECT 1 FROM game_accounts WHERE discord_account_id IS NULL LIMIT 1")).first()
        is not None
    ):
        raise RuntimeError("cannot restore required Discord pointers while a game account has none")
    duplicate = bind.execute(
        sa.text("SELECT discord_account_id FROM game_accounts GROUP BY discord_account_id HAVING COUNT(*) > 1 LIMIT 1")
    ).first()
    if duplicate is not None:
        raise RuntimeError("cannot restore unique Discord pointers while a Discord account owns multiple games")
    op.drop_table("account_registration_operation_audits")
    op.drop_table("account_registration_requests")
    with op.batch_alter_table("game_accounts") as batch:
        batch.alter_column("discord_account_id", existing_type=BIGINT, nullable=False)
        batch.create_unique_constraint(_GAME_ACCOUNT_DISCORD_UNIQUE, ["discord_account_id"])
        batch.drop_index(_GAME_ACCOUNT_DISCORD_INDEX)


def _make_game_account_discord_pointer_optional() -> None:
    inspector = sa.inspect(op.get_bind())
    main_persona_fk = next(
        (
            foreign_key["name"]
            for foreign_key in inspector.get_foreign_keys("personas")
            if foreign_key["referred_table"] == "game_accounts"
            and tuple(foreign_key["constrained_columns"]) == ("main_game_account_id",)
        ),
        None,
    )
    unique_name = next(
        (
            constraint["name"]
            for constraint in inspector.get_unique_constraints("game_accounts")
            if tuple(constraint["column_names"]) == ("discord_account_id",)
        ),
        None,
    )
    has_nonunique_discord_index = any(
        tuple(index["column_names"]) == ("discord_account_id",) and index["name"] != unique_name
        for index in inspector.get_indexes("game_accounts")
    )
    if main_persona_fk is not None:
        with op.batch_alter_table("personas") as batch:
            batch.drop_constraint(main_persona_fk, type_="foreignkey")
    if unique_name is not None and not has_nonunique_discord_index:
        op.create_index(_GAME_ACCOUNT_DISCORD_INDEX, "game_accounts", ["discord_account_id"], unique=False)
    with op.batch_alter_table("game_accounts") as batch:
        if unique_name is not None:
            batch.drop_constraint(unique_name, type_="unique")
        batch.alter_column("discord_account_id", existing_type=BIGINT, nullable=True)
    if main_persona_fk is not None:
        with op.batch_alter_table("personas") as batch:
            batch.create_foreign_key(
                main_persona_fk,
                "game_accounts",
                ["main_game_account_id"],
                ["id"],
            )
