"""Application contracts for staff display-information corrections."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.identity import (
    StaffDisplayEditAuditType,
    StaffDisplayEditCommands,
    StaffDisplayEditGameAccountNotFoundError,
    StaffDisplayEditIdempotencyConflictError,
    StaffDisplayEditNoChangeError,
    StaffDisplayEditPersonaNotFoundError,
    StaffDisplayEditQueries,
    StaffDisplayEditStaleError,
    StaffDisplayGameAccountPage,
    StaffGameAccountDisplayState,
    StaffPersonaDisplayState,
    StoredStaffDisplayEditOperation,
    UpdatedGameAccountDisplayInfo,
    UpdatedPersonaDisplayName,
    UpdateGameAccountDisplayInfo,
    UpdatePersonaDisplayName,
)
from uma_st2.domain.identity import GameRegion, PersonaStatus

NOW = datetime(2026, 9, 3, 11, 0, tzinfo=UTC)
PERSONA_ID = "11111111-2222-4333-8444-555555555555"


def _persona_state(**changes: object) -> StaffPersonaDisplayState:
    values: dict[str, object] = {
        "guild_id": "987",
        "persona_id": PERSONA_ID,
        "display_name": "기존 Persona",
        "status": PersonaStatus.EXPELLED,
    }
    values.update(changes)
    return StaffPersonaDisplayState(**values)  # type: ignore[arg-type]


def _account_state(**changes: object) -> StaffGameAccountDisplayState:
    values: dict[str, object] = {
        "guild_id": "987",
        "persona_id": PERSONA_ID,
        "persona_display_name": "기존 Persona",
        "persona_status": PersonaStatus.WITHDRAWN,
        "game_account_id": 71,
        "game_region": GameRegion.KR,
        "uma_pid": None,
        "nickname": "기존 계정",
        "affiliation": None,
    }
    values.update(changes)
    return StaffGameAccountDisplayState(**values)  # type: ignore[arg-type]


def _persona_command(state: StaffPersonaDisplayState, **changes: object) -> UpdatePersonaDisplayName:
    values: dict[str, object] = {
        "guild_id": state.guild_id,
        "target_persona_id": state.persona_id,
        "display_name": "수정 Persona",
        "reason": "표시명 정정",
        "updated_by_discord_user_id": "900",
        "expected_target_fingerprint": state.state_fingerprint,
        "idempotency_key": "persona-display-1",
        "correlation_id": "persona-display-1",
    }
    values.update(changes)
    return UpdatePersonaDisplayName(**values)  # type: ignore[arg-type]


def _account_command(state: StaffGameAccountDisplayState, **changes: object) -> UpdateGameAccountDisplayInfo:
    values: dict[str, object] = {
        "guild_id": state.guild_id,
        "target_persona_id": state.persona_id,
        "game_account_id": state.game_account_id,
        "nickname": "수정 계정",
        "affiliation": "새 소속",
        "reason": "계정 표시 정정",
        "updated_by_discord_user_id": "900",
        "expected_target_fingerprint": state.state_fingerprint,
        "idempotency_key": "account-display-1",
        "correlation_id": "account-display-1",
    }
    values.update(changes)
    return UpdateGameAccountDisplayInfo(**values)  # type: ignore[arg-type]


class RecordingRepository:
    def __init__(
        self,
        *,
        persona_state: StaffPersonaDisplayState | None = None,
        account_state: StaffGameAccountDisplayState | None = None,
    ) -> None:
        self.persona_state = persona_state
        self.account_state = account_state
        self.operations: dict[str, StoredStaffDisplayEditOperation] = {}
        self.persona_updates = 0
        self.account_updates = 0

    def lock_persona_state(self, **_kwargs: object) -> StaffPersonaDisplayState | None:
        return self.persona_state

    def lock_game_account_state(self, **_kwargs: object) -> StaffGameAccountDisplayState | None:
        return self.account_state

    def find_operation(self, *, idempotency_key: str) -> StoredStaffDisplayEditOperation | None:
        return self.operations.get(idempotency_key)

    def update_persona(
        self,
        *,
        command: UpdatePersonaDisplayName,
        state: StaffPersonaDisplayState,
        updated_at: datetime,
    ) -> UpdatedPersonaDisplayName:
        self.persona_updates += 1
        result = UpdatedPersonaDisplayName(
            persona_id=state.persona_id,
            previous_display_name=state.display_name,
            display_name=command.display_name,
            status=state.status,
            reason=command.reason,
            updated_at=updated_at,
        )
        self.operations[command.idempotency_key] = StoredStaffDisplayEditOperation(
            request_fingerprint=command.request_fingerprint,
            type=StaffDisplayEditAuditType.PERSONA_UPDATED.value,
            persona_id=state.persona_id,
            game_account_id=None,
            after_data=result.to_audit_payload(),
        )
        self.persona_state = replace(state, display_name=command.display_name)
        return result

    def update_game_account(
        self,
        *,
        command: UpdateGameAccountDisplayInfo,
        state: StaffGameAccountDisplayState,
        updated_at: datetime,
    ) -> UpdatedGameAccountDisplayInfo:
        self.account_updates += 1
        result = UpdatedGameAccountDisplayInfo(
            persona_id=state.persona_id,
            persona_display_name=state.persona_display_name,
            persona_status=state.persona_status,
            game_account_id=state.game_account_id,
            game_region=state.game_region,
            uma_pid=state.uma_pid,
            previous_nickname=state.nickname,
            nickname=command.nickname,
            previous_affiliation=state.affiliation,
            affiliation=command.affiliation,
            reason=command.reason,
            updated_at=updated_at,
        )
        self.operations[command.idempotency_key] = StoredStaffDisplayEditOperation(
            request_fingerprint=command.request_fingerprint,
            type=StaffDisplayEditAuditType.GAME_ACCOUNT_UPDATED.value,
            persona_id=state.persona_id,
            game_account_id=state.game_account_id,
            after_data=result.to_audit_payload(),
        )
        self.account_state = replace(
            state,
            nickname=command.nickname,
            affiliation=command.affiliation,
        )
        return result


class UnitOfWork:
    def __init__(self, repository: object) -> None:
        self.staff_display_edit = repository

    def __enter__(self) -> UnitOfWork:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _commands(repository: RecordingRepository) -> StaffDisplayEditCommands:
    return StaffDisplayEditCommands(CommandRunner(lambda: UnitOfWork(repository)), clock=lambda: NOW)


def test_persona_display_edit_allows_terminal_status_and_exact_retry() -> None:
    state = _persona_state()
    repository = RecordingRepository(persona_state=state)
    command = _persona_command(state)

    created = _commands(repository).update_persona(command)
    retried = _commands(repository).update_persona(command)

    assert created.previous_display_name == "기존 Persona"
    assert created.display_name == "수정 Persona"
    assert created.status is PersonaStatus.EXPELLED
    assert retried == replace(created, exact_retry=True)
    assert repository.persona_updates == 1


def test_game_account_display_edit_allows_nullable_pid_terminal_owner_and_exact_retry() -> None:
    state = _account_state()
    repository = RecordingRepository(account_state=state)
    command = _account_command(state)

    created = _commands(repository).update_game_account(command)
    retried = _commands(repository).update_game_account(command)

    assert created.uma_pid is None
    assert created.persona_status is PersonaStatus.WITHDRAWN
    assert created.previous_affiliation is None
    assert created.affiliation == "새 소속"
    assert retried == replace(created, exact_retry=True)
    assert repository.account_updates == 1


def test_display_edit_rejects_missing_stale_no_change_and_changed_key_without_write() -> None:
    persona = _persona_state()
    missing = RecordingRepository()
    with pytest.raises(StaffDisplayEditPersonaNotFoundError):
        _commands(missing).update_persona(_persona_command(persona))
    with pytest.raises(StaffDisplayEditGameAccountNotFoundError):
        _commands(missing).update_game_account(_account_command(_account_state()))

    stale = RecordingRepository(persona_state=replace(persona, display_name="다른 현재값"))
    with pytest.raises(StaffDisplayEditStaleError):
        _commands(stale).update_persona(_persona_command(persona))

    unchanged = RecordingRepository(persona_state=persona)
    with pytest.raises(StaffDisplayEditNoChangeError):
        _commands(unchanged).update_persona(_persona_command(persona, display_name=persona.display_name))

    repository = RecordingRepository(persona_state=persona)
    commands = _commands(repository)
    command = _persona_command(persona)
    commands.update_persona(command)
    with pytest.raises(StaffDisplayEditIdempotencyConflictError):
        commands.update_persona(replace(command, reason="다른 사유"))

    assert missing.persona_updates == missing.account_updates == 0
    assert stale.persona_updates == unchanged.persona_updates == 0


class QueryRepository:
    def __init__(
        self,
        *,
        persona_state: StaffPersonaDisplayState | None,
        account_state: StaffGameAccountDisplayState | None,
    ) -> None:
        self.persona_state = persona_state
        self.account_state = account_state

    def get_persona_state(self, **_kwargs: object) -> StaffPersonaDisplayState | None:
        return self.persona_state

    def list_game_accounts(self, **kwargs: object) -> StaffDisplayGameAccountPage | None:
        if self.persona_state is None:
            return None
        page = int(kwargs["offset"]) // int(kwargs["limit"])
        return StaffDisplayGameAccountPage(
            persona=self.persona_state,
            items=() if self.account_state is None else (self.account_state,),
            page=page,
            total_count=0 if self.account_state is None else 1,
        )

    def get_game_account_state(self, **_kwargs: object) -> StaffGameAccountDisplayState | None:
        return self.account_state


def _queries(repository: QueryRepository) -> StaffDisplayEditQueries:
    return StaffDisplayEditQueries(QueryRunner(lambda: UnitOfWork(repository)))


def test_display_edit_queries_normalize_preview_and_page_without_write() -> None:
    persona = _persona_state(status=PersonaStatus.NORMAL)
    account = _account_state(persona_status=PersonaStatus.NORMAL)
    queries = _queries(QueryRepository(persona_state=persona, account_state=account))

    persona_preview = queries.get_persona_preview(
        guild_id=" 987 ",
        persona_id=f" {PERSONA_ID} ",
        display_name=" 수정 Persona ",
        reason=" 이름 정정 ",
    )
    account_preview = queries.get_game_account_preview(
        guild_id="987",
        persona_id=PERSONA_ID,
        game_account_id=71,
        nickname=" 수정 계정 ",
        affiliation=" ",
        reason=" 계정 정정 ",
    )
    page = queries.list_game_accounts(guild_id="987", persona_id=PERSONA_ID)

    assert persona_preview.display_name == "수정 Persona"
    assert persona_preview.reason == "이름 정정"
    assert account_preview.nickname == "수정 계정"
    assert account_preview.affiliation is None
    assert account_preview.reason == "계정 정정"
    assert page.items == (account,)


def test_display_edit_queries_reject_no_change_and_missing_targets() -> None:
    persona = _persona_state()
    account = _account_state()
    queries = _queries(QueryRepository(persona_state=persona, account_state=account))

    with pytest.raises(StaffDisplayEditNoChangeError):
        queries.get_persona_preview(
            guild_id="987",
            persona_id=PERSONA_ID,
            display_name=persona.display_name,
            reason="사유",
        )
    with pytest.raises(StaffDisplayEditNoChangeError):
        queries.get_game_account_preview(
            guild_id="987",
            persona_id=PERSONA_ID,
            game_account_id=account.game_account_id,
            nickname=account.nickname,
            affiliation=account.affiliation,
            reason="사유",
        )

    missing = _queries(QueryRepository(persona_state=None, account_state=None))
    with pytest.raises(StaffDisplayEditPersonaNotFoundError):
        missing.list_game_accounts(guild_id="987", persona_id=PERSONA_ID)
    with pytest.raises(StaffDisplayEditGameAccountNotFoundError):
        missing.get_game_account_preview(
            guild_id="987",
            persona_id=PERSONA_ID,
            game_account_id=71,
            nickname="수정",
            affiliation=None,
            reason="사유",
        )
