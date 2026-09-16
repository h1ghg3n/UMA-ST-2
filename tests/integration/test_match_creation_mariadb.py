"""MariaDB atomicity and exact-retry evidence for native Match creation."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.engine import Engine

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.match import (
    CreateMatch,
    MatchConditionValues,
    MatchCreationCommands,
    MatchCreationIdempotencyConflictError,
    MatchStaffCreationQueries,
)
from uma_st2.domain.match import (
    MatchGrade,
    MatchSeason,
    MatchSourceKind,
    MatchStatus,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
)
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyMatchCreationUnitOfWorkFactory,
    SqlAlchemyMatchStaffCreationQueryUnitOfWorkFactory,
)
from uma_st2.infrastructure.database.orm import (
    MatchConditionORM,
    MatchOperationORM,
    MatchORM,
    OperationORM,
    StadiumCourseORM,
    StadiumORM,
)

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class SeededCourse:
    stadium_id: int
    course_id: int


def _seed_course(engine: Engine, *, suffix: str) -> SeededCourse:
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    external_base = int(suffix[:12], 16)
    with engine.begin() as connection:
        stadium_id = connection.execute(
            StadiumORM.__table__.insert().values(
                external_id=external_base,
                name_jp=f"Tokyo {suffix}",
                name_ko=f"도쿄 {suffix}",
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        course_id = connection.execute(
            StadiumCourseORM.__table__.insert().values(
                stadium_id=stadium_id,
                external_id=external_base + 1,
                surface="turf",
                distance=2400,
                direction="left",
                layout="standard",
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
    return SeededCourse(stadium_id=stadium_id, course_id=course_id)


def _command(
    seeded: SeededCourse,
    *,
    suffix: str,
    idempotency_key: str | None = None,
    name: str | None = None,
) -> CreateMatch:
    return CreateMatch(
        name=name or f"Native Match {suffix}",
        description="MariaDB creation evidence",
        grade=MatchGrade.LISTED,
        stadium_course_id=seeded.course_id,
        scheduled_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        condition=MatchConditionValues(
            season=MatchSeason.AUTUMN,
            weather=MatchWeather.SUNNY,
            time_of_day=MatchTimeOfDay.DAY,
            track_condition=MatchTrackCondition.FIRM,
        ),
        idempotency_key=idempotency_key or f"match-create-{suffix}",
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id=f"match-{suffix}",
        reason="integration creation",
    )


def _creation_services(
    engine: Engine,
) -> tuple[MatchCreationCommands, MatchStaffCreationQueries]:
    runtime = DatabaseRuntime.from_engine(engine)
    commands = MatchCreationCommands(
        CommandRunner(SqlAlchemyMatchCreationUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: datetime.now(UTC),
    )
    queries = MatchStaffCreationQueries(
        QueryRunner(SqlAlchemyMatchStaffCreationQueryUnitOfWorkFactory(runtime.session_factory))
    )
    return commands, queries


def _cleanup(engine: Engine, seeded: SeededCourse) -> None:
    with engine.begin() as connection:
        match_ids = tuple(connection.scalars(select(MatchORM.id).where(MatchORM.stadium_course_id == seeded.course_id)))
        operation_ids = (
            tuple(
                connection.scalars(
                    select(MatchOperationORM.operation_id).where(MatchOperationORM.match_id.in_(match_ids))
                )
            )
            if match_ids
            else ()
        )
        if operation_ids:
            connection.execute(delete(MatchOperationORM).where(MatchOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        if match_ids:
            connection.execute(delete(MatchConditionORM).where(MatchConditionORM.match_id.in_(match_ids)))
            connection.execute(delete(MatchORM).where(MatchORM.id.in_(match_ids)))
        connection.execute(delete(StadiumCourseORM).where(StadiumCourseORM.id == seeded.course_id))
        connection.execute(delete(StadiumORM).where(StadiumORM.id == seeded.stadium_id))


def _resolve(future: Future[object]) -> object:
    return future.result(timeout=15)


def test_native_creation_persists_course_audit_and_exact_retry(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_course(migrated_engine, suffix=suffix)
    commands, queries = _creation_services(migrated_engine)
    request = _command(seeded, suffix=suffix)

    try:
        choices = queries.list_course_choices()
        selected = next(choice for choice in choices if choice.id == seeded.course_id)
        created = commands.create_match(request)
        retried = commands.create_match(request)

        assert selected.stadium_name == f"도쿄 {suffix}"
        assert retried == created
        with migrated_engine.connect() as connection:
            stored = connection.execute(
                select(
                    MatchORM.name,
                    MatchORM.source_kind,
                    MatchORM.status,
                    MatchORM.grade,
                    MatchORM.scheduled_at,
                ).where(MatchORM.id == created.snapshot.match_id)
            ).one()
            audit = connection.execute(
                select(
                    MatchOperationORM.type,
                    MatchOperationORM.before_data,
                    MatchOperationORM.after_data,
                    OperationORM.request_fingerprint,
                    OperationORM.reason,
                )
                .join(OperationORM, OperationORM.id == MatchOperationORM.operation_id)
                .where(MatchOperationORM.match_id == created.snapshot.match_id)
            ).one()
            match_count = connection.scalar(
                select(func.count()).select_from(MatchORM).where(MatchORM.stadium_course_id == seeded.course_id)
            )
            condition = connection.execute(
                select(
                    MatchConditionORM.season,
                    MatchConditionORM.weather,
                    MatchConditionORM.time_of_day,
                    MatchConditionORM.track_condition,
                ).where(MatchConditionORM.match_id == created.snapshot.match_id)
            ).one()

        assert match_count == 1
        assert stored.name == request.name
        assert stored.source_kind == MatchSourceKind.NATIVE_V2.value
        assert stored.status == MatchStatus.SCHEDULED.value
        assert stored.grade == MatchGrade.LISTED.value
        assert stored.scheduled_at == datetime(2026, 9, 2, 12, 0)
        assert condition == ("autumn", "sunny", "day", "firm")
        assert audit.type == "match_created"
        assert audit.before_data is None
        assert audit.after_data["match_id"] == created.snapshot.match_id
        assert audit.after_data["course"]["id"] == seeded.course_id
        assert audit.after_data["schema_version"] == 2
        assert audit.after_data["condition"]["values"] == {
            "season": "autumn",
            "weather": "sunny",
            "time_of_day": "day",
            "track_condition": "firm",
        }
        assert audit.request_fingerprint == request.request_fingerprint
        assert audit.reason == "integration creation"

        with pytest.raises(MatchCreationIdempotencyConflictError):
            commands.create_match(_command(seeded, suffix=suffix, name="Changed Match"))
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count()).select_from(MatchORM).where(MatchORM.stadium_course_id == seeded.course_id)
                )
                == 1
            )
    finally:
        _cleanup(migrated_engine, seeded)


def test_concurrent_same_request_serializes_to_one_match(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_course(migrated_engine, suffix=suffix)
    request = _command(seeded, suffix=suffix)
    barrier = Barrier(2)

    def create() -> object:
        commands, _ = _creation_services(migrated_engine)
        barrier.wait(timeout=10)
        return commands.create_match(request)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [_resolve(future) for future in (executor.submit(create), executor.submit(create))]

        assert results[0] == results[1]
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count()).select_from(MatchORM).where(MatchORM.stadium_course_id == seeded.course_id)
                )
                == 1
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(MatchOperationORM)
                    .where(MatchOperationORM.match_id == results[0].snapshot.match_id)  # type: ignore[union-attr]
                )
                == 1
            )
    finally:
        _cleanup(migrated_engine, seeded)


def test_condition_insert_failure_rolls_back_match_and_audit(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_course(migrated_engine, suffix=suffix)
    commands, _ = _creation_services(migrated_engine)

    def fail_condition_insert(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if statement.lstrip().lower().startswith("insert into match_conditions"):
            raise RuntimeError("injected condition failure")

    event.listen(migrated_engine, "before_cursor_execute", fail_condition_insert)
    try:
        with pytest.raises(RuntimeError, match="injected condition failure"):
            commands.create_match(_command(seeded, suffix=suffix))
    finally:
        event.remove(migrated_engine, "before_cursor_execute", fail_condition_insert)

    try:
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count()).select_from(MatchORM).where(MatchORM.stadium_course_id == seeded.course_id)
                )
                == 0
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(OperationORM)
                    .where(OperationORM.idempotency_key == f"match-create-{suffix}")
                )
                == 0
            )
    finally:
        _cleanup(migrated_engine, seeded)
