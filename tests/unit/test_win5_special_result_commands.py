"""Application tests for authoritative Special WIN5 result mutations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.win5 import (
    SaveSpecialWin5Result,
    StoredWin5SpecialResultOperation,
    Win5SpecialResultAuditRecord,
    Win5SpecialResultAuditType,
    Win5SpecialResultCommands,
    Win5SpecialResultIdempotencyConflictError,
    Win5SpecialResultImmutableError,
    Win5SpecialResultInvalidSourceError,
    Win5SpecialResultRaceTarget,
    Win5SpecialResultRoundTarget,
    Win5SpecialResultUnavailableError,
    Win5SpecialResultVersionConflictError,
    Win5SpecialResultWinnerInput,
)
from uma_st2.domain.win5 import (
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SpecialResultWinner,
    fingerprint_special_result,
)

NOW = datetime(2026, 8, 26, 1, 2, tzinfo=UTC)


def _round(
    *,
    status: Win5RoundStatus = Win5RoundStatus.CLOSED,
    source_kind: Win5RoundSourceKind = Win5RoundSourceKind.NATIVE_V2,
) -> Win5SpecialResultRoundTarget:
    return Win5SpecialResultRoundTarget(
        id=11,
        season_id=7,
        name="제3회 Special",
        type=Win5RoundType.SPECIAL,
        status=status,
        season_status=Win5SeasonStatus.ACTIVE,
        source_kind=source_kind,
    )


def _races(*, void_ids: tuple[int, ...] = ()) -> tuple[Win5SpecialResultRaceTarget, ...]:
    return tuple(
        Win5SpecialResultRaceTarget(
            id=race_id,
            name=f"Race {race_id}",
            void_reason=f"void {race_id}" if race_id in void_ids else None,
            voided_at=NOW if race_id in void_ids else None,
        )
        for race_id in (101, 102, 103)
    )


def _desired(*, swap_first_two: bool = False) -> tuple[Win5SpecialResultWinnerInput, ...]:
    gates = [3, 7, 1]
    if swap_first_two:
        gates[:2] = reversed(gates[:2])
    return tuple(
        Win5SpecialResultWinnerInput(race_id=race_id, gate_number=gate_number)
        for race_id, gate_number in zip((101, 102, 103), gates, strict=True)
    )


def _current() -> tuple[Win5SpecialResultWinner, ...]:
    return tuple(
        Win5SpecialResultWinner(id=result_id, race_id=race_id, gate_number=gate_number)
        for result_id, race_id, gate_number in (
            (201, 101, 3),
            (202, 102, 7),
            (203, 103, 1),
        )
    )


def _command(
    *,
    winners: tuple[Win5SpecialResultWinnerInput, ...] | None = None,
    expected_result_fingerprint: str | None = None,
    idempotency_key: str = "special-result-command",
) -> SaveSpecialWin5Result:
    return SaveSpecialWin5Result(
        round_id=11,
        winners=_desired() if winners is None else winners,
        expected_result_fingerprint=expected_result_fingerprint,
        idempotency_key=idempotency_key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id="special-result-test",
        reason="운영 검토",
    )


class RecordingRepository:
    def __init__(
        self,
        *,
        round_: Win5SpecialResultRoundTarget | None = None,
        races: tuple[Win5SpecialResultRaceTarget, ...] | None = None,
        current: tuple[Win5SpecialResultWinner, ...] = (),
        stored: StoredWin5SpecialResultOperation | None = None,
        has_score_events: bool = False,
    ) -> None:
        self.round = round_ or _round()
        self.races = races if races is not None else _races()
        self.current = current
        self.stored = stored
        self.has_events = has_score_events
        self.calls: list[tuple[str, object]] = []
        self.replacements: list[tuple[Win5SpecialResultWinner, ...]] = []
        self.audits: list[Win5SpecialResultAuditRecord] = []

    def lock_round(self, *, round_id: int) -> Win5SpecialResultRoundTarget | None:
        self.calls.append(("lock_round", round_id))
        return self.round

    def find_operation(self, *, idempotency_key: str) -> StoredWin5SpecialResultOperation | None:
        self.calls.append(("find_operation", idempotency_key))
        return self.stored

    def lock_round_races(self, *, round_id: int) -> tuple[Win5SpecialResultRaceTarget, ...]:
        self.calls.append(("lock_round_races", round_id))
        return self.races

    def lock_current_results(self, *, round_id: int) -> tuple[Win5SpecialResultWinner, ...]:
        self.calls.append(("lock_current_results", round_id))
        return self.current

    def has_score_events(self, *, round_id: int) -> bool:
        self.calls.append(("has_score_events", round_id))
        return self.has_events

    def create_results(
        self,
        *,
        winners: tuple[Win5SpecialResultWinnerInput, ...],
        created_at: datetime,
    ) -> tuple[Win5SpecialResultWinner, ...]:
        self.calls.append(("create_results", (winners, created_at)))
        return tuple(
            Win5SpecialResultWinner(
                id=500 + index,
                race_id=winner.race_id,
                gate_number=winner.gate_number,
            )
            for index, winner in enumerate(winners, start=1)
        )

    def replace_results(self, *, winners: tuple[Win5SpecialResultWinner, ...]) -> None:
        self.calls.append(("replace_results", winners))
        self.replacements.append(winners)

    def add_result_audit(
        self,
        *,
        command: SaveSpecialWin5Result,
        round_: Win5SpecialResultRoundTarget,
        record: Win5SpecialResultAuditRecord,
        created_at: datetime,
    ) -> None:
        self.calls.append(("add_result_audit", (command, round_, record, created_at)))
        self.audits.append(record)


@dataclass
class RecordingUnitOfWork:
    win5_special_results: RecordingRepository
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


def _commands(repository: RecordingRepository) -> tuple[Win5SpecialResultCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return Win5SpecialResultCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_initial_result_entry_persists_complete_bundle_and_one_audit() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)

    result = commands.save_result(_command())

    assert result.operation_type == Win5SpecialResultAuditType.ENTERED
    assert tuple((item.race_id, item.gate_number) for item in result.winners) == (
        (101, 3),
        (102, 7),
        (103, 1),
    )
    assert result.result_fingerprint == fingerprint_special_result(result.winners)
    assert [name for name, _ in repository.calls] == [
        "lock_round",
        "find_operation",
        "lock_round_races",
        "lock_current_results",
        "has_score_events",
        "create_results",
        "add_result_audit",
    ]
    assert repository.audits[0].before_data is None
    assert repository.audits[0].after_data == result.to_audit_payload()
    assert factory.created[0].commit_count == 1


def test_correction_preserves_result_ids_and_audits_before_after() -> None:
    current = _current()
    current_fingerprint = fingerprint_special_result(current)
    repository = RecordingRepository(current=current)
    commands, _ = _commands(repository)

    result = commands.save_result(
        _command(
            winners=_desired(swap_first_two=True),
            expected_result_fingerprint=current_fingerprint,
        )
    )

    assert result.operation_type == Win5SpecialResultAuditType.CORRECTED
    assert tuple(item.id for item in result.winners) == tuple(item.id for item in current)
    assert tuple(item.gate_number for item in result.winners) == (7, 3, 1)
    assert repository.replacements == [result.winners]
    audit = repository.audits[0]
    assert audit.before_data is not None
    assert audit.before_data["result_fingerprint"] == current_fingerprint
    assert audit.after_data["result_fingerprint"] == result.result_fingerprint


def test_matching_expected_fingerprint_exact_state_is_zero_write_noop() -> None:
    current = _current()
    repository = RecordingRepository(current=current)
    commands, factory = _commands(repository)

    result = commands.save_result(
        _command(
            expected_result_fingerprint=fingerprint_special_result(current),
            idempotency_key="special-result-noop",
        )
    )

    assert result.operation_type is None
    assert result.winners == current
    assert repository.replacements == []
    assert repository.audits == []
    assert factory.created[0].commit_count == 1


def test_stale_or_partial_current_result_fails_before_write() -> None:
    stale_repository = RecordingRepository(current=_current())
    commands, stale_factory = _commands(stale_repository)
    with pytest.raises(Win5SpecialResultVersionConflictError, match="fingerprint conflict"):
        commands.save_result(_command(expected_result_fingerprint="0" * 64))
    assert stale_repository.replacements == []
    assert stale_factory.created[0].rollback_count == 1

    partial_repository = RecordingRepository(current=_current()[:2])
    commands, _ = _commands(partial_repository)
    with pytest.raises(Win5SpecialResultInvalidSourceError, match="non-void Race"):
        commands.save_result(_command())
    assert partial_repository.replacements == []


@pytest.mark.parametrize(
    "winners",
    [
        _desired()[:2],
        (*_desired()[:2], Win5SpecialResultWinnerInput(race_id=999, gate_number=1)),
        (
            Win5SpecialResultWinnerInput(race_id=101, gate_number=3),
            Win5SpecialResultWinnerInput(race_id=101, gate_number=4),
            Win5SpecialResultWinnerInput(race_id=103, gate_number=1),
        ),
    ],
)
def test_desired_bundle_requires_exactly_one_winner_for_every_round_race(
    winners: tuple[Win5SpecialResultWinnerInput, ...],
) -> None:
    repository = RecordingRepository()
    commands, _ = _commands(repository)

    with pytest.raises(Win5SpecialResultInvalidSourceError, match="exactly one winner"):
        commands.save_result(_command(winners=winners))

    assert repository.audits == []


def test_reference_entries_do_not_constrain_special_winner_gate() -> None:
    repository = RecordingRepository(races=_races())
    commands, _ = _commands(repository)
    winners = (
        Win5SpecialResultWinnerInput(race_id=101, gate_number=99),
        Win5SpecialResultWinnerInput(race_id=102, gate_number=88),
        Win5SpecialResultWinnerInput(race_id=103, gate_number=77),
    )

    result = commands.save_result(_command(winners=winners))

    assert tuple(item.gate_number for item in result.winners) == (99, 88, 77)


def test_scored_round_or_existing_score_event_makes_result_immutable() -> None:
    scored = RecordingRepository(round_=_round(status=Win5RoundStatus.SCORED))
    commands, _ = _commands(scored)
    with pytest.raises(Win5SpecialResultImmutableError):
        commands.save_result(_command())

    event_backed = RecordingRepository(current=_current(), has_score_events=True)
    commands, _ = _commands(event_backed)
    with pytest.raises(Win5SpecialResultImmutableError, match="score events"):
        commands.save_result(_command(expected_result_fingerprint=fingerprint_special_result(_current())))
    assert event_backed.replacements == []


def test_mixed_void_entry_persists_only_non_void_winners() -> None:
    repository = RecordingRepository(races=_races(void_ids=(102,)))
    commands, factory = _commands(repository)
    winners = (
        Win5SpecialResultWinnerInput(race_id=101, gate_number=3),
        Win5SpecialResultWinnerInput(race_id=103, gate_number=1),
    )

    result = commands.save_result(_command(winners=winners))

    assert tuple((winner.race_id, winner.gate_number) for winner in result.winners) == (
        (101, 3),
        (103, 1),
    )
    assert repository.audits[0].after_data == result.to_audit_payload()
    assert factory.created[0].commit_count == 1


def test_mixed_void_correction_preserves_non_void_result_ids() -> None:
    current = (
        Win5SpecialResultWinner(id=201, race_id=101, gate_number=3),
        Win5SpecialResultWinner(id=203, race_id=103, gate_number=1),
    )
    repository = RecordingRepository(races=_races(void_ids=(102,)), current=current)
    commands, _ = _commands(repository)
    desired = (
        Win5SpecialResultWinnerInput(race_id=101, gate_number=9),
        Win5SpecialResultWinnerInput(race_id=103, gate_number=8),
    )

    result = commands.save_result(
        _command(
            winners=desired,
            expected_result_fingerprint=fingerprint_special_result(current),
        )
    )

    assert tuple(winner.id for winner in result.winners) == (201, 203)
    assert tuple(winner.gate_number for winner in result.winners) == (9, 8)
    assert repository.replacements == [result.winners]


def test_stale_void_mapping_or_all_void_round_fails_before_write() -> None:
    stale_repository = RecordingRepository(races=_races(void_ids=(102,)))
    stale_commands, stale_factory = _commands(stale_repository)

    with pytest.raises(Win5SpecialResultInvalidSourceError, match="non-void Race"):
        stale_commands.save_result(_command())

    assert stale_repository.audits == []
    assert stale_factory.created[0].rollback_count == 1

    all_void_repository = RecordingRepository(races=_races(void_ids=(101, 102, 103)))
    all_void_commands, _ = _commands(all_void_repository)
    with pytest.raises(Win5SpecialResultInvalidSourceError, match="all-void"):
        all_void_commands.save_result(_command(winners=()))
    assert all_void_repository.audits == []


def test_exact_retry_returns_stored_result_without_loading_sources() -> None:
    first_repository = RecordingRepository()
    first_commands, _ = _commands(first_repository)
    command = _command()
    stored_result = first_commands.save_result(command)
    repository = RecordingRepository(
        round_=_round(status=Win5RoundStatus.SCORED),
        stored=StoredWin5SpecialResultOperation(
            request_fingerprint=command.request_fingerprint,
            type=Win5SpecialResultAuditType.ENTERED.value,
            round_id=11,
            after_data=stored_result.to_audit_payload(),
        ),
    )
    commands, _ = _commands(repository)

    retried = commands.save_result(command)

    assert retried == stored_result
    assert [name for name, _ in repository.calls] == ["lock_round", "find_operation"]


def test_reused_idempotency_key_with_changed_bundle_is_conflict() -> None:
    original = _command()
    repository = RecordingRepository(
        stored=StoredWin5SpecialResultOperation(
            request_fingerprint=original.request_fingerprint,
            type=Win5SpecialResultAuditType.ENTERED.value,
            round_id=11,
            after_data=None,
        )
    )
    commands, _ = _commands(repository)

    with pytest.raises(Win5SpecialResultIdempotencyConflictError):
        commands.save_result(_command(winners=_desired(swap_first_two=True)))

    assert repository.audits == []


def test_imported_round_rejects_special_result_before_idempotency_lookup() -> None:
    repository = RecordingRepository(round_=_round(source_kind=Win5RoundSourceKind.IMPORTED_V1))
    commands, _ = _commands(repository)

    with pytest.raises(Win5SpecialResultUnavailableError, match="read-only"):
        commands.save_result(_command())

    assert [name for name, _ in repository.calls] == ["lock_round"]
    assert repository.audits == []
