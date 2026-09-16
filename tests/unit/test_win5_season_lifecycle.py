"""WIN5 Season lifecycle application-boundary tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.win5 import (
    ChangedWin5Season,
    CreateWin5Season,
    StoredWin5SeasonOperation,
    TransitionWin5Season,
    UpdateWin5SeasonMetadata,
    Win5SeasonAction,
    Win5SeasonAuditRecord,
    Win5SeasonAuditType,
    Win5SeasonLifecycleCommands,
    Win5SeasonLifecycleIdempotencyConflictError,
    Win5SeasonLifecycleUnavailableError,
    Win5SeasonRoundState,
    Win5SeasonSnapshot,
)
from uma_st2.domain.win5 import Win5RoundStatus, Win5SeasonStatus

NOW = datetime(2026, 8, 27, 9, 0, tzinfo=UTC)


def _snapshot(
    *,
    status: Win5SeasonStatus = Win5SeasonStatus.DRAFT,
    rounds: Win5SeasonRoundState | None = None,
) -> Win5SeasonSnapshot:
    return Win5SeasonSnapshot(
        id=7,
        name="2026 하반기",
        status=status,
        starts_at=None,
        ends_at=None,
        rounds=rounds or Win5SeasonRoundState(),
    )


def _create_command(*, key: str = "season-create-1") -> CreateWin5Season:
    return CreateWin5Season(
        name="2026 하반기",
        starts_at=None,
        ends_at=None,
        idempotency_key=key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id="season-interaction-1",
    )


def _transition_command(
    action: Win5SeasonAction,
    *,
    key: str | None = None,
) -> TransitionWin5Season:
    return TransitionWin5Season(
        season_id=7,
        action=action,
        idempotency_key=key or f"season-{action.value}-7",
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id="season-interaction-2",
    )


class RecordingRepository:
    def __init__(
        self,
        *,
        current: Win5SeasonSnapshot | None = None,
        statuses: tuple[Win5RoundStatus, ...] = (),
        stored: StoredWin5SeasonOperation | None = None,
    ) -> None:
        self.current = current or _snapshot()
        self.statuses = statuses
        self.stored = stored
        self.calls: list[str] = []
        self.status_updates: list[tuple[Win5SeasonStatus, Win5SeasonStatus]] = []
        self.metadata_updates: list[UpdateWin5SeasonMetadata] = []
        self.audits: list[Win5SeasonAuditRecord] = []

    def find_operation(self, *, idempotency_key: str) -> StoredWin5SeasonOperation | None:
        self.calls.append("find_operation")
        return self.stored

    def lock_season(self, *, season_id: int) -> Win5SeasonSnapshot | None:
        self.calls.append("lock_season")
        return self.current

    def lock_round_statuses(self, *, season_id: int) -> tuple[Win5RoundStatus, ...]:
        self.calls.append("lock_round_statuses")
        return self.statuses

    def create_draft(self, *, command: CreateWin5Season, created_at: datetime) -> Win5SeasonSnapshot:
        self.calls.append("create_draft")
        assert created_at == NOW
        return _snapshot()

    def update_status(
        self,
        *,
        season_id: int,
        expected_status: Win5SeasonStatus,
        target_status: Win5SeasonStatus,
        changed_at: datetime,
    ) -> None:
        self.calls.append("update_status")
        assert changed_at == NOW
        self.status_updates.append((expected_status, target_status))

    def update_metadata(self, *, command: UpdateWin5SeasonMetadata, changed_at: datetime) -> None:
        self.calls.append("update_metadata")
        assert changed_at == NOW
        self.metadata_updates.append(command)

    def add_audit(
        self,
        *,
        command: CreateWin5Season | TransitionWin5Season | UpdateWin5SeasonMetadata,
        record: Win5SeasonAuditRecord,
        created_at: datetime,
    ) -> None:
        self.calls.append("add_audit")
        assert created_at == NOW
        self.audits.append(record)


@dataclass
class RecordingUnitOfWork:
    win5_season_lifecycle: RecordingRepository
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
        if self.commits == 0:
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


def _commands(repository: RecordingRepository) -> tuple[Win5SeasonLifecycleCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return Win5SeasonLifecycleCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_create_produces_only_draft_with_one_audit() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)

    result = commands.create_season(_create_command())

    assert result.snapshot.status == Win5SeasonStatus.DRAFT
    assert result.operation_type == Win5SeasonAuditType.CREATED
    assert repository.calls == ["find_operation", "create_draft", "add_audit"]
    assert repository.audits[0].before is None
    assert factory.created[0].commits == 1


@pytest.mark.parametrize(
    ("action", "current_status", "statuses", "target_status"),
    [
        (
            Win5SeasonAction.ACTIVATE,
            Win5SeasonStatus.DRAFT,
            (Win5RoundStatus.SETUP, Win5RoundStatus.SETUP),
            Win5SeasonStatus.ACTIVE,
        ),
        (
            Win5SeasonAction.CLOSE,
            Win5SeasonStatus.ACTIVE,
            (Win5RoundStatus.SCORED, Win5RoundStatus.CANCELLED),
            Win5SeasonStatus.CLOSED,
        ),
        (Win5SeasonAction.CLOSE, Win5SeasonStatus.ACTIVE, (), Win5SeasonStatus.CLOSED),
        (Win5SeasonAction.CANCEL, Win5SeasonStatus.DRAFT, (), Win5SeasonStatus.CANCELLED),
    ],
)
def test_allowed_transition_matrix_commits_one_status_and_audit(
    action: Win5SeasonAction,
    current_status: Win5SeasonStatus,
    statuses: tuple[Win5RoundStatus, ...],
    target_status: Win5SeasonStatus,
) -> None:
    repository = RecordingRepository(current=_snapshot(status=current_status), statuses=statuses)
    commands, _ = _commands(repository)

    result = commands.transition_season(_transition_command(action))

    assert result.snapshot.status == target_status
    assert repository.status_updates == [(current_status, target_status)]
    assert len(repository.audits) == 1


@pytest.mark.parametrize(
    ("action", "current_status", "statuses"),
    [
        (Win5SeasonAction.ACTIVATE, Win5SeasonStatus.DRAFT, (Win5RoundStatus.OPEN,)),
        (Win5SeasonAction.CLOSE, Win5SeasonStatus.ACTIVE, (Win5RoundStatus.CLOSED,)),
        (Win5SeasonAction.CANCEL, Win5SeasonStatus.DRAFT, (Win5RoundStatus.SETUP,)),
        (Win5SeasonAction.ACTIVATE, Win5SeasonStatus.CLOSED, ()),
    ],
)
def test_disallowed_transition_is_zero_write(
    action: Win5SeasonAction,
    current_status: Win5SeasonStatus,
    statuses: tuple[Win5RoundStatus, ...],
) -> None:
    repository = RecordingRepository(current=_snapshot(status=current_status), statuses=statuses)
    commands, factory = _commands(repository)

    with pytest.raises(Win5SeasonLifecycleUnavailableError):
        commands.transition_season(_transition_command(action))

    assert repository.status_updates == []
    assert repository.audits == []
    assert factory.created[0].rollbacks == 1


@pytest.mark.parametrize("status", tuple(Win5SeasonStatus))
def test_metadata_update_is_allowed_at_every_status(status: Win5SeasonStatus) -> None:
    repository = RecordingRepository(current=_snapshot(status=status))
    commands, _ = _commands(repository)
    command = UpdateWin5SeasonMetadata(
        season_id=7,
        name="수정된 시즌",
        starts_at=NOW,
        ends_at=None,
        idempotency_key=f"season-edit-{status.value}",
        actor_discord_user_id="123456789",
    )

    result = commands.update_metadata(command)

    assert result.snapshot.status == status
    assert result.snapshot.name == "수정된 시즌"
    assert repository.metadata_updates == [command]
    assert repository.audits[0].type == Win5SeasonAuditType.METADATA_UPDATED


def test_identical_metadata_update_is_audit_free_noop() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)
    command = UpdateWin5SeasonMetadata(
        season_id=7,
        name="2026 하반기",
        starts_at=None,
        ends_at=None,
        idempotency_key="season-edit-noop",
        actor_discord_user_id="123456789",
    )

    result = commands.update_metadata(command)

    assert result == ChangedWin5Season(snapshot=_snapshot(), operation_type=None, changed=False)
    assert repository.metadata_updates == []
    assert repository.audits == []
    assert factory.created[0].commits == 1


def test_exact_retry_returns_stored_after_snapshot_without_second_write() -> None:
    command = _transition_command(Win5SeasonAction.ACTIVATE)
    stored_snapshot = _snapshot(status=Win5SeasonStatus.ACTIVE)
    repository = RecordingRepository(
        stored=StoredWin5SeasonOperation(
            request_fingerprint=command.request_fingerprint,
            type=Win5SeasonAuditType.ACTIVATED.value,
            season_id=7,
            after_data=stored_snapshot.to_audit_payload(),
        )
    )
    commands, _ = _commands(repository)

    result = commands.transition_season(command)

    assert result.snapshot == stored_snapshot
    assert repository.status_updates == []
    assert repository.audits == []


def test_reused_idempotency_key_for_another_command_is_conflict() -> None:
    command = _create_command(key="reused")
    repository = RecordingRepository(
        stored=StoredWin5SeasonOperation(
            request_fingerprint="0" * 64,
            type=Win5SeasonAuditType.CLOSED.value,
            season_id=7,
            after_data=None,
        )
    )
    commands, _ = _commands(repository)

    with pytest.raises(Win5SeasonLifecycleIdempotencyConflictError):
        commands.create_season(command)

    assert repository.audits == []
