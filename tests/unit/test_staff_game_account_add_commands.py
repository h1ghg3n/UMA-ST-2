"""Application contracts for staff peer GameAccount addition."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.identity import (
    AddedGameAccount,
    AddGameAccountToPersona,
    StaffGameAccountAddAuditType,
    StaffGameAccountAddCommands,
    StaffGameAccountAddIdempotencyConflictError,
    StaffGameAccountAddPersonaNotFoundError,
    StaffGameAccountAddPersonaRestrictedError,
    StaffGameAccountAddPidUnavailableError,
    StaffGameAccountAddQueries,
    StaffGameAccountAddStaleError,
    StaffGameAccountAddState,
    StoredStaffGameAccountAddOperation,
)
from uma_st2.domain.identity import GameRegion, PersonaStatus

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
PERSONA_ID = "11111111-2222-4333-8444-555555555555"


def _state(**changes: object) -> StaffGameAccountAddState:
    values: dict[str, object] = {
        "guild_id": "987",
        "persona_id": PERSONA_ID,
        "persona_display_name": "기존 Persona",
        "persona_status": PersonaStatus.NORMAL,
        "has_wallet": True,
        "game_account_count": 1,
        "qualifying_game_account_count": 1,
        "game_region": GameRegion.KR,
        "uma_pid": "123456789",
        "registered_game_account_id": None,
        "registered_persona_id": None,
    }
    values.update(changes)
    return StaffGameAccountAddState(**values)  # type: ignore[arg-type]


def _command(state: StaffGameAccountAddState, **changes: object) -> AddGameAccountToPersona:
    values: dict[str, object] = {
        "guild_id": state.guild_id,
        "target_persona_id": state.persona_id,
        "game_region": state.game_region,
        "uma_pid": state.uma_pid,
        "nickname": "새 계정",
        "affiliation": "소속",
        "reason": "복수 계정 등록 확인",
        "added_by_discord_user_id": "900",
        "expected_target_fingerprint": state.state_fingerprint,
        "idempotency_key": "game-account-add-1",
        "correlation_id": "game-account-add-1",
    }
    values.update(changes)
    return AddGameAccountToPersona(**values)  # type: ignore[arg-type]


class RecordingRepository:
    def __init__(self, state: StaffGameAccountAddState | None = None) -> None:
        self.state = state
        self.operations: dict[str, StoredStaffGameAccountAddOperation] = {}
        self.calls: list[str] = []
        self.add_count = 0

    def lock_target_state(self, **_kwargs: object) -> StaffGameAccountAddState | None:
        self.calls.append("target")
        return self.state

    def find_operation(self, *, idempotency_key: str) -> StoredStaffGameAccountAddOperation | None:
        self.calls.append("operation")
        return self.operations.get(idempotency_key)

    def add_game_account(
        self,
        *,
        command: AddGameAccountToPersona,
        state: StaffGameAccountAddState,
        added_at: datetime,
    ) -> AddedGameAccount:
        self.calls.append("add")
        self.add_count += 1
        result = AddedGameAccount(
            persona_id=state.persona_id,
            persona_display_name=state.persona_display_name,
            persona_status=state.persona_status,
            game_account_id=81,
            game_region=command.game_region,
            uma_pid=command.uma_pid,
            nickname=command.nickname,
            affiliation=command.affiliation,
            has_wallet=state.has_wallet,
            game_account_count=state.game_account_count + 1,
            qualifying_game_account_count=state.qualifying_game_account_count + 1,
            reason=command.reason,
            added_at=added_at,
        )
        self.operations[command.idempotency_key] = StoredStaffGameAccountAddOperation(
            request_fingerprint=command.request_fingerprint,
            type=StaffGameAccountAddAuditType.ADDED.value,
            persona_id=result.persona_id,
            game_account_id=result.game_account_id,
            after_data=result.to_audit_payload(),
        )
        self.state = replace(
            state,
            game_account_count=result.game_account_count,
            qualifying_game_account_count=result.qualifying_game_account_count,
            registered_game_account_id=result.game_account_id,
            registered_persona_id=result.persona_id,
        )
        return result


class AddUnitOfWork:
    def __init__(self, repository: RecordingRepository) -> None:
        self.staff_game_account_add = repository

    def __enter__(self) -> AddUnitOfWork:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _commands(repository: RecordingRepository) -> StaffGameAccountAddCommands:
    return StaffGameAccountAddCommands(
        CommandRunner(lambda: AddUnitOfWork(repository)),
        clock=lambda: NOW,
    )


def test_peer_add_commits_once_and_exact_retry_returns_stored_receipt() -> None:
    repository = RecordingRepository(_state())
    commands = _commands(repository)
    command = _command(repository.state)  # type: ignore[arg-type]

    created = commands.add(command)
    retried = commands.add(command)

    assert created.game_account_id == 81
    assert created.game_account_count == created.qualifying_game_account_count == 2
    assert created.member_mutation_eligible is True
    assert created.exact_retry is False
    assert retried == replace(created, exact_retry=True)
    assert repository.add_count == 1
    assert repository.calls == ["target", "operation", "add", "target", "operation"]


@pytest.mark.parametrize("status", [PersonaStatus.NORMAL, PersonaStatus.WARNING, PersonaStatus.PENDING_APPROVAL])
def test_peer_add_allows_each_nonterminal_persona_status(status: PersonaStatus) -> None:
    state = _state(persona_status=status, has_wallet=False)
    result = _commands(RecordingRepository(state)).add(_command(state))

    assert result.persona_status is status
    assert result.member_mutation_eligible is False


@pytest.mark.parametrize("status", [PersonaStatus.EXPELLED, PersonaStatus.WITHDRAWN])
def test_peer_add_rejects_terminal_persona_without_write(status: PersonaStatus) -> None:
    state = _state(persona_status=status)
    repository = RecordingRepository(state)

    with pytest.raises(StaffGameAccountAddPersonaRestrictedError):
        _commands(repository).add(_command(state))

    assert repository.add_count == 0


def test_peer_add_rejects_missing_persona_existing_pid_and_stale_preview() -> None:
    missing = RecordingRepository(None)
    with pytest.raises(StaffGameAccountAddPersonaNotFoundError):
        _commands(missing).add(_command(_state()))

    existing_state = _state(registered_game_account_id=71, registered_persona_id="other-persona")
    existing = RecordingRepository(existing_state)
    with pytest.raises(StaffGameAccountAddPidUnavailableError):
        _commands(existing).add(_command(_state()))

    current = _state(game_account_count=2, qualifying_game_account_count=2)
    stale = RecordingRepository(current)
    with pytest.raises(StaffGameAccountAddStaleError):
        _commands(stale).add(_command(_state()))

    assert missing.add_count == existing.add_count == stale.add_count == 0


def test_peer_add_changed_payload_reusing_key_conflicts() -> None:
    state = _state()
    repository = RecordingRepository(state)
    commands = _commands(repository)
    command = _command(state)

    commands.add(command)
    with pytest.raises(StaffGameAccountAddIdempotencyConflictError):
        commands.add(replace(command, reason="다른 사유"))


class QueryRepository:
    def __init__(self, state: StaffGameAccountAddState | None) -> None:
        self.state = state

    def get_target_state(self, **_kwargs: object) -> StaffGameAccountAddState | None:
        return self.state


class QueryUnitOfWork:
    def __init__(self, repository: QueryRepository) -> None:
        self.staff_game_account_add = repository

    def __enter__(self) -> QueryUnitOfWork:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def test_peer_add_preview_normalizes_input_and_requires_reason_without_write() -> None:
    queries = StaffGameAccountAddQueries(QueryRunner(lambda: QueryUnitOfWork(QueryRepository(_state()))))

    preview = queries.get_preview(
        guild_id=" 987 ",
        persona_id=f" {PERSONA_ID} ",
        game_region=GameRegion.KR,
        uma_pid=" 123456789 ",
        nickname=" 새 계정 ",
        affiliation="   ",
        reason=" 복수 계정 확인 ",
    )

    assert preview.nickname == "새 계정"
    assert preview.affiliation is None
    assert preview.reason == "복수 계정 확인"
    with pytest.raises(ValueError, match="reason"):
        queries.get_preview(
            guild_id="987",
            persona_id=PERSONA_ID,
            game_region=GameRegion.KR,
            uma_pid="123456789",
            nickname="새 계정",
            affiliation=None,
            reason="   ",
        )
