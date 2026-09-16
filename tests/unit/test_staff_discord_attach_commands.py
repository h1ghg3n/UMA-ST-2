"""Application contracts for staff direct Discord access attachment."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.identity import (
    AttachDiscordAccountToPersona,
    AttachedDiscordAccount,
    StaffDiscordAttachCommands,
    StaffDiscordAttachIdempotencyConflictError,
    StaffDiscordAttachPendingRequestError,
    StaffDiscordAttachQueries,
    StaffDiscordAttachStaleError,
    StaffDiscordAttachState,
    StaffDiscordAttachTargetLinkedError,
    StoredStaffDiscordAttachOperation,
)
from uma_st2.domain.identity import PersonaStatus

NOW = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
PERSONA_ID = "11111111-2222-4333-8444-555555555555"


def _state(**changes: object) -> StaffDiscordAttachState:
    values: dict[str, object] = {
        "guild_id": "987",
        "persona_id": PERSONA_ID,
        "persona_display_name": "기존 Persona",
        "persona_status": PersonaStatus.NORMAL,
        "target_discord_user_id": "123456",
        "current_persona_id": None,
        "active_registration_request_id": None,
        "has_wallet": True,
        "qualifying_game_account_count": 1,
    }
    values.update(changes)
    return StaffDiscordAttachState(**values)  # type: ignore[arg-type]


def _command(state: StaffDiscordAttachState, **changes: object) -> AttachDiscordAccountToPersona:
    values: dict[str, object] = {
        "guild_id": state.guild_id,
        "target_persona_id": state.persona_id,
        "target_discord_user_id": state.target_discord_user_id,
        "attached_by_discord_user_id": "900",
        "expected_target_fingerprint": state.state_fingerprint,
        "idempotency_key": "attach-1",
        "correlation_id": "attach-1",
        "operational_note": "기존 Persona access 연결",
    }
    values.update(changes)
    return AttachDiscordAccountToPersona(**values)  # type: ignore[arg-type]


class RecordingAttachRepository:
    def __init__(self, state: StaffDiscordAttachState | None = None) -> None:
        self.state = state or _state()
        self.operations: dict[str, StoredStaffDiscordAttachOperation] = {}
        self.calls: list[str] = []
        self.attach_count = 0

    def lock_target_state(self, **_kwargs: object) -> StaffDiscordAttachState | None:
        self.calls.append("target")
        return self.state

    def find_operation(self, *, idempotency_key: str) -> StoredStaffDiscordAttachOperation | None:
        self.calls.append("operation")
        return self.operations.get(idempotency_key)

    def attach(
        self,
        *,
        command: AttachDiscordAccountToPersona,
        state: StaffDiscordAttachState,
        attached_at: datetime,
    ) -> AttachedDiscordAccount:
        self.calls.append("attach")
        self.attach_count += 1
        result = AttachedDiscordAccount(
            persona_id=state.persona_id,
            persona_display_name=state.persona_display_name,
            persona_status=state.persona_status,
            target_discord_user_id=state.target_discord_user_id,
            has_wallet=state.has_wallet,
            qualifying_game_account_count=state.qualifying_game_account_count,
            operational_note=command.operational_note,
            attached_at=attached_at,
        )
        self.operations[command.idempotency_key] = StoredStaffDiscordAttachOperation(
            request_fingerprint=command.request_fingerprint,
            type="discord_account_attached",
            persona_id=state.persona_id,
            discord_user_id=state.target_discord_user_id,
            after_data=result.to_audit_payload(),
        )
        self.state = replace(state, current_persona_id=state.persona_id)
        return result


class AttachUnitOfWork:
    def __init__(self, repository: RecordingAttachRepository) -> None:
        self.staff_discord_attach = repository

    def __enter__(self) -> AttachUnitOfWork:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _commands(repository: RecordingAttachRepository) -> StaffDiscordAttachCommands:
    return StaffDiscordAttachCommands(
        CommandRunner(lambda: AttachUnitOfWork(repository)),
        clock=lambda: NOW,
    )


def test_attach_commits_once_and_same_key_returns_stored_receipt() -> None:
    repository = RecordingAttachRepository()
    commands = _commands(repository)
    command = _command(repository.state)

    created = commands.attach(command)
    retried = commands.attach(command)

    assert created.exact_retry is False
    assert created.member_mutation_eligible is True
    assert retried == replace(created, exact_retry=True)
    assert repository.attach_count == 1
    assert repository.calls == ["target", "operation", "attach", "target", "operation"]


def test_attach_rejects_stale_preview_existing_link_and_active_request() -> None:
    repository = RecordingAttachRepository()
    with pytest.raises(StaffDiscordAttachStaleError):
        _commands(repository).attach(_command(repository.state, expected_target_fingerprint="0" * 64))

    repository.state = replace(repository.state, current_persona_id=PERSONA_ID)
    with pytest.raises(StaffDiscordAttachTargetLinkedError):
        _commands(repository).attach(_command(_state(), idempotency_key="attach-linked"))

    repository.state = _state(active_registration_request_id=51)
    with pytest.raises(StaffDiscordAttachPendingRequestError):
        _commands(repository).attach(_command(_state(), idempotency_key="attach-pending"))

    assert repository.attach_count == 0


def test_changed_payload_same_key_conflicts_after_commit() -> None:
    repository = RecordingAttachRepository()
    commands = _commands(repository)
    command = _command(repository.state)
    commands.attach(command)

    with pytest.raises(StaffDiscordAttachIdempotencyConflictError):
        commands.attach(replace(command, operational_note="다른 메모"))

    assert repository.attach_count == 1


class RecordingQueryRepository:
    def __init__(self, state: StaffDiscordAttachState | None) -> None:
        self.state = state

    def get_target_state(self, **_kwargs: object) -> StaffDiscordAttachState | None:
        return self.state


class QueryUnitOfWork:
    def __init__(self, repository: RecordingQueryRepository) -> None:
        self.staff_discord_attach = repository

    def __enter__(self) -> QueryUnitOfWork:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def test_preview_is_read_only_and_exposes_current_eligibility_without_selecting_account() -> None:
    state = _state(
        persona_status=PersonaStatus.WARNING,
        qualifying_game_account_count=2,
    )
    queries = StaffDiscordAttachQueries(QueryRunner(lambda: QueryUnitOfWork(RecordingQueryRepository(state))))

    preview = queries.get_preview(
        guild_id=state.guild_id,
        persona_id=state.persona_id,
        discord_user_id=state.target_discord_user_id,
    )

    assert preview == state
    assert preview.member_mutation_eligible is True

    pending_queries = StaffDiscordAttachQueries(
        QueryRunner(lambda: QueryUnitOfWork(RecordingQueryRepository(_state(active_registration_request_id=51))))
    )
    with pytest.raises(StaffDiscordAttachPendingRequestError):
        pending_queries.get_preview(
            guild_id="987",
            persona_id=PERSONA_ID,
            discord_user_id="123456",
        )


@pytest.mark.parametrize("discord_user_id", ["", "0", "01", "１２３", "abc"])
def test_attach_command_rejects_noncanonical_discord_snowflake(discord_user_id: str) -> None:
    with pytest.raises(ValueError, match="discord_user_id"):
        _command(_state(), target_discord_user_id=discord_user_id)
