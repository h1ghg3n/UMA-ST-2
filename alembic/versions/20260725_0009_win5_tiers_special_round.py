"""apply final WIN5 tier, dual-score, and special-round contract

Revision ID: 20260725_0009
Revises: 20260725_0008
Create Date: 2026-07-25 16:30:00.000000

The only supported direct input is revision 20260725_0008 with no operational
WIN5 data. The old workbook is rebuilt for the final format, so this migration
does not infer tiers, score semantics, or special-Race membership from the
superseded fixed-TOP5 shape.

The target stores one canonical authoritative result per physical Race so a
normal Round and a Breeders' Cup Day Round cannot persist contradictory results.

This revision is stop-the-world and forward-only. Production rollback restores
the verified pre-deployment MariaDB backup.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260725_0009"
down_revision: str | Sequence[str] | None = "20260725_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PREVIOUS_REVISION = "20260725_0008"
BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
WIN5_OPERATIONAL_TABLES = (
    "win5_operation_audits",
    "win5_score_events",
    "win5_judgements",
    "win5_results",
    "win5_picks",
    "win5_entries",
    "win5_scores",
    "win5_rounds",
    "win5_seasons",
)


def upgrade() -> None:
    connection = op.get_bind()
    _preflight(connection)

    with op.batch_alter_table("win5_rounds") as batch:
        batch.add_column(
            sa.Column(
                "round_type",
                sa.String(32),
                server_default="normal",
                nullable=False,
            )
        )
        batch.alter_column(
            "race_id",
            existing_type=BIGINT,
            nullable=True,
        )
        batch.create_check_constraint(
            "win5_round_type_race",
            "(round_type = 'normal' AND race_id IS NOT NULL) OR (round_type = 'breeders_cup_day' AND race_id IS NULL)",
        )

    op.create_table(
        "win5_round_races",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("round_id", BIGINT, nullable=False),
        sa.Column("race_id", BIGINT, nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["round_id"], ["win5_rounds.id"]),
        sa.ForeignKeyConstraint(["race_id"], ["races.id"]),
        sa.UniqueConstraint("id", "round_id", name="uq_win5_round_races_id_round"),
        sa.UniqueConstraint("round_id", "race_id", name="uq_win5_round_races_round_race"),
        sa.UniqueConstraint("round_id", "display_order", name="uq_win5_round_races_round_order"),
        sa.UniqueConstraint("race_id", name="uq_win5_round_races_race_id"),
        sa.CheckConstraint("display_order > 0", name="positive_win5_round_race_order"),
    )

    # MariaDB may use the old UNIQUE indexes as the supporting indexes for
    # existing round foreign keys. Keep temporary FK indexes until the new
    # round-prefixed UNIQUE constraints exist.
    op.create_index(
        "_wu13b_tmp_win5_entries_round_fk",
        "win5_entries",
        ["round_id"],
    )
    with op.batch_alter_table("win5_entries") as batch:
        batch.drop_constraint("uq_win5_entries_accepted_account_round", type_="unique")
        batch.add_column(
            sa.Column(
                "prediction_tier",
                sa.String(32),
                server_default="top5",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("special_round_race_id", BIGINT, nullable=True))
        batch.add_column(sa.Column("normal_accepted_marker", sa.String(16), nullable=True))
        batch.create_foreign_key(
            "fk_win5_entries_special_round_race",
            "win5_round_races",
            ["special_round_race_id", "round_id"],
            ["id", "round_id"],
        )
        batch.create_unique_constraint(
            "uq_win5_entries_normal_accepted_account_round",
            ["round_id", "game_account_id", "normal_accepted_marker"],
        )
        batch.create_unique_constraint(
            "uq_win5_entries_special_accepted_account_race",
            ["special_round_race_id", "game_account_id", "accepted_marker"],
        )
        batch.create_check_constraint(
            "win5_prediction_tier",
            "prediction_tier IN ('top1', 'top3', 'top5', 'special_winner')",
        )
        batch.create_check_constraint(
            "win5_entry_prediction_scope",
            "(prediction_tier IN ('top1', 'top3', 'top5') "
            "AND special_round_race_id IS NULL "
            "AND ((status = 'accepted' AND normal_accepted_marker = 'accepted') "
            "OR (status = 'cancelled' AND normal_accepted_marker IS NULL))) OR "
            "(prediction_tier = 'special_winner' "
            "AND special_round_race_id IS NOT NULL "
            "AND normal_accepted_marker IS NULL)",
        )
        batch.alter_column(
            "prediction_tier",
            existing_type=sa.String(32),
            server_default=None,
        )
    op.drop_index(
        "_wu13b_tmp_win5_entries_round_fk",
        table_name="win5_entries",
    )

    op.create_index(
        "_wu13b_tmp_win5_results_round_fk",
        "win5_results",
        ["round_id"],
    )
    with op.batch_alter_table("win5_results") as batch:
        batch.drop_constraint("uq_win5_results_round_id", type_="unique")
        batch.alter_column(
            "race_id",
            existing_type=BIGINT,
            nullable=False,
        )
        batch.create_unique_constraint(
            "uq_win5_results_race_id",
            ["race_id"],
        )
    op.create_index(
        "ix_win5_results_round_id",
        "win5_results",
        ["round_id"],
    )
    op.drop_index(
        "_wu13b_tmp_win5_results_round_fk",
        table_name="win5_results",
    )

    with op.batch_alter_table("win5_judgements") as batch:
        batch.drop_constraint("win5_judgement_hit_count", type_="check")
        batch.drop_constraint("win5_judgement_score_delta", type_="check")
        batch.add_column(sa.Column("prediction_tier", sa.String(length=32), nullable=False))
        batch.add_column(sa.Column("exact_position_count", sa.Integer(), nullable=False))
        batch.add_column(sa.Column("on_board_wrong_position_count", sa.Integer(), nullable=False))
        batch.add_column(sa.Column("off_board_count", sa.Integer(), nullable=False))
        batch.add_column(sa.Column("season_score_delta", sa.Integer(), nullable=False))
        batch.add_column(sa.Column("top1_score_delta", sa.Integer(), nullable=False))
        batch.drop_column("hit_count")
        batch.drop_column("score_delta")
        batch.create_check_constraint(
            "win5_judgement_exact_count",
            "exact_position_count >= 0 AND exact_position_count <= 5",
        )
        batch.create_check_constraint(
            "win5_judgement_board_count",
            "on_board_wrong_position_count >= 0 AND on_board_wrong_position_count <= 5",
        )
        batch.create_check_constraint(
            "win5_judgement_off_board_count",
            "off_board_count >= 0 AND off_board_count <= 5",
        )
        batch.create_check_constraint(
            "win5_judgement_pick_count",
            "exact_position_count + on_board_wrong_position_count + off_board_count BETWEEN 1 AND 5",
        )
        batch.create_check_constraint(
            "win5_judgement_prediction_tier",
            "prediction_tier IN ('top1', 'top3', 'top5', 'special_winner')",
        )
        batch.create_check_constraint(
            "win5_judgement_season_score",
            "(prediction_tier IN ('top1', 'top3', 'top5') "
            "AND season_score_delta = exact_position_count * 3 + on_board_wrong_position_count) "
            "OR (prediction_tier = 'special_winner' "
            "AND exact_position_count + off_board_count = 1 "
            "AND on_board_wrong_position_count = 0 "
            "AND season_score_delta = exact_position_count)",
        )
        batch.create_check_constraint(
            "win5_judgement_top1_score",
            "(prediction_tier = 'top1' "
            "AND top1_score_delta = CASE WHEN exact_position_count = 1 THEN 3 ELSE 0 END) OR "
            "(prediction_tier IN ('top3', 'top5') AND top1_score_delta = 0) OR "
            "(prediction_tier = 'special_winner' AND top1_score_delta = exact_position_count)",
        )

    with op.batch_alter_table("win5_scores") as batch:
        batch.drop_constraint("nonnegative_win5_score", type_="check")
        batch.alter_column(
            "score",
            existing_type=sa.Integer(),
            new_column_name="season_score",
            existing_nullable=False,
            server_default="0",
        )
        batch.add_column(
            sa.Column(
                "top1_score",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch.create_check_constraint(
            "nonnegative_win5_season_score",
            "season_score >= 0",
        )
        batch.create_check_constraint(
            "nonnegative_win5_top1_score",
            "top1_score >= 0",
        )

    with op.batch_alter_table("win5_score_events") as batch:
        batch.drop_constraint("win5_score_event_delta", type_="check")
        batch.alter_column(
            "score_delta",
            existing_type=sa.Integer(),
            new_column_name="season_score_delta",
            existing_nullable=False,
        )
        batch.add_column(sa.Column("top1_score_delta", sa.Integer(), nullable=False))
        batch.create_check_constraint(
            "win5_score_event_season_delta",
            "season_score_delta >= 0 AND season_score_delta <= 15",
        )
        batch.create_check_constraint(
            "win5_score_event_top1_delta",
            "top1_score_delta >= 0 AND top1_score_delta <= 3",
        )

    _verify_target(sa.inspect(connection))


def downgrade() -> None:
    raise RuntimeError(
        "WU13B corrective downgrade is unsupported; restore the verified pre-deployment database snapshot"
    )


def _preflight(connection: sa.Connection) -> None:
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    required_tables = {
        "alembic_version",
        "races",
        "race_entries",
        *WIN5_OPERATIONAL_TABLES,
    }
    if not required_tables.issubset(tables):
        _unsupported("required 0008 tables are missing")
    current_revision = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    if current_revision != PREVIOUS_REVISION:
        _unsupported("database is not at exact revision 20260725_0008")

    target_markers = {
        "win5_round_races",
    }
    if target_markers & tables:
        _unsupported("corrective target table already exists")
    marker_columns = {
        "win5_rounds": {"round_type"},
        "win5_entries": {
            "prediction_tier",
            "special_round_race_id",
            "normal_accepted_marker",
        },
        "win5_judgements": {
            "prediction_tier",
            "exact_position_count",
            "on_board_wrong_position_count",
            "off_board_count",
            "season_score_delta",
            "top1_score_delta",
        },
        "win5_scores": {"season_score", "top1_score"},
        "win5_score_events": {"season_score_delta", "top1_score_delta"},
    }
    for table_name, markers in marker_columns.items():
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if columns & markers:
            _unsupported(f"corrective target marker exists on {table_name}")

    marker_constraints = {
        "win5_rounds": {"win5_round_type_race"},
        "win5_entries": {
            "fk_win5_entries_special_round_race",
            "uq_win5_entries_normal_accepted_account_round",
            "uq_win5_entries_special_accepted_account_race",
            "win5_prediction_tier",
            "win5_entry_prediction_scope",
        },
        "win5_results": {
            "uq_win5_results_race_id",
            "ix_win5_results_round_id",
        },
        "win5_judgements": {
            "win5_judgement_exact_count",
            "win5_judgement_board_count",
            "win5_judgement_off_board_count",
            "win5_judgement_pick_count",
            "win5_judgement_prediction_tier",
            "win5_judgement_season_score",
            "win5_judgement_top1_score",
        },
        "win5_scores": {
            "nonnegative_win5_season_score",
            "nonnegative_win5_top1_score",
        },
        "win5_score_events": {
            "win5_score_event_season_delta",
            "win5_score_event_top1_delta",
        },
    }
    for table_name, markers in marker_constraints.items():
        actual = {
            *(item["name"] for item in inspector.get_unique_constraints(table_name)),
            *(item["name"] for item in inspector.get_check_constraints(table_name)),
            *(item["name"] for item in inspector.get_foreign_keys(table_name)),
            *(item["name"] for item in inspector.get_indexes(table_name)),
        }
        if markers & actual:
            _unsupported(f"corrective target constraint or index exists on {table_name}")

    temporary_indexes = {
        "_wu13b_tmp_win5_entries_round_fk",
        "_wu13b_tmp_win5_results_round_fk",
    }
    for table_name in ("win5_entries", "win5_results"):
        actual_indexes = {item["name"] for item in inspector.get_indexes(table_name)}
        if temporary_indexes & actual_indexes:
            _unsupported(f"corrective temporary index exists on {table_name}")

    for table_name in WIN5_OPERATIONAL_TABLES:
        if connection.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first() is not None:
            _unsupported(f"{table_name} must be empty; rebuild WIN5 data in the final format")


def _verify_target(inspector: sa.Inspector) -> None:
    tables = set(inspector.get_table_names())
    if "win5_round_races" not in tables:
        raise RuntimeError("WU13B corrective target verification failed for special Round races")

    required_columns = {
        "win5_rounds": {"round_type", "race_id"},
        "win5_round_races": {
            "id",
            "round_id",
            "race_id",
            "display_order",
            "created_at",
        },
        "win5_entries": {
            "prediction_tier",
            "special_round_race_id",
            "normal_accepted_marker",
        },
        "win5_judgements": {
            "prediction_tier",
            "exact_position_count",
            "on_board_wrong_position_count",
            "off_board_count",
            "season_score_delta",
            "top1_score_delta",
        },
        "win5_scores": {"season_score", "top1_score"},
        "win5_score_events": {"season_score_delta", "top1_score_delta"},
    }
    for table_name, columns in required_columns.items():
        actual = {column["name"] for column in inspector.get_columns(table_name)}
        if not columns.issubset(actual):
            raise RuntimeError(f"WU13B corrective target verification failed for {table_name} columns")

    required_uniques = {
        "win5_round_races": {
            "uq_win5_round_races_id_round",
            "uq_win5_round_races_round_race",
            "uq_win5_round_races_round_order",
            "uq_win5_round_races_race_id",
        },
        "win5_entries": {
            "uq_win5_entries_normal_accepted_account_round",
            "uq_win5_entries_special_accepted_account_race",
        },
        "win5_results": {"uq_win5_results_race_id"},
    }
    for table_name, names in required_uniques.items():
        actual = {item["name"] for item in inspector.get_unique_constraints(table_name)}
        if not names.issubset(actual):
            raise RuntimeError(f"WU13B corrective target verification failed for {table_name} uniqueness")

    result_indexes = {item["name"] for item in inspector.get_indexes("win5_results")}
    if "ix_win5_results_round_id" not in result_indexes:
        raise RuntimeError("WU13B corrective target verification failed for WIN5 result indexes")


def _unsupported(detail: str) -> None:
    raise RuntimeError(f"UNSUPPORTED_DATABASE_SHAPE: {detail}")
