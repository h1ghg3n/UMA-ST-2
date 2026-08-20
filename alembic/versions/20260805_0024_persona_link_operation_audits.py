"""add immutable Persona link-console operation audits

Revision ID: 20260805_0024
Revises: 20260804_0023
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_0024"
down_revision: str | Sequence[str] | None = "20260804_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
TABLE_NAME = "persona_link_operation_audits"


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if TABLE_NAME in tables:
        return
    op.create_table(
        TABLE_NAME,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("target_persona_id", sa.String(length=36), nullable=False),
        sa.Column("from_persona_id", sa.String(length=36), nullable=True),
        sa.Column("discord_account_id", BIGINT, nullable=True),
        sa.Column("game_account_id", BIGINT, nullable=True),
        sa.Column("before_json", sa.JSON(), nullable=False),
        sa.Column("after_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["target_persona_id"], ["personas.id"], name="fk_persona_link_audits_target_persona"),
        sa.ForeignKeyConstraint(["from_persona_id"], ["personas.id"], name="fk_persona_link_audits_from_persona"),
        sa.ForeignKeyConstraint(
            ["discord_account_id"], ["discord_accounts.id"], name="fk_persona_link_audits_discord_account"
        ),
        sa.ForeignKeyConstraint(["game_account_id"], ["game_accounts.id"], name="fk_persona_link_audits_game_account"),
        sa.UniqueConstraint("operation_id", name="uq_persona_link_operation_audits_operation_id"),
        sa.CheckConstraint(
            "action IN ('discord_attach', 'discord_detach', 'discord_transfer', "
            "'game_attach', 'game_detach', 'game_transfer', 'set_main_game_account', "
            "'set_display_name', 'restore_display_name_source')",
            name="ck_persona_link_operation_audits_action",
        ),
        sa.CheckConstraint("source = 'server_console'", name="ck_persona_link_operation_audits_source"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text(f"SELECT 1 FROM {TABLE_NAME} LIMIT 1")).first() is not None:
        raise RuntimeError("cannot downgrade Persona link console after audit data exists")
    op.drop_table(TABLE_NAME)
