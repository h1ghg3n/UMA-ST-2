"""Request-only Account registration application boundary tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.identity import (
    AccountRegistrationAlreadyLinkedError,
    AccountRegistrationAlreadyPendingError,
    AccountRegistrationAuditType,
    AccountRegistrationCommands,
    AccountRegistrationIdempotencyConflictError,
    AccountRegistrationPidUnavailableError,
    AccountRegistrationRequester,
    AccountRegistrationRequestSnapshot,
    ActiveAccountRegistrationRequest,
    StoredAccountRegistrationOperation,
    SubmitAccountRegistrationRequest,
)
from uma_st2.domain.identity import GameRegion, RegistrationRequestInvariantError, RegistrationRequestStatus

NOW = datetime(2026, 9, 2, 4, 30, tzinfo=UTC)


def _command(**changes: object) -> SubmitAccountRegistrationRequest:
    values: dict[str, object] = {
        "guild_id": "987",
        "actor_discord_user_id": "123",
        "discord_display_name_snapshot": "Discord 표시명",
        "game_region": GameRegion.KR,
        "uma_pid": " 123456789 ",
        "nickname": " 게임 닉네임 ",
        "affiliation": " 소속 ",
        "idempotency_key": "555",
        "correlation_id": "555",
    }
    values.update(changes)
    return SubmitAccountRegistrationRequest(**values)  # type: ignore[arg-type]


class RecordingRegistrationRepository:
    def __init__(self) -> None:
        self.requester = AccountRegistrationRequester(
            discord_account_id=7,
            discord_user_id="123",
            persona_id=None,
        )
        self.active: ActiveAccountRegistrationRequest | None = None
        self.pid_exists = False
        self.operation: StoredAccountRegistrationOperation | None = None
        self.created = 0
        self.audited = 0

    def find_operation(self, *, idempotency_key: str) -> StoredAccountRegistrationOperation | None:
        return self.operation if idempotency_key == "555" else None

    def lock_or_create_requester(self, **_kwargs: object) -> AccountRegistrationRequester:
        return self.requester

    def find_active_request(self, **_kwargs: object) -> ActiveAccountRegistrationRequest | None:
        return self.active

    def registered_game_account_exists(self, **_kwargs: object) -> bool:
        return self.pid_exists

    def create_request(
        self,
        *,
        command: SubmitAccountRegistrationRequest,
        requester: AccountRegistrationRequester,
        created_at: datetime,
    ) -> AccountRegistrationRequestSnapshot:
        self.created += 1
        return AccountRegistrationRequestSnapshot(
            request_id=51,
            discord_account_id=requester.discord_account_id,
            guild_id=command.guild_id,
            requester_discord_user_id=command.actor_discord_user_id,
            discord_display_name_snapshot=command.discord_display_name_snapshot,
            game_region=command.game_region,
            uma_pid=command.uma_pid,
            nickname=command.nickname,
            affiliation=command.affiliation,
            status=RegistrationRequestStatus.PENDING,
            created_at=created_at,
        )

    def add_request_audit(
        self,
        *,
        command: SubmitAccountRegistrationRequest,
        snapshot: AccountRegistrationRequestSnapshot,
        created_at: datetime,
    ) -> None:
        assert created_at == snapshot.created_at
        self.audited += 1
        self.operation = StoredAccountRegistrationOperation(
            request_fingerprint=command.request_fingerprint,
            type=AccountRegistrationAuditType.REQUESTED.value,
            persona_id=None,
            game_account_id=None,
            discord_user_id=command.actor_discord_user_id,
            after_data=snapshot.to_audit_payload(),
        )


class RecordingRegistrationUnitOfWork:
    def __init__(self, repository: RecordingRegistrationRepository) -> None:
        self.account_registration = repository

    def __enter__(self) -> RecordingRegistrationUnitOfWork:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _commands(repository: RecordingRegistrationRepository) -> AccountRegistrationCommands:
    return AccountRegistrationCommands(
        CommandRunner(lambda: RecordingRegistrationUnitOfWork(repository)),
        clock=lambda: NOW,
    )


def test_request_normalizes_input_creates_pending_fact_and_exact_retry_uses_audit() -> None:
    repository = RecordingRegistrationRepository()
    commands = _commands(repository)
    command = _command()

    created = commands.submit_registration_request(command)
    retried = commands.submit_registration_request(command)

    assert command.uma_pid == "123456789"
    assert command.nickname == "게임 닉네임"
    assert command.affiliation == "소속"
    assert created.snapshot.status is RegistrationRequestStatus.PENDING
    assert created.exact_retry is False
    assert retried == type(created)(snapshot=created.snapshot, exact_retry=True)
    assert repository.created == 1
    assert repository.audited == 1


def test_request_rejects_linked_actor_active_request_and_registered_pid_without_write() -> None:
    repository = RecordingRegistrationRepository()
    commands = _commands(repository)

    repository.requester = AccountRegistrationRequester(
        discord_account_id=7,
        discord_user_id="123",
        persona_id="persona-1",
    )
    with pytest.raises(AccountRegistrationAlreadyLinkedError):
        commands.submit_registration_request(_command())

    repository.requester = AccountRegistrationRequester(
        discord_account_id=7,
        discord_user_id="123",
        persona_id=None,
    )
    repository.active = ActiveAccountRegistrationRequest(request_id=88)
    with pytest.raises(AccountRegistrationAlreadyPendingError) as pending:
        commands.submit_registration_request(_command())
    assert pending.value.request_id == 88

    repository.active = None
    repository.pid_exists = True
    with pytest.raises(AccountRegistrationPidUnavailableError):
        commands.submit_registration_request(_command())

    assert repository.created == 0
    assert repository.audited == 0


def test_changed_payload_with_same_key_conflicts_without_second_write() -> None:
    repository = RecordingRegistrationRepository()
    commands = _commands(repository)
    commands.submit_registration_request(_command())

    with pytest.raises(AccountRegistrationIdempotencyConflictError):
        commands.submit_registration_request(_command(nickname="다른 닉네임"))

    assert repository.created == 1
    assert repository.audited == 1


@pytest.mark.parametrize("uma_pid", ["", "0", "0123", "１２３", "12 3", "abc", "1" * 33])
def test_native_registration_pid_contract_fails_closed(uma_pid: str) -> None:
    with pytest.raises(RegistrationRequestInvariantError):
        _command(uma_pid=uma_pid)
