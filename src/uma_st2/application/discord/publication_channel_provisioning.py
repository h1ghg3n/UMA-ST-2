"""Canonical bind for one provider-provisioned publication channel."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, QueryRunner, UnitOfWork
from uma_st2.application.publication import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    SUPPORTED_PUBLICATION_DELIVERY_ROUTES,
    WIN5_ANNOUNCEMENT_DESTINATION_KIND,
)
from uma_st2.domain.publication import PublicationStatus
from uma_st2.shared import normalize_utc_datetime

from .guild_settings_update import DiscordGuildSettingsUpdateState

DISCORD_PUBLICATION_CHANNEL_PROVISIONING_AUDIT_SCHEMA_VERSION: Final = 1
DISCORD_PUBLICATION_CHANNEL_PROVISIONING_OPERATION_TYPE: Final = "discord_publication_channel_provisioned"
DISCORD_PUBLICATION_CHANNEL_PERMISSION_PROFILE: Final = "public_announcement_v1"
DISCORD_PUBLICATION_CHANNEL_PROVISIONING_REASON: Final = "Automatic Discord publication channel provisioning."
DISCORD_PUBLICATION_CHANNEL_NAMES: Final = {
    WIN5_ANNOUNCEMENT_DESTINATION_KIND: "umabot-win5",
    MATCH_ANNOUNCEMENT_DESTINATION_KIND: "umabot-room-match",
}
_MAX_DISCORD_SNOWFLAKE: Final = 2**64 - 1


class DiscordPublicationChannelProvisioningError(ValueError):
    """Base error for a rejected automatic channel bind."""


class DiscordPublicationChannelProvisioningInvalidSourceError(DiscordPublicationChannelProvisioningError):
    """Stored settings or publication evidence is malformed."""


class DiscordPublicationChannelProvisioningUnavailableError(DiscordPublicationChannelProvisioningError):
    """The exact eligible settings/publication pair is no longer available."""


class DiscordPublicationChannelProvisioningStaleError(DiscordPublicationChannelProvisioningError):
    """Settings or publication authority changed before canonical bind."""


class DiscordPublicationChannelProvisioningIdempotencyConflictError(DiscordPublicationChannelProvisioningError):
    """The idempotency key belongs to another logical request."""


class DiscordPublicationChannelProvisioningConcurrentConflictError(DiscordPublicationChannelProvisioningError):
    """A concurrent writer forced the canonical bind to roll back."""


class DiscordPublicationChannelProvisioningAuditError(DiscordPublicationChannelProvisioningError):
    """Stored operation evidence is incomplete or malformed."""


def _snowflake(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or not 1 <= int(value) <= _MAX_DISCORD_SNOWFLAKE
    ):
        raise DiscordPublicationChannelProvisioningError(f"{field_name} must be a positive decimal Discord snowflake.")
    return value


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DiscordPublicationChannelProvisioningError(f"{field_name} must be a positive integer.")
    return value


def _bounded_text(value: object, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise DiscordPublicationChannelProvisioningError(f"{field_name} must be text.")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length or any(ord(character) < 32 for character in normalized):
        raise DiscordPublicationChannelProvisioningError(
            f"{field_name} must contain 1 to {max_length} printable characters."
        )
    return normalized


def _sha256(value: object, *, field_name: str) -> str:
    text = _bounded_text(value, field_name=field_name, max_length=64)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise DiscordPublicationChannelProvisioningError(f"{field_name} must be a SHA-256 digest.")
    return text


def _fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _destination_values(
    state: DiscordGuildSettingsUpdateState,
    destination_kind: str,
) -> tuple[bool, str | None]:
    if destination_kind == WIN5_ANNOUNCEMENT_DESTINATION_KIND:
        return (
            state.editable.win5_announcements_enabled,
            state.editable.win5_announcement_channel_id,
        )
    if destination_kind == MATCH_ANNOUNCEMENT_DESTINATION_KIND:
        return (
            state.editable.match_announcements_enabled,
            state.editable.match_announcement_channel_id,
        )
    raise DiscordPublicationChannelProvisioningError("destination_kind is unsupported.")


def publication_channel_settings_fingerprint(
    state: DiscordGuildSettingsUpdateState,
    *,
    destination_kind: str,
) -> str:
    """Fingerprint only authority relevant to one automatic destination bind."""

    if not isinstance(state, DiscordGuildSettingsUpdateState):
        raise DiscordPublicationChannelProvisioningError("settings state must be complete.")
    enabled, channel_id = _destination_values(state, destination_kind)
    return _fingerprint(
        {
            "schema": "discord-publication-channel-settings-authority-v1",
            "guild_id": state.guild_id,
            "destination_kind": destination_kind,
            "announcements_enabled": enabled,
            "target_channel_id": channel_id,
            "settings_created_at": state.created_at.isoformat(),
        }
    )


@dataclass(frozen=True, slots=True)
class AwaitingDiscordPublicationChannel:
    """Detached oldest publication and settings authority for provider work."""

    publication_id: int
    guild_id: str
    destination_kind: str
    event_type: str
    payload_fingerprint: str
    expected_settings_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "publication_id", _positive_int(self.publication_id, field_name="publication_id"))
        object.__setattr__(self, "guild_id", _snowflake(self.guild_id, field_name="guild_id"))
        destination_kind = _bounded_text(
            self.destination_kind,
            field_name="destination_kind",
            max_length=32,
        )
        event_type = _bounded_text(self.event_type, field_name="event_type", max_length=64)
        if (destination_kind, event_type) not in SUPPORTED_PUBLICATION_DELIVERY_ROUTES:
            raise DiscordPublicationChannelProvisioningError("Publication route is unsupported.")
        object.__setattr__(self, "destination_kind", destination_kind)
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(
            self,
            "payload_fingerprint",
            _sha256(self.payload_fingerprint, field_name="payload_fingerprint"),
        )
        object.__setattr__(
            self,
            "expected_settings_fingerprint",
            _sha256(self.expected_settings_fingerprint, field_name="expected_settings_fingerprint"),
        )

    @property
    def channel_name(self) -> str:
        try:
            return DISCORD_PUBLICATION_CHANNEL_NAMES[self.destination_kind]
        except KeyError as error:
            raise DiscordPublicationChannelProvisioningError("Publication destination has no channel name.") from error


@dataclass(frozen=True, slots=True)
class StoredAwaitingDiscordPublication:
    """Locked current publication state used by the Final command."""

    publication_id: int
    guild_id: str
    destination_kind: str
    event_type: str
    payload_fingerprint: str
    status: PublicationStatus
    target_channel_id: str | None
    attempt_count: int
    discord_message_id: str | None
    last_error_code: str | None
    failure_stage: str | None
    attempt_started_at: datetime | None
    published_at: datetime | None
    updated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "publication_id", _positive_int(self.publication_id, field_name="publication_id"))
        object.__setattr__(self, "guild_id", _snowflake(self.guild_id, field_name="guild_id"))
        destination_kind = _bounded_text(
            self.destination_kind,
            field_name="destination_kind",
            max_length=32,
        )
        event_type = _bounded_text(self.event_type, field_name="event_type", max_length=64)
        if (destination_kind, event_type) not in SUPPORTED_PUBLICATION_DELIVERY_ROUTES:
            raise DiscordPublicationChannelProvisioningError("Locked publication route is unsupported.")
        object.__setattr__(self, "destination_kind", destination_kind)
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(
            self,
            "payload_fingerprint",
            _sha256(self.payload_fingerprint, field_name="payload_fingerprint"),
        )
        object.__setattr__(self, "status", PublicationStatus(self.status))
        if self.target_channel_id is not None:
            object.__setattr__(
                self,
                "target_channel_id",
                _snowflake(self.target_channel_id, field_name="target_channel_id"),
            )
        if isinstance(self.attempt_count, bool) or not isinstance(self.attempt_count, int) or self.attempt_count < 0:
            raise DiscordPublicationChannelProvisioningError("attempt_count must be a non-negative integer.")
        if self.discord_message_id is not None:
            object.__setattr__(
                self,
                "discord_message_id",
                _snowflake(self.discord_message_id, field_name="discord_message_id"),
            )
        for field_name in ("last_error_code", "failure_stage"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _bounded_text(value, field_name=field_name, max_length=64))
        for field_name in ("attempt_started_at", "published_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    normalize_utc_datetime(value, field_name=field_name),
                )
        object.__setattr__(
            self,
            "updated_at",
            normalize_utc_datetime(self.updated_at, field_name="updated_at"),
        )

    @property
    def pristine_awaiting(self) -> bool:
        return (
            self.status == PublicationStatus.AWAITING_CHANNEL
            and self.target_channel_id is None
            and self.attempt_count == 0
            and self.discord_message_id is None
            and self.last_error_code is None
            and self.failure_stage is None
            and self.attempt_started_at is None
            and self.published_at is None
        )


@dataclass(frozen=True, slots=True)
class ProvisionDiscordPublicationChannel:
    """Bind one provider channel to its exact triggering awaiting publication."""

    target: AwaitingDiscordPublicationChannel
    target_channel_id: str
    actor_discord_user_id: str
    idempotency_key: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target, AwaitingDiscordPublicationChannel):
            raise DiscordPublicationChannelProvisioningError("target must be complete.")
        object.__setattr__(
            self,
            "target_channel_id",
            _snowflake(self.target_channel_id, field_name="target_channel_id"),
        )
        object.__setattr__(
            self,
            "actor_discord_user_id",
            _snowflake(self.actor_discord_user_id, field_name="actor_discord_user_id"),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            _bounded_text(self.idempotency_key, field_name="idempotency_key", max_length=128),
        )
        if self.correlation_id is not None:
            object.__setattr__(
                self,
                "correlation_id",
                _bounded_text(self.correlation_id, field_name="correlation_id", max_length=128),
            )

    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(
            {
                "schema": "discord-publication-channel-provision-command-v1",
                "publication_id": self.target.publication_id,
                "guild_id": self.target.guild_id,
                "destination_kind": self.target.destination_kind,
                "event_type": self.target.event_type,
                "payload_fingerprint": self.target.payload_fingerprint,
                "expected_settings_fingerprint": self.target.expected_settings_fingerprint,
                "target_channel_id": self.target_channel_id,
                "channel_name": self.target.channel_name,
                "permission_profile": DISCORD_PUBLICATION_CHANNEL_PERMISSION_PROFILE,
                "actor_discord_user_id": self.actor_discord_user_id,
            }
        )


@dataclass(frozen=True, slots=True)
class ProvisionedDiscordPublicationChannel:
    """Committed settings/audit/publication bind receipt."""

    operation_id: int
    publication_id: int
    guild_id: str
    destination_kind: str
    event_type: str
    payload_fingerprint: str
    target_channel_id: str
    channel_name: str
    permission_profile: str
    before: DiscordGuildSettingsUpdateState
    after: DiscordGuildSettingsUpdateState
    ready_at: datetime
    exact_retry: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation_id", _positive_int(self.operation_id, field_name="operation_id"))
        object.__setattr__(self, "publication_id", _positive_int(self.publication_id, field_name="publication_id"))
        object.__setattr__(self, "guild_id", _snowflake(self.guild_id, field_name="guild_id"))
        destination_kind = _bounded_text(
            self.destination_kind,
            field_name="destination_kind",
            max_length=32,
        )
        event_type = _bounded_text(self.event_type, field_name="event_type", max_length=64)
        if (destination_kind, event_type) not in SUPPORTED_PUBLICATION_DELIVERY_ROUTES:
            raise DiscordPublicationChannelProvisioningAuditError("Stored publication route is unsupported.")
        object.__setattr__(self, "destination_kind", destination_kind)
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(
            self,
            "payload_fingerprint",
            _sha256(self.payload_fingerprint, field_name="payload_fingerprint"),
        )
        object.__setattr__(
            self,
            "target_channel_id",
            _snowflake(self.target_channel_id, field_name="target_channel_id"),
        )
        object.__setattr__(
            self, "channel_name", _bounded_text(self.channel_name, field_name="channel_name", max_length=100)
        )
        object.__setattr__(
            self,
            "permission_profile",
            _bounded_text(self.permission_profile, field_name="permission_profile", max_length=64),
        )
        if self.channel_name != DISCORD_PUBLICATION_CHANNEL_NAMES.get(self.destination_kind):
            raise DiscordPublicationChannelProvisioningAuditError("Stored channel name is malformed.")
        if self.permission_profile != DISCORD_PUBLICATION_CHANNEL_PERMISSION_PROFILE:
            raise DiscordPublicationChannelProvisioningAuditError("Stored permission profile is unsupported.")
        if not isinstance(self.before, DiscordGuildSettingsUpdateState) or not isinstance(
            self.after, DiscordGuildSettingsUpdateState
        ):
            raise DiscordPublicationChannelProvisioningAuditError("Stored settings states are malformed.")
        if self.before.guild_id != self.guild_id or self.after.guild_id != self.guild_id:
            raise DiscordPublicationChannelProvisioningAuditError("Stored settings guild identity is malformed.")
        if (
            self.before.operator_role_id != self.after.operator_role_id
            or self.before.bot_manager_role_id != self.after.bot_manager_role_id
            or self.before.created_at != self.after.created_at
        ):
            raise DiscordPublicationChannelProvisioningAuditError("Immutable settings authority changed.")
        before_enabled, before_channel = _destination_values(self.before, self.destination_kind)
        after_enabled, after_channel = _destination_values(self.after, self.destination_kind)
        if (
            not before_enabled
            or before_channel is not None
            or not after_enabled
            or after_channel != self.target_channel_id
        ):
            raise DiscordPublicationChannelProvisioningAuditError("Stored destination transition is malformed.")
        before_payload = self.before.editable.to_payload()
        after_payload = self.after.editable.to_payload()
        changed = {key for key in before_payload if before_payload[key] != after_payload[key]}
        expected_field = (
            "win5_announcement_channel_id"
            if self.destination_kind == WIN5_ANNOUNCEMENT_DESTINATION_KIND
            else "match_announcement_channel_id"
        )
        if changed != {expected_field}:
            raise DiscordPublicationChannelProvisioningAuditError(
                "Stored settings changed outside the destination bind."
            )
        object.__setattr__(self, "ready_at", normalize_utc_datetime(self.ready_at, field_name="ready_at"))
        if self.after.updated_at != self.ready_at or self.ready_at < self.before.updated_at:
            raise DiscordPublicationChannelProvisioningAuditError("Stored settings ready timestamp is malformed.")
        if not isinstance(self.exact_retry, bool):
            raise DiscordPublicationChannelProvisioningAuditError("exact_retry must be a boolean.")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": DISCORD_PUBLICATION_CHANNEL_PROVISIONING_AUDIT_SCHEMA_VERSION,
            "publication_id": self.publication_id,
            "guild_id": self.guild_id,
            "destination_kind": self.destination_kind,
            "event_type": self.event_type,
            "payload_fingerprint": self.payload_fingerprint,
            "target_channel_id": self.target_channel_id,
            "channel_name": self.channel_name,
            "permission_profile": self.permission_profile,
            "before": self.before.to_payload(),
            "after": self.after.to_payload(),
            "ready_at": self.ready_at.isoformat(),
        }

    @classmethod
    def from_payload(
        cls,
        *,
        operation_id: int,
        payload: Mapping[str, object],
    ) -> ProvisionedDiscordPublicationChannel:
        expected = {
            "schema_version",
            "publication_id",
            "guild_id",
            "destination_kind",
            "event_type",
            "payload_fingerprint",
            "target_channel_id",
            "channel_name",
            "permission_profile",
            "before",
            "after",
            "ready_at",
        }
        if (
            set(payload) != expected
            or payload.get("schema_version") != DISCORD_PUBLICATION_CHANNEL_PROVISIONING_AUDIT_SCHEMA_VERSION
        ):
            raise DiscordPublicationChannelProvisioningAuditError("Stored provisioning receipt keys are malformed.")
        before = payload["before"]
        after = payload["after"]
        ready_at = payload["ready_at"]
        if not isinstance(before, Mapping) or not isinstance(after, Mapping) or not isinstance(ready_at, str):
            raise DiscordPublicationChannelProvisioningAuditError("Stored provisioning receipt values are malformed.")
        try:
            return cls(
                operation_id=operation_id,
                publication_id=payload["publication_id"],  # type: ignore[arg-type]
                guild_id=payload["guild_id"],  # type: ignore[arg-type]
                destination_kind=payload["destination_kind"],  # type: ignore[arg-type]
                event_type=payload["event_type"],  # type: ignore[arg-type]
                payload_fingerprint=payload["payload_fingerprint"],  # type: ignore[arg-type]
                target_channel_id=payload["target_channel_id"],  # type: ignore[arg-type]
                channel_name=payload["channel_name"],  # type: ignore[arg-type]
                permission_profile=payload["permission_profile"],  # type: ignore[arg-type]
                before=DiscordGuildSettingsUpdateState.from_payload(before),
                after=DiscordGuildSettingsUpdateState.from_payload(after),
                ready_at=datetime.fromisoformat(ready_at),
            )
        except DiscordPublicationChannelProvisioningAuditError:
            raise
        except (TypeError, ValueError) as error:
            raise DiscordPublicationChannelProvisioningAuditError(
                "Stored provisioning receipt is malformed."
            ) from error


@dataclass(frozen=True, slots=True)
class StoredDiscordPublicationChannelProvisioningOperation:
    operation_id: int
    request_fingerprint: str | None
    operation_type: str | None
    guild_id: str | None
    after_data: object


class DiscordPublicationChannelProvisioningQueryRepository(Protocol):
    def find_oldest_target(self, *, guild_id: str) -> AwaitingDiscordPublicationChannel | None: ...


class DiscordPublicationChannelProvisioningQueryUnitOfWork(UnitOfWork, Protocol):
    @property
    def discord_publication_channel_provisioning(
        self,
    ) -> DiscordPublicationChannelProvisioningQueryRepository: ...


@dataclass(frozen=True, slots=True)
class DiscordPublicationChannelProvisioningQueries:
    """Read one detached automatic provider target without locking."""

    query_runner: QueryRunner[DiscordPublicationChannelProvisioningQueryUnitOfWork]

    def find_target(self, *, guild_id: str) -> AwaitingDiscordPublicationChannel | None:
        normalized_guild = _snowflake(guild_id, field_name="guild_id")
        try:
            target = self.query_runner.run(
                lambda unit_of_work: unit_of_work.discord_publication_channel_provisioning.find_oldest_target(
                    guild_id=normalized_guild
                )
            )
        except DiscordPublicationChannelProvisioningError:
            raise
        except (TypeError, ValueError) as error:
            raise DiscordPublicationChannelProvisioningInvalidSourceError(
                "Stored automatic publication-channel target is malformed."
            ) from error
        if target is not None and target.guild_id != normalized_guild:
            raise DiscordPublicationChannelProvisioningInvalidSourceError(
                "Automatic publication-channel query returned another guild."
            )
        return target


class DiscordPublicationChannelProvisioningRepository(Protocol):
    def lock_settings(self, *, guild_id: str) -> DiscordGuildSettingsUpdateState | None: ...

    def find_operation(
        self,
        *,
        idempotency_key: str,
    ) -> StoredDiscordPublicationChannelProvisioningOperation | None: ...

    def lock_publication(self, *, publication_id: int) -> StoredAwaitingDiscordPublication | None: ...

    def bind_channel(
        self,
        *,
        before: DiscordGuildSettingsUpdateState,
        destination_kind: str,
        target_channel_id: str,
        publication_id: int,
        ready_at: datetime,
    ) -> DiscordGuildSettingsUpdateState: ...

    def add_audit(
        self,
        *,
        command: ProvisionDiscordPublicationChannel,
        before: DiscordGuildSettingsUpdateState,
        after: DiscordGuildSettingsUpdateState,
        ready_at: datetime,
    ) -> int: ...


class DiscordPublicationChannelProvisioningUnitOfWork(UnitOfWork, Protocol):
    @property
    def discord_publication_channel_provisioning(self) -> DiscordPublicationChannelProvisioningRepository: ...


@dataclass(frozen=True, slots=True)
class DiscordPublicationChannelProvisioningCommands:
    """Commit one settings/audit/triggering-publication bind."""

    command_runner: CommandRunner[DiscordPublicationChannelProvisioningUnitOfWork]
    clock: Callable[[], datetime]

    def provision(
        self,
        command: ProvisionDiscordPublicationChannel,
    ) -> ProvisionedDiscordPublicationChannel:
        return self.command_runner.run(
            lambda unit_of_work: self._provision(
                unit_of_work.discord_publication_channel_provisioning,
                command,
            )
        )

    def _provision(
        self,
        repository: DiscordPublicationChannelProvisioningRepository,
        command: ProvisionDiscordPublicationChannel,
    ) -> ProvisionedDiscordPublicationChannel:
        before = repository.lock_settings(guild_id=command.target.guild_id)
        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_retry(stored=stored, command=command)
        if before is None:
            raise DiscordPublicationChannelProvisioningUnavailableError("Discord guild settings do not exist.")
        current_settings_fingerprint = publication_channel_settings_fingerprint(
            before,
            destination_kind=command.target.destination_kind,
        )
        if current_settings_fingerprint != command.target.expected_settings_fingerprint:
            raise DiscordPublicationChannelProvisioningStaleError(
                "Publication channel settings changed before canonical bind."
            )
        enabled, channel_id = _destination_values(before, command.target.destination_kind)
        if not enabled or channel_id is not None:
            raise DiscordPublicationChannelProvisioningUnavailableError(
                "Publication destination is no longer enabled and unset."
            )
        publication = repository.lock_publication(publication_id=command.target.publication_id)
        if publication is None:
            raise DiscordPublicationChannelProvisioningUnavailableError("Triggering publication does not exist.")
        if (
            publication.guild_id != command.target.guild_id
            or publication.destination_kind != command.target.destination_kind
            or publication.event_type != command.target.event_type
            or publication.payload_fingerprint != command.target.payload_fingerprint
            or not publication.pristine_awaiting
        ):
            raise DiscordPublicationChannelProvisioningStaleError(
                "Triggering publication changed before canonical bind."
            )
        clock_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        ready_at = max(clock_at, before.updated_at, publication.updated_at)
        after = repository.bind_channel(
            before=before,
            destination_kind=command.target.destination_kind,
            target_channel_id=command.target_channel_id,
            publication_id=command.target.publication_id,
            ready_at=ready_at,
        )
        operation_id = repository.add_audit(
            command=command,
            before=before,
            after=after,
            ready_at=ready_at,
        )
        return ProvisionedDiscordPublicationChannel(
            operation_id=operation_id,
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

    @staticmethod
    def _resolve_retry(
        *,
        stored: StoredDiscordPublicationChannelProvisioningOperation,
        command: ProvisionDiscordPublicationChannel,
    ) -> ProvisionedDiscordPublicationChannel:
        if stored.request_fingerprint != command.request_fingerprint:
            raise DiscordPublicationChannelProvisioningIdempotencyConflictError(
                "Idempotency key is already bound to another channel-provisioning request."
            )
        if (
            stored.operation_type != DISCORD_PUBLICATION_CHANNEL_PROVISIONING_OPERATION_TYPE
            or stored.guild_id != command.target.guild_id
            or not isinstance(stored.after_data, Mapping)
        ):
            raise DiscordPublicationChannelProvisioningAuditError("Stored channel-provisioning operation is malformed.")
        result = ProvisionedDiscordPublicationChannel.from_payload(
            operation_id=stored.operation_id,
            payload=stored.after_data,
        )
        if (
            result.publication_id != command.target.publication_id
            or result.destination_kind != command.target.destination_kind
            or result.event_type != command.target.event_type
            or result.payload_fingerprint != command.target.payload_fingerprint
            or result.target_channel_id != command.target_channel_id
            or publication_channel_settings_fingerprint(
                result.before,
                destination_kind=result.destination_kind,
            )
            != command.target.expected_settings_fingerprint
        ):
            raise DiscordPublicationChannelProvisioningAuditError("Stored channel-provisioning context is malformed.")
        return replace(result, exact_retry=True)
