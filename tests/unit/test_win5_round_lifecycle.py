"""WIN5 Round open/close application-boundary tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.win5 import (
    StoredWin5RoundLifecycleOperation,
    TransitionWin5Round,
    Win5RoundGraphState,
    Win5RoundLifecycleAction,
    Win5RoundLifecycleAuditRecord,
    Win5RoundLifecycleAuditType,
    Win5RoundLifecycleCommands,
    Win5RoundLifecycleIdempotencyConflictError,
    Win5RoundLifecycleInvalidSourceError,
    Win5RoundLifecycleRoundTarget,
    Win5RoundLifecycleSeason,
    Win5RoundLifecycleUnavailableError,
    Win5RoundOpenLimitError,
)
from uma_st2.domain.win5 import Win5RoundSourceKind, Win5RoundStatus, Win5RoundType, Win5SeasonStatus

NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


def _season(*, status: Win5SeasonStatus = Win5SeasonStatus.ACTIVE) -> Win5RoundLifecycleSeason:
    return Win5RoundLifecycleSeason(id=7, name="2026 하반기", status=status)


def _round(
    *,
    round_type: Win5RoundType = Win5RoundType.NORMAL,
    status: Win5RoundStatus = Win5RoundStatus.SETUP,
    source_kind: Win5RoundSourceKind = Win5RoundSourceKind.NATIVE_V2,
) -> Win5RoundLifecycleRoundTarget:
    return Win5RoundLifecycleRoundTarget(
        id=11,
        season_id=7,
        name="제3회 아리마 기념",
        type=round_type,
        status=status,
        source_kind=source_kind,
    )


def _command(
    *,
    action: Win5RoundLifecycleAction = Win5RoundLifecycleAction.OPEN,
    idempotency_key: str = "win5-round-open-11-v1",
) -> TransitionWin5Round:
    return TransitionWin5Round(
        round_id=11,
        action=action,
        idempotency_key=idempotency_key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id="interaction-lifecycle-55",
        reason="operator confirmed",
    )


class RecordingRepository:
    def __init__(
        self,
        *,
        season: Win5RoundLifecycleSeason | None = None,
        round_: Win5RoundLifecycleRoundTarget | None = None,
        stored: StoredWin5RoundLifecycleOperation | None = None,
        graph: Win5RoundGraphState | None = None,
        open_count: int = 24,
    ) -> None:
        self.season = _season() if season is None else season
        self.round = _round() if round_ is None else round_
        self.stored = stored
        self.graph = Win5RoundGraphState(1, 5, 0) if graph is None else graph
        self.open_count = open_count
        self.calls: list[tuple[str, object]] = []
        self.updates: list[tuple[int, Win5RoundStatus, Win5RoundStatus, datetime]] = []
        self.audits: list[Win5RoundLifecycleAuditRecord] = []

    def find_round_season_id(self, *, round_id: int) -> int | None:
        self.calls.append(("find_round_season_id", round_id))
        return None if self.round is None else self.round.season_id

    def lock_season(self, *, season_id: int) -> Win5RoundLifecycleSeason | None:
        self.calls.append(("lock_season", season_id))
        return self.season

    def get_season(self, *, season_id: int) -> Win5RoundLifecycleSeason | None:
        self.calls.append(("get_season", season_id))
        return self.season

    def lock_round(self, *, round_id: int) -> Win5RoundLifecycleRoundTarget | None:
        self.calls.append(("lock_round", round_id))
        return self.round

    def find_operation(self, *, idempotency_key: str) -> StoredWin5RoundLifecycleOperation | None:
        self.calls.append(("find_operation", idempotency_key))
        return self.stored

    def load_round_graph_state(self, *, round_id: int) -> Win5RoundGraphState:
        self.calls.append(("load_round_graph_state", round_id))
        return self.graph

    def count_open_rounds(self, *, season_id: int) -> int:
        self.calls.append(("count_open_rounds", season_id))
        return self.open_count

    def update_round_status(
        self,
        *,
        round_id: int,
        expected_status: Win5RoundStatus,
        target_status: Win5RoundStatus,
        changed_at: datetime,
    ) -> None:
        self.calls.append(("update_round_status", round_id))
        self.updates.append((round_id, expected_status, target_status, changed_at))

    def add_lifecycle_audit(
        self,
        *,
        command: TransitionWin5Round,
        record: Win5RoundLifecycleAuditRecord,
        created_at: datetime,
    ) -> None:
        self.calls.append(("add_lifecycle_audit", command.idempotency_key))
        assert created_at == NOW
        self.audits.append(record)


@dataclass
class RecordingUnitOfWork:
    win5_round_lifecycle: RecordingRepository
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


def _commands(repository: RecordingRepository) -> tuple[Win5RoundLifecycleCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return Win5RoundLifecycleCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_open_locks_season_before_round_and_commits_one_audited_transition() -> None:
    repository = RecordingRepository(open_count=24)
    commands, factory = _commands(repository)

    result = commands.transition_round(_command())

    assert result.status == Win5RoundStatus.OPEN
    assert result.operation_type == Win5RoundLifecycleAuditType.OPENED
    assert [name for name, _ in repository.calls] == [
        "find_round_season_id",
        "lock_season",
        "lock_round",
        "find_operation",
        "load_round_graph_state",
        "count_open_rounds",
        "update_round_status",
        "add_lifecycle_audit",
    ]
    assert repository.updates == [(11, Win5RoundStatus.SETUP, Win5RoundStatus.OPEN, NOW)]
    assert repository.audits[0].before.status == Win5RoundStatus.SETUP
    assert repository.audits[0].after.status == Win5RoundStatus.OPEN
    assert factory.created[0].commit_count == 1


def test_special_multi_race_round_consumes_one_round_slot_and_needs_no_entries() -> None:
    repository = RecordingRepository(
        round_=_round(round_type=Win5RoundType.SPECIAL),
        graph=Win5RoundGraphState(race_count=5, race_entry_count=0, result_count=0),
        open_count=24,
    )
    commands, _ = _commands(repository)

    result = commands.transition_round(_command())

    assert result.round_type == Win5RoundType.SPECIAL
    assert result.status == Win5RoundStatus.OPEN
    assert [item for item in repository.calls if item[0] == "count_open_rounds"] == [("count_open_rounds", 7)]


def test_open_limit_of_25_rolls_back_without_update_or_audit() -> None:
    repository = RecordingRepository(open_count=25)
    commands, factory = _commands(repository)

    with pytest.raises(Win5RoundOpenLimitError, match="25"):
        commands.transition_round(_command())

    assert repository.updates == []
    assert repository.audits == []
    assert factory.created[0].commit_count == 0
    assert factory.created[0].rollback_count == 1


@pytest.mark.parametrize(
    ("round_type", "graph", "message"),
    [
        (Win5RoundType.NORMAL, Win5RoundGraphState(2, 10, 0), "exactly one Race"),
        (Win5RoundType.NORMAL, Win5RoundGraphState(1, 4, 0), "at least five"),
        (Win5RoundType.SPECIAL, Win5RoundGraphState(0, 0, 0), "one or more Races"),
        (Win5RoundType.SPECIAL, Win5RoundGraphState(5, 0, 1), "result-bearing"),
    ],
)
def test_open_rejects_unready_or_result_bearing_round_graph(
    round_type: Win5RoundType,
    graph: Win5RoundGraphState,
    message: str,
) -> None:
    repository = RecordingRepository(round_=_round(round_type=round_type), graph=graph)
    commands, _ = _commands(repository)

    with pytest.raises(Win5RoundLifecycleInvalidSourceError, match=message):
        commands.transition_round(_command())

    assert repository.updates == []
    assert repository.audits == []


def test_close_locks_only_round_root_and_allows_inactive_parent_repair() -> None:
    repository = RecordingRepository(
        season=_season(status=Win5SeasonStatus.CLOSED),
        round_=_round(status=Win5RoundStatus.OPEN),
    )
    commands, factory = _commands(repository)

    result = commands.transition_round(
        _command(
            action=Win5RoundLifecycleAction.CLOSE,
            idempotency_key="win5-round-close-11-v1",
        )
    )

    assert result.status == Win5RoundStatus.CLOSED
    assert [name for name, _ in repository.calls] == [
        "lock_round",
        "get_season",
        "find_operation",
        "update_round_status",
        "add_lifecycle_audit",
    ]
    assert factory.created[0].commit_count == 1


def test_exact_retry_returns_stored_transition_after_required_root_locks() -> None:
    initial_repository = RecordingRepository()
    initial_commands, _ = _commands(initial_repository)
    stored_result = initial_commands.transition_round(_command())
    command = _command()
    repository = RecordingRepository(
        round_=_round(status=Win5RoundStatus.OPEN),
        stored=StoredWin5RoundLifecycleOperation(
            request_fingerprint=command.request_fingerprint,
            type=Win5RoundLifecycleAuditType.OPENED.value,
            season_id=stored_result.season_id,
            round_id=stored_result.round_id,
            after_data=stored_result.snapshot.to_audit_payload(),
        ),
    )
    commands, factory = _commands(repository)

    retried = commands.transition_round(command)

    assert retried == stored_result
    assert [name for name, _ in repository.calls] == [
        "find_round_season_id",
        "lock_season",
        "lock_round",
        "find_operation",
    ]
    assert repository.updates == []
    assert repository.audits == []
    assert factory.created[0].commit_count == 1


def test_reused_idempotency_key_for_other_action_is_conflict() -> None:
    command = _command(action=Win5RoundLifecycleAction.CLOSE, idempotency_key="reused")
    repository = RecordingRepository(
        round_=_round(status=Win5RoundStatus.OPEN),
        stored=StoredWin5RoundLifecycleOperation(
            request_fingerprint="0" * 64,
            type=Win5RoundLifecycleAuditType.OPENED.value,
            season_id=7,
            round_id=11,
            after_data=None,
        ),
    )
    commands, _ = _commands(repository)

    with pytest.raises(Win5RoundLifecycleIdempotencyConflictError):
        commands.transition_round(command)

    assert repository.updates == []
    assert repository.audits == []


def test_imported_round_rejects_lifecycle_mutation_before_idempotency_lookup() -> None:
    repository = RecordingRepository(round_=_round(source_kind=Win5RoundSourceKind.IMPORTED_V1))
    commands, _ = _commands(repository)

    with pytest.raises(Win5RoundLifecycleUnavailableError, match="read-only"):
        commands.transition_round(_command())

    assert [name for name, _ in repository.calls] == [
        "find_round_season_id",
        "lock_season",
        "lock_round",
    ]
    assert repository.updates == []
    assert repository.audits == []
