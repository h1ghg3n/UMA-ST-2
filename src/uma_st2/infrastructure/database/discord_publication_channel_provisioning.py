"""SQLAlchemy persistence for automatic publication-channel binding."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from uma_st2.application.discord import (
    DISCORD_PUBLICATION_CHANNEL_PERMISSION_PROFILE,
    DISCORD_PUBLICATION_CHANNEL_PROVISIONING_AUDIT_SCHEMA_VERSION,
    DISCORD_PUBLICATION_CHANNEL_PROVISIONING_OPERATION_TYPE,
    DISCORD_PUBLICATION_CHANNEL_PROVISIONING_REASON,
    AwaitingDiscordPublicationChannel,
    DiscordGuildEditableSettings,
    DiscordGuildSettingsUpdateState,
    DiscordPublicationChannelProvisioningAuditError,
    DiscordPublicationChannelProvisioningConcurrentConflictError,
    DiscordPublicationChannelProvisioningIdempotencyConflictError,
    DiscordPublicationChannelProvisioningInvalidSourceError,
    ProvisionDiscordPublicationChannel,
    ProvisionedDiscordPublicationChannel,
    StoredAwaitingDiscordPublication,
    StoredDiscordPublicationChannelProvisioningOperation,
    publication_channel_settings_fingerprint,
)
from uma_st2.application.publication import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    SUPPORTED_PUBLICATION_DELIVERY_ROUTES,
    WIN5_ANNOUNCEMENT_DESTINATION_KIND,
)
from uma_st2.domain.publication import PublicationStatus

from .datetime_codec import from_database_utc, to_database_utc
from .orm import BotGuildSettingORM, DiscordPublicationORM, OperationORM, SettingsOperationORM
from .registration_bootstrap import is_mariadb_deadlock, sqlite_next_id
from .uow import SessionFactory, SqlAlchemyFeatureUnitOfWork, SqlAlchemyFeatureUnitOfWorkFactory


def _supported_route_clause():
    return or_(
        *(
            and_(
                DiscordPublicationORM.destination_kind == destination_kind,
                DiscordPublicationORM.event_type == event_type,
            )
            for destination_kind, event_type in SUPPORTED_PUBLICATION_DELIVERY_ROUTES
        )
    )


def _settings_state(setting: BotGuildSettingORM) -> DiscordGuildSettingsUpdateState:
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


class SqlAlchemyDiscordPublicationChannelProvisioningQueryRepository:
    """Find one oldest eligible provider target without taking a row lock."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find_oldest_target(self, *, guild_id: str) -> AwaitingDiscordPublicationChannel | None:
        row = self._session.execute(
            select(DiscordPublicationORM, BotGuildSettingORM)
            .join(BotGuildSettingORM, BotGuildSettingORM.guild_id == DiscordPublicationORM.guild_id)
            .where(
                DiscordPublicationORM.guild_id == guild_id,
                _supported_route_clause(),
                DiscordPublicationORM.status == PublicationStatus.AWAITING_CHANNEL.value,
                DiscordPublicationORM.target_channel_id.is_(None),
                DiscordPublicationORM.attempt_count == 0,
                DiscordPublicationORM.discord_message_id.is_(None),
                DiscordPublicationORM.last_error_code.is_(None),
                DiscordPublicationORM.failure_stage.is_(None),
                DiscordPublicationORM.attempt_started_at.is_(None),
                DiscordPublicationORM.published_at.is_(None),
                or_(
                    and_(
                        DiscordPublicationORM.destination_kind == WIN5_ANNOUNCEMENT_DESTINATION_KIND,
                        BotGuildSettingORM.win5_announcements_enabled.is_(True),
                        BotGuildSettingORM.win5_announcement_channel_id.is_(None),
                    ),
                    and_(
                        DiscordPublicationORM.destination_kind == MATCH_ANNOUNCEMENT_DESTINATION_KIND,
                        BotGuildSettingORM.match_announcements_enabled.is_(True),
                        BotGuildSettingORM.match_announcement_channel_id.is_(None),
                    ),
                ),
            )
            .order_by(DiscordPublicationORM.id)
            .limit(1)
        ).one_or_none()
        if row is None:
            return None
        publication, setting = row
        try:
            state = _settings_state(setting)
            return AwaitingDiscordPublicationChannel(
                publication_id=publication.id,
                guild_id=publication.guild_id,
                destination_kind=publication.destination_kind,
                event_type=publication.event_type,
                payload_fingerprint=publication.payload_fingerprint,
                expected_settings_fingerprint=publication_channel_settings_fingerprint(
                    state,
                    destination_kind=publication.destination_kind,
                ),
            )
        except (TypeError, ValueError) as error:
            raise DiscordPublicationChannelProvisioningInvalidSourceError(
                "Stored automatic publication-channel target is malformed."
            ) from error


