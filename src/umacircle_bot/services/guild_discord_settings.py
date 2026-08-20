from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from umacircle_bot.db.models import GuildDiscordSettings, GuildDiscordSettingsAudit
from umacircle_bot.domain.errors import (
    GuildDiscordSettingsConflictError,
    GuildDiscordSettingsError,
)
from umacircle_bot.domain.guild_discord_settings import (
    DEFAULT_GUILD_DISCORD_SETTINGS,
    GuildDiscordSettingsValues,
    normalize_discord_snowflake,
    normalize_guild_discord_settings,
)
from umacircle_bot.domain.time import database_datetime_as_utc

SETTINGS_UPDATE_ACTION = "guild_discord_settings_update"


@dataclass(frozen=True, slots=True)
class UpdateGuildDiscordSettingsCommand:
    guild_id: str
    expected_revision: int
    values: GuildDiscordSettingsValues
    actor_discord_user_id: str
    idempotency_key: str
    reason: str


@dataclass(frozen=True, slots=True)
class BindProvisionedChannelCommand:
    guild_id: str
    channel_kind: str
    provisioned_channel_id: str
    actor_discord_user_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class GuildDiscordSettingsDTO:
    id: int | None
    guild_id: str
    revision_number: int
    win5_announcement_channel_id: str | None
    room_match_announcement_channel_id: str | None
    log_channel_id: str | None
    operator_role_id: str | None
    bot_manager_role_id: str | None
    default_timezone: str
    win5_announcements_enabled: bool
    room_match_announcements_enabled: bool
    is_persisted: bool
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class GuildDiscordSettingsMutationDTO:
    action: str
    audit_id: int
    settings: GuildDiscordSettingsDTO


def get_guild_discord_settings(
    session: Session,
    *,
    guild_id: str,
) -> GuildDiscordSettingsDTO:
    normalized_guild_id = normalize_discord_snowflake(guild_id, field="guild ID")
    settings = _load_settings(session, normalized_guild_id, lock=False)
    if settings is None:
        return _default_dto(normalized_guild_id)
    return _settings_dto(settings)


def bootstrap_guild_discord_settings(
    session: Session,
    *,
    command: UpdateGuildDiscordSettingsCommand,
) -> GuildDiscordSettingsMutationDTO:
    if command.expected_revision != 0:
        raise GuildDiscordSettingsError("bootstrap requires expected revision 0")
    return update_guild_discord_settings(session, command=command)


def update_guild_discord_settings(
    session: Session,
    *,
    command: UpdateGuildDiscordSettingsCommand,
) -> GuildDiscordSettingsMutationDTO:
    guild_id = normalize_discord_snowflake(command.guild_id, field="guild ID")
    expected_revision = _nonnegative_int(command.expected_revision, field="expected revision")
    values = normalize_guild_discord_settings(command.values)
    _validate_role_ids_for_guild(guild_id, values)
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = _text(command.reason, field="reason", maximum=255)
    fingerprint = _fingerprint(
        {
            "action": SETTINGS_UPDATE_ACTION,
            "guild_id": guild_id,
            "expected_revision": expected_revision,
            "values": asdict(values),
            "actor": actor,
            "reason": reason,
        }
    )

    _ensure_application_transaction(session)
    try:
        with session.begin_nested():
            audit = _load_audit(session, request_key)
            if audit is not None:
                return _idempotent_result(audit, fingerprint=fingerprint)

            settings = _load_settings(session, guild_id, lock=False)
            created = False
            if settings is None:
                created = _insert_settings_if_absent(
                    session,
                    guild_id=guild_id,
                    revision_number=1,
                    values=values,
                )
            settings = _load_settings(session, guild_id, lock=True)
            if settings is None:
                raise GuildDiscordSettingsConflictError("guild Discord settings bootstrap did not create a durable row")
            audit = _load_audit(session, request_key, lock=True)
            if audit is not None:
                return _idempotent_result(audit, fingerprint=fingerprint)

            actual_revision = 0 if created else settings.revision_number
            if expected_revision != actual_revision:
                raise GuildDiscordSettingsConflictError(
                    f"guild Discord settings revision changed; expected {expected_revision}, current {actual_revision}"
                )
            before = _settings_json(_default_dto(guild_id) if created else _settings_dto(settings))
            if not created:
                settings.win5_announcement_channel_id = values.win5_announcement_channel_id
                settings.room_match_announcement_channel_id = values.room_match_announcement_channel_id
                settings.log_channel_id = values.log_channel_id
                settings.operator_role_id = values.operator_role_id
                settings.bot_manager_role_id = values.bot_manager_role_id
                settings.default_timezone = values.default_timezone
                settings.win5_announcements_enabled = values.win5_announcements_enabled
                settings.room_match_announcements_enabled = values.room_match_announcements_enabled
                settings.revision_number += 1
            session.flush()
            settings = _require_locked_settings(session, guild_id)
            after = _settings_json(_settings_dto(settings))
            audit = GuildDiscordSettingsAudit(
                guild_discord_settings_id=settings.id,
                guild_id=guild_id,
                action=SETTINGS_UPDATE_ACTION,
                actor_discord_user_id=actor,
                idempotency_key=request_key,
                request_fingerprint=fingerprint,
                before_json=before,
                after_json=after,
                reason=reason,
            )
            session.add(audit)
            session.flush()
            return _result_from_audit(audit)
    except IntegrityError as exc:
        return _recover_concurrent_retry(
            session,
            error=exc,
            request_key=request_key,
            fingerprint=fingerprint,
        )


