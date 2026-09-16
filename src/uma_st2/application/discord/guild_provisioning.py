"""Create-only provisioning boundary for one Discord guild settings row."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Final, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.shared import normalize_utc_datetime

DISCORD_GUILD_PROVISIONING_AUDIT_SCHEMA_VERSION: Final = 1
DISCORD_GUILD_PROVISIONING_OPERATION_TYPE: Final = "discord_guild_settings_provisioned"
_MAX_DISCORD_SNOWFLAKE: Final = 2**64 - 1


class DiscordGuildProvisioningError(ValueError):
    """Base error for rejected initial Discord guild provisioning."""


class DiscordGuildProvisioningConflictError(DiscordGuildProvisioningError):
    """The guild or idempotency key is already bound to different settings."""


class DiscordGuildProvisioningAuditError(DiscordGuildProvisioningError):
    """Stored exact-retry evidence is absent or malformed."""


def _snowflake(value: str | None, *, field_name: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or not 1 <= int(value) <= _MAX_DISCORD_SNOWFLAKE
    ):
        qualifier = "optional " if optional else ""
        raise DiscordGuildProvisioningError(f"{field_name} must be an {qualifier}positive decimal Discord snowflake.")
    return value


def _required_text(value: str, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise DiscordGuildProvisioningError(f"{field_name} must be text.")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length or any(ord(character) < 32 for character in normalized):
        raise DiscordGuildProvisioningError(f"{field_name} must contain 1 to {max_length} printable characters.")
    return normalized


@dataclass(frozen=True, slots=True)
class DiscordGuildProvisioningValues:
    """Complete reviewed initial values for one canonical guild setting."""

    guild_id: str
    win5_announcement_channel_id: str | None
    match_announcement_channel_id: str | None
    log_channel_id: str | None
    operator_role_id: str | None
    bot_manager_role_id: str | None
    default_timezone: str
    win5_announcements_enabled: bool
    match_announcements_enabled: bool

    def __post_init__(self) -> None:
        guild_id = _snowflake(self.guild_id, field_name="guild_id")
        win5_channel_id = _snowflake(
            self.win5_announcement_channel_id,
            field_name="win5_announcement_channel_id",
            optional=True,
        )
        match_channel_id = _snowflake(
            self.match_announcement_channel_id,
            field_name="match_announcement_channel_id",
            optional=True,
        )
        log_channel_id = _snowflake(
            self.log_channel_id,
            field_name="log_channel_id",
            optional=True,
        )
        operator_role_id = _snowflake(
            self.operator_role_id,
            field_name="operator_role_id",
            optional=True,
        )
        bot_manager_role_id = _snowflake(
            self.bot_manager_role_id,
            field_name="bot_manager_role_id",
            optional=True,
        )
        if operator_role_id is None and bot_manager_role_id is None:
            raise DiscordGuildProvisioningError("At least one staff Role ID must be configured.")
        if guild_id in {operator_role_id, bot_manager_role_id}:
            raise DiscordGuildProvisioningError("A staff Role ID must not use the guild @everyone Role ID.")
        timezone = _required_text(self.default_timezone, field_name="default_timezone", max_length=32)
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise DiscordGuildProvisioningError("default_timezone must be an available IANA timezone.") from exc
        if not isinstance(self.win5_announcements_enabled, bool):
            raise DiscordGuildProvisioningError("win5_announcements_enabled must be a boolean.")
        if not isinstance(self.match_announcements_enabled, bool):
            raise DiscordGuildProvisioningError("match_announcements_enabled must be a boolean.")
        object.__setattr__(self, "guild_id", guild_id)
        object.__setattr__(self, "win5_announcement_channel_id", win5_channel_id)
        object.__setattr__(self, "match_announcement_channel_id", match_channel_id)
        object.__setattr__(self, "log_channel_id", log_channel_id)
        object.__setattr__(self, "operator_role_id", operator_role_id)
        object.__setattr__(self, "bot_manager_role_id", bot_manager_role_id)
        object.__setattr__(self, "default_timezone", timezone)

    def to_payload(self) -> dict[str, object]:
        return {
            "guild_id": self.guild_id,
            "win5_announcement_channel_id": self.win5_announcement_channel_id,
            "match_announcement_channel_id": self.match_announcement_channel_id,
            "log_channel_id": self.log_channel_id,
            "operator_role_id": self.operator_role_id,
            "bot_manager_role_id": self.bot_manager_role_id,
            "default_timezone": self.default_timezone,
            "win5_announcements_enabled": self.win5_announcements_enabled,
            "match_announcements_enabled": self.match_announcements_enabled,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> DiscordGuildProvisioningValues:
        expected = {
            "guild_id",
            "win5_announcement_channel_id",
            "match_announcement_channel_id",
            "log_channel_id",
            "operator_role_id",
            "bot_manager_role_id",
            "default_timezone",
            "win5_announcements_enabled",
            "match_announcements_enabled",
        }
        if set(payload) != expected:
            raise DiscordGuildProvisioningAuditError("Stored guild settings keys are malformed.")
        try:
            return cls(
                guild_id=payload["guild_id"],  # type: ignore[arg-type]
                win5_announcement_channel_id=payload["win5_announcement_channel_id"],  # type: ignore[arg-type]
                match_announcement_channel_id=payload["match_announcement_channel_id"],  # type: ignore[arg-type]
                log_channel_id=payload["log_channel_id"],  # type: ignore[arg-type]
                operator_role_id=payload["operator_role_id"],  # type: ignore[arg-type]
                bot_manager_role_id=payload["bot_manager_role_id"],  # type: ignore[arg-type]
                default_timezone=payload["default_timezone"],  # type: ignore[arg-type]
                win5_announcements_enabled=payload["win5_announcements_enabled"],  # type: ignore[arg-type]
                match_announcements_enabled=payload["match_announcements_enabled"],  # type: ignore[arg-type]
            )
        except DiscordGuildProvisioningError as exc:
            raise DiscordGuildProvisioningAuditError("Stored guild settings values are malformed.") from exc


@dataclass(frozen=True, slots=True)
class ProvisionDiscordGuildSettings:
    """One reviewed create-only provisioning request."""

    values: DiscordGuildProvisioningValues
    actor_discord_user_id: str
    reason: str
    request_fingerprint: str = field(init=False)
    idempotency_key: str = field(init=False)
    correlation_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.values, DiscordGuildProvisioningValues):
            raise DiscordGuildProvisioningError("values must be DiscordGuildProvisioningValues.")
        actor = _snowflake(self.actor_discord_user_id, field_name="actor_discord_user_id")
        reason = _required_text(self.reason, field_name="reason", max_length=255)
        canonical = {
            "schema_version": DISCORD_GUILD_PROVISIONING_AUDIT_SCHEMA_VERSION,
            "settings": self.values.to_payload(),
            "actor_discord_user_id": actor,
            "reason": reason,
        }
        fingerprint = sha256(
            json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "actor_discord_user_id", actor)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "request_fingerprint", fingerprint)
        object.__setattr__(
            self,
            "idempotency_key",
            f"discord-guild-provision:{self.values.guild_id}:{fingerprint}",
        )
        object.__setattr__(self, "correlation_id", f"discord-guild-provision:{fingerprint}")


@dataclass(frozen=True, slots=True)
class DiscordGuildSettingsSnapshot:
    values: DiscordGuildProvisioningValues
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.values, DiscordGuildProvisioningValues):
            raise DiscordGuildProvisioningAuditError("Stored snapshot values are malformed.")
        object.__setattr__(
            self,
            "created_at",
            normalize_utc_datetime(self.created_at, field_name="created_at"),
        )
        object.__setattr__(
            self,
            "updated_at",
            normalize_utc_datetime(self.updated_at, field_name="updated_at"),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "values": self.values.to_payload(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> DiscordGuildSettingsSnapshot:
        if set(payload) != {"values", "created_at", "updated_at"}:
            raise DiscordGuildProvisioningAuditError("Stored provisioning snapshot keys are malformed.")
        values = payload["values"]
        created_at = payload["created_at"]
        updated_at = payload["updated_at"]
        if not isinstance(values, Mapping) or not isinstance(created_at, str) or not isinstance(updated_at, str):
            raise DiscordGuildProvisioningAuditError("Stored provisioning snapshot values are malformed.")
        try:
            return cls(
                values=DiscordGuildProvisioningValues.from_payload(values),
                created_at=datetime.fromisoformat(created_at),
                updated_at=datetime.fromisoformat(updated_at),
            )
        except DiscordGuildProvisioningAuditError:
            raise
        except (TypeError, ValueError) as exc:
            raise DiscordGuildProvisioningAuditError("Stored provisioning timestamps are malformed.") from exc


@dataclass(frozen=True, slots=True)
class StoredDiscordGuildProvisioningOperation:
    operation_id: int
    request_fingerprint: str | None
    type: str | None
    after_data: object


@dataclass(frozen=True, slots=True)
class ProvisionedDiscordGuildSettings:
    operation_id: int
    snapshot: DiscordGuildSettingsSnapshot
    created: bool


class DiscordGuildProvisioningRepository(Protocol):
    def find_operation(self, *, idempotency_key: str) -> StoredDiscordGuildProvisioningOperation | None: ...

    def lock_settings(self, *, guild_id: str) -> DiscordGuildSettingsSnapshot | None: ...

    def create_settings(
        self,
        *,
        values: DiscordGuildProvisioningValues,
        created_at: datetime,
    ) -> DiscordGuildSettingsSnapshot: ...

    def add_audit(
        self,
        *,
        command: ProvisionDiscordGuildSettings,
        snapshot: DiscordGuildSettingsSnapshot,
        created_at: datetime,
    ) -> int: ...


class DiscordGuildProvisioningUnitOfWork(UnitOfWork, Protocol):
    @property
    def discord_guild_provisioning(self) -> DiscordGuildProvisioningRepository: ...


class DiscordGuildProvisioningCommands:
    """Provision exactly one initial settings row without an overwrite path."""

    def __init__(
        self,
        command_runner: CommandRunner[DiscordGuildProvisioningUnitOfWork],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._command_runner = command_runner
        self._clock = clock

    def provision(self, command: ProvisionDiscordGuildSettings) -> ProvisionedDiscordGuildSettings:
        return self._command_runner.run(lambda unit_of_work: self._provision(unit_of_work, command))

    def _provision(
        self,
        unit_of_work: DiscordGuildProvisioningUnitOfWork,
        command: ProvisionDiscordGuildSettings,
    ) -> ProvisionedDiscordGuildSettings:
        repository = unit_of_work.discord_guild_provisioning
        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._exact_retry(command, stored)

        current = repository.lock_settings(guild_id=command.values.guild_id)
        if current is not None:
            stored = repository.find_operation(idempotency_key=command.idempotency_key)
            if stored is not None:
                return self._exact_retry(command, stored)
            raise DiscordGuildProvisioningConflictError(
                "Discord guild settings already exist; create-only provisioning cannot overwrite them."
            )

        created_at = normalize_utc_datetime(self._clock(), field_name="created_at")
        snapshot = repository.create_settings(values=command.values, created_at=created_at)
        operation_id = repository.add_audit(
            command=command,
            snapshot=snapshot,
            created_at=created_at,
        )
        return ProvisionedDiscordGuildSettings(
            operation_id=operation_id,
            snapshot=snapshot,
            created=True,
        )

    @staticmethod
    def _exact_retry(
        command: ProvisionDiscordGuildSettings,
        stored: StoredDiscordGuildProvisioningOperation,
    ) -> ProvisionedDiscordGuildSettings:
        if stored.request_fingerprint != command.request_fingerprint:
            raise DiscordGuildProvisioningConflictError("The provisioning idempotency key has different content.")
        if stored.type != DISCORD_GUILD_PROVISIONING_OPERATION_TYPE or not isinstance(stored.after_data, Mapping):
            raise DiscordGuildProvisioningAuditError("Stored provisioning audit is malformed.")
        if set(stored.after_data) != {"schema_version", "settings"}:
            raise DiscordGuildProvisioningAuditError("Stored provisioning audit keys are malformed.")
        if stored.after_data["schema_version"] != DISCORD_GUILD_PROVISIONING_AUDIT_SCHEMA_VERSION:
            raise DiscordGuildProvisioningAuditError("Stored provisioning audit version is unsupported.")
        settings = stored.after_data["settings"]
        if not isinstance(settings, Mapping):
            raise DiscordGuildProvisioningAuditError("Stored provisioning settings snapshot is malformed.")
        snapshot = DiscordGuildSettingsSnapshot.from_payload(settings)
        if snapshot.values != command.values:
            raise DiscordGuildProvisioningAuditError("Stored provisioning settings do not match the request.")
        return ProvisionedDiscordGuildSettings(
            operation_id=stored.operation_id,
            snapshot=snapshot,
            created=False,
        )
