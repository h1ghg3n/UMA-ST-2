"""Guarded WIN5 setup-Round deletion application tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.win5 import (
    DeleteWin5SetupRound,
    StoredWin5SetupRoundDeletionOperation,
    Win5SetupRoundDeletionAuditError,
    Win5SetupRoundDeletionAuditRecord,
    Win5SetupRoundDeletionAuditType,
    Win5SetupRoundDeletionCommands,
    Win5SetupRoundDeletionEntry,
    Win5SetupRoundDeletionIdempotencyConflictError,
    Win5SetupRoundDeletionInvalidSourceError,
    Win5SetupRoundDeletionRace,
    Win5SetupRoundDeletionSeason,
    Win5SetupRoundDeletionSnapshot,
    Win5SetupRoundDeletionUnavailableError,
    Win5SetupRoundDeletionVersionConflictError,
    Win5SetupRoundDependencyState,
)
from uma_st2.domain.win5 import Win5RoundSourceKind, Win5RoundStatus, Win5RoundType, Win5SeasonStatus

NOW = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)


def _season(*, status: Win5SeasonStatus = Win5SeasonStatus.ACTIVE) -> Win5SetupRoundDeletionSeason:
    return Win5SetupRoundDeletionSeason(id=7, name="2026 하반기", status=status)


def _snapshot(
    *,
    season_status: Win5SeasonStatus = Win5SeasonStatus.ACTIVE,
    round_status: Win5RoundStatus = Win5RoundStatus.SETUP,
    races: tuple[Win5SetupRoundDeletionRace, ...] | None = None,
    source_kind: Win5RoundSourceKind = Win5RoundSourceKind.NATIVE_V2,
) -> Win5SetupRoundDeletionSnapshot:
    if races is None:
        races = (
            Win5SetupRoundDeletionRace(
                id=101,
                name="아리마 기념",
                scheduled_at=datetime(2026, 8, 30, 6, 30, tzinfo=UTC),
                entries=tuple(
                    Win5SetupRoundDeletionEntry(id=200 + gate, gate_number=gate, name=f"말 {gate}")
                    for gate in (1, 2, 4, 7, 8)
                ),
            ),
        )
    return Win5SetupRoundDeletionSnapshot(
        season_id=7,
        season_name="2026 하반기",
        season_status=season_status,
        round_id=11,
        round_name="제3회 아리마 기념",
        round_type=Win5RoundType.NORMAL,
        round_status=round_status,
        opens_at=None,
        closes_at=None,
        races=races,
        source_kind=source_kind,
    )


def _command(
    snapshot: Win5SetupRoundDeletionSnapshot,
    *,
    idempotency_key: str = "win5-setup-round-delete:55",
) -> DeleteWin5SetupRound:
    return DeleteWin5SetupRound(
        season_id=snapshot.season_id,
        round_id=snapshot.round_id,
        expected_graph_fingerprint=snapshot.graph_fingerprint,
        idempotency_key=idempotency_key,
        actor_discord_user_id="123456789",
        reason="게이트 번호 오입력으로 재생성",
        guild_id="987654321",
        correlation_id="55",
    )


class RecordingRepository:
    def __init__(
        self,
        *,
        snapshot: Win5SetupRoundDeletionSnapshot | None = None,
        season: Win5SetupRoundDeletionSeason | None = None,
        dependencies: Win5SetupRoundDependencyState | None = None,
        stored: StoredWin5SetupRoundDeletionOperation | None = None,
    ) -> None:
        self.snapshot = snapshot or _snapshot()
        self.season = season or _season(status=self.snapshot.season_status)
        self.dependencies = dependencies or Win5SetupRoundDependencyState(0, 0, 0, 0)
        self.stored = stored
        self.calls: list[str] = []
        self.deleted: list[Win5SetupRoundDeletionSnapshot] = []
        self.audits: list[Win5SetupRoundDeletionAuditRecord] = []

    def lock_season(self, *, season_id: int) -> Win5SetupRoundDeletionSeason | None:
        self.calls.append("lock_season")
        return self.season if self.season is not None and self.season.id == season_id else None

    def find_operation(self, *, idempotency_key: str) -> StoredWin5SetupRoundDeletionOperation | None:
        self.calls.append("find_operation")
        return self.stored

    def lock_round_graph(
        self,
        *,
        season: Win5SetupRoundDeletionSeason,
        round_id: int,
    ) -> Win5SetupRoundDeletionSnapshot | None:
        self.calls.append("lock_round_graph")
        if self.snapshot.round_id != round_id or self.snapshot.season_id != season.id:
            return None
        return self.snapshot

    def lock_dependency_state(
        self,
        *,
        round_id: int,
        race_ids: tuple[int, ...],
    ) -> Win5SetupRoundDependencyState:
        self.calls.append("lock_dependency_state")
        assert round_id == self.snapshot.round_id
        assert race_ids == tuple(race.id for race in self.snapshot.races)
        return self.dependencies

    def delete_round_graph(self, *, snapshot: Win5SetupRoundDeletionSnapshot) -> None:
        self.calls.append("delete_round_graph")
        self.deleted.append(snapshot)

    def add_deletion_audit(
        self,
        *,
        command: DeleteWin5SetupRound,
        record: Win5SetupRoundDeletionAuditRecord,
        created_at: datetime,
    ) -> None:
        self.calls.append("add_deletion_audit")
        assert created_at == NOW
        assert command.reason == "게이트 번호 오입력으로 재생성"
        self.audits.append(record)


@dataclass
class RecordingUnitOfWork:
    win5_setup_round_deletion: RecordingRepository
    commit_count: int = 0
    rollback_count: int = 0

    def __enter__(self) -> RecordingUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self.commit_count == 0 and self.rollback_count == 0:
            self.rollback_count += 1
        return False

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


class RecordingFactory:
    def __init__(self, repository: RecordingRepository) -> None:
        self.repository = repository
        self.created: list[RecordingUnitOfWork] = []

    def __call__(self) -> RecordingUnitOfWork:
        unit_of_work = RecordingUnitOfWork(self.repository)
        self.created.append(unit_of_work)
        return unit_of_work


def _commands(repository: RecordingRepository) -> tuple[Win5SetupRoundDeletionCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return Win5SetupRoundDeletionCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_deletion_removes_complete_graph_and_retains_full_audit_snapshot() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)
    command = _command(repository.snapshot)

    result = commands.delete_round(command)

    assert result.snapshot == repository.snapshot
    assert result.operation_type == Win5SetupRoundDeletionAuditType.DELETED
    assert repository.calls == [
        "lock_season",
        "find_operation",
        "lock_round_graph",
        "lock_dependency_state",
        "delete_round_graph",
        "add_deletion_audit",
    ]
    assert repository.deleted == [repository.snapshot]
    assert repository.audits[0].before_data == repository.snapshot.to_audit_payload()
    assert repository.audits[0].after_data == {
        "schema_version": 1,
        "deleted": True,
        "season_id": 7,
        "round_id": 11,
        "graph_fingerprint": repository.snapshot.graph_fingerprint,
    }
    assert factory.created[0].commit_count == 1


def test_deletion_allows_incomplete_zero_race_setup_graph_for_cleanup() -> None:
    repository = RecordingRepository(snapshot=_snapshot(races=()))
    commands, _ = _commands(repository)

    result = commands.delete_round(_command(repository.snapshot))

    assert result.snapshot.race_count == 0
    assert result.snapshot.entry_count == 0


@pytest.mark.parametrize(
    "dependencies",
    [
        Win5SetupRoundDependencyState(1, 0, 0, 0),
        Win5SetupRoundDependencyState(0, 1, 0, 0),
        Win5SetupRoundDependencyState(0, 0, 1, 0),
        Win5SetupRoundDependencyState(0, 0, 0, 1),
    ],
)
def test_deletion_rejects_every_downstream_fact_without_delete_or_audit(
    dependencies: Win5SetupRoundDependencyState,
) -> None:
    repository = RecordingRepository(dependencies=dependencies)
    commands, factory = _commands(repository)

    with pytest.raises(Win5SetupRoundDeletionUnavailableError, match="downstream"):
        commands.delete_round(_command(repository.snapshot))

    assert repository.deleted == []
    assert repository.audits == []
    assert factory.created[0].rollback_count == 1


@pytest.mark.parametrize(
    ("season_status", "round_status"),
    [
        (Win5SeasonStatus.CLOSED, Win5RoundStatus.SETUP),
        (Win5SeasonStatus.ACTIVE, Win5RoundStatus.OPEN),
        (Win5SeasonStatus.ACTIVE, Win5RoundStatus.CLOSED),
        (Win5SeasonStatus.ACTIVE, Win5RoundStatus.SCORED),
    ],
)
def test_deletion_rejects_terminal_season_or_non_setup_round(
    season_status: Win5SeasonStatus,
    round_status: Win5RoundStatus,
) -> None:
    snapshot = _snapshot(season_status=season_status, round_status=round_status)
    repository = RecordingRepository(snapshot=snapshot)
    commands, _ = _commands(repository)

    with pytest.raises(Win5SetupRoundDeletionUnavailableError):
        commands.delete_round(_command(snapshot))

    assert repository.deleted == []


def test_deletion_rejects_stale_graph_fingerprint_before_dependency_read() -> None:
    repository = RecordingRepository()
    commands, _ = _commands(repository)
    command = DeleteWin5SetupRound(
        season_id=7,
        round_id=11,
        expected_graph_fingerprint="0" * 64,
        idempotency_key="stale",
        actor_discord_user_id="123",
        reason="stale preview",
    )

    with pytest.raises(Win5SetupRoundDeletionVersionConflictError):
        commands.delete_round(command)

    assert repository.calls == ["lock_season", "find_operation", "lock_round_graph"]


def test_deletion_rejects_round_graph_that_disagrees_with_locked_season() -> None:
    snapshot = _snapshot()
    repository = RecordingRepository(
        snapshot=snapshot,
        season=Win5SetupRoundDeletionSeason(
            id=snapshot.season_id,
            name="renamed after malformed projection",
            status=snapshot.season_status,
        ),
    )
    commands, _ = _commands(repository)

    with pytest.raises(Win5SetupRoundDeletionInvalidSourceError, match="parent Season"):
        commands.delete_round(_command(snapshot))

    assert repository.calls == ["lock_season", "find_operation", "lock_round_graph"]
    assert repository.deleted == []


def test_exact_retry_reconstructs_deleted_graph_from_retained_before_snapshot() -> None:
    snapshot = _snapshot()
    command = _command(snapshot)
    record = Win5SetupRoundDeletionAuditRecord(before=snapshot)
    repository = RecordingRepository(
        stored=StoredWin5SetupRoundDeletionOperation(
            request_fingerprint=command.request_fingerprint,
            type=Win5SetupRoundDeletionAuditType.DELETED.value,
            season_id=snapshot.season_id,
            round_id=snapshot.round_id,
            before_data=record.before_data,
            after_data=record.after_data,
        )
    )
    commands, factory = _commands(repository)

    result = commands.delete_round(command)

    assert result.snapshot == snapshot
    assert repository.calls == ["lock_season", "find_operation"]
    assert repository.deleted == []
    assert factory.created[0].commit_count == 1


def test_changed_command_reusing_deletion_key_is_conflict() -> None:
    snapshot = _snapshot()
    command = _command(snapshot)
    repository = RecordingRepository(
        stored=StoredWin5SetupRoundDeletionOperation(
            request_fingerprint="0" * 64,
            type=Win5SetupRoundDeletionAuditType.DELETED.value,
            season_id=7,
            round_id=11,
            before_data=None,
            after_data=None,
        )
    )
    commands, _ = _commands(repository)

    with pytest.raises(Win5SetupRoundDeletionIdempotencyConflictError):
        commands.delete_round(command)


def test_exact_retry_rejects_malformed_tombstone() -> None:
    snapshot = _snapshot()
    command = _command(snapshot)
    repository = RecordingRepository(
        stored=StoredWin5SetupRoundDeletionOperation(
            request_fingerprint=command.request_fingerprint,
            type=Win5SetupRoundDeletionAuditType.DELETED.value,
            season_id=7,
            round_id=11,
            before_data=snapshot.to_audit_payload(),
            after_data={"schema_version": 1, "deleted": False},
        )
    )
    commands, _ = _commands(repository)

    with pytest.raises(Win5SetupRoundDeletionAuditError, match="malformed"):
        commands.delete_round(command)


def test_command_requires_non_empty_reason_and_lowercase_sha256() -> None:
    snapshot = _snapshot()
    with pytest.raises(ValueError, match="reason"):
        DeleteWin5SetupRound(
            season_id=7,
            round_id=11,
            expected_graph_fingerprint=snapshot.graph_fingerprint,
            idempotency_key="delete",
            actor_discord_user_id="123",
            reason="   ",
        )
    with pytest.raises(ValueError, match="SHA-256"):
        DeleteWin5SetupRound(
            season_id=7,
            round_id=11,
            expected_graph_fingerprint="A" * 64,
            idempotency_key="delete",
            actor_discord_user_id="123",
            reason="정정",
        )


def test_imported_round_rejects_setup_deletion_before_dependency_locks() -> None:
    snapshot = _snapshot(source_kind=Win5RoundSourceKind.IMPORTED_V1)
    repository = RecordingRepository(snapshot=snapshot)
    commands, _ = _commands(repository)

    with pytest.raises(Win5SetupRoundDeletionUnavailableError, match="read-only"):
        commands.delete_round(_command(snapshot))

    assert repository.calls == ["lock_season", "find_operation", "lock_round_graph"]
    assert repository.deleted == []
    assert repository.audits == []
