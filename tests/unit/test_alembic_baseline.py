"""Fast structural checks for the current pre-release V2 initial baseline."""

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from alembic import command
from uma_st2.infrastructure.database import Base

CURRENT_REVISION = "20260913_0002"


def _alembic_config(connection: object) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def test_initial_revision_builds_the_canonical_schema_from_empty_database() -> None:
    engine = create_engine("sqlite://")
    try:
        with engine.connect() as connection:
            config = _alembic_config(connection)
            command.upgrade(config, "head")

            assert MigrationContext.configure(connection).get_current_revision() == CURRENT_REVISION
            assert set(Base.metadata.tables) <= set(inspect(connection).get_table_names())

            matches = {column["name"]: column for column in inspect(connection).get_columns("matches")}
            match_result_submissions = {
                column["name"]: column for column in inspect(connection).get_columns("match_result_submissions")
            }
            win5_results = {column["name"]: column for column in inspect(connection).get_columns("win5_results")}
            win5_picks = {column["name"]: column for column in inspect(connection).get_columns("win5_submission_picks")}
            win5_submissions = {
                column["name"]: column for column in inspect(connection).get_columns("win5_submissions")
            }
            win5_rounds = {column["name"]: column for column in inspect(connection).get_columns("win5_rounds")}
            win5_seasons = {column["name"]: column for column in inspect(connection).get_columns("win5_seasons")}
            win5_races = {column["name"]: column for column in inspect(connection).get_columns("win5_races")}
            win5_score_events = {
                column["name"]: column for column in inspect(connection).get_columns("win5_score_events")
            }
            win5_score_items = {
                column["name"]: column for column in inspect(connection).get_columns("win5_score_event_items")
            }
            assert win5_results["race_entry_id"]["nullable"] is True
            assert win5_results["gate_number"]["nullable"] is True
            assert win5_picks["race_entry_id"]["nullable"] is True
            assert win5_picks["gate_number"]["nullable"] is True
            assert win5_submissions["version"]["nullable"] is False
            assert win5_submissions["version"]["default"] == "1"
            assert win5_rounds["source_kind"]["nullable"] is False
            assert win5_rounds["source_kind"]["default"] == "'native_v2'"
            assert win5_rounds["name"]["nullable"] is False
            assert "round_number" not in win5_rounds
            assert win5_seasons["active_marker"]["nullable"] is True
            assert win5_races["void_reason"]["nullable"] is True
            assert win5_races["voided_at"]["nullable"] is True
            assert win5_score_events["operation_id"]["nullable"] is True
            assert win5_score_events["race_id"]["nullable"] is True
            assert win5_score_items["race_id"]["nullable"] is False
            assert win5_score_items["submission_pick_id"]["nullable"] is True
            assert win5_score_items["matched_result_id"]["nullable"] is True
            assert matches["source_kind"]["nullable"] is False
            assert matches["source_kind"]["default"] == "'native_v2'"
            assert matches["terminal_reason"]["nullable"] is True
            assert match_result_submissions["candidate_json"]["nullable"] is False
            assert match_result_submissions["pending_marker"]["nullable"] is True
            assert match_result_submissions["confirmed_marker"]["nullable"] is True

            command.upgrade(config, "head")
            assert MigrationContext.configure(connection).get_current_revision() == CURRENT_REVISION
    finally:
        engine.dispose()
