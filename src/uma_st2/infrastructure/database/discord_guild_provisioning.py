"""SQLAlchemy persistence for create-only Discord guild provisioning."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from uma_st2.application.discord import (
    DISCORD_GUILD_PROVISIONING_AUDIT_SCHEMA_VERSION,
    DISCORD_GUILD_PROVISIONING_OPERATION_TYPE,
    DiscordGuildProvisioningConflictError,
    DiscordGuildProvisioningValues,
    DiscordGuildSettingsSnapshot,
    ProvisionDiscordGuildSettings,
    StoredDiscordGuildProvisioningOperation,
)

from .datetime_codec import from_database_utc, to_database_utc
from .orm import BotGuildSettingORM, OperationORM, SettingsOperationORM
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


class SqlAlchemyDiscordGuildProvisioningRepository:
    """Lock, create, and audit one initial guild settings row."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find_operation(self, *, idempotency_key: str) -> StoredDiscordGuildProvisioningOperation | None:
        row = self._session.execute(
            select(
                OperationORM.id,
                OperationORM.request_fingerprint,
                SettingsOperationORM.type,
                SettingsOperationORM.after_data,
            )
            .outerjoin(SettingsOperationORM, SettingsOperationORM.operation_id == OperationORM.id)
            .where(OperationORM.idempotency_key == idempotency_key)
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        return StoredDiscordGuildProvisioningOperation(
            operation_id=row.id,
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            after_data=row.after_data,
        )

    def lock_settings(self, *, guild_id: str) -> DiscordGuildSettingsSnapshot | None:
        setting = self._session.scalar(
            select(BotGuildSettingORM).where(BotGuildSettingORM.guild_id == guild_id).with_for_update()
        )
        return None if setting is None else _snapshot(setting)

    def create_settings(
        self,
        *,
        values: DiscordGuildProvisioningValues,
        created_at: datetime,
    ) -> DiscordGuildSettingsSnapshot:
        stored_at = to_database_utc(created_at, field_name="created_at")
        setting = BotGuildSettingORM(
            guild_id=values.guild_id,
            win5_announcement_channel_id=values.win5_announcement_channel_id,
            match_announcement_channel_id=values.match_announcement_channel_id,
            log_channel_id=values.log_channel_id,
            operator_role_id=values.operator_role_id,
            bot_manager_role_id=values.bot_manager_role_id,
            default_timezone=values.default_timezone,
            win5_announcements_enabled=values.win5_announcements_enabled,
            match_announcements_enabled=values.match_announcements_enabled,
            created_at=stored_at,
            updated_at=stored_at,
        )
        self._session.add(setting)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise DiscordGuildProvisioningConflictError(
                "Discord guild settings were provisioned concurrently."
            ) from exc
        return _snapshot(setting)

    def add_audit(
        self,
        *,
        command: ProvisionDiscordGuildSettings,
        snapshot: DiscordGuildSettingsSnapshot,
        created_at: datetime,
    ) -> int:
        operation = OperationORM(
            guild_id=command.values.guild_id,
            correlation_id=command.correlation_id,
            actor_discord_user_id=command.actor_discord_user_id,
            idempotency_key=command.idempotency_key,
            request_fingerprint=command.request_fingerprint,
            reason=command.reason,
            created_at=to_database_utc(created_at, field_name="created_at"),
        )
        self._session.add(operation)
        self._session.flush()
        self._session.add(
            SettingsOperationORM(
                operation_id=operation.id,
                guild_id=command.values.guild_id,
                type=DISCORD_GUILD_PROVISIONING_OPERATION_TYPE,
                before_data=None,
                after_data={
                    "schema_version": DISCORD_GUILD_PROVISIONING_AUDIT_SCHEMA_VERSION,
                    "settings": snapshot.to_payload(),
                },
            )
        )
        self._session.flush()
        return operation.id


def _snapshot(setting: BotGuildSettingORM) -> DiscordGuildSettingsSnapshot:
    return DiscordGuildSettingsSnapshot(
        values=DiscordGuildProvisioningValues(
            guild_id=setting.guild_id,
            win5_announcement_channel_id=setting.win5_announcement_channel_id,
            match_announcement_channel_id=setting.match_announcement_channel_id,
            log_channel_id=setting.log_channel_id,
            operator_role_id=setting.operator_role_id,
            bot_manager_role_id=setting.bot_manager_role_id,
            default_timezone=setting.default_timezone,
            win5_announcements_enabled=setting.win5_announcements_enabled,
            match_announcements_enabled=setting.match_announcements_enabled,
        ),
        created_at=from_database_utc(setting.created_at, field_name="BotGuildSetting.created_at"),
        updated_at=from_database_utc(setting.updated_at, field_name="BotGuildSetting.updated_at"),
    )


class SqlAlchemyDiscordGuildProvisioningUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete UoW exposing initial Discord guild provisioning."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._provisioning: SqlAlchemyDiscordGuildProvisioningRepository | None = None

    @property
    def discord_guild_provisioning(self) -> SqlAlchemyDiscordGuildProvisioningRepository:
        return self._require_active_repository(self._provisioning)

    def _activate_repositories(self) -> None:
        self._provisioning = SqlAlchemyDiscordGuildProvisioningRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._provisioning = None


class SqlAlchemyDiscordGuildProvisioningUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyDiscordGuildProvisioningUnitOfWork]
):
    """Create one provisioning UoW per one-shot command."""

    unit_of_work_type = SqlAlchemyDiscordGuildProvisioningUnitOfWork
