"""Focused contract tests for the canonical V2 ORM metadata."""

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.dialects.mysql.mariadb import MariaDBDialect
from sqlalchemy.schema import CreateTable

from uma_st2.infrastructure.database import Base

EXPECTED_TABLES = {
    "bet_operations",
    "bets",
    "bot_guild_settings",
    "circle_points",
    "discord_accounts",
    "discord_publications",
    "game_account_registration_requests",
    "game_accounts",
    "identity_operations",
    "match_conditions",
    "match_entries",
    "match_operations",
    "match_result_submissions",
    "matches",
    "operations",
    "personas",
    "point_transactions",
    "rating_transactions",
    "rating_rule_versions",
    "rating_rules",
    "ratings",
    "settings_operations",
    "stadium_courses",
    "stadiums",
    "umamusume_variants",
    "umamusumes",
    "win5_operations",
    "win5_race_entries",
    "win5_races",
    "win5_results",
    "win5_rounds",
    "win5_score_event_items",
    "win5_score_events",
    "win5_scores",
    "win5_seasons",
    "win5_submission_picks",
    "win5_submissions",
}


def _unique_column_sets(table_name: str) -> set[tuple[str, ...]]:
    table = Base.metadata.tables[table_name]
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _composite_foreign_keys(table_name: str) -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
    table = Base.metadata.tables[table_name]
    return {
        (
            tuple(element.parent.name for element in constraint.elements),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint) and len(constraint.elements) > 1
    }


def _index_column_sets(table_name: str) -> set[tuple[str, ...]]:
    table = Base.metadata.tables[table_name]
    return {tuple(column.name for column in index.columns) for index in table.indexes}


def test_metadata_contains_exact_canonical_table_set() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_transition_identity_fit_is_physical_schema_compatible() -> None:
    personas = Base.metadata.tables["personas"]
    game_accounts = Base.metadata.tables["game_accounts"]
    requests = Base.metadata.tables["game_account_registration_requests"]

    assert isinstance(personas.c.id.type, String)
    assert personas.c.id.type.length == 36
    assert game_accounts.c.persona_id.nullable is False
    assert game_accounts.c.game_region.nullable is False
    assert game_accounts.c.uma_pid.nullable is True
    assert game_accounts.c.nickname.nullable is False
    assert ("game_region", "uma_pid") in _unique_column_sets("game_accounts")
    assert requests.c.uma_pid.nullable is False
    assert requests.c.discord_display_name_snapshot.nullable is False
    assert requests.c.affiliation.nullable is True


def test_match_source_lifecycle_and_grade_contract_is_physical() -> None:
    matches = Base.metadata.tables["matches"]

    assert matches.c.source_kind.nullable is False
    assert matches.c.source_kind.server_default is not None
    assert str(matches.c.source_kind.server_default.arg) == "'native_v2'"
    assert matches.c.source_kind.type.enums == ["native_v2", "imported_v1"]
    assert matches.c.grade.type.enums == ["G1", "G2", "G3", "LISTED", "OP"]
    assert matches.c.status.type.enums == [
        "scheduled",
        "entry_confirmed",
        "betting_open",
        "betting_closed",
        "result_confirmed",
        "settled",
        "cancelled",
        "voided",
    ]
    assert matches.c.terminal_reason.nullable is True
    assert matches.c.terminal_reason.type.length == 255
    assert ("source_kind", "status") in _index_column_sets("matches")


def test_match_conditions_allow_random_configured_values() -> None:
    conditions = Base.metadata.tables["match_conditions"]

    assert conditions.c.weather.type.enums == ["sunny", "cloudy", "rain", "snow", "random"]
    assert conditions.c.track_condition.type.enums == ["firm", "good", "soft", "heavy", "random"]


def test_match_odds_refresh_cursor_is_guild_scoped_operational_state() -> None:
    settings = Base.metadata.tables["bot_guild_settings"]

    assert settings.c.match_odds_refresh_mode.nullable is False
    assert settings.c.match_odds_refresh_mode.type.enums == ["normal", "live"]
    assert str(settings.c.match_odds_refresh_mode.server_default.arg) == "'normal'"
    assert settings.c.match_odds_refresh_next_at.nullable is True
    assert settings.c.match_odds_refresh_sequence.nullable is False
    assert str(settings.c.match_odds_refresh_sequence.server_default.arg) == "0"
    assert settings.c.match_odds_last_projection_fingerprint.nullable is True
    assert settings.c.match_odds_last_projection_fingerprint.type.length == 64


