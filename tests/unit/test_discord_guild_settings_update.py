"""Runtime Discord guild settings update contract tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from uma_st2.application.discord import (
    DISCORD_GUILD_SETTINGS_UPDATE_OPERATION_TYPE,
    DiscordGuildEditableSettings,
    DiscordGuildSettingsUpdateCommands,
    DiscordGuildSettingsUpdateIdempotencyConflictError,
    DiscordGuildSettingsUpdateNoChangeError,
    DiscordGuildSettingsUpdateQueries,
    DiscordGuildSettingsUpdateStaleError,
    DiscordGuildSettingsUpdateState,
    StoredDiscordGuildSettingsUpdateOperation,
    UpdatedDiscordGuildSettings,
    UpdateDiscordGuildSettings,
)
from uma_st2.application.execution import CommandRunner, QueryRunner

NOW = datetime(2026, 9, 5, 2, 30, tzinfo=UTC)
GUILD_ID = "100000000000000001"
ACTOR_ID = "400000000000000001"


def _editable(**changes: object) -> DiscordGuildEditableSettings:
    values: dict[str, object] = {
        "win5_announcement_channel_id": "200000000000000001",
        "match_announcement_channel_id": "200000000000000002",
        "log_channel_id": "200000000000000003",
        "default_timezone": "Asia/Seoul",
        "win5_announcements_enabled": True,
        "match_announcements_enabled": True,
    }
    values.update(changes)
    return DiscordGuildEditableSettings(**values)  # type: ignore[arg-type]


def _state(
    *, editable: DiscordGuildEditableSettings | None = None, updated_at: datetime = NOW
) -> DiscordGuildSettingsUpdateState:
    return DiscordGuildSettingsUpdateState(
        guild_id=GUILD_ID,
        editable=editable or _editable(),
        operator_role_id="300000000000000001",
        bot_manager_role_id="300000000000000002",
        created_at=NOW - timedelta(days=10),
        updated_at=updated_at,
    )


def _command(
    state: DiscordGuildSettingsUpdateState,
    *,
    desired: DiscordGuildEditableSettings | None = None,
    key: str = "settings-final-1",
) -> UpdateDiscordGuildSettings:
    return UpdateDiscordGuildSettings(
        guild_id=GUILD_ID,
        desired=desired or _editable(default_timezone="UTC", match_announcements_enabled=False),
        reason="운영 공지 설정 정정",
        expected_state_fingerprint=state.state_fingerprint,
        actor_discord_user_id=ACTOR_ID,
        idempotency_key=key,
        correlation_id=key,
    )


class FakeSettingsRepository:
    def __init__(self, current: DiscordGuildSettingsUpdateState | None) -> None:
        self.current = current
        self.operations: dict[str, StoredDiscordGuildSettingsUpdateOperation] = {}
        self.update_calls = 0
        self.audit_calls = 0

    def get_state(self, *, guild_id: str) -> DiscordGuildSettingsUpdateState | None:
        return self.current if self.current is not None and self.current.guild_id == guild_id else None

    def lock_state(self, *, guild_id: str) -> DiscordGuildSettingsUpdateState | None:
        return self.get_state(guild_id=guild_id)

    def find_operation(self, *, idempotency_key: str) -> StoredDiscordGuildSettingsUpdateOperation | None:
        return self.operations.get(idempotency_key)

    def update_settings(
        self,
        *,
        before: DiscordGuildSettingsUpdateState,
        desired: DiscordGuildEditableSettings,
        updated_at: datetime,
    ) -> DiscordGuildSettingsUpdateState:
        self.update_calls += 1
        self.current = DiscordGuildSettingsUpdateState(
            guild_id=before.guild_id,
            editable=desired,
            operator_role_id=before.operator_role_id,
            bot_manager_role_id=before.bot_manager_role_id,
            created_at=before.created_at,
            updated_at=updated_at,
        )
        return self.current

    def add_audit(
        self,
        *,
        command: UpdateDiscordGuildSettings,
        before: DiscordGuildSettingsUpdateState,
        after: DiscordGuildSettingsUpdateState,
        changed_fields: tuple[str, ...],
        created_at: datetime,
    ) -> int:
        assert created_at == after.updated_at
        self.audit_calls += 1
        result = UpdatedDiscordGuildSettings(
            operation_id=71,
            before=before,
            after=after,
            changed_fields=changed_fields,
            reason=command.reason,
        )
        self.operations[command.idempotency_key] = StoredDiscordGuildSettingsUpdateOperation(
            operation_id=71,
            request_fingerprint=command.request_fingerprint,
            operation_type=DISCORD_GUILD_SETTINGS_UPDATE_OPERATION_TYPE,
            guild_id=command.guild_id,
            after_data=result.to_payload(),
        )
        return 71


class FakeSettingsUnitOfWork:
    def __init__(self, repository: FakeSettingsRepository) -> None:
        self.discord_guild_settings_update = repository
        self.commits = 0

    def __enter__(self) -> FakeSettingsUnitOfWork:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None


def _queries(repository: FakeSettingsRepository) -> DiscordGuildSettingsUpdateQueries:
    return DiscordGuildSettingsUpdateQueries(QueryRunner(lambda: FakeSettingsUnitOfWork(repository)))


def _commands(repository: FakeSettingsRepository) -> DiscordGuildSettingsUpdateCommands:
    return DiscordGuildSettingsUpdateCommands(
        CommandRunner(lambda: FakeSettingsUnitOfWork(repository)),
        clock=lambda: NOW + timedelta(minutes=1),
    )


def test_preview_is_complete_read_only_and_rejects_semantically_stale_state() -> None:
    state = _state()
    repository = FakeSettingsRepository(state)
    desired = _editable(default_timezone="UTC", log_channel_id=None)

    preview = _queries(repository).get_preview(
        guild_id=GUILD_ID,
        desired=desired,
        reason="운영 destination 변경",
        expected_state_fingerprint=state.state_fingerprint,
    )

    assert preview.before == state
    assert preview.desired == desired
    assert preview.changed_fields == ("log_channel_id", "default_timezone")
    assert repository.update_calls == repository.audit_calls == 0

    repository.current = _state(updated_at=NOW + timedelta(seconds=1))
    timestamp_only_preview = _queries(repository).get_preview(
        guild_id=GUILD_ID,
        desired=desired,
        reason="운영 destination 변경",
        expected_state_fingerprint=state.state_fingerprint,
    )
    assert timestamp_only_preview.before.updated_at == NOW + timedelta(seconds=1)

    repository.current = _state(
        editable=_editable(match_announcements_enabled=False),
        updated_at=NOW + timedelta(seconds=2),
    )
    with pytest.raises(DiscordGuildSettingsUpdateStaleError):
        _queries(repository).get_preview(
            guild_id=GUILD_ID,
            desired=desired,
            reason="운영 destination 변경",
            expected_state_fingerprint=state.state_fingerprint,
        )


def test_update_preserves_immutable_authority_and_exact_retry_uses_stored_receipt() -> None:
    before = _state()
    repository = FakeSettingsRepository(before)
    command = _command(before)

    created = _commands(repository).update(command)
    retried = _commands(repository).update(command)

    assert created.operation_id == retried.operation_id == 71
    assert created.exact_retry is False
    assert retried.exact_retry is True
    assert created.after.editable == command.desired
    assert created.after.operator_role_id == before.operator_role_id
    assert created.after.bot_manager_role_id == before.bot_manager_role_id
    assert created.after.created_at == before.created_at
    assert created.changed_fields == ("default_timezone", "match_announcements_enabled")
    assert repository.update_calls == repository.audit_calls == 1


def test_update_rejects_stale_no_change_and_changed_key_without_write() -> None:
    original = _state()
    stale_repository = FakeSettingsRepository(
        _state(
            editable=_editable(log_channel_id=None),
            updated_at=NOW + timedelta(seconds=1),
        )
    )
    with pytest.raises(DiscordGuildSettingsUpdateStaleError):
        _commands(stale_repository).update(_command(original))
    assert stale_repository.update_calls == stale_repository.audit_calls == 0

    no_change_repository = FakeSettingsRepository(original)
    with pytest.raises(DiscordGuildSettingsUpdateNoChangeError):
        _commands(no_change_repository).update(_command(original, desired=original.editable))
    assert no_change_repository.update_calls == no_change_repository.audit_calls == 0

    conflict_repository = FakeSettingsRepository(original)
    first = _command(original)
    _commands(conflict_repository).update(first)
    changed = UpdateDiscordGuildSettings(
        guild_id=GUILD_ID,
        desired=first.desired,
        reason="다른 사유",
        expected_state_fingerprint=original.state_fingerprint,
        actor_discord_user_id=ACTOR_ID,
        idempotency_key=first.idempotency_key,
        correlation_id=first.correlation_id,
    )
    with pytest.raises(DiscordGuildSettingsUpdateIdempotencyConflictError):
        _commands(conflict_repository).update(changed)
    assert conflict_repository.update_calls == conflict_repository.audit_calls == 1


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"default_timezone": "Not/A-Timezone"}, "IANA timezone"),
        ({"win5_announcement_channel_id": "0"}, "snowflake"),
        ({"match_announcements_enabled": 1}, "boolean"),
    ],
)
def test_editable_values_fail_closed_before_uow(changes: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _editable(**changes)
