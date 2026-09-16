"""WIN5 Round creation application-boundary tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.win5 import (
    CreateWin5Round,
    StoredWin5RoundCreationOperation,
    Win5CreatedEntry,
    Win5CreatedRace,
    Win5CreatedRoundGraph,
    Win5RoundCreationAuditError,
    Win5RoundCreationAuditRecord,
    Win5RoundCreationAuditType,
    Win5RoundCreationCommands,
    Win5RoundCreationEntryInput,
    Win5RoundCreationIdempotencyConflictError,
    Win5RoundCreationRaceInput,
    Win5RoundCreationSeason,
    Win5RoundCreationUnavailableError,
)
from uma_st2.domain.win5 import Win5RoundStatus, Win5RoundType, Win5SeasonStatus

NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 8, 30, 6, 30, tzinfo=UTC)
_DEFAULT_SEASON = object()
NORMAL_ENTRIES = (
    Win5RoundCreationEntryInput(gate_number=1, name="스페셜 위크"),
    Win5RoundCreationEntryInput(gate_number=2, name="사일런스 스즈카"),
    Win5RoundCreationEntryInput(gate_number=4, name="토카이 테이오"),
    Win5RoundCreationEntryInput(gate_number=7, name="메지로 맥퀸"),
    Win5RoundCreationEntryInput(gate_number=8, name="라이스 샤워"),
)


def _season(*, status: Win5SeasonStatus = Win5SeasonStatus.ACTIVE) -> Win5RoundCreationSeason:
    return Win5RoundCreationSeason(id=7, name="2026 하반기", status=status)


def _command(
    *,
    round_type: Win5RoundType = Win5RoundType.NORMAL,
    round_name: str = "제3회 아리마 기념",
    race_names: tuple[str, ...] = ("아리마 기념",),
    idempotency_key: str = "win5-normal-round-create-11-v1",
) -> CreateWin5Round:
    return CreateWin5Round(
        season_id=7,
        round_type=round_type,
        round_name=round_name,
        races=tuple(
            Win5RoundCreationRaceInput(
                name=name,
                scheduled_at=SCHEDULED_AT if round_type == Win5RoundType.NORMAL else None,
                entries=NORMAL_ENTRIES if round_type == Win5RoundType.NORMAL else (),
            )
            for name in race_names
        ),
        idempotency_key=idempotency_key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id="interaction-create-55",
        reason="operator confirmed",
    )


class RecordingRepository:
    def __init__(
        self,
        *,
        season: Win5RoundCreationSeason | None | object = _DEFAULT_SEASON,
        stored: StoredWin5RoundCreationOperation | None = None,
    ) -> None:
        self.season = _season() if season is _DEFAULT_SEASON else season
        self.stored = stored
        self.calls: list[tuple[str, object]] = []
        self.audits: list[Win5RoundCreationAuditRecord] = []
        self.created_commands: list[CreateWin5Round] = []

    def lock_season(self, *, season_id: int) -> Win5RoundCreationSeason | None:
        self.calls.append(("lock_season", season_id))
        assert self.season is None or isinstance(self.season, Win5RoundCreationSeason)
        return self.season

    def find_operation(self, *, idempotency_key: str) -> StoredWin5RoundCreationOperation | None:
        self.calls.append(("find_operation", idempotency_key))
        return self.stored

    def create_round_graph(
        self,
        *,
        command: CreateWin5Round,
        created_at: datetime,
    ) -> Win5CreatedRoundGraph:
        self.calls.append(("create_round_graph", command.round_type))
        assert created_at == NOW
        self.created_commands.append(command)
        return Win5CreatedRoundGraph(
            round_id=11,
            races=tuple(
                Win5CreatedRace(
                    id=101 + index,
                    name=race.name,
                    scheduled_at=race.scheduled_at,
                    entries=tuple(
                        Win5CreatedEntry(
                            id=201 + entry_index,
                            gate_number=entry.gate_number,
                            name=entry.name,
                        )
                        for entry_index, entry in enumerate(race.entries)
                    ),
                )
                for index, race in enumerate(command.races)
            ),
        )

    def add_creation_audit(
        self,
        *,
        command: CreateWin5Round,
        record: Win5RoundCreationAuditRecord,
        created_at: datetime,
    ) -> None:
        self.calls.append(("add_creation_audit", command.idempotency_key))
        assert created_at == NOW
        self.audits.append(record)


@dataclass
class RecordingUnitOfWork:
    win5_round_creation: RecordingRepository
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


def _commands(repository: RecordingRepository) -> tuple[Win5RoundCreationCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return Win5RoundCreationCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_normal_creation_commits_one_setup_round_race_and_season_aware_audit() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)

    result = commands.create_round(_command())

    assert result.operation_type == Win5RoundCreationAuditType.NORMAL_CREATED
    assert result.snapshot.round_status == Win5RoundStatus.SETUP
    assert result.snapshot.season_id == 7
    assert result.snapshot.season_name == "2026 하반기"
    assert tuple(race.name for race in result.snapshot.races) == ("아리마 기념",)
    assert result.snapshot.races[0].scheduled_at == SCHEDULED_AT
    assert tuple((entry.gate_number, entry.name) for entry in result.snapshot.races[0].entries) == tuple(
        (entry.gate_number, entry.name) for entry in NORMAL_ENTRIES
    )
    assert [name for name, _ in repository.calls] == [
        "lock_season",
        "find_operation",
        "create_round_graph",
        "add_creation_audit",
    ]
    assert repository.audits[0].after.to_audit_payload()["season_status"] == "active"
    assert repository.audits[0].after.to_audit_payload()["schema_version"] == 2
    assert factory.created[0].commit_count == 1


@pytest.mark.parametrize("season_status", [Win5SeasonStatus.DRAFT, Win5SeasonStatus.ACTIVE])
def test_special_creation_preserves_order_in_draft_or_active_season(
    season_status: Win5SeasonStatus,
) -> None:
    repository = RecordingRepository(season=_season(status=season_status))
    commands, _ = _commands(repository)
    command = _command(
        round_type=Win5RoundType.SPECIAL,
        round_name="여름 특별전",
        race_names=("삿포로 1R", "니가타 2R", "코쿠라 3R"),
        idempotency_key=f"special-{season_status.value}",
    )

    result = commands.create_round(command)

    assert result.operation_type == Win5RoundCreationAuditType.SPECIAL_CREATED
    assert result.snapshot.season_status == season_status
    assert tuple(race.name for race in result.snapshot.races) == (
        "삿포로 1R",
        "니가타 2R",
        "코쿠라 3R",
    )
    payload = result.snapshot.to_audit_payload()
    assert [race["race_name"] for race in payload["races"]] == [  # type: ignore[index]
        "삿포로 1R",
        "니가타 2R",
        "코쿠라 3R",
    ]


@pytest.mark.parametrize("status", [Win5SeasonStatus.CLOSED, Win5SeasonStatus.CANCELLED])
def test_creation_rejects_terminal_season_without_graph_or_audit(status: Win5SeasonStatus) -> None:
    repository = RecordingRepository(season=_season(status=status))
    commands, factory = _commands(repository)

    with pytest.raises(Win5RoundCreationUnavailableError, match="draft or active"):
        commands.create_round(_command())

    assert repository.created_commands == []
    assert repository.audits == []
    assert factory.created[0].rollback_count == 1


def test_creation_rejects_missing_season_before_any_write() -> None:
    repository = RecordingRepository(season=None)
    commands, factory = _commands(repository)

    with pytest.raises(Win5RoundCreationUnavailableError, match="does not exist"):
        commands.create_round(_command())

    assert repository.calls == [("lock_season", 7)]
    assert factory.created[0].rollback_count == 1


def test_creation_rolls_back_if_repository_returns_drifted_entry_facts() -> None:
    class DriftedRepository(RecordingRepository):
        def create_round_graph(
            self,
            *,
            command: CreateWin5Round,
            created_at: datetime,
        ) -> Win5CreatedRoundGraph:
            graph = super().create_round_graph(command=command, created_at=created_at)
            race = graph.races[0]
            return Win5CreatedRoundGraph(
                round_id=graph.round_id,
                races=(
                    Win5CreatedRace(
                        id=race.id,
                        name=race.name,
                        scheduled_at=race.scheduled_at,
                        entries=(
                            Win5CreatedEntry(
                                id=race.entries[0].id,
                                gate_number=race.entries[0].gate_number,
                                name="다른 말",
                            ),
                            *race.entries[1:],
                        ),
                    ),
                ),
            )

    repository = DriftedRepository()
    commands, factory = _commands(repository)

    with pytest.raises(Win5RoundCreationAuditError, match="does not match"):
        commands.create_round(_command())

    assert repository.audits == []
    assert factory.created[0].rollback_count == 1


def test_command_enforces_normal_single_race_and_aware_optional_schedule() -> None:
    with pytest.raises(ValueError, match="exactly one Race"):
        _command(race_names=("Race A", "Race B"))

    with pytest.raises(ValueError, match="timezone-aware"):
        Win5RoundCreationRaceInput(
            name="Race A",
            scheduled_at=datetime(2026, 8, 30, 15, 30),
        )


def test_command_enforces_normal_entry_cardinality_and_gate_uniqueness() -> None:
    with pytest.raises(ValueError, match="at least 5 Entries"):
        CreateWin5Round(
            season_id=7,
            round_type=Win5RoundType.NORMAL,
            round_name="라운드",
            races=(
                Win5RoundCreationRaceInput(
                    name="Race",
                    entries=NORMAL_ENTRIES[:4],
                ),
            ),
            idempotency_key="too-few-entries",
            actor_discord_user_id="123456789",
        )

    with pytest.raises(ValueError, match="gate numbers must be unique"):
        Win5RoundCreationRaceInput(
            name="Race",
            entries=(
                Win5RoundCreationEntryInput(gate_number=1, name="말 A"),
                Win5RoundCreationEntryInput(gate_number=1, name="말 B"),
            ),
        )


def test_special_creation_rejects_reference_entries() -> None:
    with pytest.raises(ValueError, match="does not accept reference Entries"):
        CreateWin5Round(
            season_id=7,
            round_type=Win5RoundType.SPECIAL,
            round_name="특별전",
            races=(
                Win5RoundCreationRaceInput(
                    name="Race",
                    entries=NORMAL_ENTRIES,
                ),
            ),
            idempotency_key="special-with-entries",
            actor_discord_user_id="123456789",
        )


def test_exact_retry_returns_stored_created_graph_without_duplicate_write() -> None:
    command = _command()
    initial_repository = RecordingRepository()
    initial_commands, _ = _commands(initial_repository)
    initial = initial_commands.create_round(command)
    repository = RecordingRepository(
        stored=StoredWin5RoundCreationOperation(
            request_fingerprint=command.request_fingerprint,
            type=initial.operation_type.value,
            season_id=initial.snapshot.season_id,
            round_id=initial.snapshot.round_id,
            after_data=initial.snapshot.to_audit_payload(),
        )
    )
    commands, factory = _commands(repository)

    retried = commands.create_round(command)

    assert retried == initial
    assert [name for name, _ in repository.calls] == ["lock_season", "find_operation"]
    assert repository.created_commands == []
    assert repository.audits == []
    assert factory.created[0].commit_count == 1


def test_reused_key_with_changed_round_title_is_conflict() -> None:
    command = _command(round_name="변경된 제목")
    repository = RecordingRepository(
        stored=StoredWin5RoundCreationOperation(
            request_fingerprint="0" * 64,
            type=Win5RoundCreationAuditType.NORMAL_CREATED.value,
            season_id=7,
            round_id=11,
            after_data=None,
        )
    )
    commands, _ = _commands(repository)

    with pytest.raises(Win5RoundCreationIdempotencyConflictError):
        commands.create_round(command)

    assert repository.created_commands == []


def test_exact_retry_rejects_malformed_or_mismatched_creation_audit() -> None:
    command = _command()
    repository = RecordingRepository(
        stored=StoredWin5RoundCreationOperation(
            request_fingerprint=command.request_fingerprint,
            type=Win5RoundCreationAuditType.NORMAL_CREATED.value,
            season_id=7,
            round_id=11,
            after_data={"schema_version": 2},
        )
    )
    commands, _ = _commands(repository)

    with pytest.raises(Win5RoundCreationAuditError, match="malformed"):
        commands.create_round(command)
