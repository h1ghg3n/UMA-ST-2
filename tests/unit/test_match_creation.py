"""Native Match creation application-boundary tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.match import (
    CreateMatch,
    MatchConditionRecord,
    MatchConditionValues,
    MatchCreationAuditError,
    MatchCreationCommands,
    MatchCreationCourse,
    MatchCreationIdempotencyConflictError,
    MatchCreationSnapshot,
    MatchCreationUnavailableError,
    StoredMatchCreationOperation,
)
from uma_st2.domain.match import (
    MatchDirection,
    MatchGrade,
    MatchSeason,
    MatchSourceKind,
    MatchStatus,
    MatchSurface,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
    StadiumCourseLayout,
)

NOW = datetime(2026, 8, 27, 3, 30, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _course() -> MatchCreationCourse:
    return MatchCreationCourse(
        id=31,
        stadium_id=7,
        stadium_name="도쿄",
        surface=MatchSurface.TURF,
        distance=2400,
        direction=MatchDirection.LEFT,
        layout=StadiumCourseLayout.STANDARD,
    )


def _condition_values() -> MatchConditionValues:
    return MatchConditionValues(
        season=MatchSeason.AUTUMN,
        weather=MatchWeather.SUNNY,
        time_of_day=MatchTimeOfDay.DAY,
        track_condition=MatchTrackCondition.FIRM,
    )


def _condition_record(created_at: datetime = NOW) -> MatchConditionRecord:
    return MatchConditionRecord(values=_condition_values(), created_at=created_at, updated_at=created_at)


def _command(*, idempotency_key: str = "match-create:555", name: str = "제12회 정기전") -> CreateMatch:
    return CreateMatch(
        name=name,
        description="9월 정기 룸매치",
        grade=MatchGrade.G1,
        stadium_course_id=31,
        scheduled_at=SCHEDULED_AT,
        condition=_condition_values(),
        idempotency_key=idempotency_key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id="555",
        reason="운영진 생성",
    )


class RecordingRepository:
    def __init__(
        self,
        *,
        course: MatchCreationCourse | None = None,
        stored: StoredMatchCreationOperation | None = None,
        drift: str | None = None,
    ) -> None:
        self.course = _course() if course is None else course
        self.stored = stored
        self.drift = drift
        self.calls: list[str] = []
        self.audits: list[MatchCreationSnapshot] = []

    def lock_course(self, *, stadium_course_id: int) -> MatchCreationCourse | None:
        self.calls.append("lock_course")
        assert stadium_course_id == 31
        return self.course

    def find_operation(self, *, idempotency_key: str) -> StoredMatchCreationOperation | None:
        self.calls.append("find_operation")
        return self.stored

    def create_match(
        self,
        *,
        command: CreateMatch,
        course: MatchCreationCourse,
        created_at: datetime,
    ) -> MatchCreationSnapshot:
        self.calls.append("create_match")
        return MatchCreationSnapshot(
            match_id=91,
            name="drifted" if self.drift == "name" else command.name,
            description=command.description,
            source_kind=MatchSourceKind.NATIVE_V2,
            grade=command.grade,
            course=course,
            scheduled_at=command.scheduled_at,
            condition=_condition_record(created_at),
            status=MatchStatus.SCHEDULED,
            created_at=created_at,
        )

    def add_creation_audit(
        self,
        *,
        command: CreateMatch,
        snapshot: MatchCreationSnapshot,
        created_at: datetime,
    ) -> None:
        self.calls.append("add_creation_audit")
        assert command.idempotency_key
        assert created_at == NOW
        self.audits.append(snapshot)


@dataclass
class RecordingUnitOfWork:
    match_creation: RecordingRepository
    commits: int = 0
    rollbacks: int = 0

    def __enter__(self) -> RecordingUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self.commits == 0 and self.rollbacks == 0:
            self.rollbacks += 1
        return False

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class RecordingFactory:
    def __init__(self, repository: RecordingRepository) -> None:
        self.repository = repository
        self.created: list[RecordingUnitOfWork] = []

    def __call__(self) -> RecordingUnitOfWork:
        unit_of_work = RecordingUnitOfWork(self.repository)
        self.created.append(unit_of_work)
        return unit_of_work


def _commands(repository: RecordingRepository) -> tuple[MatchCreationCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return MatchCreationCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_native_creation_commits_scheduled_match_and_audit() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)

    result = commands.create_match(_command())

    assert result.snapshot.match_id == 91
    assert result.snapshot.source_kind == MatchSourceKind.NATIVE_V2
    assert result.snapshot.status == MatchStatus.SCHEDULED
    assert result.snapshot.course == _course()
    assert result.snapshot.to_audit_payload()["schema_version"] == 2
    assert repository.calls == ["lock_course", "find_operation", "create_match", "add_creation_audit"]
    assert repository.audits == [result.snapshot]
    assert factory.created[0].commits == 1


def test_exact_retry_returns_stored_snapshot_without_second_write() -> None:
    command = _command()
    snapshot = MatchCreationSnapshot(
        match_id=91,
        name=command.name,
        description=command.description,
        source_kind=MatchSourceKind.NATIVE_V2,
        grade=command.grade,
        course=_course(),
        scheduled_at=command.scheduled_at,
        condition=_condition_record(),
        status=MatchStatus.SCHEDULED,
        created_at=NOW,
    )
    repository = RecordingRepository(
        stored=StoredMatchCreationOperation(
            request_fingerprint=command.request_fingerprint,
            type="match_created",
            match_id=91,
            after_data=snapshot.to_audit_payload(),
        )
    )
    commands, factory = _commands(repository)

    result = commands.create_match(command)

    assert result.snapshot == snapshot
    assert repository.calls == ["lock_course", "find_operation"]
    assert factory.created[0].commits == 1


def test_same_key_changed_payload_conflicts_without_write() -> None:
    original = _command()
    snapshot = MatchCreationSnapshot(
        match_id=91,
        name=original.name,
        description=original.description,
        source_kind=MatchSourceKind.NATIVE_V2,
        grade=original.grade,
        course=_course(),
        scheduled_at=original.scheduled_at,
        condition=_condition_record(),
        status=MatchStatus.SCHEDULED,
        created_at=NOW,
    )
    repository = RecordingRepository(
        stored=StoredMatchCreationOperation(
            request_fingerprint=original.request_fingerprint,
            type="match_created",
            match_id=91,
            after_data=snapshot.to_audit_payload(),
        )
    )
    commands, factory = _commands(repository)

    with pytest.raises(MatchCreationIdempotencyConflictError):
        commands.create_match(_command(name="다른 제목"))

    assert repository.calls == ["lock_course", "find_operation"]
    assert factory.created[0].rollbacks == 1


def test_missing_course_rejects_before_operation_or_write() -> None:
    repository = RecordingRepository()
    repository.course = None
    commands, factory = _commands(repository)

    with pytest.raises(MatchCreationUnavailableError, match="does not exist"):
        commands.create_match(_command())

    assert repository.calls == ["lock_course"]
    assert factory.created[0].rollbacks == 1


def test_drifted_persistence_snapshot_rolls_back_before_audit() -> None:
    repository = RecordingRepository(drift="name")
    commands, factory = _commands(repository)

    with pytest.raises(MatchCreationAuditError, match="do not match"):
        commands.create_match(_command())

    assert repository.audits == []
    assert factory.created[0].rollbacks == 1


def test_create_match_normalizes_optional_text_and_requires_aware_time() -> None:
    command = CreateMatch(
        name="  정기전  ",
        description="   ",
        grade="LISTED",
        stadium_course_id=31,
        scheduled_at=SCHEDULED_AT,
        condition=_condition_values(),
        idempotency_key=" key ",
        actor_discord_user_id=" 1 ",
    )

    assert command.name == "정기전"
    assert command.description is None
    assert command.grade == MatchGrade.LISTED
    assert command.idempotency_key == "key"

    with pytest.raises(ValueError, match="timezone-aware"):
        CreateMatch(
            name="정기전",
            description=None,
            grade=MatchGrade.G1,
            stadium_course_id=31,
            scheduled_at=datetime(2026, 9, 2, 12, 0),
            condition=_condition_values(),
            idempotency_key="key",
            actor_discord_user_id="1",
        )
