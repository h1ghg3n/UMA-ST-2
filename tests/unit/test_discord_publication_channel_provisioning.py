"""Application contract tests for automatic publication-channel binding."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from uma_st2.application.discord import (
    DISCORD_PUBLICATION_CHANNEL_PERMISSION_PROFILE,
    DISCORD_PUBLICATION_CHANNEL_PROVISIONING_OPERATION_TYPE,
    AwaitingDiscordPublicationChannel,
    DiscordGuildEditableSettings,
    DiscordGuildSettingsUpdateState,
    DiscordPublicationChannelProvisioningCommands,
    DiscordPublicationChannelProvisioningIdempotencyConflictError,
    DiscordPublicationChannelProvisioningQueries,
    DiscordPublicationChannelProvisioningStaleError,
    ProvisionDiscordPublicationChannel,
    ProvisionedDiscordPublicationChannel,
    StoredAwaitingDiscordPublication,
    StoredDiscordPublicationChannelProvisioningOperation,
    publication_channel_settings_fingerprint,
)
from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.publication import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    MATCH_BETTING_OPENED_EVENT_TYPE,
)
from uma_st2.domain.publication import PublicationStatus

NOW = datetime(2026, 9, 5, 5, 0, tzinfo=UTC)
GUILD_ID = "100000000000000001"
BOT_ID = "400000000000000001"
CHANNEL_ID = "200000000000000001"
PAYLOAD_FINGERPRINT = "a" * 64


def _settings(
    *,
    match_channel_id: str | None = None,
    match_enabled: bool = True,
    timezone: str = "Asia/Seoul",
    updated_at: datetime = NOW,
) -> DiscordGuildSettingsUpdateState:
    return DiscordGuildSettingsUpdateState(
        guild_id=GUILD_ID,
        editable=DiscordGuildEditableSettings(
            win5_announcement_channel_id=None,
            match_announcement_channel_id=match_channel_id,
            log_channel_id=None,
            default_timezone=timezone,
            win5_announcements_enabled=True,
            match_announcements_enabled=match_enabled,
        ),
        operator_role_id="300000000000000001",
        bot_manager_role_id="300000000000000002",
        created_at=NOW - timedelta(days=10),
        updated_at=updated_at,
    )


def _target(settings: DiscordGuildSettingsUpdateState | None = None) -> AwaitingDiscordPublicationChannel:
    current = settings or _settings()
    return AwaitingDiscordPublicationChannel(
        publication_id=71,
        guild_id=GUILD_ID,
        destination_kind=MATCH_ANNOUNCEMENT_DESTINATION_KIND,
        event_type=MATCH_BETTING_OPENED_EVENT_TYPE,
        payload_fingerprint=PAYLOAD_FINGERPRINT,
        expected_settings_fingerprint=publication_channel_settings_fingerprint(
            current,
            destination_kind=MATCH_ANNOUNCEMENT_DESTINATION_KIND,
        ),
    )


def _publication(**changes: object) -> StoredAwaitingDiscordPublication:
    values: dict[str, object] = {
        "publication_id": 71,
        "guild_id": GUILD_ID,
        "destination_kind": MATCH_ANNOUNCEMENT_DESTINATION_KIND,
        "event_type": MATCH_BETTING_OPENED_EVENT_TYPE,
        "payload_fingerprint": PAYLOAD_FINGERPRINT,
        "status": PublicationStatus.AWAITING_CHANNEL,
        "target_channel_id": None,
        "attempt_count": 0,
        "discord_message_id": None,
        "last_error_code": None,
        "failure_stage": None,
        "attempt_started_at": None,
        "published_at": None,
        "updated_at": NOW + timedelta(minutes=2),
    }
    values.update(changes)
    return StoredAwaitingDiscordPublication(**values)  # type: ignore[arg-type]


def _command(
    target: AwaitingDiscordPublicationChannel,
    *,
    channel_id: str = CHANNEL_ID,
    key: str = "publication-channel-71",
) -> ProvisionDiscordPublicationChannel:
    return ProvisionDiscordPublicationChannel(
        target=target,
        target_channel_id=channel_id,
        actor_discord_user_id=BOT_ID,
        idempotency_key=key,
        correlation_id=key,
    )


class FakeRepository:
    def __init__(
        self,
        *,
        settings: DiscordGuildSettingsUpdateState | None,
        target: AwaitingDiscordPublicationChannel | None = None,
        publication: StoredAwaitingDiscordPublication | None = None,
    ) -> None:
        self.settings = settings
        self.target = target
        self.publication = publication
        self.operations: dict[str, StoredDiscordPublicationChannelProvisioningOperation] = {}
        self.bind_calls = 0
        self.audit_calls = 0

    def find_oldest_target(self, *, guild_id: str) -> AwaitingDiscordPublicationChannel | None:
        return self.target if self.target is not None and self.target.guild_id == guild_id else None

    def lock_settings(self, *, guild_id: str) -> DiscordGuildSettingsUpdateState | None:
        return self.settings if self.settings is not None and self.settings.guild_id == guild_id else None

    def find_operation(
        self,
        *,
        idempotency_key: str,
    ) -> StoredDiscordPublicationChannelProvisioningOperation | None:
        return self.operations.get(idempotency_key)

    def lock_publication(self, *, publication_id: int) -> StoredAwaitingDiscordPublication | None:
        if self.publication is None or self.publication.publication_id != publication_id:
            return None
        return self.publication

    def bind_channel(
        self,
        *,
        before: DiscordGuildSettingsUpdateState,
        destination_kind: str,
        target_channel_id: str,
        publication_id: int,
        ready_at: datetime,
    ) -> DiscordGuildSettingsUpdateState:
        assert destination_kind == MATCH_ANNOUNCEMENT_DESTINATION_KIND
        assert publication_id == 71
        self.bind_calls += 1
        editable = replace(before.editable, match_announcement_channel_id=target_channel_id)
        self.settings = replace(before, editable=editable, updated_at=ready_at)
        assert self.publication is not None
        self.publication = replace(
            self.publication,
            status=PublicationStatus.READY,
            target_channel_id=target_channel_id,
            updated_at=ready_at,
        )
        return self.settings

    def add_audit(
        self,
        *,
        command: ProvisionDiscordPublicationChannel,
        before: DiscordGuildSettingsUpdateState,
        after: DiscordGuildSettingsUpdateState,
        ready_at: datetime,
    ) -> int:
        self.audit_calls += 1
        result = ProvisionedDiscordPublicationChannel(
            operation_id=91,
            publication_id=command.target.publication_id,
            guild_id=command.target.guild_id,
            destination_kind=command.target.destination_kind,
            event_type=command.target.event_type,
            payload_fingerprint=command.target.payload_fingerprint,
            target_channel_id=command.target_channel_id,
            channel_name=command.target.channel_name,
            permission_profile=DISCORD_PUBLICATION_CHANNEL_PERMISSION_PROFILE,
            before=before,
            after=after,
            ready_at=ready_at,
        )
        self.operations[command.idempotency_key] = StoredDiscordPublicationChannelProvisioningOperation(
            operation_id=91,
            request_fingerprint=command.request_fingerprint,
            operation_type=DISCORD_PUBLICATION_CHANNEL_PROVISIONING_OPERATION_TYPE,
            guild_id=command.target.guild_id,
            after_data=result.to_payload(),
        )
        return 91


class FakeUnitOfWork:
    def __init__(self, repository: FakeRepository) -> None:
        self.discord_publication_channel_provisioning = repository
        self.commits = 0

    def __enter__(self) -> FakeUnitOfWork:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None


def _queries(repository: FakeRepository) -> DiscordPublicationChannelProvisioningQueries:
    return DiscordPublicationChannelProvisioningQueries(QueryRunner(lambda: FakeUnitOfWork(repository)))


def _commands(repository: FakeRepository) -> DiscordPublicationChannelProvisioningCommands:
    return DiscordPublicationChannelProvisioningCommands(
        CommandRunner(lambda: FakeUnitOfWork(repository)),
        clock=lambda: NOW + timedelta(minutes=1),
    )


def test_query_returns_only_the_detached_oldest_eligible_target() -> None:
    settings = _settings()
    target = _target(settings)
    repository = FakeRepository(settings=settings, target=target)

    assert _queries(repository).find_target(guild_id=GUILD_ID) == target
    repository.target = None
    assert _queries(repository).find_target(guild_id=GUILD_ID) is None


def test_relevant_settings_fingerprint_ignores_unrelated_and_timestamp_churn() -> None:
    original = _settings()
    unrelated = replace(
        _settings(timezone="UTC", updated_at=NOW + timedelta(minutes=5)),
        operator_role_id="300000000000000003",
    )

    assert publication_channel_settings_fingerprint(
        original,
        destination_kind=MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    ) == publication_channel_settings_fingerprint(
        unrelated,
        destination_kind=MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    )
    assert publication_channel_settings_fingerprint(
        _settings(match_enabled=False),
        destination_kind=MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    ) != publication_channel_settings_fingerprint(
        original,
        destination_kind=MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    )


def test_bind_updates_one_destination_and_triggering_publication_then_exact_retries() -> None:
    settings = _settings()
    target = _target(settings)
    repository = FakeRepository(settings=settings, target=target, publication=_publication())
    command = _command(target)

    created = _commands(repository).provision(command)
    retried = _commands(repository).provision(command)

    assert created.operation_id == retried.operation_id == 91
    assert created.exact_retry is False
    assert retried.exact_retry is True
    assert created.after.editable.match_announcement_channel_id == CHANNEL_ID
    assert created.ready_at == NOW + timedelta(minutes=2)
    assert created.after.editable.win5_announcement_channel_id is None
    assert repository.publication is not None
    assert repository.publication.status == PublicationStatus.READY
    assert repository.publication.target_channel_id == CHANNEL_ID
    assert repository.bind_calls == repository.audit_calls == 1


@pytest.mark.parametrize(
    "publication",
    [
        _publication(status=PublicationStatus.READY, target_channel_id=CHANNEL_ID),
        _publication(payload_fingerprint="b" * 64),
        _publication(attempt_count=1),
    ],
)
def test_bind_rejects_stale_or_non_pristine_publication_without_write(
    publication: StoredAwaitingDiscordPublication,
) -> None:
    settings = _settings()
    target = _target(settings)
    repository = FakeRepository(settings=settings, publication=publication)

    with pytest.raises(DiscordPublicationChannelProvisioningStaleError):
        _commands(repository).provision(_command(target))

    assert repository.bind_calls == repository.audit_calls == 0


def test_bind_rejects_relevant_settings_drift_without_publication_lock_or_write() -> None:
    original = _settings()
    target = _target(original)
    repository = FakeRepository(
        settings=_settings(match_enabled=False, updated_at=NOW + timedelta(seconds=1)),
        publication=_publication(),
    )

    with pytest.raises(DiscordPublicationChannelProvisioningStaleError):
        _commands(repository).provision(_command(target))

    assert repository.bind_calls == repository.audit_calls == 0


def test_same_key_changed_channel_is_an_idempotency_conflict() -> None:
    settings = _settings()
    target = _target(settings)
    repository = FakeRepository(settings=settings, publication=_publication())
    command = _command(target)
    _commands(repository).provision(command)

    with pytest.raises(DiscordPublicationChannelProvisioningIdempotencyConflictError):
        _commands(repository).provision(_command(target, channel_id="200000000000000002", key=command.idempotency_key))
    assert repository.bind_calls == repository.audit_calls == 1
