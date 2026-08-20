"""enforce one full-rank WIN5 submission per account and round

Revision ID: 20260725_0008
Revises: 20260724_0007
Create Date: 2026-07-25 00:08:00.000000

The only supported direct input is the exact WU13A schema at revision
20260724_0007. Compatible historical rows must already be TOP5 submissions
with exactly five ordered picks, and at most one accepted submission may exist
per account and round. Unsupported data fails before the first DDL.

This revision is stop-the-world and forward-only. Production rollback restores
the verified pre-deployment MariaDB backup.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260725_0008"
down_revision: str | Sequence[str] | None = "20260724_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PREVIOUS_REVISION = "20260724_0007"
BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
ENTRY_COLUMNS = {
    "id",
    "season_id",
    "round_id",
    "game_account_id",
    "prediction_tier",
    "idempotency_key",
    "request_fingerprint",
    "ordered_picks_fingerprint",
    "accepted_picks_fingerprint",
    "status",
    "cancelled_at",
    "cancelled_by_discord_user_id",
    "created_at",
    "updated_at",
}
PICK_COLUMNS = {"id", "win5_entry_id", "pick_order", "entry_number", "created_at"}
ENTRY_UNIQUES = {
    "uq_win5_entries_idempotency_key",
    "uq_win5_entries_accepted_prediction",
}
ENTRY_CHECKS = {
    "ck_win5_entries_win5_prediction_tier",
    "ck_win5_entries_win5_entry_status",
    "ck_win5_entries_win5_entry_state",
}


def upgrade() -> None:
    connection = op.get_bind()
    _preflight(connection)

    op.add_column("win5_entries", sa.Column("accepted_marker", sa.String(16), nullable=True))
    connection.execute(
        sa.text("UPDATE win5_entries SET accepted_marker = CASE WHEN status = 'accepted' THEN 'accepted' ELSE NULL END")
    )
    with op.batch_alter_table("win5_entries") as batch:
        batch.create_unique_constraint(
            "uq_win5_entries_accepted_account_round",
            ["round_id", "game_account_id", "accepted_marker"],
        )
        batch.drop_constraint("uq_win5_entries_accepted_prediction", type_="unique")
        batch.drop_constraint("win5_prediction_tier", type_="check")
        batch.drop_constraint("win5_entry_state", type_="check")
        batch.drop_column("prediction_tier")
        batch.drop_column("accepted_picks_fingerprint")
        batch.create_check_constraint(
            "win5_entry_state",
            "(status = 'accepted' AND accepted_marker = 'accepted' "
            "AND cancelled_at IS NULL AND cancelled_by_discord_user_id IS NULL) OR "
            "(status = 'cancelled' AND accepted_marker IS NULL "
            "AND cancelled_at IS NOT NULL AND cancelled_by_discord_user_id IS NOT NULL)",
        )
    with op.batch_alter_table("win5_results") as batch:
        batch.create_unique_constraint("uq_win5_results_round_id", ["round_id"])
    with op.batch_alter_table("win5_judgements") as batch:
        batch.create_unique_constraint("uq_win5_judgements_entry_id", ["win5_entry_id"])
        batch.create_check_constraint("win5_judgement_hit_count", "hit_count >= 0 AND hit_count <= 5")
        batch.create_check_constraint("win5_judgement_score_delta", "score_delta = hit_count")
    with op.batch_alter_table("win5_scores") as batch:
        batch.create_unique_constraint(
            "uq_win5_scores_season_account",
            ["season_id", "game_account_id"],
        )
        batch.drop_constraint("uq_win5_scores_season_id", type_="unique")
        batch.create_check_constraint("nonnegative_win5_score", "score >= 0")
    with op.batch_alter_table("win5_score_events") as batch:
        batch.alter_column(
            "win5_entry_id",
            existing_type=BIGINT,
            nullable=False,
        )
        batch.create_unique_constraint("uq_win5_score_events_entry_id", ["win5_entry_id"])
        batch.create_check_constraint(
            "win5_score_event_delta",
            "score_delta >= 0 AND score_delta <= 5",
        )

    _verify_target(sa.inspect(connection))


def downgrade() -> None:
    raise RuntimeError("WU13B downgrade is unsupported; restore the verified pre-deployment database snapshot")


def _preflight(connection: sa.Connection) -> None:
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    if not {"alembic_version", "win5_entries", "win5_picks"}.issubset(tables):
        _unsupported("required WU13A tables are missing")
    current_revision = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    if current_revision != PREVIOUS_REVISION:
        _unsupported("database is not at the exact WU13A predecessor revision")
    if {column["name"] for column in inspector.get_columns("win5_entries")} != ENTRY_COLUMNS:
        _unsupported("win5_entries is not the exact WU13A shape")
    if {column["name"] for column in inspector.get_columns("win5_picks")} != PICK_COLUMNS:
        _unsupported("win5_picks is not the exact WU13A shape")
    if {item["name"] for item in inspector.get_unique_constraints("win5_entries")} != ENTRY_UNIQUES:
        _unsupported("win5_entries unique constraints are not the exact WU13A shape")
    if {item["name"] for item in inspector.get_check_constraints("win5_entries")} != ENTRY_CHECKS:
        _unsupported("win5_entries check constraints are not the exact WU13A shape")

    invalid_entry = connection.execute(
        sa.text(
            "SELECT id FROM win5_entries "
            "WHERE prediction_tier <> 'top5' "
            "OR (status = 'accepted' AND (accepted_picks_fingerprint IS NULL "
            "OR accepted_picks_fingerprint <> ordered_picks_fingerprint "
            "OR cancelled_at IS NOT NULL OR cancelled_by_discord_user_id IS NOT NULL)) "
            "OR (status = 'cancelled' AND (accepted_picks_fingerprint IS NOT NULL "
            "OR cancelled_at IS NULL OR cancelled_by_discord_user_id IS NULL)) "
            "LIMIT 1"
        )
    ).first()
    if invalid_entry is not None:
        _unsupported("existing WIN5 submissions are not compatible full-rank rows")

    duplicate_accepted = connection.execute(
        sa.text(
            "SELECT round_id, game_account_id FROM win5_entries "
            "WHERE status = 'accepted' "
            "GROUP BY round_id, game_account_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate_accepted is not None:
        _unsupported("more than one accepted WIN5 submission exists for an account and round")

    invalid_picks = connection.execute(
        sa.text(
            "SELECT entry.id FROM win5_entries AS entry "
            "LEFT JOIN win5_picks AS pick ON pick.win5_entry_id = entry.id "
            "GROUP BY entry.id "
            "HAVING COUNT(pick.id) <> 5 OR MIN(pick.pick_order) <> 1 "
            "OR MAX(pick.pick_order) <> 5 OR COUNT(DISTINCT pick.pick_order) <> 5 "
            "OR COUNT(DISTINCT pick.entry_number) <> 5 "
            "LIMIT 1"
        )
    ).first()
    if invalid_picks is not None:
        _unsupported("existing WIN5 submissions must contain exactly five distinct ordered picks")
    for table_name in ("win5_results", "win5_judgements", "win5_scores", "win5_score_events"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first() is not None:
            _unsupported(f"{table_name} must be empty before enabling authoritative WU13B scoring")


def _verify_target(inspector: sa.Inspector) -> None:
    columns = {column["name"] for column in inspector.get_columns("win5_entries")}
    if "accepted_marker" not in columns or {"prediction_tier", "accepted_picks_fingerprint"} & columns:
        raise RuntimeError("WU13B target verification failed for win5_entries columns")
    uniques = {item["name"] for item in inspector.get_unique_constraints("win5_entries")}
    if "uq_win5_entries_accepted_account_round" not in uniques:
        raise RuntimeError("WU13B target verification failed for accepted submission uniqueness")
    checks = {item["name"] for item in inspector.get_check_constraints("win5_entries")}
    if "ck_win5_entries_win5_entry_state" not in checks:
        raise RuntimeError("WU13B target verification failed for submission state")
    required_uniques = {
        "win5_results": "uq_win5_results_round_id",
        "win5_judgements": "uq_win5_judgements_entry_id",
        "win5_scores": "uq_win5_scores_season_account",
        "win5_score_events": "uq_win5_score_events_entry_id",
    }
    for table_name, constraint_name in required_uniques.items():
        uniques = {item["name"] for item in inspector.get_unique_constraints(table_name)}
        if constraint_name not in uniques:
            raise RuntimeError(f"WU13B target verification failed for {table_name} uniqueness")
    score_event_columns = {column["name"]: column for column in inspector.get_columns("win5_score_events")}
    if score_event_columns["win5_entry_id"]["nullable"]:
        raise RuntimeError("WU13B target verification failed for score event provenance")


def _unsupported(detail: str) -> None:
    raise RuntimeError(f"UNSUPPORTED_DATABASE_SHAPE: {detail}")