def bind_provisioned_channel_if_unset(
    session: Session,
    *,
    command: BindProvisionedChannelCommand,
) -> GuildDiscordSettingsMutationDTO:
    guild_id = normalize_discord_snowflake(command.guild_id, field="guild ID")
    channel_id = normalize_discord_snowflake(
        command.provisioned_channel_id,
        field="provisioned channel ID",
    )
    field_name = {
        "win5_announcement": "win5_announcement_channel_id",
        "room_match_announcement": "room_match_announcement_channel_id",
        "log_mirror": "log_channel_id",
    }.get(command.channel_kind)
    if field_name is None:
        raise GuildDiscordSettingsError("managed channel kind is invalid")
    actor = _text(command.actor_discord_user_id, field="actor ID", maximum=32)
    request_key = _text(command.idempotency_key, field="idempotency key", maximum=128)
    reason = "automatic managed channel provisioning"
    fingerprint = _fingerprint(
        {
            "action": SETTINGS_UPDATE_ACTION,
            "operation": "bind_provisioned_channel_if_unset",
            "guild_id": guild_id,
            "channel_kind": command.channel_kind,
            "channel_id": channel_id,
            "actor": actor,
            "reason": reason,
        }
    )
    _ensure_application_transaction(session)
    try:
        with session.begin_nested():
            audit = _load_audit(session, request_key)
            if audit is not None:
                return _idempotent_result(audit, fingerprint=fingerprint)

            values = asdict(DEFAULT_GUILD_DISCORD_SETTINGS)
            values[field_name] = channel_id
            settings = _load_settings(session, guild_id, lock=False)
            created = False
            if settings is None:
                created = _insert_settings_if_absent(
                    session,
                    guild_id=guild_id,
                    revision_number=1,
                    values=GuildDiscordSettingsValues(**values),
                )
            settings = _load_settings(session, guild_id, lock=True)
            if settings is None:
                raise GuildDiscordSettingsConflictError(
                    "managed channel bootstrap did not create a durable settings row"
                )
            audit = _load_audit(session, request_key, lock=True)
            if audit is not None:
                return _idempotent_result(audit, fingerprint=fingerprint)

            before = _settings_json(_default_dto(guild_id) if created else _settings_dto(settings))
            if not created and getattr(settings, field_name) is None:
                setattr(settings, field_name, channel_id)
                settings.revision_number += 1
            session.flush()
            settings = _require_locked_settings(session, guild_id)
            audit = GuildDiscordSettingsAudit(
                guild_discord_settings_id=settings.id,
                guild_id=guild_id,
                action=SETTINGS_UPDATE_ACTION,
                actor_discord_user_id=actor,
                idempotency_key=request_key,
                request_fingerprint=fingerprint,
                before_json=before,
                after_json=_settings_json(_settings_dto(settings)),
                reason=reason,
            )
            session.add(audit)
            session.flush()
            return _result_from_audit(audit)
    except IntegrityError as exc:
        with session.begin_nested():
            audit = _load_audit(session, request_key, lock=True)
            if audit is not None:
                return _idempotent_result(audit, fingerprint=fingerprint)
            settings = _load_settings(session, guild_id, lock=True)
            if settings is None:
                raise GuildDiscordSettingsConflictError("concurrent managed channel binding conflicted") from exc
            snapshot = _settings_json(_settings_dto(settings))
            audit = GuildDiscordSettingsAudit(
                guild_discord_settings_id=settings.id,
                guild_id=guild_id,
                action=SETTINGS_UPDATE_ACTION,
                actor_discord_user_id=actor,
                idempotency_key=request_key,
                request_fingerprint=fingerprint,
                before_json=snapshot,
                after_json=snapshot,
                reason=reason,
            )
            session.add(audit)
            session.flush()
            return _result_from_audit(audit)


