"""add immutable RatingRule versions and workbook provenance

Revision ID: 20260731_0017
Revises: 20260730_0016
Create Date: 2026-07-31 01:30:00.000000

This revision deliberately does not create rating calculations, odds snapshots,
settlement orchestration, or Discord UI.  Existing unversioned Rating rows are
preserved with a NULL version reference; new engine-created events must provide
an explicit immutable version.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260731_0017"
down_revision: str | Sequence[str] | None = "20260730_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT_PK = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def upgrade() -> None:
    op.create_table(
        "rating_rule_versions",
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("source_identifier", sa.String(length=200), nullable=False),
        sa.Column("source_checksum", sa.String(length=64), nullable=False),
        sa.Column("source_sheet_name", sa.String(length=100), nullable=False),
        sa.Column("source_range", sa.String(length=64), nullable=False),
        sa.Column("rule_set_checksum", sa.String(length=64), nullable=False),
        sa.Column("rule_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("version_number > 0", name="ck_rating_rule_versions_positive_version_number"),
        sa.CheckConstraint("rule_count > 0", name="ck_rating_rule_versions_positive_rule_count"),
        sa.UniqueConstraint("version_number", name="uq_rating_rule_versions_version_number"),
        sa.UniqueConstraint(
            "source_identifier",
            "source_checksum",
            "source_sheet_name",
            "source_range",
            name="uq_rating_rule_versions_source",
        ),
    )
    _replace_rating_rules_table()
    with op.batch_alter_table("rating_events") as batch:
        batch.add_column(sa.Column("rating_rule_version_id", BIGINT_PK, nullable=True))
        batch.create_foreign_key(
            "fk_rating_events_rating_rule_version_id",
            "rating_rule_versions",
            ["rating_rule_version_id"],
            ["id"],
        )
        batch.create_index("ix_rating_events_rating_rule_version_id", ["rating_rule_version_id"])


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM rating_rule_versions LIMIT 1")).first() is not None:
        raise RuntimeError(
            "UNSUPPORTED_DATABASE_SHAPE: immutable RatingRule versions exist; "
            "restore the pre-upgrade backup to roll back"
        )
    if (
        connection.execute(
            sa.text("SELECT 1 FROM rating_events WHERE rating_rule_version_id IS NOT NULL LIMIT 1")
        ).first()
        is not None
    ):
        raise RuntimeError(
            "UNSUPPORTED_DATABASE_SHAPE: RatingEvents reference immutable rule versions; "
            "restore the pre-upgrade backup to roll back"
        )
    with op.batch_alter_table("rating_events") as batch:
        batch.drop_index("ix_rating_events_rating_rule_version_id")
        batch.drop_constraint("fk_rating_events_rating_rule_version_id", type_="foreignkey")
        batch.drop_column("rating_rule_version_id")
    _replace_rating_rules_table(downgrade=True)
    op.drop_table("rating_rule_versions")


def _replace_rating_rules_table(*, downgrade: bool = False) -> None:
    target_name = "rating_rules_replacement"
    version_column = () if downgrade else (sa.Column("rating_rule_version_id", BIGINT_PK, nullable=True),)
    version_constraint = (
        sa.UniqueConstraint(
            "grade", "participant_count", "converted_rank", name="uq_rating_rules_grade_participants_rank"
        )
        if downgrade
        else sa.UniqueConstraint(
            "rating_rule_version_id",
            "grade",
            "participant_count",
            "converted_rank",
            name="uq_rating_rules_version_grade_participants_rank",
        )
    )
    foreign_key = (
        () if downgrade else (sa.ForeignKeyConstraint(["rating_rule_version_id"], ["rating_rule_versions.id"]),)
    )
    op.create_table(
        target_name,
        sa.Column("id", BIGINT_PK, primary_key=True, autoincrement=True),
        *version_column,
        sa.Column("grade", sa.String(length=8), nullable=False),
        sa.Column("participant_count", sa.Integer(), nullable=False),
        sa.Column("converted_rank", sa.Integer(), nullable=False),
        sa.Column("base_delta", sa.Numeric(30, 18), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        version_constraint,
        *foreign_key,
    )
    source_columns = "id, grade, participant_count, converted_rank, base_delta, created_at, updated_at"
    target_columns = source_columns if downgrade else "id, rating_rule_version_id, " + source_columns[4:]
    select_columns = source_columns if downgrade else "id, NULL, " + source_columns[4:]
    op.execute(sa.text(f"INSERT INTO {target_name} ({target_columns}) SELECT {select_columns} FROM rating_rules"))
    op.drop_table("rating_rules")
    op.rename_table(target_name, "rating_rules")
    if not downgrade:
        op.create_index("ix_rating_rules_rating_rule_version_id", "rating_rules", ["rating_rule_version_id"])
