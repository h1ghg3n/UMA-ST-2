"""MariaDB verification for the fresh V2 canonical schema."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.runtime.migration import MigrationContext
from mariadb_test_support import upgrade_to_head
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.rating import RatingRuleSeedCommands, SeedRatingRuleVersion
from uma_st2.application.win5 import (
    CancelWin5Submission,
    SavedWin5Submission,
    SaveWin5Submission,
    Win5SubmissionPickInput,
    Win5SubmissionVersionConflictError,
)
from uma_st2.compose import compose_win5_member_commands
from uma_st2.domain.match import MatchGrade
from uma_st2.domain.rating import RatingRule
from uma_st2.infrastructure.database import (
    Base,
    DatabaseRuntime,
    SqlAlchemyRatingRuleSeedUnitOfWorkFactory,
    SqlAlchemyUnitOfWork,
)
from uma_st2.infrastructure.database.orm import (
    DiscordAccountORM,
    GameAccountORM,
    MatchORM,
    MatchResultSubmissionORM,
    OperationORM,
    PersonaORM,
    RatingRuleORM,
    RatingRuleVersionORM,
    StadiumCourseORM,
    StadiumORM,
    Win5OperationORM,
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5RoundORM,
    Win5SeasonORM,
    Win5SubmissionORM,
    Win5SubmissionPickORM,
)

pytestmark = pytest.mark.integration

CURRENT_REVISION = "20260913_0002"


def test_fresh_mariadb_upgrade_is_current_and_repeatable(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        upgrade_to_head(connection)

        assert MigrationContext.configure(connection).get_current_revision() == CURRENT_REVISION
        assert set(inspect(connection).get_table_names()) == set(Base.metadata.tables) | {"alembic_version"}


def test_mariadb_rating_rule_seed_is_versioned_and_idempotent(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    command_ = SeedRatingRuleVersion(
        source_identifier=f"integration-rating-{suffix}",
        source_checksum=suffix.ljust(64, "0"),
        source_sheet_name="Rate 기준표",
        source_range="A1:D4",
        rules=(
            RatingRule(MatchGrade.G1, 2, 1, Decimal(22)),
            RatingRule(MatchGrade.G1, 2, 2, Decimal("-9.7336")),
        ),
    )
    runtime = DatabaseRuntime.from_engine(migrated_engine)
    commands = RatingRuleSeedCommands(
        CommandRunner(SqlAlchemyRatingRuleSeedUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: datetime(2026, 8, 28, 7, 0, tzinfo=UTC),
    )

    first = commands.seed(command_)
    second = commands.seed(command_)

    assert first.created is True
    assert second.created is False
    assert first.version_id == second.version_id
    with runtime.session_factory() as session:
        stored = session.get(RatingRuleVersionORM, first.version_id)
        assert stored is not None
        assert stored.rule_set_checksum == command_.rule_set_checksum
        rules = tuple(
            session.scalars(
                select(RatingRuleORM)
                .where(RatingRuleORM.rating_rule_version_id == first.version_id)
                .order_by(RatingRuleORM.converted_rank)
            )
        )
        assert [(rule.grade, rule.converted_rank, rule.base_delta) for rule in rules] == [
            ("G1", 1, Decimal("22.000000000000000000")),
            ("G1", 2, Decimal("-9.733600000000000000")),
        ]
        session.execute(delete(RatingRuleORM).where(RatingRuleORM.rating_rule_version_id == first.version_id))
        session.execute(delete(RatingRuleVersionORM).where(RatingRuleVersionORM.id == first.version_id))
        session.commit()


def test_mariadb_schema_uses_utf8mb4_strict_mode_and_round_trips_unicode(
    migrated_engine: Engine,
) -> None:
    persona_id = str(uuid4())
    display_name = "버추얼 트레센-日本語"
    now = datetime.now(UTC).replace(tzinfo=None)

    with migrated_engine.begin() as connection:
        charset, collation, sql_mode = connection.execute(
            text("SELECT @@character_set_database, @@collation_database, @@SESSION.sql_mode")
        ).one()

        assert charset == "utf8mb4"
        assert collation.startswith("utf8mb4_")
        assert "STRICT_ALL_TABLES" in sql_mode or "STRICT_TRANS_TABLES" in sql_mode

        connection.execute(
            PersonaORM.__table__.insert().values(
                id=persona_id,
                display_name=display_name,
                created_at=now,
                updated_at=now,
            )
        )
        stored_name = connection.scalar(select(PersonaORM.display_name).where(PersonaORM.id == persona_id))
        assert stored_name == display_name
        connection.execute(PersonaORM.__table__.delete().where(PersonaORM.id == persona_id))


def test_mariadb_preserves_required_identity_and_composite_constraints(
    migrated_engine: Engine,
) -> None:
    inspector = inspect(migrated_engine)
    game_account_columns = {column["name"]: column for column in inspector.get_columns("game_accounts")}
    game_account_uniques = {
        tuple(constraint["column_names"]) for constraint in inspector.get_unique_constraints("game_accounts")
    }
    match_entry_foreign_keys = {
        (tuple(constraint["constrained_columns"]), tuple(constraint["referred_columns"]))
        for constraint in inspector.get_foreign_keys("match_entries")
    }
    match_entry_columns = {column["name"]: column for column in inspector.get_columns("match_entries")}
    match_entry_uniques = {
        tuple(constraint["column_names"]) for constraint in inspector.get_unique_constraints("match_entries")
    }
    match_entry_indexes = {tuple(index["column_names"]) for index in inspector.get_indexes("match_entries")}

    assert game_account_columns["persona_id"]["nullable"] is False
    assert game_account_columns["game_region"]["nullable"] is False
    assert game_account_columns["uma_pid"]["nullable"] is True
    assert ("game_region", "uma_pid") in game_account_uniques
    assert match_entry_columns["rating_disposition"]["nullable"] is True
    assert ("match_id", "game_account_id") not in match_entry_uniques
    assert ("match_id", "game_account_id") in match_entry_indexes
    assert (
        ("umamusume_variant_id", "umamusume_id"),
        ("id", "umamusume_id"),
    ) in match_entry_foreign_keys


def test_mariadb_match_conditions_allow_random_configured_values(migrated_engine: Engine) -> None:
    columns = {column["name"]: column for column in inspect(migrated_engine).get_columns("match_conditions")}

    assert columns["weather"]["type"].enums == ["sunny", "cloudy", "rain", "snow", "random"]
    assert columns["track_condition"]["type"].enums == ["firm", "good", "soft", "heavy", "random"]


def test_mariadb_match_result_submission_schema_and_singleton_markers(
    migrated_engine: Engine,
) -> None:
    inspector = inspect(migrated_engine)
    match_columns = {column["name"]: column for column in inspector.get_columns("matches")}
    submission_columns = {column["name"]: column for column in inspector.get_columns("match_result_submissions")}
    submission_uniques = {
        tuple(constraint["column_names"]) for constraint in inspector.get_unique_constraints("match_result_submissions")
    }
    submission_checks = {
        constraint["name"] for constraint in inspector.get_check_constraints("match_result_submissions")
    }
    submission_foreign_keys = {
        tuple(constraint["constrained_columns"]): constraint
        for constraint in inspector.get_foreign_keys("match_result_submissions")
    }

    assert match_columns["source_kind"]["nullable"] is False
    assert str(match_columns["source_kind"]["default"]).strip("'") == "native_v2"
    assert match_columns["terminal_reason"]["nullable"] is True
    assert submission_columns["candidate_json"]["nullable"] is False
    assert submission_columns["pending_marker"]["nullable"] is True
    assert submission_columns["confirmed_marker"]["nullable"] is True
    assert {
        ("match_id", "revision_number"),
        ("match_id", "pending_marker"),
        ("match_id", "confirmed_marker"),
        ("submitted_operation_id",),
        ("rejected_operation_id",),
        ("confirmed_operation_id",),
    } <= submission_uniques
    assert submission_checks == {
        "ck_match_result_submissions_confirmed_marker_is_true_or_null",
        "ck_match_result_submissions_pending_marker_is_true_or_null",
    }
    for column_name in (
        "submitted_operation_id",
        "rejected_operation_id",
        "confirmed_operation_id",
    ):
        foreign_key = submission_foreign_keys[(column_name,)]
        assert foreign_key["referred_table"] == "operations"
        assert foreign_key["referred_columns"] == ["id"]
        assert foreign_key["options"].get("ondelete") == "SET NULL"

    unique_suffix = uuid4().hex
    now = datetime.now(UTC).replace(tzinfo=None)
    external_id = int(unique_suffix[:15], 16)
    with migrated_engine.begin() as connection:
        stadium_id = connection.execute(
            StadiumORM.__table__.insert().values(
                external_id=external_id,
                name_jp=f"Result submission stadium {unique_suffix}",
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        course_id = connection.execute(
            StadiumCourseORM.__table__.insert().values(
                stadium_id=stadium_id,
                external_id=external_id + 1,
                surface="turf",
                distance=2000,
                direction="right",
                layout="standard",
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        match_id = connection.execute(
            MatchORM.__table__.insert().values(
                name=f"Result submission Match {unique_suffix}",
                grade="OP",
                stadium_course_id=course_id,
                scheduled_at=now,
                status="result_confirmed",
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]

    def submission_values(
        revision_number: int,
        *,
        status: str,
        pending_marker: bool | None = None,
        confirmed_marker: bool | None = None,
    ) -> dict[str, object]:
        return {
            "match_id": match_id,
            "revision_number": revision_number,
            "source_kind": "manual",
            "status": status,
            "pending_marker": pending_marker,
            "confirmed_marker": confirmed_marker,
            "candidate_json": {"schema_version": 1, "entries": []},
            "created_at": now,
            "updated_at": now,
        }

    try:
        with migrated_engine.begin() as connection:
            assert connection.scalar(select(MatchORM.source_kind).where(MatchORM.id == match_id)) == "native_v2"
            connection.execute(
                MatchResultSubmissionORM.__table__.insert().values(
                    **submission_values(1, status="pending", pending_marker=True)
                )
            )
            connection.execute(
                MatchResultSubmissionORM.__table__.insert().values(
                    **submission_values(2, status="confirmed", confirmed_marker=True)
                )
            )

        with pytest.raises(IntegrityError):
            with migrated_engine.begin() as connection:
                connection.execute(
                    MatchResultSubmissionORM.__table__.insert().values(
                        **submission_values(3, status="pending", pending_marker=True)
                    )
                )

        with pytest.raises(IntegrityError):
            with migrated_engine.begin() as connection:
                connection.execute(
                    MatchResultSubmissionORM.__table__.insert().values(
                        **submission_values(4, status="confirmed", confirmed_marker=True)
                    )
                )

        with pytest.raises(OperationalError) as invalid_marker:
            with migrated_engine.begin() as connection:
                connection.execute(
                    MatchResultSubmissionORM.__table__.insert().values(
                        **submission_values(5, status="superseded", pending_marker=False)
                    )
                )
        assert invalid_marker.value.orig.args[0] == 4025

        with migrated_engine.connect() as connection:
            stored = connection.execute(
                select(
                    MatchResultSubmissionORM.revision_number,
                    MatchResultSubmissionORM.pending_marker,
                    MatchResultSubmissionORM.confirmed_marker,
                )
                .where(MatchResultSubmissionORM.match_id == match_id)
                .order_by(MatchResultSubmissionORM.revision_number)
            ).all()
        assert stored == [(1, True, None), (2, None, True)]
    finally:
        with migrated_engine.begin() as connection:
            connection.execute(delete(MatchResultSubmissionORM).where(MatchResultSubmissionORM.match_id == match_id))
            connection.execute(delete(MatchORM).where(MatchORM.id == match_id))
            connection.execute(delete(StadiumCourseORM).where(StadiumCourseORM.id == course_id))
            connection.execute(delete(StadiumORM).where(StadiumORM.id == stadium_id))


def test_mariadb_win5_special_payload_columns_match_metadata(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)
    result_columns = {column["name"]: column for column in inspector.get_columns("win5_results")}
    pick_columns = {column["name"]: column for column in inspector.get_columns("win5_submission_picks")}
    round_columns = {column["name"]: column for column in inspector.get_columns("win5_rounds")}
    round_indexes = {tuple(index["column_names"]) for index in inspector.get_indexes("win5_rounds")}

    assert result_columns["race_entry_id"]["nullable"] is True
    assert result_columns["gate_number"]["nullable"] is True
    assert pick_columns["race_entry_id"]["nullable"] is True
    assert pick_columns["gate_number"]["nullable"] is True
    assert round_columns["source_kind"]["nullable"] is False
    assert str(round_columns["source_kind"]["default"]).strip("'") == "native_v2"
    assert ("source_kind", "status") in round_indexes
    assert round_columns["name"]["nullable"] is False
    assert "round_number" not in round_columns


def test_mariadb_win5_submission_version_matches_metadata(migrated_engine: Engine) -> None:
    submission_columns = {column["name"]: column for column in inspect(migrated_engine).get_columns("win5_submissions")}

    assert submission_columns["version"]["nullable"] is False
    assert str(submission_columns["version"]["default"]) == "1"


def test_mariadb_win5_season_singleton_marker_matches_metadata(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)
    season_columns = {column["name"]: column for column in inspector.get_columns("win5_seasons")}
    season_uniques = {
        tuple(constraint["column_names"]) for constraint in inspector.get_unique_constraints("win5_seasons")
    }
    season_checks = {constraint["name"] for constraint in inspector.get_check_constraints("win5_seasons")}

    assert season_columns["active_marker"]["nullable"] is True
    assert ("active_marker",) in season_uniques
    assert "ck_win5_seasons_active_marker_is_true_or_null" in season_checks


def test_mariadb_concurrent_win5_season_activation_allows_one_singleton_marker(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    now = datetime.now(UTC).replace(tzinfo=None)
    with migrated_engine.begin() as connection:
        season_ids = tuple(
            connection.execute(
                Win5SeasonORM.__table__.insert().values(
                    name=f"Concurrent activation Season {index} {suffix}",
                    status="draft",
                    active_marker=None,
                    created_at=now,
                    updated_at=now,
                )
            ).inserted_primary_key[0]
            for index in range(1, 3)
        )

    start = Barrier(2)

    def activate(season_id: int) -> str:
        try:
            with migrated_engine.begin() as connection:
                start.wait(timeout=10)
                changed = connection.execute(
                    update(Win5SeasonORM)
                    .where(
                        Win5SeasonORM.id == season_id,
                        Win5SeasonORM.status == "draft",
                        Win5SeasonORM.active_marker.is_(None),
                    )
                    .values(status="active", active_marker=True, updated_at=now)
                )
                assert changed.rowcount == 1
            return "activated"
        except IntegrityError:
            return "conflict"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(activate, season_ids))

        with migrated_engine.connect() as connection:
            stored_states = tuple(
                connection.execute(
                    select(Win5SeasonORM.status, Win5SeasonORM.active_marker)
                    .where(Win5SeasonORM.id.in_(season_ids))
                    .order_by(Win5SeasonORM.id)
                )
            )

        assert sorted(outcomes) == ["activated", "conflict"]
        assert sorted(stored_states) == [("active", True), ("draft", None)]
    finally:
        with migrated_engine.begin() as connection:
            connection.execute(delete(Win5SeasonORM).where(Win5SeasonORM.id.in_(season_ids)))


def test_mariadb_unit_of_work_commit_and_rollback_boundaries(migrated_engine: Engine) -> None:
    runtime = DatabaseRuntime.from_engine(migrated_engine)
    command_runner = CommandRunner(runtime.unit_of_work_factory)
    query_runner = QueryRunner(runtime.unit_of_work_factory)
    key_prefix = f"uow-test-{uuid4()}"
    committed_key = f"{key_prefix}-committed"
    failed_key = f"{key_prefix}-failed"
    query_key = f"{key_prefix}-query"
    keys = [committed_key, failed_key, query_key]
    now = datetime.now(UTC).replace(tzinfo=None)

    def add_operation(unit_of_work: SqlAlchemyUnitOfWork, idempotency_key: str) -> None:
        unit_of_work.session.add(OperationORM(idempotency_key=idempotency_key, created_at=now))
        unit_of_work.session.flush()

    def fail_after_write(unit_of_work: SqlAlchemyUnitOfWork) -> None:
        add_operation(unit_of_work, failed_key)
        raise ValueError("rollback command")

    try:
        command_runner.run(lambda unit_of_work: add_operation(unit_of_work, committed_key))
        with pytest.raises(ValueError, match="rollback command"):
            command_runner.run(fail_after_write)
        query_runner.run(lambda unit_of_work: add_operation(unit_of_work, query_key))

        with migrated_engine.connect() as connection:
            stored_keys = set(
                connection.scalars(select(OperationORM.idempotency_key).where(OperationORM.idempotency_key.in_(keys)))
            )

        assert stored_keys == {committed_key}
    finally:
        with migrated_engine.begin() as connection:
            connection.execute(delete(OperationORM).where(OperationORM.idempotency_key.in_(keys)))


def test_mariadb_concurrent_win5_cancellation_has_one_version_change_and_audit(
    migrated_engine: Engine,
) -> None:
    runtime = DatabaseRuntime.from_engine(migrated_engine)
    commands = compose_win5_member_commands(runtime)
    persona_id = str(uuid4())
    unique_suffix = uuid4().hex
    discord_user_id = str(int(unique_suffix[:15], 16))
    guild_id = str(int(unique_suffix[15:30], 16))
    correlation_id = f"win5-cancel-{unique_suffix}"
    now = datetime.now(UTC).replace(tzinfo=None)

    with migrated_engine.begin() as connection:
        connection.execute(
            PersonaORM.__table__.insert().values(
                id=persona_id,
                display_name="WIN5 취소 테스트",
                status="normal",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            DiscordAccountORM.__table__.insert().values(
                discord_user_id=discord_user_id,
                persona_id=persona_id,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            GameAccountORM.__table__.insert().values(
                persona_id=persona_id,
                game_region="KR",
                uma_pid=f"8{unique_suffix[:15]}",
                nickname="WIN5 취소 테스트 계정",
                affiliation=None,
                created_at=now,
                updated_at=now,
            )
        )
        season_id = connection.execute(
            Win5SeasonORM.__table__.insert().values(
                name=f"Season {unique_suffix}",
                status="active",
                active_marker=True,
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        round_id = connection.execute(
            Win5RoundORM.__table__.insert().values(
                season_id=season_id,
                type="normal",
                status="open",
                name=f"제1회 취소 테스트 {unique_suffix}",
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        race_id = connection.execute(
            Win5RaceORM.__table__.insert().values(
                round_id=round_id,
                name="아리마 기념",
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        race_entry_id = connection.execute(
            Win5RaceEntryORM.__table__.insert().values(
                race_id=race_id,
                gate_number=3,
                name="테스트 우마무스메",
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        submission_id = connection.execute(
            Win5SubmissionORM.__table__.insert().values(
                round_id=round_id,
                persona_id=persona_id,
                tier="TOP3",
                status="accepted",
                active_marker=True,
                version=4,
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        connection.execute(
            Win5SubmissionPickORM.__table__.insert().values(
                submission_id=submission_id,
                race_id=race_id,
                race_entry_id=race_entry_id,
                gate_number=None,
                position=1,
            )
        )

    command_ = CancelWin5Submission(
        round_id=round_id,
        submission_id=submission_id,
        expected_version=4,
        actor_discord_user_id=discord_user_id,
        guild_id=guild_id,
        correlation_id=correlation_id,
        reason="동시 취소 검증",
    )
    start = Barrier(2)

    def cancel_after_barrier() -> object:
        start.wait()
        return commands.cancel_submission(command_)

    try:
        results: list[object] = []
        conflicts: list[Win5SubmissionVersionConflictError] = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(cancel_after_barrier) for _ in range(2)]
            for future in futures:
                try:
                    results.append(future.result(timeout=15))
                except Win5SubmissionVersionConflictError as exc:
                    conflicts.append(exc)

        assert len(results) == 1
        assert len(conflicts) == 1

        with migrated_engine.connect() as connection:
            submission = connection.execute(
                select(
                    Win5SubmissionORM.status,
                    Win5SubmissionORM.active_marker,
                    Win5SubmissionORM.version,
                ).where(Win5SubmissionORM.id == submission_id)
            ).one()
            stored_picks = connection.execute(
                select(
                    Win5SubmissionPickORM.race_id,
                    Win5SubmissionPickORM.position,
                    Win5SubmissionPickORM.race_entry_id,
                    Win5SubmissionPickORM.gate_number,
                ).where(Win5SubmissionPickORM.submission_id == submission_id)
            ).all()
            audit_rows = connection.execute(
                select(
                    OperationORM.guild_id,
                    OperationORM.actor_discord_user_id,
                    OperationORM.reason,
                    Win5OperationORM.season_id,
                    Win5OperationORM.round_id,
                    Win5OperationORM.submission_id,
                    Win5OperationORM.type,
                    Win5OperationORM.before_data,
                    Win5OperationORM.after_data,
                )
                .join(Win5OperationORM, Win5OperationORM.operation_id == OperationORM.id)
                .where(OperationORM.correlation_id == correlation_id)
            ).all()

        assert submission == ("cancelled", None, 5)
        assert stored_picks == [(race_id, 1, race_entry_id, None)]
        assert len(audit_rows) == 1
        audit = audit_rows[0]
        assert audit.guild_id == guild_id
        assert audit.actor_discord_user_id == discord_user_id
        assert audit.reason == "동시 취소 검증"
        assert audit.season_id == season_id
        assert audit.round_id == round_id
        assert audit.submission_id == submission_id
        assert audit.type == "submission_cancelled"
        assert audit.before_data["version"] == 4
        assert audit.after_data["version"] == 5
        assert audit.before_data["picks"] == audit.after_data["picks"]
    finally:
        with migrated_engine.begin() as connection:
            operation_ids = tuple(
                connection.scalars(select(OperationORM.id).where(OperationORM.correlation_id == correlation_id))
            )
            if operation_ids:
                connection.execute(delete(Win5OperationORM).where(Win5OperationORM.operation_id.in_(operation_ids)))
                connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
            connection.execute(
                delete(Win5SubmissionPickORM).where(Win5SubmissionPickORM.submission_id == submission_id)
            )
            connection.execute(delete(Win5SubmissionORM).where(Win5SubmissionORM.id == submission_id))
            connection.execute(delete(Win5RaceEntryORM).where(Win5RaceEntryORM.id == race_entry_id))
            connection.execute(delete(Win5RaceORM).where(Win5RaceORM.id == race_id))
            connection.execute(delete(Win5RoundORM).where(Win5RoundORM.id == round_id))
            connection.execute(delete(Win5SeasonORM).where(Win5SeasonORM.id == season_id))
            connection.execute(delete(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == discord_user_id))
            connection.execute(delete(GameAccountORM).where(GameAccountORM.persona_id == persona_id))
            connection.execute(delete(PersonaORM).where(PersonaORM.id == persona_id))


def test_mariadb_win5_save_serializes_create_replaces_picks_and_skips_noop_audit(
    migrated_engine: Engine,
) -> None:
    commands = compose_win5_member_commands(DatabaseRuntime.from_engine(migrated_engine))
    persona_id = str(uuid4())
    unique_suffix = uuid4().hex
    discord_user_id = str(int(unique_suffix[:15], 16))
    guild_id = str(int(unique_suffix[15:30], 16))
    correlations = tuple(f"win5-save-{phase}-{unique_suffix}" for phase in ("create", "update", "noop"))
    now = datetime.now(UTC).replace(tzinfo=None)

    with migrated_engine.begin() as connection:
        connection.execute(
            PersonaORM.__table__.insert().values(
                id=persona_id,
                display_name="WIN5 저장 테스트",
                status="normal",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            DiscordAccountORM.__table__.insert().values(
                discord_user_id=discord_user_id,
                persona_id=persona_id,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            GameAccountORM.__table__.insert().values(
                persona_id=persona_id,
                game_region="KR",
                uma_pid=f"7{unique_suffix[:15]}",
                nickname="WIN5 저장 테스트 계정",
                affiliation=None,
                created_at=now,
                updated_at=now,
            )
        )
        season_id = connection.execute(
            Win5SeasonORM.__table__.insert().values(
                name=f"Season {unique_suffix}",
                status="active",
                active_marker=True,
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        round_id = connection.execute(
            Win5RoundORM.__table__.insert().values(
                season_id=season_id,
                type="normal",
                status="open",
                name=f"제1회 저장 테스트 {unique_suffix}",
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        race_id = connection.execute(
            Win5RaceORM.__table__.insert().values(
                round_id=round_id,
                name="재팬 컵",
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        race_entry_ids = tuple(
            connection.execute(
                Win5RaceEntryORM.__table__.insert().values(
                    race_id=race_id,
                    gate_number=gate_number,
                    name=f"테스트 우마무스메 {gate_number}",
                    created_at=now,
                    updated_at=now,
                )
            ).inserted_primary_key[0]
            for gate_number in range(1, 4)
        )

    submission_id: int | None = None
    try:
        create_command = SaveWin5Submission(
            round_id=round_id,
            tier="TOP3",
            picks=(
                Win5SubmissionPickInput(race_id, 1, race_entry_id=race_entry_ids[0]),
                Win5SubmissionPickInput(race_id, 2, race_entry_id=race_entry_ids[1]),
            ),
            actor_discord_user_id=discord_user_id,
            guild_id=guild_id,
            correlation_id=correlations[0],
        )
        start = Barrier(2)

        def save_after_barrier() -> SavedWin5Submission:
            start.wait()
            return commands.save_submission(create_command)

        created_results: list[SavedWin5Submission] = []
        create_conflicts: list[Win5SubmissionVersionConflictError] = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(save_after_barrier) for _ in range(2)]
            for future in futures:
                try:
                    created_results.append(future.result(timeout=15))
                except Win5SubmissionVersionConflictError as exc:
                    create_conflicts.append(exc)

        assert len(created_results) == 1
        assert len(create_conflicts) == 1
        created = created_results[0]
        submission_id = created.submission_id
        updated = commands.save_submission(
            SaveWin5Submission(
                round_id=round_id,
                submission_id=submission_id,
                expected_version=1,
                tier="TOP3",
                picks=(Win5SubmissionPickInput(race_id, 1, race_entry_id=race_entry_ids[2]),),
                actor_discord_user_id=discord_user_id,
                guild_id=guild_id,
                correlation_id=correlations[1],
            )
        )
        no_op = commands.save_submission(
            SaveWin5Submission(
                round_id=round_id,
                submission_id=submission_id,
                expected_version=2,
                tier="TOP3",
                picks=(Win5SubmissionPickInput(race_id, 1, race_entry_id=race_entry_ids[2]),),
                actor_discord_user_id=discord_user_id,
                guild_id=guild_id,
                correlation_id=correlations[2],
            )
        )

        assert created.version == 1
        assert updated.version == 2
        assert no_op.version == 2

        with migrated_engine.connect() as connection:
            submission = connection.execute(
                select(
                    Win5SubmissionORM.tier,
                    Win5SubmissionORM.status,
                    Win5SubmissionORM.active_marker,
                    Win5SubmissionORM.version,
                ).where(Win5SubmissionORM.id == submission_id)
            ).one()
            stored_picks = connection.execute(
                select(
                    Win5SubmissionPickORM.race_id,
                    Win5SubmissionPickORM.position,
                    Win5SubmissionPickORM.race_entry_id,
                    Win5SubmissionPickORM.gate_number,
                ).where(Win5SubmissionPickORM.submission_id == submission_id)
            ).all()
            audit_rows = connection.execute(
                select(
                    OperationORM.correlation_id,
                    Win5OperationORM.season_id,
                    Win5OperationORM.round_id,
                    Win5OperationORM.submission_id,
                    Win5OperationORM.type,
                    Win5OperationORM.before_data,
                    Win5OperationORM.after_data,
                )
                .join(Win5OperationORM, Win5OperationORM.operation_id == OperationORM.id)
                .where(OperationORM.correlation_id.in_(correlations))
                .order_by(Win5OperationORM.operation_id)
            ).all()

        assert submission == ("TOP3", "accepted", True, 2)
        assert stored_picks == [(race_id, 1, race_entry_ids[2], None)]
        assert [audit.correlation_id for audit in audit_rows] == list(correlations[:2])
        assert all(audit.season_id == season_id for audit in audit_rows)
        assert all(audit.round_id == round_id for audit in audit_rows)
        assert all(audit.submission_id == submission_id for audit in audit_rows)
        assert all(audit.type == "submission_saved" for audit in audit_rows)
        assert audit_rows[0].before_data is None
        assert audit_rows[0].after_data["version"] == 1
        assert audit_rows[1].before_data["version"] == 1
        assert audit_rows[1].after_data["version"] == 2
    finally:
        with migrated_engine.begin() as connection:
            operation_ids = tuple(
                connection.scalars(select(OperationORM.id).where(OperationORM.correlation_id.in_(correlations)))
            )
            if operation_ids:
                connection.execute(delete(Win5OperationORM).where(Win5OperationORM.operation_id.in_(operation_ids)))
                connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
            if submission_id is not None:
                connection.execute(
                    delete(Win5SubmissionPickORM).where(Win5SubmissionPickORM.submission_id == submission_id)
                )
                connection.execute(delete(Win5SubmissionORM).where(Win5SubmissionORM.id == submission_id))
            connection.execute(delete(Win5RaceEntryORM).where(Win5RaceEntryORM.id.in_(race_entry_ids)))
            connection.execute(delete(Win5RaceORM).where(Win5RaceORM.id == race_id))
            connection.execute(delete(Win5RoundORM).where(Win5RoundORM.id == round_id))
            connection.execute(delete(Win5SeasonORM).where(Win5SeasonORM.id == season_id))
            connection.execute(delete(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == discord_user_id))
            connection.execute(delete(GameAccountORM).where(GameAccountORM.persona_id == persona_id))
            connection.execute(delete(PersonaORM).where(PersonaORM.id == persona_id))
