"""Application contracts for staff Persona status management."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.identity import (
    ChangedPersonaStatus,
    ChangePersonaStatus,
    StaffPersonaStatusAuditError,
    StaffPersonaStatusAuditType,
    StaffPersonaStatusCommands,
    StaffPersonaStatusIdempotencyConflictError,
    StaffPersonaStatusNoChangeError,
    StaffPersonaStatusNotFoundError,
    StaffPersonaStatusQueries,
    StaffPersonaStatusStaleError,
    StaffPersonaStatusState,
    StoredStaffPersonaStatusOperation,
)
from uma_st2.domain.identity import PersonaStatus

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
PERSONA_ID = "11111111-2222-4333-8444-555555555555"


def _state(status: PersonaStatus = PersonaStatus.NORMAL, **changes: object) -> StaffPersonaStatusState:
    values: dict[str, object] = {
        "guild_id": "987",
        "persona_id": PERSONA_ID,
        "display_name": "테스트 Persona",
        "status": status,
    }
    values.update(changes)
    return StaffPersonaStatusState(**values)  # type: ignore[arg-type]


def _command(
    state: StaffPersonaStatusState,
    desired_status: PersonaStatus,
    **changes: object,
) -> ChangePersonaStatus:
    values: dict[str, object] = {
        "guild_id": state.guild_id,
        "target_persona_id": state.persona_id,
        "desired_status": desired_status,
        "reason": "운영 상태 정정",
        "updated_by_discord_user_id": "900",
        "expected_target_fingerprint": state.state_fingerprint,
        "idempotency_key": "persona-status-1",
        "correlation_id": "persona-status-1",
    }
    values.update(changes)
    return ChangePersonaStatus(**values)  # type: ignore[arg-type]


class RecordingRepository:
    def __init__(self, state: StaffPersonaStatusState | None) -> None:
        self.state = state
        self.operations: dict[str, StoredStaffPersonaStatusOperation] = {}
        self.changes = 0

    def lock_state(self, **_kwargs: object) -> StaffPersonaStatusState | None:
        return self.state

    def get_state(self, **_kwargs: object) -> StaffPersonaStatusState | None:
        return self.state

    def find_operation(self, *, idempotency_key: str) -> StoredStaffPersonaStatusOperation | None:
        return self.operations.get(idempotency_key)

    def change_status(
        self,
        *,
        command: ChangePersonaStatus,
        state: StaffPersonaStatusState,
        updated_at: datetime,
    ) -> ChangedPersonaStatus:
        self.changes += 1
        result = ChangedPersonaStatus(
            persona_id=state.persona_id,
            display_name=state.display_name,
            previous_status=state.status,
            status=command.desired_status,
            reason=command.reason,
            updated_at=updated_at,
        )
        self.operations[command.idempotency_key] = StoredStaffPersonaStatusOperation(
            request_fingerprint=command.request_fingerprint,
            type=StaffPersonaStatusAuditType.PERSONA_STATUS_CHANGED.value,
            persona_id=state.persona_id,
            game_account_id=None,
            after_data=result.to_audit_payload(),
        )
        self.state = replace(state, status=command.desired_status)
        return result


class UnitOfWork:
    def __init__(self, repository: RecordingRepository) -> None:
        self.staff_persona_status = repository

    def __enter__(self) -> UnitOfWork:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _commands(repository: RecordingRepository) -> StaffPersonaStatusCommands:
    return StaffPersonaStatusCommands(CommandRunner(lambda: UnitOfWork(repository)), clock=lambda: NOW)


def _queries(repository: RecordingRepository) -> StaffPersonaStatusQueries:
    return StaffPersonaStatusQueries(QueryRunner(lambda: UnitOfWork(repository)))


ALL_TRANSITIONS = [
    (current, desired) for current in PersonaStatus for desired in PersonaStatus if current is not desired
]


@pytest.mark.parametrize(("current", "desired"), ALL_TRANSITIONS)
def test_all_canonical_status_transitions_are_explicit_and_exact_retryable(
    current: PersonaStatus,
    desired: PersonaStatus,
) -> None:
    state = _state(current)
    repository = RecordingRepository(state)
    command = _command(state, desired)

    created = _commands(repository).change(command)
    retried = _commands(repository).change(command)

    assert created.previous_status is current
    assert created.status is desired
    assert created.reason == "운영 상태 정정"
    assert retried == replace(created, exact_retry=True)
    assert repository.state is not None and repository.state.status is desired
    assert repository.changes == 1


def test_status_preview_is_normalized_and_reports_status_gate_effect() -> None:
    repository = RecordingRepository(_state(PersonaStatus.EXPELLED))

    preview = _queries(repository).get_preview(
        guild_id=" 987 ",
        persona_id=f" {PERSONA_ID} ",
        desired_status="warning",
        reason=" 복귀 검토 완료 ",
    )

    assert preview.state.status is PersonaStatus.EXPELLED
    assert preview.desired_status is PersonaStatus.WARNING
    assert preview.reason == "복귀 검토 완료"
    assert preview.resulting_status_gate_open is True


def test_status_change_rejects_missing_stale_no_change_and_changed_key_without_write() -> None:
    state = _state(PersonaStatus.NORMAL)
    missing = RecordingRepository(None)
    with pytest.raises(StaffPersonaStatusNotFoundError):
        _commands(missing).change(_command(state, PersonaStatus.WARNING))

    stale = RecordingRepository(replace(state, display_name="새 표시명"))
    with pytest.raises(StaffPersonaStatusStaleError):
        _commands(stale).change(_command(state, PersonaStatus.WARNING))

    unchanged = RecordingRepository(state)
    with pytest.raises(StaffPersonaStatusNoChangeError):
        _commands(unchanged).change(_command(state, PersonaStatus.NORMAL))
    with pytest.raises(StaffPersonaStatusNoChangeError):
        _queries(unchanged).get_preview(
            guild_id="987",
            persona_id=PERSONA_ID,
            desired_status=PersonaStatus.NORMAL,
            reason="변경 없음",
        )

    repository = RecordingRepository(state)
    commands = _commands(repository)
    command = _command(state, PersonaStatus.WARNING)
    commands.change(command)
    with pytest.raises(StaffPersonaStatusIdempotencyConflictError):
        commands.change(replace(command, desired_status=PersonaStatus.EXPELLED))

    assert missing.changes == stale.changes == unchanged.changes == 0
    assert repository.changes == 1


def test_status_retry_fails_closed_on_malformed_audit_receipt() -> None:
    state = _state()
    repository = RecordingRepository(state)
    command = _command(state, PersonaStatus.WARNING)
    repository.operations[command.idempotency_key] = StoredStaffPersonaStatusOperation(
        request_fingerprint=command.request_fingerprint,
        type=StaffPersonaStatusAuditType.PERSONA_STATUS_CHANGED.value,
        persona_id=state.persona_id,
        game_account_id=None,
        after_data={"schema_version": 999},
    )

    with pytest.raises(StaffPersonaStatusAuditError):
        _commands(repository).change(command)

    assert repository.changes == 0


def test_status_input_requires_reason_and_valid_actor() -> None:
    state = _state()

    with pytest.raises(ValueError):
        _command(state, PersonaStatus.WARNING, reason=" ")
    with pytest.raises(ValueError):
        _command(state, PersonaStatus.WARNING, updated_by_discord_user_id="not-a-snowflake")
