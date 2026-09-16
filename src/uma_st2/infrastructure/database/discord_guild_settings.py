"""SQLAlchemy projection for Discord runtime guild settings."""

from __future__ import annotations

from sqlalchemy.orm import Session

from uma_st2.application.discord import DiscordGuildRuntimeSettings

from .orm import BotGuildSettingORM
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


class SqlAlchemyDiscordGuildSettingsQueryRepository:
    """Read immutable channel and Role values for runtime authorization."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_guild_settings(
        self,
        *,
        guild_id: str,
    ) -> DiscordGuildRuntimeSettings | None:
        settings = self._session.get(BotGuildSettingORM, guild_id)
        if settings is None:
            return None
        return DiscordGuildRuntimeSettings(
            guild_id=settings.guild_id,
            win5_announcement_channel_id=settings.win5_announcement_channel_id,
            match_announcement_channel_id=settings.match_announcement_channel_id,
            operator_role_id=settings.operator_role_id,
            bot_manager_role_id=settings.bot_manager_role_id,
            win5_announcements_enabled=settings.win5_announcements_enabled,
        )


class SqlAlchemyDiscordGuildSettingsQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW exposing Discord runtime settings."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._queries: SqlAlchemyDiscordGuildSettingsQueryRepository | None = None

    @property
    def discord_guild_settings(self) -> SqlAlchemyDiscordGuildSettingsQueryRepository:
        return self._require_active_repository(self._queries)

    def _activate_repositories(self) -> None:
        self._queries = SqlAlchemyDiscordGuildSettingsQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._queries = None


class SqlAlchemyDiscordGuildSettingsQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyDiscordGuildSettingsQueryUnitOfWork]
):
    """Create one fresh guild-settings query UoW per runtime check."""

    unit_of_work_type = SqlAlchemyDiscordGuildSettingsQueryUnitOfWork
