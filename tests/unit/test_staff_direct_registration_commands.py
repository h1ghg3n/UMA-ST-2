"""Application contracts for staff direct account registration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.identity import (
    ACCOUNT_REGISTRATION_INITIAL_GRANT,
    AccountRegistrationBootstrap,
    CreateAccountRegistrationBootstrap,
    DirectlyRegisterDiscordAccount,
    StaffDirectRegistrationCommands,
    StaffDirectRegistrationIdempotencyConflictError,
    StaffDirectRegistrationPendingRequestError,
    StaffDirectRegistrationPidUnavailableError,
    StaffDirectRegistrationQueries,
    StaffDirectRegistrationStaleError,
    StaffDirectRegistrationState,
    StaffDirectRegistrationTargetLinkedError,
    StoredStaffDirectRegistrationOperation,
)
from uma_st2.domain.identity import GameRegion, PersonaStatus

NOW = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)
PERSONA_ID = "11111111-2222-4333-8444-555555555555"


def _state(**changes: object) -> StaffDirectRegistrationState:
    values: dict[str, object] = {
        "guild_id": "987",
        "target_discord_user_id": "123456",
        "game_region": GameRegion.KR,
        "uma_pid": "123456789",
        "current_persona_id": None,
        "active_registration_request_id": None,
        "registered_game_account_id": None,
    }
    values.update(changes)
    return StaffDirectRegistrationState(**values)  # type: ignore[arg-type]


def _command(state: StaffDirectRegistrationState, **changes: object) -> DirectlyRegisterDiscordAccount:
    values: dict[str, object] = {
        "guild_id": state.guild_id,
        "target_discord_user_id": state.target_discord_user_id,
        "target_display_name_snapshot": "Discord 표시명",
        "game_region": state.game_region,
        "uma_pid": state.uma_pid,
        "nickname": "게임 닉네임",
        "affiliation": "소속",
        "registered_by_discord_user_id": "900",
        "expected_target_fingerprint": state.state_fingerprint,
        "idempotency_key": "direct-register-1",
        "correlation_id": "direct-register-1",
        "operational_note": "운영 확인 완료",
    }
    values.update(changes)
    return DirectlyRegisterDiscordAccount(**values)  # type: ignore[arg-type]


class RecordingRepository:
    def __init__(self, state: StaffDirectRegistrationState | None = None) -> None:
        self.state = state or _state()
        self.operations: dict[str, StoredStaffDirectRegistrationOperation] = {}
        self.calls: list[str] = []
        self.bootstrap_count = 0

    def lock_target_state(self, **_kwargs: object) -> StaffDirectRegistrationState:
        self.calls.append("target")
        return self.state

    def find_operation(self, *, idempotency_key: str) -> StoredStaffDirectRegistrationOperation | None:
        self.calls.append("operation")
        return self.operations.get(idempotency_key)

    def create_registration_bootstrap(
        self,
        *,
        command: CreateAccountRegistrationBootstrap,
    ) -> AccountRegistrationBootstrap:
        self.calls.append("bootstrap")
        self.bootstrap_count += 1
        self.state = replace(self.state, current_persona_id=command.persona_id)
        return AccountRegistrationBootstrap(
            operation_id=71,
            persona_id=command.persona_id,
            persona_display_name=command.persona_display_name,
            persona_status=PersonaStatus.NORMAL,
            target_discord_user_id=command.target_discord_user_id,
            game_account_id=81,
            game_region=command.game_region,
            uma_pid=command.uma_pid,
            nickname=command.nickname,
            affiliation=command.affiliation,
            wallet_balance=ACCOUNT_REGISTRATION_INITIAL_GRANT,
            initial_grant_amount=ACCOUNT_REGISTRATION_INITIAL_GRANT,
            registered_at=command.registered_at,
        )

    def complete_registration_bootstrap_audit(
        self,
        *,
        command: CreateAccountRegistrationBootstrap,
        bootstrap: AccountRegistrationBootstrap,
        before_data: Mapping[str, object] | None,
        after_data: Mapping[str, object],
    ) -> None:
        self.calls.append("audit")
        assert before_data is not None
        self.operations[command.idempotency_key] = StoredStaffDirectRegistrationOperation(
            request_fingerprint=command.request_fingerprint,
            type=command.audit_type.value,
            persona_id=bootstrap.persona_id,
            game_account_id=bootstrap.game_account_id,
            discord_user_id=bootstrap.target_discord_user_id,
            after_data=after_data,
        )


class DirectRegistrationUnitOfWork:
    def __init__(self, repository: RecordingRepository) -> None:
        self.staff_direct_registration = repository

    def __enter__(self) -> DirectRegistrationUnitOfWork:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _commands(repository: RecordingRepository) -> StaffDirectRegistrationCommands:
    return StaffDirectRegistrationCommands(
        CommandRunner(lambda: DirectRegistrationUnitOfWork(repository)),
        clock=lambda: NOW,
        persona_id_factory=lambda: PERSONA_ID,
    )


def test_direct_registration_bootstraps_once_and_exact_retry_returns_stored_receipt() -> None:
    repository = RecordingRepository()
    commands = _commands(repository)
    command = _command(repository.state)

    created = commands.register(command)
    retried = commands.register(command)

    assert created.persona_id == PERSONA_ID
    assert created.wallet_balance == created.initial_grant_amount == 500
    assert created.operational_note == "운영 확인 완료"
    assert created.exact_retry is False
    assert retried == replace(created, exact_retry=True)
    assert repository.bootstrap_count == 1
    assert repository.calls == [
        "target",
        "operation",
        "bootstrap",
        "audit",
        "target",
        "operation",
    ]


@pytest.mark.parametrize(
    ("state", "error_type"),
    [
        (_state(current_persona_id="existing-persona"), StaffDirectRegistrationTargetLinkedError),
        (_state(active_registration_request_id=51), StaffDirectRegistrationPendingRequestError),
        (_state(registered_game_account_id=81), StaffDirectRegistrationPidUnavailableError),
    ],
)
def test_direct_registration_rejects_unavailable_current_authority_without_bootstrap(
    state: StaffDirectRegistrationState,
    error_type: type[Exception],
) -> None:
    repository = RecordingRepository(state)

    with pytest.raises(error_type):
        _commands(repository).register(_command(_state()))

    assert repository.bootstrap_count == 0


def test_direct_registration_rejects_stale_preview_and_changed_payload_retry() -> None:
    repository = RecordingRepository()
    commands = _commands(repository)
    command = _command(repository.state)

    with pytest.raises(StaffDirectRegistrationStaleError):
        commands.register(replace(command, expected_target_fingerprint="0" * 64))

    created = commands.register(command)
    with pytest.raises(StaffDirectRegistrationIdempotencyConflictError):
        commands.register(replace(command, operational_note="다른 메모"))

    assert created.exact_retry is False
    assert repository.bootstrap_count == 1


class QueryRepository:
    def __init__(self, state: StaffDirectRegistrationState) -> None:
        self.state = state

    def get_target_state(self, **_kwargs: object) -> StaffDirectRegistrationState:
        return self.state


class QueryUnitOfWork:
    def __init__(self, repository: QueryRepository) -> None:
        self.staff_direct_registration = repository

    def __enter__(self) -> QueryUnitOfWork:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def test_preview_normalizes_adapter_input_without_writing() -> None:
    repository = QueryRepository(_state())
    queries = StaffDirectRegistrationQueries(QueryRunner(lambda: QueryUnitOfWork(repository)))

    preview = queries.get_preview(
        guild_id=" 987 ",
        target_discord_user_id="123456",
        target_display_name_snapshot=" Discord 표시명 ",
        game_region=GameRegion.KR,
        uma_pid=" 123456789 ",
        nickname=" 게임 닉네임 ",
        affiliation=" 소속 ",
        operational_note="   ",
    )

    assert preview.target_display_name_snapshot == "Discord 표시명"
    assert preview.nickname == "게임 닉네임"
    assert preview.affiliation == "소속"
    assert preview.operational_note is None
