"""Application tests for complete native scheduled Match setup replacement."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.match import (
    MatchConditionRecord,
    MatchConditionValues,
    MatchCreationCourse,
    MatchSetupAuditError,
    MatchSetupCommands,
    MatchSetupIdempotencyConflictError,
    MatchSetupNoChangeError,
    MatchSetupReasonRequiredError,
    MatchSetupStaleError,
    MatchSetupTarget,
    MatchSetupUnavailableError,
    StoredMatchSetupOperation,
    UpdateMatchSetup,
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

PREVIEWED_AT = datetime(2026, 8, 31, 2, 0, tzinfo=UTC)
NOW = PREVIEWED_AT + timedelta(minutes=5)
SCHEDULED_AT = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _course(*, course_id: int = 31, stadium_name: str = "도쿄") -> MatchCreationCourse:
    return MatchCreationCourse(
        id=course_id,
        stadium_id=7,
        stadium_name=stadium_name,
        surface=MatchSurface.TURF,
        distance=2400,
        direction=MatchDirection.LEFT,
        layout=StadiumCourseLayout.STANDARD,
    )


def _values(*, weather: MatchWeather = MatchWeather.SUNNY) -> MatchConditionValues:
    return MatchConditionValues(
        season=MatchSeason.AUTUMN,
        weather=weather,
        time_of_day=MatchTimeOfDay.DAY,
        track_condition=MatchTrackCondition.FIRM,
    )


def _record(
    *,
    values: MatchConditionValues | None = None,
    updated_at: datetime = PREVIEWED_AT,
) -> MatchConditionRecord:
    return MatchConditionRecord(
        values=values or _values(),
        created_at=PREVIEWED_AT,
        updated_at=updated_at,
    )


def _target(
    *,
    source_kind: MatchSourceKind = MatchSourceKind.NATIVE_V2,
    status: MatchStatus = MatchStatus.SCHEDULED,
    condition: MatchConditionRecord | None = None,
    setup_version: int | None = 41,
) -> MatchSetupTarget:
    return MatchSetupTarget(
        match_id=91,
        name="기존 정기전",
        description="기존 설명",
        source_kind=source_kind,
        grade=MatchGrade.G2,
        course=_course(),
        scheduled_at=SCHEDULED_AT,
        status=status,
        condition=_record() if condition is None else condition,
        updated_at=PREVIEWED_AT,
        setup_version=setup_version,
    )


def _command(
    target: MatchSetupTarget,
    *,
    name: str = "수정된 정기전",
    reason: str | None = "운영 일정 정정",
    key: str = "match-setup:555",
) -> UpdateMatchSetup:
    return UpdateMatchSetup(
        match_id=target.match_id,
        name=name,
        description="수정된 설명",
        grade=MatchGrade.G1,
        stadium_course_id=32,
        scheduled_at=SCHEDULED_AT + timedelta(days=1),
        condition=_values(weather=MatchWeather.RAIN),
        expected_state_fingerprint=target.state_fingerprint,
        expected_setup_version=target.setup_version,
        idempotency_key=key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id="555",
        reason=reason,
    )


class RecordingRepository:
    def __init__(
        self,
        *,
        target: MatchSetupTarget | None = None,
        course: MatchCreationCourse | None = None,
        stored: StoredMatchSetupOperation | None = None,
        drift: bool = False,
    ) -> None:
        self.target = target
        self.course = _course(course_id=32, stadium_name="교토") if course is None else course
        self.stored = stored
        self.drift = drift
        self.calls: list[str] = []
        self.audits: list[tuple[MatchSetupTarget, MatchSetupTarget]] = []

    def lock_target(self, *, match_id: int) -> MatchSetupTarget | None:
        self.calls.append("lock_target")
        assert match_id == 91
        return self.target

    def lock_course(self, *, stadium_course_id: int) -> MatchCreationCourse | None:
        self.calls.append("lock_course")
        assert stadium_course_id in {31, 32}
        return self.course

    def find_operation(self, *, idempotency_key: str) -> StoredMatchSetupOperation | None:
        self.calls.append("find_operation")
        assert idempotency_key
        return self.stored

    def replace_setup(
        self,
        *,
        target: MatchSetupTarget,
        command: UpdateMatchSetup,
        course: MatchCreationCourse,
        changed_at: datetime,
    ) -> MatchSetupTarget:
        self.calls.append("replace_setup")
        return MatchSetupTarget(
            match_id=target.match_id,
            name="drifted" if self.drift else command.name,
            description=command.description,
            source_kind=target.source_kind,
            grade=command.grade,
            course=course,
            scheduled_at=command.scheduled_at,
            status=target.status,
            condition=MatchConditionRecord(
                values=command.condition,
                created_at=target.condition.created_at if target.condition is not None else changed_at,
                updated_at=changed_at,
            ),
            updated_at=changed_at,
            setup_version=target.setup_version,
        )

    def add_audit(
        self,
        *,
        command: UpdateMatchSetup,
        before: MatchSetupTarget,
        after: MatchSetupTarget,
        created_at: datetime,
    ) -> None:
        self.calls.append("add_audit")
        assert command.reason == "운영 일정 정정"
        assert created_at == NOW
        self.audits.append((before, after))


@dataclass
class RecordingUnitOfWork:
    match_setup: RecordingRepository
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


def _commands(repository: RecordingRepository) -> tuple[MatchSetupCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return MatchSetupCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_complete_setup_edit_commits_one_before_after_audit() -> None:
    target = _target()
    repository = RecordingRepository(target=target)
    commands, factory = _commands(repository)

    result = commands.update_setup(_command(target))

    assert result.snapshot.name == "수정된 정기전"
    assert result.snapshot.course.id == 32
    assert result.snapshot.condition is not None
    assert result.snapshot.condition.values.weather == MatchWeather.RAIN
    assert result.snapshot.setup_version is None
    assert result.snapshot.to_audit_payload()["schema_version"] == 1
    assert repository.calls == [
        "lock_target",
        "find_operation",
        "lock_course",
        "replace_setup",
        "add_audit",
    ]
    assert repository.audits == [(target, result.snapshot)]
    assert factory.created[0].commits == 1


def test_exact_retry_returns_stored_snapshot_without_second_write() -> None:
    target = _target()
    command = _command(target)
    after = RecordingRepository(target=target).replace_setup(
        target=target,
        command=command,
        course=_course(course_id=32, stadium_name="교토"),
        changed_at=NOW,
    )
    after = MatchSetupTarget.from_audit_payload(after.to_audit_payload())
    repository = RecordingRepository(
        target=target,
        stored=StoredMatchSetupOperation(
            request_fingerprint=command.request_fingerprint,
            type="match_setup_updated",
            match_id=target.match_id,
            after_data=after.to_audit_payload(),
        ),
    )
    repository.calls.clear()
    commands, factory = _commands(repository)

    assert commands.update_setup(command).snapshot == after
    assert repository.calls == ["lock_target", "find_operation"]
    assert factory.created[0].commits == 1


def test_same_key_changed_payload_conflicts_without_write() -> None:
    target = _target()
    original = _command(target)
    repository = RecordingRepository(
        target=target,
        stored=StoredMatchSetupOperation(
            request_fingerprint=original.request_fingerprint,
            type="match_setup_updated",
            match_id=target.match_id,
            after_data={},
        ),
    )
    commands, factory = _commands(repository)

    with pytest.raises(MatchSetupIdempotencyConflictError):
        commands.update_setup(_command(target, name="또 다른 제목"))

    assert repository.calls == ["lock_target", "find_operation"]
    assert factory.created[0].rollbacks == 1


def test_no_change_is_zero_write_even_without_reason() -> None:
    target = _target()
    command = UpdateMatchSetup(
        match_id=target.match_id,
        name=target.name,
        description=target.description,
        grade=target.grade,
        stadium_course_id=target.course.id,
        scheduled_at=target.scheduled_at,
        condition=target.condition.values,  # type: ignore[union-attr]
        expected_state_fingerprint=target.state_fingerprint,
        expected_setup_version=target.setup_version,
        idempotency_key="match-setup:no-change",
        actor_discord_user_id="123",
        reason=None,
    )
    repository = RecordingRepository(target=target, course=target.course)
    commands, factory = _commands(repository)

    with pytest.raises(MatchSetupNoChangeError):
        commands.update_setup(command)

    assert "replace_setup" not in repository.calls
    assert factory.created[0].rollbacks == 1


def test_changed_setup_requires_reason() -> None:
    target = _target()
    repository = RecordingRepository(target=target)
    commands, factory = _commands(repository)

    with pytest.raises(MatchSetupReasonRequiredError):
        commands.update_setup(_command(target, reason=None))

    assert "replace_setup" not in repository.calls
    assert factory.created[0].rollbacks == 1


def test_stale_fingerprint_or_version_rejects_before_course_lock() -> None:
    preview = _target()
    current = replace(preview, name="다른 운영자 변경")
    repository = RecordingRepository(target=current)
    commands, factory = _commands(repository)

    with pytest.raises(MatchSetupStaleError):
        commands.update_setup(_command(preview))

    assert repository.calls == ["lock_target", "find_operation"]
    assert factory.created[0].rollbacks == 1


@pytest.mark.parametrize(
    ("source_kind", "status"),
    (
        (MatchSourceKind.IMPORTED_V1, MatchStatus.SCHEDULED),
        (MatchSourceKind.NATIVE_V2, MatchStatus.BETTING_OPEN),
    ),
)
def test_imported_or_open_match_is_frozen(
    source_kind: MatchSourceKind,
    status: MatchStatus,
) -> None:
    target = _target(source_kind=source_kind, status=status)
    repository = RecordingRepository(target=target)
    commands, factory = _commands(repository)

    with pytest.raises(MatchSetupUnavailableError):
        commands.update_setup(_command(target))

    assert repository.calls == ["lock_target", "find_operation"]
    assert factory.created[0].rollbacks == 1


def test_missing_course_and_persistence_drift_roll_back() -> None:
    target = _target()
    missing = RecordingRepository(target=target)
    missing.course = None
    commands, factory = _commands(missing)

    with pytest.raises(MatchSetupUnavailableError, match="course"):
        commands.update_setup(_command(target))
    assert factory.created[0].rollbacks == 1

    drifted = RecordingRepository(target=target, drift=True)
    commands, factory = _commands(drifted)
    with pytest.raises(MatchSetupAuditError, match="do not match"):
        commands.update_setup(_command(target))
    assert drifted.audits == []
    assert factory.created[0].rollbacks == 1
