"""SQLAlchemy persistence for audited runtime Discord settings updates."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from uma_st2.application.discord import (
    DISCORD_GUILD_SETTINGS_UPDATE_OPERATION_TYPE,
    DiscordGuildEditableSettings,
    DiscordGuildSettingsUpdateAuditError,
    DiscordGuildSettingsUpdateConcurrentConflictError,
    DiscordGuildSettingsUpdateIdempotencyConflictError,
    DiscordGuildSettingsUpdateState,
    StoredDiscordGuildSettingsUpdateOperation,
    UpdatedDiscordGuildSettings,
    UpdateDiscordGuildSettings,
)

from .datetime_codec import from_database_utc, to_database_utc
from .orm import BotGuildSettingORM, OperationORM, SettingsOperationORM
from .registration_bootstrap import is_mariadb_deadlock, sqlite_next_id
from .uow import SessionFactory, SqlAlchemyFeatureUnitOfWork, SqlAlchemyFeatureUnitOfWorkFactory


def _state(setting: BotGuildSettingORM) -> DiscordGuildSettingsUpdateState:
    return DiscordGuildSettingsUpdateState(
        guild_id=setting.guild_id,
        editable=DiscordGuildEditableSettings(
            win5_announcement_channel_id=setting.win5_announcement_channel_id,
            match_announcement_channel_id=setting.match_announcement_channel_id,
            log_channel_id=setting.log_channel_id,
            default_timezone=setting.default_timezone,
            win5_announcements_enabled=setting.win5_announcements_enabled,
            match_announcements_enabled=setting.match_announcements_enabled,
        ),
        operator_role_id=setting.operator_role_id,
        bot_manager_role_id=setting.bot_manager_role_id,
        created_at=from_database_utc(setting.created_at, field_name="bot_guild_settings.created_at"),
        updated_at=from_database_utc(setting.updated_at, field_name="bot_guild_settings.updated_at"),
    )


class SqlAlchemyDiscordGuildSettingsUpdateQueryRepository:
    """Return one detached complete settings editor state."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_state(self, *, guild_id: str) -> DiscordGuildSettingsUpdateState | None:
        setting = self._session.get(BotGuildSettingORM, guild_id)
        return None if setting is None else _state(setting)


