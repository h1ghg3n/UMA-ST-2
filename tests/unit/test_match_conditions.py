"""Native Match condition application-boundary tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.match import (
    MatchConditionAuditType,
    MatchConditionCommands,
    MatchConditionIdempotencyConflictError,
    MatchConditionReasonRequiredError,
    MatchConditionRecord,
    MatchConditionStaleError,
    MatchConditionTarget,
    MatchConditionUnavailableError,
    MatchConditionValues,
    SetMatchConditions,
    StoredMatchConditionOperation,
)
from uma_st2.domain.match import (
    MatchSeason,
    MatchSourceKind,
    MatchStatus,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
)

NOW = datetime(2026, 8, 28, 2, 0, tzinfo=UTC)
OLD = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _values(*, weather: MatchWeather = MatchWeather.SUNNY) -> MatchConditionValues:
    return MatchConditionValues(
        season=MatchSeason.AUTUMN,
        weather=weather,
        time_of_day=MatchTimeOfDay.DAY,
        track_condition=MatchTrackCondition.FIRM,
    )


def _record(*, weather: MatchWeather = MatchWeather.SUNNY) -> MatchConditionRecord:
    return MatchConditionRecord(values=_values(weather=weather), created_at=OLD, updated_at=OLD)


def test_random_weather_and_track_condition_round_trip_as_configured_values() -> None:
    values = MatchConditionValues(
        season=MatchSeason.AUTUMN,
        weather=MatchWeather.RANDOM,
        time_of_day=MatchTimeOfDay.DAY,
        track_condition=MatchTrackCondition.RANDOM,
    )

    assert values.to_audit_payload() == {
        "season": "autumn",
        "weather": "random",
        "time_of_day": "day",
        "track_condition": "random",
    }
    assert MatchConditionValues.from_audit_payload(values.to_audit_payload()) == values


def _target(
    *,
    condition: MatchConditionRecord | None = None,
    version: int | None = None,
    source_kind: MatchSourceKind = MatchSourceKind.NATIVE_V2,
    status: MatchStatus = MatchStatus.SCHEDULED,
) -> MatchConditionTarget:
    return MatchConditionTarget(
        match_id=71,
        name="제12회 정기전",
        source_kind=source_kind,
        status=status,
        scheduled_at=SCHEDULED_AT,
        condition=condition,
        condition_version=version,
    )


def _command(
    *,
    values: MatchConditionValues | None = None,
    expected: MatchConditionRecord | None = None,
    version: int | None = None,
    reason: str | None = None,
    key: str = "match-condition:555",
) -> SetMatchConditions:
    return SetMatchConditions(
        match_id=71,
        values=values or _values(),
        expected_condition=expected,
        expected_condition_version=version,
        idempotency_key=key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id="555",
        reason=reason,
    )


class RecordingRepository:
    def __init__(
        self,
        target: MatchConditionTarget | None,
        *,
        stored: StoredMatchConditionOperation | None = None,
    ) -> None:
        self.target = target
        self.stored = stored
        self.calls: list[str] = []
        self.audits: list[tuple[MatchConditionAuditType, MatchConditionRecord | None, MatchConditionTarget]] = []

    def lock_target(self, *, match_id: int) -> MatchConditionTarget | None:
        self.calls.append("lock_target")
        assert match_id == 71
        return self.target

    def find_operation(self, *, idempotency_key: str) -> StoredMatchConditionOperation | None:
        self.calls.append("find_operation")
        assert idempotency_key
        return self.stored

    def replace_conditions(
        self,
        *,
        target: MatchConditionTarget,
        values: MatchConditionValues,
        changed_at: datetime,
    ) -> MatchConditionTarget:
        self.calls.append("replace_conditions")
        created_at = changed_at if target.condition is None else target.condition.created_at
        return MatchConditionTarget(
            match_id=target.match_id,
            name=target.name,
            source_kind=target.source_kind,
            status=target.status,
            scheduled_at=target.scheduled_at,
            condition=MatchConditionRecord(values=values, created_at=created_at, updated_at=changed_at),
            condition_version=target.condition_version,
        )

    def add_audit(
        self,
        *,
        command: SetMatchConditions,
        operation_type: MatchConditionAuditType,
        before: MatchConditionRecord | None,
        after: MatchConditionTarget,
        created_at: datetime,
    ) -> None:
        self.calls.append("add_audit")
        assert command.match_id == 71
        assert created_at == NOW
        self.audits.append((operation_type, before, after))


@dataclass
class RecordingUnitOfWork:
    match_conditions: RecordingRepository
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


def _commands(repository: RecordingRepository) -> tuple[MatchConditionCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return MatchConditionCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_initial_conditions_commit_complete_row_and_versioned_audit_without_reason() -> None:
    repository = RecordingRepository(_target())
    commands, factory = _commands(repository)

    result = commands.set_conditions(_command())

    assert result.operation_type == MatchConditionAuditType.SET
    assert result.snapshot.condition is not None
    assert result.snapshot.condition.values == _values()
    assert result.snapshot.to_audit_payload()["schema_version"] == 1
    assert repository.calls == ["lock_target", "find_operation", "replace_conditions", "add_audit"]
    assert repository.audits[0][1] is None
    assert factory.created[0].commits == 1


def test_existing_conditions_require_reason_before_replacement() -> None:
    current = _record()
    repository = RecordingRepository(_target(condition=current, version=31))
    commands, factory = _commands(repository)

    with pytest.raises(MatchConditionReasonRequiredError):
        commands.set_conditions(_command(values=_values(weather=MatchWeather.RAIN), expected=current, version=31))

    assert repository.calls == ["lock_target", "find_operation"]
    assert factory.created[0].rollbacks == 1


def test_existing_conditions_are_complete_replacement_with_before_after_audit() -> None:
    current = _record()
    repository = RecordingRepository(_target(condition=current, version=31))
    commands, factory = _commands(repository)

    result = commands.set_conditions(
        _command(
            values=_values(weather=MatchWeather.RAIN),
            expected=current,
            version=31,
            reason="공식 조건 정정",
        )
    )

    assert result.operation_type == MatchConditionAuditType.CHANGED
    assert result.snapshot.condition is not None
    assert result.snapshot.condition.values.weather == MatchWeather.RAIN
    assert repository.audits[0][1] == current
    assert factory.created[0].commits == 1


def test_stale_condition_version_rejects_without_write() -> None:
    current = _record()
    repository = RecordingRepository(_target(condition=current, version=32))
    commands, factory = _commands(repository)

    with pytest.raises(MatchConditionStaleError):
        commands.set_conditions(
            _command(
                values=_values(weather=MatchWeather.RAIN),
                expected=current,
                version=31,
                reason="공식 조건 정정",
            )
        )

    assert repository.calls == ["lock_target", "find_operation"]
    assert factory.created[0].rollbacks == 1


@pytest.mark.parametrize(
    ("source_kind", "status"),
    (
        (MatchSourceKind.IMPORTED_V1, MatchStatus.SCHEDULED),
        (MatchSourceKind.NATIVE_V2, MatchStatus.BETTING_OPEN),
    ),
)
def test_imported_or_open_match_rejects_condition_mutation(
    source_kind: MatchSourceKind,
    status: MatchStatus,
) -> None:
    repository = RecordingRepository(_target(source_kind=source_kind, status=status))
    commands, factory = _commands(repository)

    with pytest.raises(MatchConditionUnavailableError):
        commands.set_conditions(_command())

    assert repository.calls == ["lock_target", "find_operation"]
    assert factory.created[0].rollbacks == 1


def test_exact_retry_returns_stored_committed_snapshot_without_second_write() -> None:
    command = _command()
    committed = MatchConditionTarget(
        match_id=71,
        name="제12회 정기전",
        source_kind=MatchSourceKind.NATIVE_V2,
        status=MatchStatus.SCHEDULED,
        scheduled_at=SCHEDULED_AT,
        condition=MatchConditionRecord(values=command.values, created_at=NOW, updated_at=NOW),
        condition_version=None,
    )
    repository = RecordingRepository(
        _target(condition=committed.condition, version=41),
        stored=StoredMatchConditionOperation(
            request_fingerprint=command.request_fingerprint,
            type=MatchConditionAuditType.SET.value,
            match_id=71,
            after_data=committed.to_audit_payload(),
        ),
    )
    commands, factory = _commands(repository)

    result = commands.set_conditions(command)

    assert result.snapshot.condition == committed.condition
    assert repository.calls == ["lock_target", "find_operation"]
    assert factory.created[0].commits == 1


def test_same_key_with_changed_payload_conflicts() -> None:
    original = _command()
    committed = MatchConditionTarget(
        match_id=71,
        name="제12회 정기전",
        source_kind=MatchSourceKind.NATIVE_V2,
        status=MatchStatus.SCHEDULED,
        scheduled_at=SCHEDULED_AT,
        condition=MatchConditionRecord(values=original.values, created_at=NOW, updated_at=NOW),
        condition_version=None,
    )
    repository = RecordingRepository(
        _target(condition=committed.condition, version=41),
        stored=StoredMatchConditionOperation(
            request_fingerprint=original.request_fingerprint,
            type=MatchConditionAuditType.SET.value,
            match_id=71,
            after_data=committed.to_audit_payload(),
        ),
    )
    commands, factory = _commands(repository)

    with pytest.raises(MatchConditionIdempotencyConflictError):
        commands.set_conditions(_command(values=_values(weather=MatchWeather.RAIN)))

    assert factory.created[0].rollbacks == 1
