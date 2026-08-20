"""initial schema

Revision ID: 20260710_0001
Revises:
Create Date: 2026-07-10 00:01:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import umacircle_bot.db.models  # noqa: F401
from umacircle_bot.db.base import Base

revision: str = "20260710_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
WIN5_TABLES = (
    "win5_operation_audits",
    "win5_score_events",
    "win5_scores",
    "win5_judgements",
    "win5_results",
    "win5_picks",
    "win5_entries",
    "win5_round_races",
    "win5_rounds",
    "win5_seasons",
)
POST_INITIAL_RACE_RESULT_CHECKS = (
    "ck_race_results_positive_popularity_rank",
    "ck_race_results_positive_finish_time_ms",
)
POST_INITIAL_RACE_RESULT_COLUMNS = (
    "character_evaluation_rank",
    "popularity_rank",
    "finish_time_ms",
    "finish_margin_text",
)
POST_INITIAL_RATING_TYPES = {
    "rating_rules": {
        "base_delta": sa.Integer(),
    },
    "race_rating_contexts": {
        "average_rating_before": sa.Numeric(10, 2),
    },
    "rating_events": {
        "rating_before": sa.Integer(),
        "base_delta": sa.Integer(),
        "adjustment_delta": sa.Integer(),
        "total_delta": sa.Integer(),
        "rating_after": sa.Integer(),
    },
}
CURRENT_RATING_TYPE = sa.Numeric(30, 18)
POST_INITIAL_ODDS_SNAPSHOT_TABLES = (
    "room_match_odds_snapshot_entries",
    "room_match_odds_snapshots",
)
POST_INITIAL_GUILD_SETTINGS_COLUMNS = (
    "operator_role_id",
    "bot_manager_role_id",
)
PERSONA_CHILD_TABLES = (
    "discord_accounts",
    "game_accounts",
)
PERSONA_CHILD_FKS = {
    "discord_accounts": "fk_discord_accounts_persona_id_personas",
    "game_accounts": "fk_game_accounts_persona_id_personas",
}
PERSONA_CHILD_INDEXES = {
    "discord_accounts": "ix_discord_accounts_persona_id",
    "game_accounts": "ix_game_accounts_persona_id",
}
OWNER_AT_EVENT_COLUMN = "owner_at_event_persona_id"
OWNER_AT_EVENT_TABLES = (
    "race_entries",
    "race_results",
)
PERSONA_BET_COLUMN = "persona_id"
RESULT_LINKED_POINT_COLUMN = "related_race_result_id"


def upgrade() -> None:
    connection = op.get_bind()
    Base.metadata.create_all(connection)
    _restore_pre_0029_result_linked_point_schema(connection)
    _restore_pre_0027_persona_bet_schema(connection)
    _restore_pre_0026_owner_at_event_schema(connection)
    _restore_pre_0025_persona_wallet_scale_schema(connection)
    _restore_pre_0024_persona_link_console_schema(connection)
    _restore_pre_0023_account_registration_schema(connection)
    _restore_pre_0022_persona_schema(connection)
    _remove_post_initial_odds_snapshot_tables(connection)
    _restore_pre_0020_guild_settings_schema(connection)
    _restore_pre_0017_rating_schema(connection)

    # The original revision used live ORM metadata. Keep fresh-chain behavior
    # deterministic by removing fields and type changes owned by later
    # revisions and restoring the WIN5 prototype that this historical revision
    # introduced.
    with op.batch_alter_table("race_results") as batch:
        for constraint_name in POST_INITIAL_RACE_RESULT_CHECKS:
            batch.drop_constraint(op.f(constraint_name), type_="check")
        for column_name in POST_INITIAL_RACE_RESULT_COLUMNS:
            batch.drop_column(column_name)
    for table_name, columns in POST_INITIAL_RATING_TYPES.items():
        with op.batch_alter_table(table_name) as batch:
            for column_name, historical_type in columns.items():
                batch.alter_column(
                    column_name,
                    existing_type=CURRENT_RATING_TYPE,
                    type_=historical_type,
                    existing_nullable=table_name == "race_rating_contexts",
                )
    for table_name in WIN5_TABLES:
        Base.metadata.tables[table_name].drop(connection, checkfirst=True)
    _create_win5_prototype(connection)


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())


def _remove_post_initial_odds_snapshot_tables(connection: sa.Connection) -> None:
    """Remove tables introduced after the metadata-driven initial revision."""
    for table_name in POST_INITIAL_ODDS_SNAPSHOT_TABLES:
        Base.metadata.tables[table_name].drop(connection, checkfirst=True)


def _restore_pre_0029_result_linked_point_schema(connection: sa.Connection) -> None:
    """Keep live ORM metadata from leaking the 0029 Result FK into 0001."""

    table_name = "room_point_transactions"
    inspector = sa.inspect(connection)
    if RESULT_LINKED_POINT_COLUMN not in {column["name"] for column in inspector.get_columns(table_name)}:
        return
    with op.batch_alter_table(table_name) as batch:
        for foreign_key in inspector.get_foreign_keys(table_name):
            if RESULT_LINKED_POINT_COLUMN in tuple(foreign_key.get("constrained_columns") or ()):
                batch.drop_constraint(str(foreign_key["name"]), type_="foreignkey")
        for index in inspector.get_indexes(table_name):
            if RESULT_LINKED_POINT_COLUMN in tuple(index.get("column_names") or ()):
                batch.drop_index(str(index["name"]))
        batch.drop_column(RESULT_LINKED_POINT_COLUMN)


def _restore_pre_0027_persona_bet_schema(connection: sa.Connection) -> None:
    """Keep live ORM metadata from leaking the 0027 Bet owner into 0001."""

    inspector = sa.inspect(connection)
    if PERSONA_BET_COLUMN not in {column["name"] for column in inspector.get_columns("bets")}:
        return
    with op.batch_alter_table("bets") as batch:
        for foreign_key in inspector.get_foreign_keys("bets"):
            if PERSONA_BET_COLUMN in tuple(foreign_key.get("constrained_columns") or ()):
                batch.drop_constraint(str(foreign_key["name"]), type_="foreignkey")
        for index in inspector.get_indexes("bets"):
            if PERSONA_BET_COLUMN in tuple(index.get("column_names") or ()):
                batch.drop_index(str(index["name"]))
        batch.drop_column(PERSONA_BET_COLUMN)


def _restore_pre_0026_owner_at_event_schema(connection: sa.Connection) -> None:
    """Keep live ORM metadata from leaking the 0026 attribution schema into 0001."""

    inspector = sa.inspect(connection)
    for table_name in OWNER_AT_EVENT_TABLES:
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if OWNER_AT_EVENT_COLUMN not in columns:
            continue
        foreign_key_name = f"fk_{table_name}_{OWNER_AT_EVENT_COLUMN}_personas"
        index_name = f"ix_{table_name}_{OWNER_AT_EVENT_COLUMN}_race_id"
        with op.batch_alter_table(table_name) as batch:
            if any(
                foreign_key.get("name") == foreign_key_name for foreign_key in inspector.get_foreign_keys(table_name)
            ):
                batch.drop_constraint(foreign_key_name, type_="foreignkey")
            if any(index.get("name") == index_name for index in inspector.get_indexes(table_name)):
                batch.drop_index(index_name)
            batch.drop_column(OWNER_AT_EVENT_COLUMN)
        inspector = sa.inspect(connection)


def _restore_pre_0022_persona_schema(connection: sa.Connection) -> None:
    """Keep the metadata-driven historical baseline before Persona WU-P1."""

    inspector = sa.inspect(connection)
    for table_name in PERSONA_CHILD_TABLES:
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if "persona_id" not in columns:
            continue
        with op.batch_alter_table(table_name) as batch:
            if any(
                foreign_key.get("name") == PERSONA_CHILD_FKS[table_name]
                for foreign_key in inspector.get_foreign_keys(table_name)
            ):
                batch.drop_constraint(op.f(PERSONA_CHILD_FKS[table_name]), type_="foreignkey")
            if any(
                index.get("name") == PERSONA_CHILD_INDEXES[table_name] for index in inspector.get_indexes(table_name)
            ):
                batch.drop_index(op.f(PERSONA_CHILD_INDEXES[table_name]))
            batch.drop_column("persona_id")
        inspector = sa.inspect(connection)
    if "personas" in set(inspector.get_table_names()):
        op.drop_table("personas")


def _restore_pre_0023_account_registration_schema(connection: sa.Connection) -> None:
    """Keep the metadata-driven historical baseline before request-based registration."""

    for table_name in ("account_registration_operation_audits", "account_registration_requests"):
        Base.metadata.tables[table_name].drop(connection, checkfirst=True)
    inspector = sa.inspect(connection)
    unique_names = [
        constraint["name"]
        for constraint in inspector.get_unique_constraints("game_accounts")
        if tuple(constraint["column_names"]) == ("discord_account_id",)
    ]
    with op.batch_alter_table("game_accounts") as batch:
        for constraint_name in unique_names:
            batch.drop_constraint(constraint_name, type_="unique")
        batch.alter_column("discord_account_id", existing_type=BIGINT, nullable=False)
        batch.create_unique_constraint("uq_game_accounts_discord_account_id", ["discord_account_id"])


def _restore_pre_0024_persona_link_console_schema(connection: sa.Connection) -> None:
    """Remove the Persona console audit introduced after the historical baseline."""

    Base.metadata.tables["persona_link_operation_audits"].drop(connection, checkfirst=True)


def _restore_pre_0025_persona_wallet_scale_schema(connection: sa.Connection) -> None:
    """Keep the metadata-driven historical baseline before Persona wallet ownership."""

    inspector = sa.inspect(connection)
    with op.batch_alter_table("room_point_transactions") as batch:
        if "persona_id" in {column["name"] for column in inspector.get_columns("room_point_transactions")}:
            if any(
                foreign_key.get("name") == "fk_room_point_transactions_persona_id_personas"
                for foreign_key in inspector.get_foreign_keys("room_point_transactions")
            ):
                batch.drop_constraint("fk_room_point_transactions_persona_id_personas", type_="foreignkey")
            if any(
                index.get("name") == "ix_room_point_transactions_persona_id"
                for index in inspector.get_indexes("room_point_transactions")
            ):
                batch.drop_index("ix_room_point_transactions_persona_id")
            batch.drop_column("persona_id")
        batch.alter_column(
            "amount",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
            nullable=False,
        )

    inspector = sa.inspect(connection)
    with op.batch_alter_table("room_point_accounts") as batch:
        if "persona_id" in {column["name"] for column in inspector.get_columns("room_point_accounts")}:
            if any(
                foreign_key.get("name") == "fk_room_point_accounts_persona_id_personas"
                for foreign_key in inspector.get_foreign_keys("room_point_accounts")
            ):
                batch.drop_constraint("fk_room_point_accounts_persona_id_personas", type_="foreignkey")
            if any(
                constraint.get("name") == "uq_room_point_accounts_persona_id"
                for constraint in inspector.get_unique_constraints("room_point_accounts")
            ):
                batch.drop_constraint("uq_room_point_accounts_persona_id", type_="unique")
            batch.add_column(sa.Column("game_account_id", BIGINT, nullable=True))
            batch.drop_column("persona_id")
        batch.alter_column(
            "game_account_id",
            existing_type=BIGINT,
            existing_nullable=True,
            nullable=False,
        )
        batch.alter_column(
            "balance",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
            nullable=False,
        )
        batch.create_foreign_key(
            "fk_room_point_accounts_game_account_id_game_accounts",
            "game_accounts",
            ["game_account_id"],
            ["id"],
        )
        batch.create_unique_constraint("uq_room_point_accounts_game_account_id", ["game_account_id"])

    for table_name, column_name in (
        ("bets", "amount"),
        ("bet_judgements", "stake_amount"),
        ("bet_judgements", "payout_amount"),
        ("bet_judgements", "point_delta"),
    ):
        with op.batch_alter_table(table_name) as batch:
            batch.alter_column(
                column_name,
                existing_type=sa.BigInteger(),
                type_=sa.Integer(),
                existing_nullable=False,
                nullable=False,
            )


def _restore_pre_0020_guild_settings_schema(connection: sa.Connection) -> None:
    """Remove authorization fields introduced after the metadata-driven base revision."""
    with op.batch_alter_table("guild_discord_settings") as batch:
        for column_name in POST_INITIAL_GUILD_SETTINGS_COLUMNS:
            batch.drop_column(column_name)


def _restore_pre_0017_rating_schema(connection: sa.Connection) -> None:
    """Remove RatingRule provenance fields that did not exist at revision 0001."""
    with op.batch_alter_table("rating_events") as batch:
        batch.drop_constraint(op.f("fk_rating_events_rating_rule_version_id_rating_rule_versions"), type_="foreignkey")
        batch.drop_index(op.f("ix_rating_events_rating_rule_version_id"))
        batch.drop_column("rating_rule_version_id")
    Base.metadata.tables["rating_rules"].drop(connection, checkfirst=True)
    Base.metadata.tables["rating_rule_versions"].drop(connection, checkfirst=True)
    metadata = sa.MetaData(naming_convention=Base.metadata.naming_convention)
    sa.Table(
        "rating_rules",
        metadata,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("grade", sa.String(length=8), nullable=False),
        sa.Column("participant_count", sa.Integer(), nullable=False),
        sa.Column("converted_rank", sa.Integer(), nullable=False),
        sa.Column("base_delta", CURRENT_RATING_TYPE, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("grade", "participant_count", "converted_rank"),
    )
    metadata.create_all(connection, tables=[metadata.tables["rating_rules"]])


def _create_win5_prototype(connection: sa.Connection) -> None:
    metadata = sa.MetaData(naming_convention=Base.metadata.naming_convention)
    metadata.reflect(bind=connection, only=("game_accounts", "game_events", "races"))
    season = sa.Table(
        "win5_seasons",
        metadata,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    round_ = sa.Table(
        "win5_rounds",
        metadata,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("season_id", BIGINT, sa.ForeignKey("win5_seasons.id"), nullable=False),
        sa.Column("event_id", BIGINT, sa.ForeignKey("game_events.id"), nullable=True),
        sa.Column("race_id", BIGINT, sa.ForeignKey("races.id"), nullable=True),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("round_label", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("opens_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closes_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("season_id", "round_number"),
    )
    entry = sa.Table(
        "win5_entries",
        metadata,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("season_id", BIGINT, sa.ForeignKey("win5_seasons.id"), nullable=False),
        sa.Column("round_id", BIGINT, sa.ForeignKey("win5_rounds.id"), nullable=False),
        sa.Column("event_id", BIGINT, sa.ForeignKey("game_events.id"), nullable=True),
        sa.Column("game_account_id", BIGINT, sa.ForeignKey("game_accounts.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("round_id", "game_account_id"),
    )
    pick = sa.Table(
        "win5_picks",
        metadata,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("win5_entry_id", BIGINT, sa.ForeignKey("win5_entries.id"), nullable=False),
        sa.Column("race_id", BIGINT, sa.ForeignKey("races.id"), nullable=True),
        sa.Column("pick_order", sa.Integer(), nullable=False),
        sa.Column("entry_number", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    result = sa.Table(
        "win5_results",
        metadata,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("round_id", BIGINT, sa.ForeignKey("win5_rounds.id"), nullable=False),
        sa.Column("event_id", BIGINT, sa.ForeignKey("game_events.id"), nullable=True),
        sa.Column("season_id", BIGINT, sa.ForeignKey("win5_seasons.id"), nullable=False),
        sa.Column("race_id", BIGINT, sa.ForeignKey("races.id"), nullable=True),
        sa.Column("result_order", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    judgement = sa.Table(
        "win5_judgements",
        metadata,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("win5_entry_id", BIGINT, sa.ForeignKey("win5_entries.id"), nullable=False),
        sa.Column("season_id", BIGINT, sa.ForeignKey("win5_seasons.id"), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False),
        sa.Column("score_delta", sa.Integer(), nullable=False),
        sa.Column("judgement_detail_json", sa.JSON(), nullable=True),
        sa.Column("judged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("judged_by_discord_user_id", sa.String(32), nullable=True),
    )
    score = sa.Table(
        "win5_scores",
        metadata,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("season_id", BIGINT, sa.ForeignKey("win5_seasons.id"), nullable=False),
        sa.Column("game_account_id", BIGINT, sa.ForeignKey("game_accounts.id"), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("season_id", "game_account_id"),
    )
    score_event = sa.Table(
        "win5_score_events",
        metadata,
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("season_id", BIGINT, sa.ForeignKey("win5_seasons.id"), nullable=False),
        sa.Column("game_account_id", BIGINT, sa.ForeignKey("game_accounts.id"), nullable=False),
        sa.Column("win5_entry_id", BIGINT, sa.ForeignKey("win5_entries.id"), nullable=True),
        sa.Column("score_delta", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    metadata.create_all(
        connection,
        tables=(season, round_, entry, pick, result, judgement, score, score_event),
    )