class SqlAlchemyDiscordGuildSettingsUpdateRepository:
    """Lock, update, and audit one existing guild settings row."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_state(self, *, guild_id: str) -> DiscordGuildSettingsUpdateState | None:
        try:
            setting = self._session.scalar(
                select(BotGuildSettingORM).where(BotGuildSettingORM.guild_id == guild_id).with_for_update()
            )
        except OperationalError as error:
            self._raise_concurrent(error)
            raise
        return None if setting is None else _state(setting)

    def find_operation(self, *, idempotency_key: str) -> StoredDiscordGuildSettingsUpdateOperation | None:
        try:
            row = self._session.execute(
                select(
                    OperationORM.id,
                    OperationORM.request_fingerprint,
                    SettingsOperationORM.type,
                    SettingsOperationORM.guild_id,
                    SettingsOperationORM.after_data,
                )
                .outerjoin(SettingsOperationORM, SettingsOperationORM.operation_id == OperationORM.id)
                .where(OperationORM.idempotency_key == idempotency_key)
                .with_for_update()
            ).one_or_none()
        except OperationalError as error:
            self._raise_concurrent(error)
            raise
        if row is None:
            return None
        return StoredDiscordGuildSettingsUpdateOperation(
            operation_id=row.id,
            request_fingerprint=row.request_fingerprint,
            operation_type=row.type,
            guild_id=row.guild_id,
            after_data=row.after_data,
        )

    def update_settings(
        self,
        *,
        before: DiscordGuildSettingsUpdateState,
        desired: DiscordGuildEditableSettings,
        updated_at: datetime,
    ) -> DiscordGuildSettingsUpdateState:
        setting = self._session.get(BotGuildSettingORM, before.guild_id)
        if setting is None:
            raise DiscordGuildSettingsUpdateConcurrentConflictError("The locked Discord guild settings disappeared.")
        setting.win5_announcement_channel_id = desired.win5_announcement_channel_id
        setting.match_announcement_channel_id = desired.match_announcement_channel_id
        setting.log_channel_id = desired.log_channel_id
        setting.default_timezone = desired.default_timezone
        setting.win5_announcements_enabled = desired.win5_announcements_enabled
        setting.match_announcements_enabled = desired.match_announcements_enabled
        setting.updated_at = to_database_utc(updated_at, field_name="updated_at")
        try:
            self._session.flush()
        except OperationalError as error:
            self._raise_concurrent(error)
            raise
        return _state(setting)

    def add_audit(
        self,
        *,
        command: UpdateDiscordGuildSettings,
        before: DiscordGuildSettingsUpdateState,
        after: DiscordGuildSettingsUpdateState,
        changed_fields: tuple[str, ...],
        created_at: datetime,
    ) -> int:
        operation_values = {
            "guild_id": command.guild_id,
            "correlation_id": command.correlation_id,
            "actor_discord_user_id": command.actor_discord_user_id,
            "idempotency_key": command.idempotency_key,
            "request_fingerprint": command.request_fingerprint,
            "reason": command.reason,
            "created_at": to_database_utc(created_at, field_name="created_at"),
        }
        if self._session.get_bind().dialect.name == "sqlite":
            operation_values["id"] = sqlite_next_id(self._session, OperationORM.id)
        operation = OperationORM(**operation_values)
        self._session.add(operation)
        try:
            self._session.flush()
        except IntegrityError as error:
            raise DiscordGuildSettingsUpdateIdempotencyConflictError(
                "Idempotency key is already bound to another settings request."
            ) from error
        except OperationalError as error:
            self._raise_concurrent(error)
            raise

        result = UpdatedDiscordGuildSettings(
            operation_id=operation.id,
            before=before,
            after=after,
            changed_fields=changed_fields,
            reason=command.reason,
        )
        self._session.add(
            SettingsOperationORM(
                operation_id=operation.id,
                guild_id=command.guild_id,
                type=DISCORD_GUILD_SETTINGS_UPDATE_OPERATION_TYPE,
                before_data={
                    "schema_version": result.to_payload()["schema_version"],
                    "settings": before.to_payload(),
                },
                after_data=result.to_payload(),
            )
        )
        try:
            self._session.flush()
        except (IntegrityError, OperationalError) as error:
            if isinstance(error, OperationalError):
                self._raise_concurrent(error)
            raise DiscordGuildSettingsUpdateAuditError(
                "Discord guild settings update audit could not be stored."
            ) from error
        return operation.id

    @staticmethod
    def _raise_concurrent(error: OperationalError) -> None:
        if is_mariadb_deadlock(error):
            raise DiscordGuildSettingsUpdateConcurrentConflictError(
                "Concurrent settings mutation rolled back this operation."
            ) from error


class SqlAlchemyDiscordGuildSettingsUpdateQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._settings: SqlAlchemyDiscordGuildSettingsUpdateQueryRepository | None = None

    @property
    def discord_guild_settings_update(self) -> SqlAlchemyDiscordGuildSettingsUpdateQueryRepository:
        return self._require_active_repository(self._settings)

    def _activate_repositories(self) -> None:
        self._settings = SqlAlchemyDiscordGuildSettingsUpdateQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._settings = None


class SqlAlchemyDiscordGuildSettingsUpdateQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyDiscordGuildSettingsUpdateQueryUnitOfWork]
):
    unit_of_work_type = SqlAlchemyDiscordGuildSettingsUpdateQueryUnitOfWork


class SqlAlchemyDiscordGuildSettingsUpdateUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._settings: SqlAlchemyDiscordGuildSettingsUpdateRepository | None = None

    @property
    def discord_guild_settings_update(self) -> SqlAlchemyDiscordGuildSettingsUpdateRepository:
        return self._require_active_repository(self._settings)

    def _activate_repositories(self) -> None:
        self._settings = SqlAlchemyDiscordGuildSettingsUpdateRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._settings = None


class SqlAlchemyDiscordGuildSettingsUpdateUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyDiscordGuildSettingsUpdateUnitOfWork]
):
    unit_of_work_type = SqlAlchemyDiscordGuildSettingsUpdateUnitOfWork
