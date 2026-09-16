"""Normal WIN5 authoritative result application-boundary tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.win5 import (
    SaveNormalWin5Result,
    StoredWin5NormalResultOperation,
    Win5NormalResultAuditRecord,
    Win5NormalResultAuditType,
    Win5NormalResultCommands,
    Win5NormalResultIdempotencyConflictError,
    Win5NormalResultImmutableError,
    Win5NormalResultInvalidSourceError,
    Win5NormalResultPlacementInput,
    Win5NormalResultRoundTarget,
    Win5NormalResultUnavailableError,
    Win5NormalResultVersionConflictError,
)
from uma_st2.domain.win5 import (
    Win5NormalResultPlacement,
    Win5Race,
    Win5RaceEntry,
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    fingerprint_normal_result,
)

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)


def _round(
    *,
    status: Win5RoundStatus = Win5RoundStatus.CLOSED,
    source_kind: Win5RoundSourceKind = Win5RoundSourceKind.NATIVE_V2,
) -> Win5NormalResultRoundTarget:
    return Win5NormalResultRoundTarget(
        id=11,
        season_id=7,
        name="제3회 아리마 기념",
        type=Win5RoundType.NORMAL,
        status=status,
        season_status=Win5SeasonStatus.ACTIVE,
        source_kind=source_kind,
    )


def _race() -> Win5Race:
    return Win5Race(
        id=17,
        name="아리마 기념",
        entries=tuple(
            Win5RaceEntry(
                id=100 + gate_number,
                race_id=17,
                gate_number=gate_number,
                name=f"entry-{gate_number}",
            )
            for gate_number in range(1, 7)
        ),
    )


def _current() -> tuple[Win5NormalResultPlacement, ...]:
    return tuple(
        Win5NormalResultPlacement(
            id=200 + position,
            position=position,
            race_entry_id=100 + position,
        )
        for position in range(1, 6)
    )


def _desired(*, swap_first_two: bool = False) -> tuple[Win5NormalResultPlacementInput, ...]:
    entry_ids = [101, 102, 103, 104, 105]
    if swap_first_two:
        entry_ids[:2] = reversed(entry_ids[:2])
    return tuple(
        Win5NormalResultPlacementInput(position=position, race_entry_id=entry_id)
        for position, entry_id in enumerate(entry_ids, start=1)
    )


def _command(
    *,
    placements: tuple[Win5NormalResultPlacementInput, ...] | None = None,
    expected_result_fingerprint: str | None = None,
    idempotency_key: str = "normal-result-round-11-v1",
) -> SaveNormalWin5Result:
    return SaveNormalWin5Result(
        round_id=11,
        placements=_desired() if placements is None else placements,
        expected_result_fingerprint=expected_result_fingerprint,
        idempotency_key=idempotency_key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id="interaction-result-55",
        reason="operator checked source",
    )


class RecordingRepository:
    def __init__(
        self,
        *,
        round_: Win5NormalResultRoundTarget | None = None,
        stored: StoredWin5NormalResultOperation | None = None,
        races: tuple[Win5Race, ...] | None = None,
        current: tuple[Win5NormalResultPlacement, ...] = (),
        has_score_events: bool = False,
    ) -> None:
        self.round = _round() if round_ is None else round_
        self.stored = stored
        self.races = (_race(),) if races is None else races
        self.current = current
        self.score_events = has_score_events
        self.calls: list[tuple[str, object]] = []
        self.audits: list[Win5NormalResultAuditRecord] = []
        self.replacements: list[tuple[Win5NormalResultPlacement, ...]] = []

    def lock_round(self, *, round_id: int) -> Win5NormalResultRoundTarget | None:
        self.calls.append(("lock_round", round_id))
        return self.round

    def find_operation(self, *, idempotency_key: str) -> StoredWin5NormalResultOperation | None:
        self.calls.append(("find_operation", idempotency_key))
        return self.stored

    def lock_round_races(self, *, round_id: int) -> tuple[Win5Race, ...]:
        self.calls.append(("lock_round_races", round_id))
        return self.races

    def lock_current_result(self, *, race_id: int) -> tuple[Win5NormalResultPlacement, ...]:
        self.calls.append(("lock_current_result", race_id))
        return self.current

    def has_score_events(self, *, round_id: int) -> bool:
        self.calls.append(("has_score_events", round_id))
        return self.score_events

    def create_result(
        self,
        *,
        race_id: int,
        placements: tuple[Win5NormalResultPlacementInput, ...],
        created_at: datetime,
    ) -> tuple[Win5NormalResultPlacement, ...]:
        self.calls.append(("create_result", (race_id, placements, created_at)))
        return tuple(
            Win5NormalResultPlacement(
                id=200 + placement.position,
                position=placement.position,
                race_entry_id=placement.race_entry_id,
            )
            for placement in placements
        )

    def replace_result(
        self,
        *,
        race_id: int,
        placements: tuple[Win5NormalResultPlacement, ...],
    ) -> None:
        self.calls.append(("replace_result", (race_id, placements)))
        self.replacements.append(placements)

    def add_result_audit(
        self,
        *,
        command: SaveNormalWin5Result,
        round_: Win5NormalResultRoundTarget,
        record: Win5NormalResultAuditRecord,
        created_at: datetime,
    ) -> None:
        self.calls.append(("add_result_audit", (command, round_, record, created_at)))
        self.audits.append(record)


@dataclass
class RecordingUnitOfWork:
    win5_normal_results: RecordingRepository
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


def _commands(repository: RecordingRepository) -> tuple[Win5NormalResultCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return Win5NormalResultCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_initial_result_entry_persists_complete_board_and_one_audit() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)

    result = commands.save_result(_command())

    assert result.operation_type == Win5NormalResultAuditType.ENTERED
    assert tuple((item.position, item.race_entry_id) for item in result.placements) == (
        (1, 101),
        (2, 102),
        (3, 103),
        (4, 104),
        (5, 105),
    )
    assert result.result_fingerprint == fingerprint_normal_result(result.placements)
    assert [name for name, _ in repository.calls] == [
        "lock_round",
        "find_operation",
        "lock_round_races",
        "lock_current_result",
        "has_score_events",
        "create_result",
        "add_result_audit",
    ]
    audit = repository.audits[0]
    assert audit.type == Win5NormalResultAuditType.ENTERED
    assert audit.before_data is None
    assert audit.after_data == result.to_audit_payload()
    assert factory.created[0].commit_count == 1


def test_correction_preserves_result_ids_and_audits_before_after() -> None:
    current = _current()
    current_fingerprint = fingerprint_normal_result(current)
    repository = RecordingRepository(current=current)
    commands, _ = _commands(repository)

    result = commands.save_result(
        _command(
            placements=_desired(swap_first_two=True),
            expected_result_fingerprint=current_fingerprint,
        )
    )

    assert result.operation_type == Win5NormalResultAuditType.CORRECTED
    assert tuple(item.id for item in result.placements) == tuple(item.id for item in current)
    assert tuple(item.race_entry_id for item in result.placements) == (102, 101, 103, 104, 105)
    assert repository.replacements == [result.placements]
    audit = repository.audits[0]
    assert audit.before_data is not None
    assert audit.before_data["result_fingerprint"] == current_fingerprint
    assert audit.after_data["result_fingerprint"] == result.result_fingerprint


def test_matching_expected_fingerprint_exact_state_is_zero_write_noop() -> None:
    current = _current()
    repository = RecordingRepository(current=current)
    commands, factory = _commands(repository)

    result = commands.save_result(
        _command(expected_result_fingerprint=fingerprint_normal_result(current), idempotency_key="new-noop-key")
    )

    assert result.operation_type is None
    assert result.placements == current
    assert repository.replacements == []
    assert repository.audits == []
    assert factory.created[0].commit_count == 1


def test_stale_correction_fails_before_result_or_audit_write() -> None:
    repository = RecordingRepository(current=_current())
    commands, factory = _commands(repository)

    with pytest.raises(Win5NormalResultVersionConflictError, match="fingerprint conflict"):
        commands.save_result(_command(expected_result_fingerprint="0" * 64))

    assert repository.replacements == []
    assert repository.audits == []
    assert factory.created[0].rollback_count == 1


def test_partial_current_result_fails_closed_instead_of_becoming_initial_or_correction() -> None:
    repository = RecordingRepository(current=_current()[:4])
    commands, _ = _commands(repository)

    with pytest.raises(Win5NormalResultInvalidSourceError, match="position 1 through 5"):
        commands.save_result(_command())

    assert repository.replacements == []
    assert repository.audits == []


def test_scored_round_or_existing_score_event_makes_result_immutable() -> None:
    scored = RecordingRepository(round_=_round(status=Win5RoundStatus.SCORED))
    commands, _ = _commands(scored)
    with pytest.raises(Win5NormalResultImmutableError):
        commands.save_result(_command())

    event_backed = RecordingRepository(current=_current(), has_score_events=True)
    commands, _ = _commands(event_backed)
    with pytest.raises(Win5NormalResultImmutableError, match="score events"):
        commands.save_result(_command(expected_result_fingerprint=fingerprint_normal_result(_current())))
    assert event_backed.replacements == []


def test_exact_retry_returns_stored_result_without_loading_sources() -> None:
    first_repository = RecordingRepository()
    first_commands, _ = _commands(first_repository)
    stored_result = first_commands.save_result(_command())
    command = _command()
    repository = RecordingRepository(
        round_=_round(status=Win5RoundStatus.SCORED),
        stored=StoredWin5NormalResultOperation(
            request_fingerprint=command.request_fingerprint,
            type=Win5NormalResultAuditType.ENTERED.value,
            round_id=11,
            after_data=stored_result.to_audit_payload(),
        ),
    )
    commands, _ = _commands(repository)

    retried = commands.save_result(command)

    assert retried == stored_result
    assert [name for name, _ in repository.calls] == ["lock_round", "find_operation"]


def test_reused_idempotency_key_with_changed_desired_board_is_conflict() -> None:
    original = _command()
    repository = RecordingRepository(
        stored=StoredWin5NormalResultOperation(
            request_fingerprint=original.request_fingerprint,
            type=Win5NormalResultAuditType.ENTERED.value,
            round_id=11,
            after_data=None,
        )
    )
    commands, _ = _commands(repository)

    with pytest.raises(Win5NormalResultIdempotencyConflictError):
        commands.save_result(_command(placements=_desired(swap_first_two=True)))

    assert repository.audits == []


def test_desired_board_rejects_duplicate_or_foreign_race_entries() -> None:
    duplicate = list(_desired())
    duplicate[-1] = Win5NormalResultPlacementInput(position=5, race_entry_id=101)
    repository = RecordingRepository()
    commands, _ = _commands(repository)
    with pytest.raises(Win5NormalResultInvalidSourceError, match="each RaceEntry at most once"):
        commands.save_result(_command(placements=tuple(duplicate)))

    foreign = list(_desired())
    foreign[-1] = Win5NormalResultPlacementInput(position=5, race_entry_id=999)
    repository = RecordingRepository()
    commands, _ = _commands(repository)
    with pytest.raises(Win5NormalResultInvalidSourceError, match="RaceEntries from the Round"):
        commands.save_result(_command(placements=tuple(foreign)))


def test_imported_round_rejects_normal_result_before_idempotency_lookup() -> None:
    repository = RecordingRepository(round_=_round(source_kind=Win5RoundSourceKind.IMPORTED_V1))
    commands, _ = _commands(repository)

    with pytest.raises(Win5NormalResultUnavailableError, match="read-only"):
        commands.save_result(_command())

    assert [name for name, _ in repository.calls] == ["lock_round"]
    assert repository.audits == []
