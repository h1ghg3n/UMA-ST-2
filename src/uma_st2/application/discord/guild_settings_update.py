"""Audited runtime updates for one existing Discord guild settings row."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from typing import Final, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from uma_st2.application.execution import CommandRunner, QueryRunner, UnitOfWork
from uma_st2.shared import normalize_utc_datetime

DISCORD_GUILD_SETTINGS_UPDATE_AUDIT_SCHEMA_VERSION: Final = 1
DISCORD_GUILD_SETTINGS_UPDATE_OPERATION_TYPE: Final = "discord_guild_settings_updated"
_MAX_DISCORD_SNOWFLAKE: Final = 2**64 - 1
_EDITABLE_FIELDS: Final = (
    "win5_announcement_channel_id",
    "match_announcement_channel_id",
    "log_channel_id",
    "default_timezone",
    "win5_announcements_enabled",
    "match_announcements_enabled",
)


class DiscordGuildSettingsUpdateError(ValueError):
    """Base error for a rejected runtime settings update."""


class DiscordGuildSettingsUpdateUnavailableError(DiscordGuildSettingsUpdateError):
    """The configured guild settings row no longer exists."""


class DiscordGuildSettingsUpdateInvalidSourceError(DiscordGuildSettingsUpdateError):
    """Stored settings cannot form complete update authority."""


class DiscordGuildSettingsUpdateNoChangeError(DiscordGuildSettingsUpdateError):
    """The complete desired editable set is already current."""


class DiscordGuildSettingsUpdateStaleError(DiscordGuildSettingsUpdateError):
    """The settings row changed after the editor was opened."""


class DiscordGuildSettingsUpdateIdempotencyConflictError(DiscordGuildSettingsUpdateError):
    """The Final interaction key belongs to another logical request."""


class DiscordGuildSettingsUpdateConcurrentConflictError(DiscordGuildSettingsUpdateError):
    """A concurrent settings writer forced this operation to roll back."""


class DiscordGuildSettingsUpdateAuditError(DiscordGuildSettingsUpdateError):
    """Stored settings-operation evidence is incomplete or malformed."""


def _snowflake(value: object, *, field_name: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or not 1 <= int(value) <= _MAX_DISCORD_SNOWFLAKE
    ):
        qualifier = "optional " if optional else ""
        raise DiscordGuildSettingsUpdateError(f"{field_name} must be an {qualifier}positive decimal Discord snowflake.")
    return value


def _text(value: object, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise DiscordGuildSettingsUpdateError(f"{field_name} must be text.")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length or any(ord(character) < 32 for character in normalized):
        raise DiscordGuildSettingsUpdateError(f"{field_name} must contain 1 to {max_length} printable characters.")
    return normalized


def _fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DiscordGuildEditableSettings:
    """Complete `/settings`-editable values without security Role authority."""

    win5_announcement_channel_id: str | None
    match_announcement_channel_id: str | None
    log_channel_id: str | None
    default_timezone: str
    win5_announcements_enabled: bool
    match_announcements_enabled: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "win5_announcement_channel_id",
            _snowflake(
                self.win5_announcement_channel_id,
                field_name="win5_announcement_channel_id",
                optional=True,
            ),
        )
        object.__setattr__(
            self,
            "match_announcement_channel_id",
            _snowflake(
                self.match_announcement_channel_id,
                field_name="match_announcement_channel_id",
                optional=True,
            ),
        )
        object.__setattr__(
            self,
            "log_channel_id",
            _snowflake(self.log_channel_id, field_name="log_channel_id", optional=True),
        )
        timezone = _text(self.default_timezone, field_name="default_timezone", max_length=32)
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as error:
            raise DiscordGuildSettingsUpdateError("default_timezone must be an available IANA timezone.") from error
        object.__setattr__(self, "default_timezone", timezone)
        if not isinstance(self.win5_announcements_enabled, bool):
            raise DiscordGuildSettingsUpdateError("win5_announcements_enabled must be a boolean.")
        if not isinstance(self.match_announcements_enabled, bool):
            raise DiscordGuildSettingsUpdateError("match_announcements_enabled must be a boolean.")

    def to_payload(self) -> dict[str, object]:
        return {field_name: getattr(self, field_name) for field_name in _EDITABLE_FIELDS}

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> DiscordGuildEditableSettings:
        if set(payload) != set(_EDITABLE_FIELDS):
            raise DiscordGuildSettingsUpdateAuditError("Stored editable settings keys are malformed.")
        try:
            return cls(**{field_name: payload[field_name] for field_name in _EDITABLE_FIELDS})  # type: ignore[arg-type]
        except DiscordGuildSettingsUpdateError as error:
            raise DiscordGuildSettingsUpdateAuditError("Stored editable settings are malformed.") from error

    def changed_fields(self, desired: DiscordGuildEditableSettings) -> tuple[str, ...]:
        if not isinstance(desired, DiscordGuildEditableSettings):
            raise DiscordGuildSettingsUpdateError("desired settings must be complete.")
        return tuple(
            field_name for field_name in _EDITABLE_FIELDS if getattr(self, field_name) != getattr(desired, field_name)
        )


@dataclass(frozen=True, slots=True)
class DiscordGuildSettingsUpdateState:
    """Complete locked/query authority for one existing settings row."""

    guild_id: str
    editable: DiscordGuildEditableSettings
    operator_role_id: str | None
    bot_manager_role_id: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        guild_id = _snowflake(self.guild_id, field_name="guild_id")
        if not isinstance(self.editable, DiscordGuildEditableSettings):
            raise DiscordGuildSettingsUpdateError("editable settings must be complete.")
        operator_role_id = _snowflake(self.operator_role_id, field_name="operator_role_id", optional=True)
        bot_manager_role_id = _snowflake(self.bot_manager_role_id, field_name="bot_manager_role_id", optional=True)
        if operator_role_id is None and bot_manager_role_id is None:
            raise DiscordGuildSettingsUpdateError("At least one staff Role ID must remain configured.")
        if guild_id in {operator_role_id, bot_manager_role_id}:
            raise DiscordGuildSettingsUpdateError("A staff Role ID must not use the guild @everyone Role ID.")
        object.__setattr__(self, "guild_id", guild_id)
        object.__setattr__(self, "operator_role_id", operator_role_id)
        object.__setattr__(self, "bot_manager_role_id", bot_manager_role_id)
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
        if self.updated_at < self.created_at:
            raise DiscordGuildSettingsUpdateError("updated_at must not precede created_at.")

    def to_payload(self) -> dict[str, object]:
        return {
            "guild_id": self.guild_id,
            "editable": self.editable.to_payload(),
            "operator_role_id": self.operator_role_id,
            "bot_manager_role_id": self.bot_manager_role_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> DiscordGuildSettingsUpdateState:
        expected = {
            "guild_id",
            "editable",
            "operator_role_id",
            "bot_manager_role_id",
            "created_at",
            "updated_at",
        }
        if set(payload) != expected:
            raise DiscordGuildSettingsUpdateAuditError("Stored settings state keys are malformed.")
        editable = payload["editable"]
        created_at = payload["created_at"]
        updated_at = payload["updated_at"]
        if not isinstance(editable, Mapping) or not isinstance(created_at, str) or not isinstance(updated_at, str):
            raise DiscordGuildSettingsUpdateAuditError("Stored settings state values are malformed.")
        try:
            return cls(
                guild_id=payload["guild_id"],  # type: ignore[arg-type]
                editable=DiscordGuildEditableSettings.from_payload(editable),
                operator_role_id=payload["operator_role_id"],  # type: ignore[arg-type]
                bot_manager_role_id=payload["bot_manager_role_id"],  # type: ignore[arg-type]
                created_at=datetime.fromisoformat(created_at),
                updated_at=datetime.fromisoformat(updated_at),
            )
        except DiscordGuildSettingsUpdateAuditError:
            raise
        except (TypeError, ValueError) as error:
            raise DiscordGuildSettingsUpdateAuditError("Stored settings state is malformed.") from error

    @property
    def state_fingerprint(self) -> str:
        return _fingerprint(
            {
                "schema": "discord-guild-settings-state-v1",
                "guild_id": self.guild_id,
                "editable": self.editable.to_payload(),
                "operator_role_id": self.operator_role_id,
                "bot_manager_role_id": self.bot_manager_role_id,
                "created_at": self.created_at.isoformat(),
            }
        )


@dataclass(frozen=True, slots=True)
class DiscordGuildSettingsUpdatePreview:
    """Fresh zero-write before/desired consequence Preview."""

    before: DiscordGuildSettingsUpdateState
    desired: DiscordGuildEditableSettings
    reason: str
    changed_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.before, DiscordGuildSettingsUpdateState):
            raise DiscordGuildSettingsUpdateError("before state must be complete.")
        if not isinstance(self.desired, DiscordGuildEditableSettings):
            raise DiscordGuildSettingsUpdateError("desired settings must be complete.")
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255))
        expected = self.before.editable.changed_fields(self.desired)
        if not expected:
            raise DiscordGuildSettingsUpdateNoChangeError("Desired settings are already current.")
        if tuple(self.changed_fields) != expected:
            raise DiscordGuildSettingsUpdateError("changed_fields do not match the complete desired set.")


@dataclass(frozen=True, slots=True)
class UpdateDiscordGuildSettings:
    """One Final interaction updating the complete editable set."""

    guild_id: str
    desired: DiscordGuildEditableSettings
    reason: str
    expected_state_fingerprint: str
    actor_discord_user_id: str
    idempotency_key: str
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "guild_id", _snowflake(self.guild_id, field_name="guild_id"))
        if not isinstance(self.desired, DiscordGuildEditableSettings):
            raise DiscordGuildSettingsUpdateError("desired settings must be complete.")
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255))
        fingerprint = _text(
            self.expected_state_fingerprint,
            field_name="expected_state_fingerprint",
            max_length=64,
        )
        if len(fingerprint) != 64 or any(character not in "0123456789abcdef" for character in fingerprint):
            raise DiscordGuildSettingsUpdateError("expected_state_fingerprint must be a SHA-256 digest.")
        object.__setattr__(self, "expected_state_fingerprint", fingerprint)
        object.__setattr__(
            self,
            "actor_discord_user_id",
            _snowflake(self.actor_discord_user_id, field_name="actor_discord_user_id"),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            _text(self.idempotency_key, field_name="idempotency_key", max_length=128),
        )
        if self.correlation_id is not None:
            object.__setattr__(
                self,
                "correlation_id",
                _text(self.correlation_id, field_name="correlation_id", max_length=128),
            )

    @property
    def request_fingerprint(self) -> str:
        return _fingerprint(
            {
                "schema": "discord-guild-settings-update-command-v1",
                "guild_id": self.guild_id,
                "desired": self.desired.to_payload(),
                "reason": self.reason,
                "expected_state_fingerprint": self.expected_state_fingerprint,
                "actor_discord_user_id": self.actor_discord_user_id,
            }
        )


@dataclass(frozen=True, slots=True)
class UpdatedDiscordGuildSettings:
    """Committed update audit receipt."""

    operation_id: int
    before: DiscordGuildSettingsUpdateState
    after: DiscordGuildSettingsUpdateState
    changed_fields: tuple[str, ...]
    reason: str
    exact_retry: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.operation_id, bool) or not isinstance(self.operation_id, int) or self.operation_id <= 0:
            raise DiscordGuildSettingsUpdateAuditError("operation_id must be a positive integer.")
        if not isinstance(self.before, DiscordGuildSettingsUpdateState) or not isinstance(
            self.after, DiscordGuildSettingsUpdateState
        ):
            raise DiscordGuildSettingsUpdateAuditError("Update receipt states are malformed.")
        if self.before.guild_id != self.after.guild_id:
            raise DiscordGuildSettingsUpdateAuditError("Update receipt guild identity changed.")
        if (
            self.before.operator_role_id != self.after.operator_role_id
            or self.before.bot_manager_role_id != self.after.bot_manager_role_id
            or self.before.created_at != self.after.created_at
        ):
            raise DiscordGuildSettingsUpdateAuditError("Immutable settings authority changed.")
        expected = self.before.editable.changed_fields(self.after.editable)
        if not expected or tuple(self.changed_fields) != expected:
            raise DiscordGuildSettingsUpdateAuditError("Update receipt changed_fields are malformed.")
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255))
        if not isinstance(self.exact_retry, bool):
            raise DiscordGuildSettingsUpdateAuditError("exact_retry must be a boolean.")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": DISCORD_GUILD_SETTINGS_UPDATE_AUDIT_SCHEMA_VERSION,
            "before": self.before.to_payload(),
            "after": self.after.to_payload(),
            "changed_fields": list(self.changed_fields),
            "reason": self.reason,
        }

    @classmethod
    def from_payload(
        cls,
        *,
        operation_id: int,
        payload: Mapping[str, object],
    ) -> UpdatedDiscordGuildSettings:
        expected = {"schema_version", "before", "after", "changed_fields", "reason"}
        if (
            set(payload) != expected
            or payload.get("schema_version") != DISCORD_GUILD_SETTINGS_UPDATE_AUDIT_SCHEMA_VERSION
        ):
            raise DiscordGuildSettingsUpdateAuditError("Stored settings update receipt keys are malformed.")
        before = payload["before"]
        after = payload["after"]
        changed_fields = payload["changed_fields"]
        if (
            not isinstance(before, Mapping)
            or not isinstance(after, Mapping)
            or not isinstance(changed_fields, list)
            or not all(isinstance(field_name, str) for field_name in changed_fields)
        ):
            raise DiscordGuildSettingsUpdateAuditError("Stored settings update receipt values are malformed.")
        try:
            return cls(
                operation_id=operation_id,
                before=DiscordGuildSettingsUpdateState.from_payload(before),
                after=DiscordGuildSettingsUpdateState.from_payload(after),
                changed_fields=tuple(changed_fields),
                reason=payload["reason"],  # type: ignore[arg-type]
            )
        except DiscordGuildSettingsUpdateAuditError:
            raise
        except (TypeError, ValueError) as error:
            raise DiscordGuildSettingsUpdateAuditError("Stored settings update receipt is malformed.") from error


@dataclass(frozen=True, slots=True)
class StoredDiscordGuildSettingsUpdateOperation:
    operation_id: int
    request_fingerprint: str | None
    operation_type: str | None
    guild_id: str | None
    after_data: object


class DiscordGuildSettingsUpdateQueryRepository(Protocol):
    def get_state(self, *, guild_id: str) -> DiscordGuildSettingsUpdateState | None: ...


class DiscordGuildSettingsUpdateQueryUnitOfWork(UnitOfWork, Protocol):
    @property
    def discord_guild_settings_update(self) -> DiscordGuildSettingsUpdateQueryRepository: ...


@dataclass(frozen=True, slots=True)
class DiscordGuildSettingsUpdateQueries:
    """Fresh read-only state and Preview entry point."""

    query_runner: QueryRunner[DiscordGuildSettingsUpdateQueryUnitOfWork]

    def get_state(self, *, guild_id: str) -> DiscordGuildSettingsUpdateState:
        normalized_guild = _snowflake(guild_id, field_name="guild_id")
        assert normalized_guild is not None
        try:
            state = self.query_runner.run(
                lambda unit_of_work: unit_of_work.discord_guild_settings_update.get_state(guild_id=normalized_guild)
            )
        except DiscordGuildSettingsUpdateError:
            raise
        except (TypeError, ValueError) as error:
            raise DiscordGuildSettingsUpdateInvalidSourceError(
                "Stored Discord guild settings are malformed."
            ) from error
        if state is None:
            raise DiscordGuildSettingsUpdateUnavailableError("Discord guild settings do not exist.")
        if state.guild_id != normalized_guild:
            raise DiscordGuildSettingsUpdateInvalidSourceError("Discord guild settings identity is malformed.")
        return state

    def get_preview(
        self,
        *,
        guild_id: str,
        desired: DiscordGuildEditableSettings,
        reason: str,
        expected_state_fingerprint: str,
    ) -> DiscordGuildSettingsUpdatePreview:
        state = self.get_state(guild_id=guild_id)
        if state.state_fingerprint != expected_state_fingerprint:
            raise DiscordGuildSettingsUpdateStaleError("Discord guild settings changed after the editor opened.")
        return DiscordGuildSettingsUpdatePreview(
            before=state,
            desired=desired,
            reason=reason,
            changed_fields=state.editable.changed_fields(desired),
        )


class DiscordGuildSettingsUpdateRepository(Protocol):
    def lock_state(self, *, guild_id: str) -> DiscordGuildSettingsUpdateState | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredDiscordGuildSettingsUpdateOperation | None: ...

    def update_settings(
        self,
        *,
        before: DiscordGuildSettingsUpdateState,
        desired: DiscordGuildEditableSettings,
        updated_at: datetime,
    ) -> DiscordGuildSettingsUpdateState: ...

    def add_audit(
        self,
        *,
        command: UpdateDiscordGuildSettings,
        before: DiscordGuildSettingsUpdateState,
        after: DiscordGuildSettingsUpdateState,
        changed_fields: tuple[str, ...],
        created_at: datetime,
    ) -> int: ...


class DiscordGuildSettingsUpdateUnitOfWork(UnitOfWork, Protocol):
    @property
    def discord_guild_settings_update(self) -> DiscordGuildSettingsUpdateRepository: ...


@dataclass(frozen=True, slots=True)
class DiscordGuildSettingsUpdateCommands:
    """Apply one complete existing-row settings update in a fresh UoW."""

    command_runner: CommandRunner[DiscordGuildSettingsUpdateUnitOfWork]
    clock: Callable[[], datetime]

    def update(self, command: UpdateDiscordGuildSettings) -> UpdatedDiscordGuildSettings:
        return self.command_runner.run(
            lambda unit_of_work: self._update(unit_of_work.discord_guild_settings_update, command)
        )

    def _update(
        self,
        repository: DiscordGuildSettingsUpdateRepository,
        command: UpdateDiscordGuildSettings,
    ) -> UpdatedDiscordGuildSettings:
        before = repository.lock_state(guild_id=command.guild_id)
        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_retry(stored=stored, command=command)
        if before is None:
            raise DiscordGuildSettingsUpdateUnavailableError("Discord guild settings do not exist.")
        if before.state_fingerprint != command.expected_state_fingerprint:
            raise DiscordGuildSettingsUpdateStaleError("Discord guild settings changed after Preview.")
        changed_fields = before.editable.changed_fields(command.desired)
        if not changed_fields:
            raise DiscordGuildSettingsUpdateNoChangeError("Desired settings are already current.")
        updated_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        after = repository.update_settings(
            before=before,
            desired=command.desired,
            updated_at=updated_at,
        )
        operation_id = repository.add_audit(
            command=command,
            before=before,
            after=after,
            changed_fields=changed_fields,
            created_at=updated_at,
        )
        return UpdatedDiscordGuildSettings(
            operation_id=operation_id,
            before=before,
            after=after,
            changed_fields=changed_fields,
            reason=command.reason,
        )

    @staticmethod
    def _resolve_retry(
        *,
        stored: StoredDiscordGuildSettingsUpdateOperation,
        command: UpdateDiscordGuildSettings,
    ) -> UpdatedDiscordGuildSettings:
        if stored.request_fingerprint != command.request_fingerprint:
            raise DiscordGuildSettingsUpdateIdempotencyConflictError(
                "Idempotency key is already bound to another settings request."
            )
        if (
            stored.operation_type != DISCORD_GUILD_SETTINGS_UPDATE_OPERATION_TYPE
            or stored.guild_id != command.guild_id
            or not isinstance(stored.after_data, Mapping)
        ):
            raise DiscordGuildSettingsUpdateAuditError("Stored settings update evidence is malformed.")
        result = UpdatedDiscordGuildSettings.from_payload(
            operation_id=stored.operation_id,
            payload=stored.after_data,
        )
        if (
            result.before.state_fingerprint != command.expected_state_fingerprint
            or result.after.editable != command.desired
            or result.reason != command.reason
        ):
            raise DiscordGuildSettingsUpdateAuditError("Stored settings update context is malformed.")
        return replace(result, exact_retry=True)
