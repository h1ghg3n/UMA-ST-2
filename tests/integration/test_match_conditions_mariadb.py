"""MariaDB locking, audit, and exact retry evidence for Match conditions."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.match import (
    CreateMatch,
    MatchConditionCommands,
    MatchConditionStaleError,
    MatchConditionTarget,
    MatchConditionValues,
    MatchCreationCommands,
    MatchStaffConditionQueries,
    SetMatchConditions,
)
from uma_st2.domain.match import (
    MatchGrade,
    MatchSeason,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
)
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyMatchConditionUnitOfWorkFactory,
    SqlAlchemyMatchCreationUnitOfWorkFactory,
    SqlAlchemyMatchStaffConditionQueryUnitOfWorkFactory,
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

NOW = datetime(2026, 8, 28, 3, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SeededMatch:
    stadium_id: int
    course_id: int
    match_id: int
    creation_operation_id: int


def _seed_match(engine: Engine, *, suffix: str) -> SeededMatch:
    runtime = DatabaseRuntime.from_engine(engine)
    created_at = NOW - timedelta(days=1)
    external_base = int(suffix[:12], 16)
    stored_now = created_at.replace(tzinfo=None)
    with engine.begin() as connection:
        stadium_id = connection.execute(
            StadiumORM.__table__.insert().values(
                external_id=external_base,
                name_jp=f"Tokyo {suffix}",
                name_ko=f"도쿄 {suffix}",
                created_at=stored_now,
                updated_at=stored_now,
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
                created_at=stored_now,
                updated_at=stored_now,
            )
        ).inserted_primary_key[0]
    commands = MatchCreationCommands(
        CommandRunner(SqlAlchemyMatchCreationUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: created_at,
    )
    created = commands.create_match(
        CreateMatch(
            name=f"Native Match {suffix}",
            description=None,
            grade=MatchGrade.G1,
            stadium_course_id=course_id,
            scheduled_at=SCHEDULED_AT,
            condition=_values(),
            idempotency_key=f"match-create-{suffix}",
            actor_discord_user_id="123456789",
            guild_id="987654321",
            correlation_id=f"create-{suffix}",
        )
    )
    with engine.connect() as connection:
        creation_operation_id = connection.scalar(
            select(MatchOperationORM.operation_id).where(
                MatchOperationORM.match_id == created.snapshot.match_id,
                MatchOperationORM.type == "match_created",
            )
        )
    assert creation_operation_id is not None
    return SeededMatch(
        stadium_id=stadium_id,
        course_id=course_id,
        match_id=created.snapshot.match_id,
        creation_operation_id=creation_operation_id,
    )


def _services(
    engine: Engine,
    *,
    clock: datetime = NOW,
) -> tuple[MatchConditionCommands, MatchStaffConditionQueries]:
    runtime = DatabaseRuntime.from_engine(engine)
    commands = MatchConditionCommands(
        CommandRunner(SqlAlchemyMatchConditionUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: clock,
    )
    queries = MatchStaffConditionQueries(
        QueryRunner(SqlAlchemyMatchStaffConditionQueryUnitOfWorkFactory(runtime.session_factory))
    )
    return commands, queries


def _values(*, weather: MatchWeather = MatchWeather.SUNNY) -> MatchConditionValues:
    return MatchConditionValues(
        season=MatchSeason.AUTUMN,
        weather=weather,
        time_of_day=MatchTimeOfDay.DAY,
        track_condition=MatchTrackCondition.FIRM,
    )


def _request(
    seeded: SeededMatch,
    *,
    key: str,
    target: MatchConditionTarget,
    values: MatchConditionValues,
    reason: str | None = None,
) -> SetMatchConditions:
    return SetMatchConditions(
        match_id=seeded.match_id,
        values=values,
        expected_condition=target.condition,
        expected_condition_version=target.condition_version,
        idempotency_key=key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id=key,
        reason=reason,
    )


def _cleanup(engine: Engine, seeded: SeededMatch) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(
            connection.scalars(
                select(MatchOperationORM.operation_id).where(MatchOperationORM.match_id == seeded.match_id)
            )
        )
        connection.execute(delete(MatchConditionORM).where(MatchConditionORM.match_id == seeded.match_id))
        if operation_ids:
            connection.execute(delete(MatchOperationORM).where(MatchOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(MatchORM).where(MatchORM.id == seeded.match_id))
        connection.execute(delete(StadiumCourseORM).where(StadiumCourseORM.id == seeded.course_id))
        connection.execute(delete(StadiumORM).where(StadiumORM.id == seeded.stadium_id))


def _resolve(future: Future[object]) -> object:
    return future.result(timeout=15)


def test_condition_set_change_query_audit_and_exact_retry(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_match(migrated_engine, suffix=suffix)
    commands, queries = _services(migrated_engine)

    try:
        initial_target = queries.get_target(match_id=seeded.match_id)
        assert initial_target is not None and initial_target.condition is not None
        initial_request = _request(
            seeded,
            key=f"condition-change-1-{suffix}",
            target=initial_target,
            values=_values(weather=MatchWeather.RAIN),
            reason="첫 조건 정정",
        )

        initial = commands.set_conditions(initial_request)
        assert commands.set_conditions(initial_request) == initial

        current = queries.get_target(match_id=seeded.match_id)
        assert current is not None and current.condition is not None
        assert current.condition_version is not None
        changed_request = _request(
            seeded,
            key=f"condition-change-{suffix}",
            target=current,
            values=_values(weather=MatchWeather.SNOW),
            reason="공식 조건 정정",
        )
        changed_commands, _ = _services(migrated_engine, clock=NOW + timedelta(minutes=1))
        changed = changed_commands.set_conditions(changed_request)
        assert changed_commands.set_conditions(changed_request) == changed

        assert changed.snapshot.condition is not None
        assert changed.snapshot.condition.values.weather == MatchWeather.SNOW
        with pytest.raises(MatchConditionStaleError):
            changed_commands.set_conditions(
                _request(
                    seeded,
                    key=f"condition-stale-{suffix}",
                    target=initial_target,
                    values=_values(weather=MatchWeather.SNOW),
                )
            )

        with migrated_engine.connect() as connection:
            stored = connection.execute(
                select(
                    MatchConditionORM.season,
                    MatchConditionORM.weather,
                    MatchConditionORM.time_of_day,
                    MatchConditionORM.track_condition,
                ).where(MatchConditionORM.match_id == seeded.match_id)
            ).one()
            audits = connection.execute(
                select(
                    MatchOperationORM.type,
                    MatchOperationORM.before_data,
                    MatchOperationORM.after_data,
                    OperationORM.reason,
                )
                .join(OperationORM, OperationORM.id == MatchOperationORM.operation_id)
                .where(
                    MatchOperationORM.match_id == seeded.match_id,
                    MatchOperationORM.type.in_(("match_conditions_set", "match_conditions_changed")),
                )
                .order_by(MatchOperationORM.operation_id)
            ).all()

        assert stored == ("autumn", "snow", "day", "firm")
        assert [audit.type for audit in audits] == ["match_conditions_changed", "match_conditions_changed"]
        assert audits[0].before_data is not None
        assert audits[0].after_data["schema_version"] == 1
        assert audits[1].before_data["schema_version"] == 1
        assert audits[1].reason == "공식 조건 정정"
    finally:
        _cleanup(migrated_engine, seeded)


def test_concurrent_condition_changes_serialize_and_one_becomes_stale(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_match(migrated_engine, suffix=suffix)
    _commands, queries = _services(migrated_engine)
    target = queries.get_target(match_id=seeded.match_id)
    assert target is not None
    barrier = Barrier(2)

    def set_condition(index: int) -> object:
        commands, _ = _services(migrated_engine)
        request = _request(
            seeded,
            key=f"condition-concurrent-{suffix}-{index}",
            target=target,
            values=_values(weather=MatchWeather.SUNNY if index == 1 else MatchWeather.RAIN),
            reason=f"동시 조건 정정 {index}",
        )
        barrier.wait(timeout=10)
        try:
            return commands.set_conditions(request)
        except MatchConditionStaleError as error:
            return error

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [
                _resolve(future) for future in (executor.submit(set_condition, 1), executor.submit(set_condition, 2))
            ]

        assert sum(isinstance(result, MatchConditionStaleError) for result in results) == 1
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(MatchConditionORM)
                    .where(MatchConditionORM.match_id == seeded.match_id)
                )
                == 1
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(MatchOperationORM)
                    .where(
                        MatchOperationORM.match_id == seeded.match_id,
                        MatchOperationORM.type.in_(("match_conditions_set", "match_conditions_changed")),
                    )
                )
                == 1
            )
    finally:
        _cleanup(migrated_engine, seeded)