def test_match_result_submission_revision_markers_and_provenance_are_physical() -> None:
    submissions = Base.metadata.tables["match_result_submissions"]

    assert submissions.c.candidate_json.nullable is False
    assert submissions.c.pending_marker.nullable is True
    assert submissions.c.confirmed_marker.nullable is True
    assert submissions.c.source_kind.type.enums == ["manual", "ocr"]
    assert submissions.c.status.type.enums == ["pending", "confirmed", "rejected", "superseded"]
    assert {
        ("match_id", "revision_number"),
        ("match_id", "pending_marker"),
        ("match_id", "confirmed_marker"),
        ("submitted_operation_id",),
        ("rejected_operation_id",),
        ("confirmed_operation_id",),
    } <= _unique_column_sets("match_result_submissions")
    assert ("match_id",) in _index_column_sets("match_result_submissions")
    for column_name in (
        "submitted_operation_id",
        "rejected_operation_id",
        "confirmed_operation_id",
    ):
        operation_fk = next(iter(submissions.c[column_name].foreign_keys))
        assert operation_fk.target_fullname == "operations.id"
        assert operation_fk.ondelete == "SET NULL"
    assert "submitted_by_discord_user_id" not in submissions.c


def test_rating_rule_versions_are_immutable_provenance_for_transactions() -> None:
    versions = Base.metadata.tables["rating_rule_versions"]
    rules = Base.metadata.tables["rating_rules"]
    transactions = Base.metadata.tables["rating_transactions"]

    assert ("version_number",) in _unique_column_sets("rating_rule_versions")
    assert (
        "source_identifier",
        "source_checksum",
        "source_sheet_name",
        "source_range",
    ) in _unique_column_sets("rating_rule_versions")
    assert (
        "rating_rule_version_id",
        "grade",
        "participant_count",
        "converted_rank",
    ) in _unique_column_sets("rating_rules")
    assert rules.c.rating_rule_version_id.nullable is False
    assert transactions.c.rating_rule_version_id.nullable is False
    assert next(iter(rules.c.rating_rule_version_id.foreign_keys)).target_fullname == "rating_rule_versions.id"
    assert next(iter(transactions.c.rating_rule_version_id.foreign_keys)).target_fullname == "rating_rule_versions.id"
    assert versions.c.rule_set_checksum.type.length == 64


def test_canonical_composite_relationships_are_database_backed() -> None:
    assert _composite_foreign_keys("match_entries") == {
        (
            ("umamusume_variant_id", "umamusume_id"),
            ("umamusume_variants.id", "umamusume_variants.umamusume_id"),
        )
    }
    expected_win5_entry_link = {
        (
            ("race_entry_id", "race_id"),
            ("win5_race_entries.id", "win5_race_entries.race_id"),
        )
    }
    assert _composite_foreign_keys("win5_results") == expected_win5_entry_link
    assert _composite_foreign_keys("win5_submission_picks") == expected_win5_entry_link


def test_match_entry_rating_disposition_and_duplicate_account_lookup_shape() -> None:
    entries = Base.metadata.tables["match_entries"]

    assert entries.c.rating_disposition.nullable is True
    assert entries.c.rating_disposition.type.enums == ["rated", "excluded", "not_applicable"]
    assert ("match_id", "game_account_id") not in _unique_column_sets("match_entries")
    assert ("match_id", "game_account_id") in _index_column_sets("match_entries")


def test_win5_special_gate_payload_columns_are_nullable_discriminator_inputs() -> None:
    results = Base.metadata.tables["win5_results"]
    picks = Base.metadata.tables["win5_submission_picks"]

    assert results.c.race_entry_id.nullable is True
    assert results.c.gate_number.nullable is True
    assert picks.c.race_entry_id.nullable is True
    assert picks.c.gate_number.nullable is True
    assert ("race_id", "position") in _unique_column_sets("win5_results")
    assert ("submission_id", "race_id", "position") in _unique_column_sets("win5_submission_picks")


def test_win5_submission_version_is_a_non_null_server_defaulted_token() -> None:
    version = Base.metadata.tables["win5_submissions"].c.version

    assert version.nullable is False
    assert version.server_default is not None
    assert str(version.server_default.arg) == "1"


