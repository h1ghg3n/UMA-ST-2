"""MariaDB evidence for native scheduled Match setup replacement."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    MatchSetupCommands,
    MatchSetupStaleError,
    MatchStaffSetupQueries,
    UpdateMatchSetup,
)
from uma_st2.domain.match import (
    MatchGrade,
    MatchSeason,
    MatchStatus,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
)
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyMatchCreationUnitOfWorkFactory,
    SqlAlchemyMatchSetupUnitOfWorkFactory,
    SqlAlchemyMatchStaffSetupQueryUnitOfWorkFactory,
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

NOW = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SeededSetupMatch:
    match_id: int
    stadium_ids: tuple[int, int]
    course_ids: tuple[int, int]


def _conditions(*, weather: MatchWeather = MatchWeather.SUNNY) -> MatchConditionValues:
    return MatchConditionValues(
        season=MatchSeason.AUTUMN,
        weather=weather,
        time_of_day=MatchTimeOfDay.DAY,
        track_condition=MatchTrackCondition.FIRM,
    )


def _services(
    engine: Engine,
) -> tuple[MatchCreationCommands, MatchSetupCommands, MatchStaffSetupQueries]:
    runtime = DatabaseRuntime.from_engine(engine)
    return (
        MatchCreationCommands(
            CommandRunner(SqlAlchemyMatchCreationUnitOfWorkFactory(runtime.session_factory)),
            clock=lambda: NOW,
        ),
        MatchSetupCommands(
            CommandRunner(SqlAlchemyMatchSetupUnitOfWorkFactory(runtime.session_factory)),
            clock=lambda: NOW + timedelta(minutes=5),
        ),
        MatchStaffSetupQueries(QueryRunner(SqlAlchemyMatchStaffSetupQueryUnitOfWorkFactory(runtime.session_factory))),
    )


def _seed(engine: Engine, *, suffix: str) -> SeededSetupMatch:
    stored_now = (NOW - timedelta(days=1)).replace(tzinfo=None)
    external_base = int(suffix[:12], 16)
    with engine.begin() as connection:
        stadium_ids: list[int] = []
        course_ids: list[int] = []
        for offset, name in ((0, "도쿄"), (10, "교토")):
            stadium_id = connection.execute(
                StadiumORM.__table__.insert().values(
                    external_id=external_base + offset,
                    name_jp=f"Stadium {offset} {suffix}",
                    name_ko=f"{name} {suffix}",
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            ).inserted_primary_key[0]
            course_id = connection.execute(
                StadiumCourseORM.__table__.insert().values(
                    stadium_id=stadium_id,
                    external_id=external_base + offset + 1,
                    surface="turf",
                    distance=2400 + offset * 10,
                    direction="left",
                    layout="standard",
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            ).inserted_primary_key[0]
            stadium_ids.append(stadium_id)
            course_ids.append(course_id)

    creation, _, _ = _services(engine)
    created = creation.create_match(
        CreateMatch(
            name=f"Setup Match {suffix}",
            description="before",
            grade=MatchGrade.G1,
            stadium_course_id=course_ids[0],
            scheduled_at=SCHEDULED_AT,
            condition=_conditions(),
            idempotency_key=f"setup-create-{suffix}",
            actor_discord_user_id="123456789",
            guild_id="987654321",
            correlation_id=f"setup-create-{suffix}",
        )
    )
    return SeededSetupMatch(
        match_id=created.snapshot.match_id,
        stadium_ids=(stadium_ids[0], stadium_ids[1]),
        course_ids=(course_ids[0], course_ids[1]),
    )


def _request(
    seeded: SeededSetupMatch,
    *,
    target: object,
    suffix: str,
    name: str = "Edited Match",
    key: str | None = None,
) -> UpdateMatchSetup:
    return UpdateMatchSetup(
        match_id=seeded.match_id,
        name=name,
        description="after",
        grade=MatchGrade.G2,
        stadium_course_id=seeded.course_ids[1],
        scheduled_at=SCHEDULED_AT + timedelta(days=1),
        condition=_conditions(weather=MatchWeather.RAIN),
        expected_state_fingerprint=target.state_fingerprint,  # type: ignore[attr-defined]
        expected_setup_version=target.setup_version,  # type: ignore[attr-defined]
        idempotency_key=key or f"setup-edit-{suffix}",
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id=f"setup-edit-{suffix}",
        reason="운영 설정 정정",
    )


def _cleanup(engine: Engine, seeded: SeededSetupMatch) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(
            connection.scalars(
                select(MatchOperationORM.operation_id).where(MatchOperationORM.match_id == seeded.match_id)
            )
        )
        if operation_ids:
            connection.execute(delete(MatchOperationORM).where(MatchOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(MatchConditionORM).where(MatchConditionORM.match_id == seeded.match_id))
        connection.execute(delete(MatchORM).where(MatchORM.id == seeded.match_id))
        connection.execute(delete(StadiumCourseORM).where(StadiumCourseORM.id.in_(seeded.course_ids)))
        connection.execute(delete(StadiumORM).where(StadiumORM.id.in_(seeded.stadium_ids)))


def _resolve(future: Future[object]) -> object:
    return future.result(timeout=15)


def test_setup_query_edit_audit_and_exact_retry(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed(migrated_engine, suffix=suffix)
    _, setup, queries = _services(migrated_engine)
    try:
        target = queries.get_target(match_id=seeded.match_id)
        assert target is not None
        assert target.entry_count == 0
        assert target.setup.setup_version is not None
        request = _request(seeded, target=target.setup, suffix=suffix)

        updated = setup.update_setup(request)
        assert setup.update_setup(request) == updated

        with migrated_engine.connect() as connection:
            stored = connection.execute(
                select(
                    MatchORM.name,
                    MatchORM.description,
                    MatchORM.grade,
                    MatchORM.stadium_course_id,
                    MatchORM.scheduled_at,
                    MatchORM.status,
                    MatchConditionORM.weather,
                )
                .join(MatchConditionORM, MatchConditionORM.match_id == MatchORM.id)
                .where(MatchORM.id == seeded.match_id)
            ).one()
            audit = connection.execute(
                select(
                    MatchOperationORM.before_data,
                    MatchOperationORM.after_data,
                    OperationORM.reason,
                    OperationORM.request_fingerprint,
                )
                .join(OperationORM, OperationORM.id == MatchOperationORM.operation_id)
                .where(
                    MatchOperationORM.match_id == seeded.match_id,
                    MatchOperationORM.type == "match_setup_updated",
                )
            ).one()

        assert stored == (
            "Edited Match",
            "after",
            "G2",
            seeded.course_ids[1],
            (SCHEDULED_AT + timedelta(days=1)).replace(tzinfo=None),
            MatchStatus.SCHEDULED.value,
            MatchWeather.RAIN.value,
        )
        assert audit.before_data["course"]["id"] == seeded.course_ids[0]
        assert audit.after_data["course"]["id"] == seeded.course_ids[1]
        assert audit.after_data["schema_version"] == 1
        assert audit.reason == "운영 설정 정정"
        assert audit.request_fingerprint == request.request_fingerprint
    finally:
        _cleanup(migrated_engine, seeded)


def test_concurrent_setup_edits_serialize_one_preview_authority(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed(migrated_engine, suffix=suffix)
    _, _, queries = _services(migrated_engine)
    target = queries.get_target(match_id=seeded.match_id)
    assert target is not None
    barrier = Barrier(2)

    def edit(name: str, key_suffix: str) -> object:
        _, commands, _ = _services(migrated_engine)
        barrier.wait(timeout=10)
        return commands.update_setup(
            _request(
                seeded,
                target=target.setup,
                suffix=suffix,
                name=name,
                key=f"setup-concurrent-{key_suffix}-{suffix}",
            )
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(edit, "Editor A", "a"), executor.submit(edit, "Editor B", "b")]
            outcomes: list[object] = []
            for future in futures:
                try:
                    outcomes.append(_resolve(future))
                except MatchSetupStaleError as error:
                    outcomes.append(error)

        assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
        assert sum(isinstance(outcome, MatchSetupStaleError) for outcome in outcomes) == 1
        with migrated_engine.connect() as connection:
            assert connection.scalar(select(MatchORM.name).where(MatchORM.id == seeded.match_id)) in {
                "Editor A",
                "Editor B",
            }
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(MatchOperationORM)
                    .where(
                        MatchOperationORM.match_id == seeded.match_id,
                        MatchOperationORM.type == "match_setup_updated",
                    )
                )
                == 1
            )
    finally:
        _cleanup(migrated_engine, seeded)


def test_setup_audit_insert_failure_rolls_back_metadata_and_condition(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed(migrated_engine, suffix=suffix)
    _, setup, queries = _services(migrated_engine)
    target = queries.get_target(match_id=seeded.match_id)
    assert target is not None

    def fail_match_audit(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if statement.lstrip().lower().startswith("insert into match_operations"):
            raise RuntimeError("injected Match audit failure")

    event.listen(migrated_engine, "before_cursor_execute", fail_match_audit)
    try:
        with pytest.raises(RuntimeError, match="injected Match audit failure"):
            setup.update_setup(_request(seeded, target=target.setup, suffix=suffix))
    finally:
        event.remove(migrated_engine, "before_cursor_execute", fail_match_audit)

    try:
        current = queries.get_target(match_id=seeded.match_id)
        assert current == target
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(MatchOperationORM)
                    .where(
                        MatchOperationORM.match_id == seeded.match_id,
                        MatchOperationORM.type == "match_setup_updated",
                    )
                )
                == 0
            )
    finally:
        _cleanup(migrated_engine, seeded)