def _recover_concurrent_retry(
    session: Session,
    *,
    error: IntegrityError,
    request_key: str,
    fingerprint: str,
) -> GuildDiscordSettingsMutationDTO:
    with session.begin_nested():
        audit = _load_audit(session, request_key, lock=True)
        if audit is None:
            raise GuildDiscordSettingsConflictError("concurrent guild Discord settings update conflicted") from error
        return _idempotent_result(audit, fingerprint=fingerprint)


def _idempotent_result(
    audit: GuildDiscordSettingsAudit,
    *,
    fingerprint: str,
) -> GuildDiscordSettingsMutationDTO:
    if audit.action != SETTINGS_UPDATE_ACTION or audit.request_fingerprint != fingerprint:
        raise GuildDiscordSettingsConflictError(
            "idempotency key payload does not match the original guild Discord settings update"
        )
    return _result_from_audit(audit)


def _load_settings(
    session: Session,
    guild_id: str,
    *,
    lock: bool,
) -> GuildDiscordSettings | None:
    query = (
        select(GuildDiscordSettings).where(GuildDiscordSettings.guild_id == guild_id).order_by(GuildDiscordSettings.id)
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    settings = session.scalar(query)
    if settings is not None:
        _validate_settings_row(settings)
    return settings


def _load_audit(
    session: Session,
    request_key: str,
    *,
    lock: bool = False,
) -> GuildDiscordSettingsAudit | None:
    query = select(GuildDiscordSettingsAudit).where(GuildDiscordSettingsAudit.idempotency_key == request_key)
    if lock:
        query = query.with_for_update()
    return session.scalar(query)


def _insert_settings_if_absent(
    session: Session,
    *,
    guild_id: str,
    revision_number: int,
    values: GuildDiscordSettingsValues,
) -> bool:
    insert_values = {
        "guild_id": guild_id,
        "revision_number": revision_number,
        **asdict(values),
    }
    dialect_name = session.get_bind().dialect.name
    if dialect_name in {"mysql", "mariadb"}:
        session.scalar(select(func.last_insert_id(0)))
        statement = mysql_insert(GuildDiscordSettings).values(**insert_values)
        session.execute(
            statement.on_duplicate_key_update(
                id=GuildDiscordSettings.id + func.last_insert_id(0),
            )
        )
        return bool(session.scalar(select(func.last_insert_id())))
    if dialect_name == "sqlite":
        result = session.execute(
            sqlite_insert(GuildDiscordSettings)
            .values(**insert_values)
            .on_conflict_do_nothing(index_elements=["guild_id"])
        )
        return result.rowcount == 1

    settings = GuildDiscordSettings(**insert_values)
    session.add(settings)
    session.flush()
    return True


def _require_locked_settings(
    session: Session,
    guild_id: str,
) -> GuildDiscordSettings:
    settings = _load_settings(session, guild_id, lock=True)
    if settings is None:
        raise GuildDiscordSettingsConflictError("guild Discord settings row disappeared during mutation")
    return settings


def _validate_settings_row(settings: GuildDiscordSettings) -> None:
    if settings.revision_number <= 0:
        raise GuildDiscordSettingsConflictError("persisted guild Discord settings revision is invalid")
    normalized = normalize_guild_discord_settings(
        GuildDiscordSettingsValues(
            win5_announcement_channel_id=settings.win5_announcement_channel_id,
            room_match_announcement_channel_id=settings.room_match_announcement_channel_id,
            log_channel_id=settings.log_channel_id,
            operator_role_id=settings.operator_role_id,
            bot_manager_role_id=settings.bot_manager_role_id,
            default_timezone=settings.default_timezone,
            win5_announcements_enabled=settings.win5_announcements_enabled,
            room_match_announcements_enabled=settings.room_match_announcements_enabled,
        )
    )
    if asdict(normalized) != {
        "win5_announcement_channel_id": settings.win5_announcement_channel_id,
        "room_match_announcement_channel_id": settings.room_match_announcement_channel_id,
        "log_channel_id": settings.log_channel_id,
        "operator_role_id": settings.operator_role_id,
        "bot_manager_role_id": settings.bot_manager_role_id,
        "default_timezone": settings.default_timezone,
        "win5_announcements_enabled": settings.win5_announcements_enabled,
        "room_match_announcements_enabled": settings.room_match_announcements_enabled,
    }:
        raise GuildDiscordSettingsConflictError("persisted guild Discord settings are not normalized")


def _settings_dto(settings: GuildDiscordSettings) -> GuildDiscordSettingsDTO:
    _validate_settings_row(settings)
    return GuildDiscordSettingsDTO(
        id=settings.id,
        guild_id=settings.guild_id,
        revision_number=settings.revision_number,
        win5_announcement_channel_id=settings.win5_announcement_channel_id,
        room_match_announcement_channel_id=settings.room_match_announcement_channel_id,
        log_channel_id=settings.log_channel_id,
        operator_role_id=settings.operator_role_id,
        bot_manager_role_id=settings.bot_manager_role_id,
        default_timezone=settings.default_timezone,
        win5_announcements_enabled=settings.win5_announcements_enabled,
        room_match_announcements_enabled=settings.room_match_announcements_enabled,
        is_persisted=True,
        created_at=database_datetime_as_utc(settings.created_at),
        updated_at=database_datetime_as_utc(settings.updated_at),
    )


def _default_dto(guild_id: str) -> GuildDiscordSettingsDTO:
    return GuildDiscordSettingsDTO(
        id=None,
        guild_id=guild_id,
        revision_number=0,
        **asdict(DEFAULT_GUILD_DISCORD_SETTINGS),
        is_persisted=False,
        created_at=None,
        updated_at=None,
    )


def _settings_json(settings: GuildDiscordSettingsDTO) -> dict[str, object]:
    return {
        "id": settings.id,
        "guild_id": settings.guild_id,
        "revision_number": settings.revision_number,
        "win5_announcement_channel_id": settings.win5_announcement_channel_id,
        "room_match_announcement_channel_id": settings.room_match_announcement_channel_id,
        "log_channel_id": settings.log_channel_id,
        "operator_role_id": settings.operator_role_id,
        "bot_manager_role_id": settings.bot_manager_role_id,
        "default_timezone": settings.default_timezone,
        "win5_announcements_enabled": settings.win5_announcements_enabled,
        "room_match_announcements_enabled": settings.room_match_announcements_enabled,
        "is_persisted": settings.is_persisted,
        "created_at": _datetime_json(settings.created_at),
        "updated_at": _datetime_json(settings.updated_at),
    }


def _result_from_audit(audit: GuildDiscordSettingsAudit) -> GuildDiscordSettingsMutationDTO:
    return GuildDiscordSettingsMutationDTO(
        action=audit.action,
        audit_id=audit.id,
        settings=_settings_from_json(audit.after_json),
    )


def _settings_from_json(value: object) -> GuildDiscordSettingsDTO:
    if not isinstance(value, dict):
        raise GuildDiscordSettingsConflictError("stored guild Discord settings audit snapshot is invalid")
    persisted = value.get("is_persisted")
    if persisted is not True:
        raise GuildDiscordSettingsConflictError("stored guild Discord settings audit snapshot is invalid")
    return GuildDiscordSettingsDTO(
        id=_json_positive_int(value.get("id"), field="settings ID"),
        guild_id=normalize_discord_snowflake(_json_text(value.get("guild_id")), field="guild ID"),
        revision_number=_json_positive_int(value.get("revision_number"), field="revision"),
        win5_announcement_channel_id=_json_optional_snowflake(
            value.get("win5_announcement_channel_id"),
            field="WIN5 announcement channel ID",
        ),
        room_match_announcement_channel_id=_json_optional_snowflake(
            value.get("room_match_announcement_channel_id"),
            field="Room Match announcement channel ID",
        ),
        log_channel_id=_json_optional_snowflake(value.get("log_channel_id"), field="log channel ID"),
        operator_role_id=_json_optional_snowflake(value.get("operator_role_id"), field="운영자 역할 ID"),
        bot_manager_role_id=_json_optional_snowflake(value.get("bot_manager_role_id"), field="봇 관리 역할 ID"),
        default_timezone=_json_timezone(value.get("default_timezone")),
        win5_announcements_enabled=_json_boolean(value.get("win5_announcements_enabled")),
        room_match_announcements_enabled=_json_boolean(value.get("room_match_announcements_enabled")),
        is_persisted=True,
        created_at=_datetime_from_json(value.get("created_at")),
        updated_at=_datetime_from_json(value.get("updated_at")),
    )


def _ensure_application_transaction(session: Session) -> None:
    if not session.in_transaction():
        session.begin()
    connection = session.connection()
    if connection.dialect.name != "sqlite":
        return
    driver_connection = connection.connection.driver_connection
    if not driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")


def _fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _text(value: str, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise GuildDiscordSettingsError(f"{field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or not normalized.isprintable():
        raise GuildDiscordSettingsError(f"{field} must contain 1 to {maximum} printable characters")
    return normalized


def _nonnegative_int(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise GuildDiscordSettingsError(f"{field} must be a nonnegative integer")
    return value


def _validate_role_ids_for_guild(guild_id: str, values: GuildDiscordSettingsValues) -> None:
    if guild_id in {values.operator_role_id, values.bot_manager_role_id}:
        raise GuildDiscordSettingsError("`@everyone` 역할은 운영 권한으로 지정할 수 없습니다.")


def _json_text(value: object) -> str:
    if not isinstance(value, str):
        raise GuildDiscordSettingsConflictError("stored guild Discord settings audit snapshot is invalid")
    return value


def _json_positive_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise GuildDiscordSettingsConflictError(f"stored guild Discord settings {field} is invalid")
    return value


def _json_optional_snowflake(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return normalize_discord_snowflake(_json_text(value), field=field)


def _json_timezone(value: object) -> str:
    return normalize_guild_discord_settings(
        GuildDiscordSettingsValues(default_timezone=_json_text(value))
    ).default_timezone


def _json_boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise GuildDiscordSettingsConflictError("stored guild Discord settings boolean is invalid")
    return value


def _datetime_json(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _datetime_from_json(value: object) -> datetime:
    if not isinstance(value, str):
        raise GuildDiscordSettingsConflictError("stored guild Discord settings timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise GuildDiscordSettingsConflictError("stored guild Discord settings timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GuildDiscordSettingsConflictError("stored guild Discord settings timestamp is invalid")
    return parsed.astimezone(UTC)
