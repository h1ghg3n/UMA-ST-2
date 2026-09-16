"""Application tests for explicit Special WIN5 Race-void mutations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.win5 import (
    CancelledSpecialWin5Round,
    CancelSpecialWin5Round,
    SetSpecialWin5RaceVoid,
    StoredWin5SpecialVoidOperation,
    UpdatedSpecialWin5Void,
    Win5SpecialRaceVoidFact,
    Win5SpecialRoundCancellationAuditRecord,
    Win5SpecialVoidAuditRecord,
    Win5SpecialVoidAuditType,
    Win5SpecialVoidCommands,
    Win5SpecialVoidIdempotencyConflictError,
    Win5SpecialVoidImmutableError,
    Win5SpecialVoidInvalidSourceError,
    Win5SpecialVoidResultSnapshot,
    Win5SpecialVoidRoundTarget,
    Win5SpecialVoidStateSnapshot,
    Win5SpecialVoidUnavailableError,
    Win5SpecialVoidVersionConflictError,
)
from uma_st2.domain.win5 import (
    Win5ResultInvariantError,
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    fingerprint_special_void_state,
)

NOW = datetime(2026, 8, 26, 10, 30, tzinfo=UTC)
RACE_IDS = (101, 102, 103)


def _round(
    *,
    status: Win5RoundStatus = Win5RoundStatus.CLOSED,
    source_kind: Win5RoundSourceKind = Win5RoundSourceKind.NATIVE_V2,
) -> Win5SpecialVoidRoundTarget:
    return Win5SpecialVoidRoundTarget(
        id=11,
        season_id=7,
        type=Win5RoundType.SPECIAL,
        status=status,
        season_status=Win5SeasonStatus.ACTIVE,
        source_kind=source_kind,
    )


def _state(
    *,
    void_ids: tuple[int, ...] = (),
    with_results: bool = False,
    status: Win5RoundStatus = Win5RoundStatus.CLOSED,
) -> Win5SpecialVoidStateSnapshot:
    voids = tuple(
        Win5SpecialRaceVoidFact(
            race_id=race_id,
            reason=f"void {race_id}",
            voided_at=datetime(2026, 8, 26, 9, race_id % 60, tzinfo=UTC),
        )
        for race_id in void_ids
    )
    non_void_ids = tuple(race_id for race_id in RACE_IDS if race_id not in set(void_ids))
    results = (
        tuple(
            Win5SpecialVoidResultSnapshot(
                id=200 + index,
                race_id=race_id,
                gate_number=index,
            )
            for index, race_id in enumerate(non_void_ids, start=1)
        )
        if with_results
        else ()
    )
    return Win5SpecialVoidStateSnapshot(
        season_id=7,
        round_id=11,
        round_status=status,
        race_ids=RACE_IDS,
        voids=voids,
        results=results,
    )


def _command(
    state: Win5SpecialVoidStateSnapshot,
    *,
    race_id: int = 101,
    voided: bool = True,
    idempotency_key: str = "special-void-command",
) -> SetSpecialWin5RaceVoid:
    return SetSpecialWin5RaceVoid(
        round_id=11,
        race_id=race_id,
        voided=voided,
        expected_void_fingerprint=state.void_fingerprint,
        idempotency_key=idempotency_key,
        actor_discord_user_id="123456789",
        reason="  공식 취소 확인  ",
        guild_id="987654321",
        correlation_id="special-void-test",
    )


class RecordingRepository:
    def __init__(
        self,
        *,
        round_: Win5SpecialVoidRoundTarget | None = None,
        state: Win5SpecialVoidStateSnapshot | None = None,
        stored: StoredWin5SpecialVoidOperation | None = None,
        has_score_events: bool = False,
    ) -> None:
        self.round = _round() if round_ is None else round_
        self.state = _state() if state is None else state
        self.stored = stored
        self.has_events = has_score_events
        self.calls: list[tuple[str, object]] = []
        self.audits: list[Win5SpecialVoidAuditRecord] = []
        self.round_cancellation_audits: list[Win5SpecialRoundCancellationAuditRecord] = []

    def lock_round(self, *, round_id: int) -> Win5SpecialVoidRoundTarget | None:
        self.calls.append(("lock_round", round_id))
        return self.round

    def find_operation(self, *, idempotency_key: str) -> StoredWin5SpecialVoidOperation | None:
        self.calls.append(("find_operation", idempotency_key))
        return self.stored

    def lock_state(self, *, round_: Win5SpecialVoidRoundTarget) -> Win5SpecialVoidStateSnapshot:
        self.calls.append(("lock_state", round_))
        return self.state

    def has_score_events(self, *, round_id: int) -> bool:
        self.calls.append(("has_score_events", round_id))
        return self.has_events

    def clear_results(self, *, results: tuple[Win5SpecialVoidResultSnapshot, ...]) -> None:
        self.calls.append(("clear_results", results))

    def update_race_void(
        self,
        *,
        race_id: int,
        expected_voided: bool,
        target_voided: bool,
        reason: str,
        changed_at: datetime,
    ) -> None:
        self.calls.append(
            (
                "update_race_void",
                (race_id, expected_voided, target_voided, reason, changed_at),
            )
        )

    def void_races(
        self,
        *,
        race_ids: tuple[int, ...],
        reason: str,
        changed_at: datetime,
    ) -> None:
        self.calls.append(("void_races", (race_ids, reason, changed_at)))

    def cancel_round(self, *, round_id: int, changed_at: datetime) -> None:
        self.calls.append(("cancel_round", (round_id, changed_at)))

    def add_void_audit(
        self,
        *,
        command: SetSpecialWin5RaceVoid,
        record: Win5SpecialVoidAuditRecord,
        created_at: datetime,
    ) -> None:
        self.calls.append(("add_void_audit", (command, record, created_at)))
        self.audits.append(record)

    def add_round_cancellation_audit(
        self,
        *,
        command: CancelSpecialWin5Round,
        record: Win5SpecialRoundCancellationAuditRecord,
        created_at: datetime,
    ) -> None:
        self.calls.append(("add_round_cancellation_audit", (command, record, created_at)))
        self.round_cancellation_audits.append(record)


@dataclass
class RecordingUnitOfWork:
    win5_special_voids: RecordingRepository
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


def _commands(repository: RecordingRepository) -> tuple[Win5SpecialVoidCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return Win5SpecialVoidCommands(CommandRunner(factory), clock=lambda: NOW), factory


def _round_cancellation_command(
    state: Win5SpecialVoidStateSnapshot,
    *,
    idempotency_key: str = "special-round-cancellation-command",
) -> CancelSpecialWin5Round:
    return CancelSpecialWin5Round(
        round_id=11,
        expected_void_fingerprint=state.void_fingerprint,
        idempotency_key=idempotency_key,
        actor_discord_user_id="123456789",
        reason="  전체 행사 취소  ",
        guild_id="987654321",
        correlation_id="special-round-cancellation-test",
    )


def test_void_fingerprint_is_order_independent_and_rejects_duplicate_ids() -> None:
    assert fingerprint_special_void_state((103, 101)) == fingerprint_special_void_state((101, 103))
    assert fingerprint_special_void_state(()) != fingerprint_special_void_state((101,))
    with pytest.raises(Win5ResultInvariantError, match="unique"):
        fingerprint_special_void_state((101, 101))


def test_mark_void_resets_complete_unscored_results_and_audits_before_after() -> None:
    before = _state(with_results=True)
    repository = RecordingRepository(state=before)
    commands, factory = _commands(repository)

    result = commands.set_race_void(_command(before))

    assert result.operation_type == Win5SpecialVoidAuditType.VOIDED
    assert result.state.round_status == Win5RoundStatus.CLOSED
    assert tuple(void.race_id for void in result.state.voids) == (101,)
    assert result.state.results == ()
    assert [name for name, _ in repository.calls] == [
        "lock_round",
        "find_operation",
        "lock_state",
        "has_score_events",
        "clear_results",
        "update_race_void",
        "add_void_audit",
    ]
    assert repository.calls[4][1] == before.results
    assert repository.audits[0].before == before
    assert repository.audits[0].after == result
    assert repository.audits[0].after_data["void_fingerprint"] == result.state.void_fingerprint
    assert factory.created[0].commit_count == 1
    assert factory.created[0].rollback_count == 0


def test_restore_void_resets_non_void_result_bundle_and_retains_reason_in_audit() -> None:
    before = _state(void_ids=(101,), with_results=True)
    repository = RecordingRepository(state=before)
    commands, _ = _commands(repository)

    result = commands.set_race_void(_command(before, voided=False))

    assert result.operation_type == Win5SpecialVoidAuditType.RESTORED
    assert result.state.voids == ()
    assert result.state.results == ()
    update = next(value for name, value in repository.calls if name == "update_race_void")
    assert update == (101, True, False, "공식 취소 확인", NOW)
    assert repository.audits[0].before.voids[0].reason == "void 101"


def test_last_non_void_race_terminally_cancels_round_without_score_write() -> None:
    before = _state(void_ids=(101, 102))
    repository = RecordingRepository(state=before)
    commands, _ = _commands(repository)

    result = commands.set_race_void(_command(before, race_id=103))

    assert result.operation_type == Win5SpecialVoidAuditType.ALL_VOID_CANCELLED
    assert result.state.round_status == Win5RoundStatus.CANCELLED
    assert tuple(void.race_id for void in result.state.voids) == RACE_IDS
    assert [name for name, _ in repository.calls].count("cancel_round") == 1
    assert repository.audits[0].type == Win5SpecialVoidAuditType.ALL_VOID_CANCELLED


def test_matching_target_state_is_zero_write_noop() -> None:
    before = _state(void_ids=(101,))
    repository = RecordingRepository(state=before)
    commands, factory = _commands(repository)

    result = commands.set_race_void(_command(before, voided=True))

    assert result.operation_type is None
    assert result.state == before
    assert [name for name, _ in repository.calls] == [
        "lock_round",
        "find_operation",
        "lock_state",
        "has_score_events",
    ]
    assert factory.created[0].commit_count == 1


def test_stale_fingerprint_score_event_and_malformed_source_fail_before_write() -> None:
    state = _state()
    stale_repository = RecordingRepository(state=state)
    stale_commands, stale_factory = _commands(stale_repository)
    with pytest.raises(Win5SpecialVoidVersionConflictError, match="fingerprint conflict"):
        stale_commands.set_race_void(replace(_command(state), expected_void_fingerprint="0" * 64))
    assert stale_factory.created[0].commit_count == 0
    assert stale_factory.created[0].rollback_count == 1

    scored_repository = RecordingRepository(state=state, has_score_events=True)
    scored_commands, _ = _commands(scored_repository)
    with pytest.raises(Win5SpecialVoidImmutableError, match="score-backed"):
        scored_commands.set_race_void(_command(state))

    class MalformedRepository(RecordingRepository):
        def lock_state(self, *, round_: Win5SpecialVoidRoundTarget) -> Win5SpecialVoidStateSnapshot:
            raise ValueError("malformed result bundle")

    malformed_commands, _ = _commands(MalformedRepository())
    with pytest.raises(Win5SpecialVoidInvalidSourceError, match="malformed result bundle"):
        malformed_commands.set_race_void(_command(state))


def test_exact_retry_returns_stored_after_state_without_reloading_or_rewriting() -> None:
    before = _state()
    command = _command(before)
    result = UpdatedSpecialWin5Void(
        state=_state(void_ids=(101,)),
        target_race_id=101,
        target_voided=True,
        operation_type=Win5SpecialVoidAuditType.VOIDED,
    )
    repository = RecordingRepository(
        stored=StoredWin5SpecialVoidOperation(
            request_fingerprint=command.request_fingerprint,
            type=Win5SpecialVoidAuditType.VOIDED.value,
            round_id=11,
            after_data=result.to_audit_payload(),
        )
    )
    commands, factory = _commands(repository)

    assert commands.set_race_void(command) == result
    assert [name for name, _ in repository.calls] == ["lock_round", "find_operation"]
    assert factory.created[0].commit_count == 1

    conflict = _command(before, race_id=102)
    with pytest.raises(Win5SpecialVoidIdempotencyConflictError):
        commands.set_race_void(conflict)


def test_batch_round_cancellation_voids_every_remaining_race_in_one_audit() -> None:
    before = _state(void_ids=(101,), with_results=True)
    repository = RecordingRepository(state=before)
    commands, factory = _commands(repository)

    result = commands.cancel_round(_round_cancellation_command(before))

    assert result.operation_type == Win5SpecialVoidAuditType.ALL_VOID_CANCELLED
    assert result.newly_voided_race_ids == (102, 103)
    assert result.state.round_status == Win5RoundStatus.CANCELLED
    assert tuple(void.race_id for void in result.state.voids) == RACE_IDS
    assert result.state.voids[0] == before.voids[0]
    assert {void.reason for void in result.state.voids[1:]} == {"전체 행사 취소"}
    assert result.state.results == ()
    assert [name for name, _ in repository.calls] == [
        "lock_round",
        "find_operation",
        "lock_state",
        "has_score_events",
        "clear_results",
        "void_races",
        "cancel_round",
        "add_round_cancellation_audit",
    ]
    assert repository.calls[5][1] == ((102, 103), "전체 행사 취소", NOW)
    audit = repository.round_cancellation_audits[0]
    assert audit.before == before
    assert audit.after == result
    assert audit.after_data["command_scope"] == "round"
    assert audit.after_data["newly_voided_race_ids"] == [102, 103]
    assert factory.created[0].commit_count == 1


def test_batch_round_cancellation_exact_retry_uses_distinct_command_scope() -> None:
    before = _state(void_ids=(101,))
    command = _round_cancellation_command(before)
    result = CancelledSpecialWin5Round(
        state=_state(void_ids=RACE_IDS, status=Win5RoundStatus.CANCELLED),
        newly_voided_race_ids=(102, 103),
    )
    repository = RecordingRepository(
        stored=StoredWin5SpecialVoidOperation(
            request_fingerprint=command.request_fingerprint,
            type=Win5SpecialVoidAuditType.ALL_VOID_CANCELLED.value,
            round_id=11,
            after_data=result.to_audit_payload(),
        )
    )
    commands, factory = _commands(repository)

    assert commands.cancel_round(command) == result
    assert [name for name, _ in repository.calls] == ["lock_round", "find_operation"]
    assert factory.created[0].commit_count == 1

    race_command = _command(before, race_id=102, idempotency_key=command.idempotency_key)
    with pytest.raises(Win5SpecialVoidIdempotencyConflictError):
        commands.set_race_void(race_command)


def test_imported_round_rejects_void_and_cancellation_before_state_locks() -> None:
    imported = _round(source_kind=Win5RoundSourceKind.IMPORTED_V1)
    before = _state()

    void_repository = RecordingRepository(round_=imported, state=before)
    with pytest.raises(Win5SpecialVoidUnavailableError, match="read-only"):
        _commands(void_repository)[0].set_race_void(_command(before))
    assert [name for name, _ in void_repository.calls] == ["lock_round"]

    cancel_repository = RecordingRepository(round_=imported, state=before)
    with pytest.raises(Win5SpecialVoidUnavailableError, match="read-only"):
        _commands(cancel_repository)[0].cancel_round(_round_cancellation_command(before))
    assert [name for name, _ in cancel_repository.calls] == ["lock_round"]