class SqlAlchemyDiscordPublicationChannelProvisioningRepository:
    """Lock settings then one publication, bind both, and append audit."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_settings(self, *, guild_id: str) -> DiscordGuildSettingsUpdateState | None:
        try:
            setting = self._session.scalar(
                select(BotGuildSettingORM).where(BotGuildSettingORM.guild_id == guild_id).with_for_update()
            )
        except OperationalError as error:
            self._raise_concurrent(error)
            raise
        if setting is None:
            return None
        try:
            return _settings_state(setting)
        except (TypeError, ValueError) as error:
            raise DiscordPublicationChannelProvisioningInvalidSourceError(
                "Stored Discord guild settings are malformed."
            ) from error

    def find_operation(
        self,
        *,
        idempotency_key: str,
    ) -> StoredDiscordPublicationChannelProvisioningOperation | None:
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
        return StoredDiscordPublicationChannelProvisioningOperation(
            operation_id=row.id,
            request_fingerprint=row.request_fingerprint,
            operation_type=row.type,
            guild_id=row.guild_id,
            after_data=row.after_data,
        )

    def lock_publication(self, *, publication_id: int) -> StoredAwaitingDiscordPublication | None:
        try:
            publication = self._session.scalar(
                select(DiscordPublicationORM)
                .where(
                    DiscordPublicationORM.id == publication_id,
                    _supported_route_clause(),
                )
                .with_for_update()
            )
        except OperationalError as error:
            self._raise_concurrent(error)
            raise
        if publication is None:
            return None
        try:
            return StoredAwaitingDiscordPublication(
                publication_id=publication.id,
                guild_id=publication.guild_id,
                destination_kind=publication.destination_kind,
                event_type=publication.event_type,
                payload_fingerprint=publication.payload_fingerprint,
                status=PublicationStatus(publication.status),
                target_channel_id=publication.target_channel_id,
                attempt_count=publication.attempt_count,
                discord_message_id=publication.discord_message_id,
                last_error_code=publication.last_error_code,
                failure_stage=publication.failure_stage,
                attempt_started_at=(
                    None
                    if publication.attempt_started_at is None
                    else from_database_utc(
                        publication.attempt_started_at,
                        field_name="discord_publications.attempt_started_at",
                    )
                ),
                published_at=(
                    None
                    if publication.published_at is None
                    else from_database_utc(
                        publication.published_at,
                        field_name="discord_publications.published_at",
                    )
                ),
                updated_at=from_database_utc(
                    publication.updated_at,
                    field_name="discord_publications.updated_at",
                ),
            )
        except (TypeError, ValueError) as error:
            raise DiscordPublicationChannelProvisioningInvalidSourceError(
                "Stored awaiting-channel publication is malformed."
            ) from error

    def bind_channel(
        self,
        *,
        before: DiscordGuildSettingsUpdateState,
        destination_kind: str,
        target_channel_id: str,
        publication_id: int,
        ready_at: datetime,
    ) -> DiscordGuildSettingsUpdateState:
        setting = self._session.get(BotGuildSettingORM, before.guild_id)
        publication = self._session.get(DiscordPublicationORM, publication_id)
        if setting is None or publication is None:
            raise DiscordPublicationChannelProvisioningConcurrentConflictError(
                "Locked settings or publication disappeared before channel bind."
            )
        if destination_kind == WIN5_ANNOUNCEMENT_DESTINATION_KIND:
            setting.win5_announcement_channel_id = target_channel_id
        elif destination_kind == MATCH_ANNOUNCEMENT_DESTINATION_KIND:
            setting.match_announcement_channel_id = target_channel_id
        else:
            raise DiscordPublicationChannelProvisioningAuditError("Publication destination is unsupported.")
        stored_ready_at = to_database_utc(ready_at, field_name="ready_at")
        setting.updated_at = stored_ready_at
        publication.target_channel_id = target_channel_id
        publication.status = PublicationStatus.READY.value
        publication.updated_at = stored_ready_at
        try:
            self._session.flush()
        except OperationalError as error:
            self._raise_concurrent(error)
            raise
        return _settings_state(setting)

    def add_audit(
        self,
        *,
        command: ProvisionDiscordPublicationChannel,
        before: DiscordGuildSettingsUpdateState,
        after: DiscordGuildSettingsUpdateState,
        ready_at: datetime,
    ) -> int:
        operation_values = {
            "guild_id": command.target.guild_id,
            "correlation_id": command.correlation_id,
            "actor_discord_user_id": command.actor_discord_user_id,
            "idempotency_key": command.idempotency_key,
            "request_fingerprint": command.request_fingerprint,
            "reason": DISCORD_PUBLICATION_CHANNEL_PROVISIONING_REASON,
            "created_at": to_database_utc(ready_at, field_name="created_at"),
        }
        if self._session.get_bind().dialect.name == "sqlite":
            operation_values["id"] = sqlite_next_id(self._session, OperationORM.id)
        operation = OperationORM(**operation_values)
        self._session.add(operation)
        try:
            self._session.flush()
        except IntegrityError as error:
            raise DiscordPublicationChannelProvisioningIdempotencyConflictError(
                "Idempotency key is already bound to another channel-provisioning request."
            ) from error
        except OperationalError as error:
            self._raise_concurrent(error)
            raise

        result = ProvisionedDiscordPublicationChannel(
            operation_id=operation.id,
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
        self._session.add(
            SettingsOperationORM(
                operation_id=operation.id,
                guild_id=command.target.guild_id,
                type=DISCORD_PUBLICATION_CHANNEL_PROVISIONING_OPERATION_TYPE,
                before_data={
                    "schema_version": DISCORD_PUBLICATION_CHANNEL_PROVISIONING_AUDIT_SCHEMA_VERSION,
                    "settings": before.to_payload(),
                    "publication": {
                        "publication_id": command.target.publication_id,
                        "destination_kind": command.target.destination_kind,
                        "event_type": command.target.event_type,
                        "payload_fingerprint": command.target.payload_fingerprint,
                        "status": PublicationStatus.AWAITING_CHANNEL.value,
                    },
                },
                after_data=result.to_payload(),
            )
        )
        try:
            self._session.flush()
        except (IntegrityError, OperationalError) as error:
            if isinstance(error, OperationalError):
                self._raise_concurrent(error)
            raise DiscordPublicationChannelProvisioningAuditError(
                "Publication-channel provisioning audit could not be stored."
            ) from error
        return operation.id

    @staticmethod
    def _raise_concurrent(error: OperationalError) -> None:
        if is_mariadb_deadlock(error):
            raise DiscordPublicationChannelProvisioningConcurrentConflictError(
                "Concurrent publication-channel mutation rolled back this operation."
            ) from error


class SqlAlchemyDiscordPublicationChannelProvisioningQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._provisioning: SqlAlchemyDiscordPublicationChannelProvisioningQueryRepository | None = None

    @property
    def discord_publication_channel_provisioning(
        self,
    ) -> SqlAlchemyDiscordPublicationChannelProvisioningQueryRepository:
        return self._require_active_repository(self._provisioning)

    def _activate_repositories(self) -> None:
        self._provisioning = SqlAlchemyDiscordPublicationChannelProvisioningQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._provisioning = None


class SqlAlchemyDiscordPublicationChannelProvisioningQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyDiscordPublicationChannelProvisioningQueryUnitOfWork]
):
    unit_of_work_type = SqlAlchemyDiscordPublicationChannelProvisioningQueryUnitOfWork


class SqlAlchemyDiscordPublicationChannelProvisioningUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._provisioning: SqlAlchemyDiscordPublicationChannelProvisioningRepository | None = None

    @property
    def discord_publication_channel_provisioning(
        self,
    ) -> SqlAlchemyDiscordPublicationChannelProvisioningRepository:
        return self._require_active_repository(self._provisioning)

    def _activate_repositories(self) -> None:
        self._provisioning = SqlAlchemyDiscordPublicationChannelProvisioningRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._provisioning = None


class SqlAlchemyDiscordPublicationChannelProvisioningUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyDiscordPublicationChannelProvisioningUnitOfWork]
):
    unit_of_work_type = SqlAlchemyDiscordPublicationChannelProvisioningUnitOfWork