def test_win5_round_provenance_and_operator_title_are_physical() -> None:
    rounds = Base.metadata.tables["win5_rounds"]

    assert rounds.c.source_kind.nullable is False
    assert rounds.c.source_kind.server_default is not None
    assert str(rounds.c.source_kind.server_default.arg) == "'native_v2'"
    assert rounds.c.source_kind.type.enums == ["native_v2", "imported_v1"]
    assert ("source_kind", "status") in _index_column_sets("win5_rounds")
    assert rounds.c.name.nullable is False
    assert "round_number" not in rounds.c


def test_win5_season_has_nullable_singleton_active_marker_guard() -> None:
    seasons = Base.metadata.tables["win5_seasons"]

    assert seasons.c.active_marker.nullable is True
    assert ("active_marker",) in _unique_column_sets("win5_seasons")


def test_win5_special_void_fact_and_terminal_vocab_are_physical() -> None:
    rounds = Base.metadata.tables["win5_rounds"]
    races = Base.metadata.tables["win5_races"]
    score_items = Base.metadata.tables["win5_score_event_items"]

    assert races.c.void_reason.nullable is True
    assert races.c.void_reason.type.length == 255
    assert races.c.voided_at.nullable is True
    assert "cancelled" in rounds.c.status.type.enums
    assert "void" in score_items.c.outcome.type.enums


def test_win5_score_events_support_normal_and_special_judgement_provenance() -> None:
    events = Base.metadata.tables["win5_score_events"]
    items = Base.metadata.tables["win5_score_event_items"]

    assert events.c.operation_id.nullable is True
    assert next(iter(events.c.operation_id.foreign_keys)).ondelete == "SET NULL"
    assert events.c.race_id.nullable is True
    assert ("submission_id",) in _unique_column_sets("win5_score_events")
    assert ("round_id", "persona_id") in _unique_column_sets("win5_score_events")
    assert items.c.race_id.nullable is False
    assert items.c.submission_pick_id.nullable is True
    assert items.c.matched_result_id.nullable is True
    assert ("score_event_id", "race_id", "position") in _unique_column_sets("win5_score_event_items")
    assert ("submission_pick_id",) in _unique_column_sets("win5_score_event_items")


def test_only_approved_minimal_check_constraints_are_present() -> None:
    checks = {
        table.name: {constraint.name for constraint in table.constraints if isinstance(constraint, CheckConstraint)}
        for table in Base.metadata.tables.values()
    }
    assert {name: names for name, names in checks.items() if names} == {
        "bets": {"ck_bets_active_marker_is_true_or_null"},
        "game_account_registration_requests": {"ck_game_account_registration_requests_active_marker_valid"},
        "rating_transactions": {"ck_rating_transactions_balanced_rating_delta"},
        "match_result_submissions": {
            "ck_match_result_submissions_confirmed_marker_is_true_or_null",
            "ck_match_result_submissions_pending_marker_is_true_or_null",
        },
        "win5_races": {"ck_win5_races_void_fact_complete"},
        "win5_seasons": {"ck_win5_seasons_active_marker_is_true_or_null"},
        "win5_submissions": {"ck_win5_submissions_active_marker_is_true_or_null"},
    }


def test_history_rows_survive_operation_retention() -> None:
    point_operation_fk = next(iter(Base.metadata.tables["point_transactions"].c.operation_id.foreign_keys))
    rating_operation_fk = next(iter(Base.metadata.tables["rating_transactions"].c.operation_id.foreign_keys))
    result_submissions = Base.metadata.tables["match_result_submissions"]

    assert point_operation_fk.ondelete == "SET NULL"
    assert rating_operation_fk.ondelete == "SET NULL"
    assert all(
        next(iter(result_submissions.c[column_name].foreign_keys)).ondelete == "SET NULL"
        for column_name in (
            "submitted_operation_id",
            "rejected_operation_id",
            "confirmed_operation_id",
        )
    )
    assert not Base.metadata.tables["identity_operations"].c.persona_id.foreign_keys
    assert not Base.metadata.tables["match_operations"].c.match_id.foreign_keys
    assert not Base.metadata.tables["discord_publications"].c.source_id.foreign_keys


def test_all_tables_compile_for_mariadb() -> None:
    dialect = MariaDBDialect()

    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert f"CREATE TABLE {table.name}" in ddl
