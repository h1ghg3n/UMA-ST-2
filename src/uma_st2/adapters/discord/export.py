"""Discord attachment surface for versioned runtime exports."""

from __future__ import annotations

import logging
from collections import Counter
from io import BytesIO

import discord
from discord import app_commands

from uma_st2.application.exporting import (
    CirclePointExportError,
    CirclePointExports,
    MatchExportSeasonChoice,
    MatchSeasonExportError,
    MatchSeasonExports,
    Win5ExportSeasonChoice,
    Win5SeasonExportError,
    Win5SeasonExports,
)

from .common import (
    AuthorizeDiscordAutocomplete,
    BlockingApplicationRunner,
    PrepareDiscordCommand,
    run_blocking_application,
    send_deferred_attachment_safely,
    send_deferred_response_safely,
    send_ephemeral_internal_error_after_defer_safely,
)
from .strings.export import (
    CIRCLE_POINT_COMMAND_DESCRIPTION,
    CIRCLE_POINT_EXPORT_UNAVAILABLE,
    EXPORT_MATCH_GROUP_DESCRIPTION,
    EXPORT_ROOT_DESCRIPTION,
    EXPORT_WIN5_GROUP_DESCRIPTION,
    MATCH_SEASON_COMMAND_DESCRIPTION,
    MATCH_SEASON_EXPORT_UNAVAILABLE,
    MATCH_SEASON_OPTION_DESCRIPTION,
    SEASON_OPTION_NAME,
    WIN5_SEASON_COMMAND_DESCRIPTION,
    WIN5_SEASON_EXPORT_UNAVAILABLE,
    WIN5_SEASON_OPTION_DESCRIPTION,
    circle_point_export_success,
    match_season_choice_name,
    match_season_export_success,
    win5_season_choice_name,
    win5_season_export_success,
)

_WIN5_SEASON_EXPORT_COMMAND = "export.win5.season"
_MATCH_SEASON_EXPORT_COMMAND = "export.match.season"
_CIRCLE_POINT_EXPORT_COMMAND = "export.circle-points"

logger = logging.getLogger(__name__)


class CirclePointExportDiscordAdapter:
    """Authorize, generate and attach the current Circle Point workbook."""

    def __init__(
        self,
        *,
        exports: CirclePointExports,
        prepare_command: PrepareDiscordCommand,
        blocking_runner: BlockingApplicationRunner = run_blocking_application,
    ) -> None:
        self.exports = exports
        self.prepare_command = prepare_command
        self.blocking_runner = blocking_runner

    async def export_current(self, interaction: discord.Interaction) -> None:
        """Generate and deliver one private current snapshot attachment."""

        if not await self.prepare_command(
            interaction,
            _CIRCLE_POINT_EXPORT_COMMAND,
            ephemeral=True,
        ):
            return
        try:
            artifact = await self.blocking_runner(self.exports.export_current)
        except CirclePointExportError:
            await send_deferred_response_safely(
                interaction,
                _CIRCLE_POINT_EXPORT_COMMAND,
                CIRCLE_POINT_EXPORT_UNAVAILABLE,
            )
            return
        except Exception:
            await send_ephemeral_internal_error_after_defer_safely(
                interaction,
                _CIRCLE_POINT_EXPORT_COMMAND,
            )
            return

        content = circle_point_export_success(
            row_count=artifact.row_count,
            sha256_hex=artifact.sha256_hex,
        )
        attachment = discord.File(BytesIO(artifact.content), filename=artifact.filename)
        try:
            await send_deferred_attachment_safely(
                interaction,
                _CIRCLE_POINT_EXPORT_COMMAND,
                content,
                attachment,
            )
        finally:
            attachment.close()


def match_export_season_choices(
    choices: tuple[MatchExportSeasonChoice, ...],
) -> list[app_commands.Choice[str]]:
    """Render bounded derived KST calendar-half choices."""

    return [
        app_commands.Choice(
            name=match_season_choice_name(choice.name),
            value=choice.key,
        )
        for choice in choices
    ]


def win5_export_season_choices(
    choices: tuple[Win5ExportSeasonChoice, ...],
) -> list[app_commands.Choice[int]]:
    """Render bounded active/closed Season choices with stable disambiguation."""

    keys = tuple((choice.name, choice.status.value) for choice in choices)
    counts = Counter(keys)
    rendered: list[app_commands.Choice[int]] = []
    for choice, key in zip(choices, keys, strict=True):
        name = win5_season_choice_name(
            name=choice.name,
            status=choice.status.value,
            season_id=choice.id,
            duplicate=counts[key] > 1,
        )
        rendered.append(app_commands.Choice(name=name, value=choice.id))
    return rendered


class Win5SeasonExportDiscordAdapter:
    """Authorize, generate and attach one complete WIN5 Season workbook."""

    def __init__(
        self,
        *,
        exports: Win5SeasonExports,
        prepare_command: PrepareDiscordCommand,
        authorize_autocomplete: AuthorizeDiscordAutocomplete,
        blocking_runner: BlockingApplicationRunner = run_blocking_application,
    ) -> None:
        self.exports = exports
        self.prepare_command = prepare_command
        self.authorize_autocomplete = authorize_autocomplete
        self.blocking_runner = blocking_runner

    async def autocomplete_seasons(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        """Return current active/closed Season choices after fresh authorization."""

        try:
            if not await self.authorize_autocomplete(interaction, _WIN5_SEASON_EXPORT_COMMAND):
                return []
            choices = await self.blocking_runner(lambda: self.exports.search_seasons(query=current, limit=25))
        except Exception:
            logger.exception("WIN5 export Season autocomplete failed")
            return []
        return win5_export_season_choices(choices)

    async def export_season(
        self,
        interaction: discord.Interaction,
        *,
        season_id: int,
    ) -> None:
        """Generate and deliver one private workbook attachment."""

        if not await self.prepare_command(
            interaction,
            _WIN5_SEASON_EXPORT_COMMAND,
            ephemeral=True,
        ):
            return
        try:
            artifact = await self.blocking_runner(lambda: self.exports.export_season(season_id=season_id))
        except Win5SeasonExportError:
            await send_deferred_response_safely(
                interaction,
                _WIN5_SEASON_EXPORT_COMMAND,
                WIN5_SEASON_EXPORT_UNAVAILABLE,
            )
            return
        except Exception:
            await send_ephemeral_internal_error_after_defer_safely(
                interaction,
                _WIN5_SEASON_EXPORT_COMMAND,
            )
            return

        content = win5_season_export_success(
            scope_name=artifact.scope_name,
            row_count=artifact.row_count,
            sha256_hex=artifact.sha256_hex,
        )
        attachment = discord.File(BytesIO(artifact.content), filename=artifact.filename)
        try:
            await send_deferred_attachment_safely(
                interaction,
                _WIN5_SEASON_EXPORT_COMMAND,
                content,
                attachment,
            )
        finally:
            attachment.close()


class ExportWin5CommandGroup(app_commands.Group):
    """WIN5 child of the V2 `/export` root."""

    def __init__(self, *, adapter: Win5SeasonExportDiscordAdapter) -> None:
        super().__init__(name="win5", description=EXPORT_WIN5_GROUP_DESCRIPTION)
        self._adapter = adapter

    async def _season_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        return await self._adapter.autocomplete_seasons(interaction, current)

    @app_commands.command(name="season", description=WIN5_SEASON_COMMAND_DESCRIPTION)
    @app_commands.rename(season=SEASON_OPTION_NAME)
    @app_commands.describe(season=WIN5_SEASON_OPTION_DESCRIPTION)
    @app_commands.autocomplete(season=_season_autocomplete)
    async def season(
        self,
        interaction: discord.Interaction,
        season: int,
    ) -> None:
        await self._adapter.export_season(interaction, season_id=season)


class MatchSeasonExportDiscordAdapter:
    """Authorize, generate, and attach one complete Match Season workbook."""

    def __init__(
        self,
        *,
        exports: MatchSeasonExports,
        prepare_command: PrepareDiscordCommand,
        authorize_autocomplete: AuthorizeDiscordAutocomplete,
        blocking_runner: BlockingApplicationRunner = run_blocking_application,
    ) -> None:
        self.exports = exports
        self.prepare_command = prepare_command
        self.authorize_autocomplete = authorize_autocomplete
        self.blocking_runner = blocking_runner

    async def autocomplete_seasons(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Return actual derived Match Seasons after fresh authorization."""

        try:
            if not await self.authorize_autocomplete(interaction, _MATCH_SEASON_EXPORT_COMMAND):
                return []
            choices = await self.blocking_runner(lambda: self.exports.search_seasons(query=current, limit=25))
        except Exception:
            logger.exception("Match export Season autocomplete failed")
            return []
        return match_export_season_choices(choices)

    async def export_season(
        self,
        interaction: discord.Interaction,
        *,
        season_key: str,
    ) -> None:
        """Generate and deliver one private Match workbook attachment."""

        if not await self.prepare_command(
            interaction,
            _MATCH_SEASON_EXPORT_COMMAND,
            ephemeral=True,
        ):
            return
        try:
            artifact = await self.blocking_runner(lambda: self.exports.export_season(season_key=season_key))
        except MatchSeasonExportError:
            await send_deferred_response_safely(
                interaction,
                _MATCH_SEASON_EXPORT_COMMAND,
                MATCH_SEASON_EXPORT_UNAVAILABLE,
            )
            return
        except Exception:
            await send_ephemeral_internal_error_after_defer_safely(
                interaction,
                _MATCH_SEASON_EXPORT_COMMAND,
            )
            return

        content = match_season_export_success(
            scope_name=artifact.scope_name,
            row_count=artifact.row_count,
            sha256_hex=artifact.sha256_hex,
        )
        attachment = discord.File(BytesIO(artifact.content), filename=artifact.filename)
        try:
            await send_deferred_attachment_safely(
                interaction,
                _MATCH_SEASON_EXPORT_COMMAND,
                content,
                attachment,
            )
        finally:
            attachment.close()


class ExportMatchCommandGroup(app_commands.Group):
    """Circle Match child of the V2 `/export` root."""

    def __init__(self, *, adapter: MatchSeasonExportDiscordAdapter) -> None:
        super().__init__(name="match", description=EXPORT_MATCH_GROUP_DESCRIPTION)
        self._adapter = adapter

    async def _season_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._adapter.autocomplete_seasons(interaction, current)

    @app_commands.command(name="season", description=MATCH_SEASON_COMMAND_DESCRIPTION)
    @app_commands.rename(season=SEASON_OPTION_NAME)
    @app_commands.describe(season=MATCH_SEASON_OPTION_DESCRIPTION)
    @app_commands.autocomplete(season=_season_autocomplete)
    async def season(
        self,
        interaction: discord.Interaction,
        season: str,
    ) -> None:
        await self._adapter.export_season(interaction, season_key=season)


class ExportCommandGroup(app_commands.Group):
    """V2 `/export` root with domain-specific child groups."""

    def __init__(
        self,
        *,
        circle_point_adapter: CirclePointExportDiscordAdapter,
        win5_group: ExportWin5CommandGroup,
        match_group: ExportMatchCommandGroup | None = None,
    ) -> None:
        super().__init__(name="export", description=EXPORT_ROOT_DESCRIPTION)
        self._circle_point_adapter = circle_point_adapter
        self.add_command(win5_group)
        if match_group is not None:
            self.add_command(match_group)

    @app_commands.command(name="circle-points", description=CIRCLE_POINT_COMMAND_DESCRIPTION)
    async def circle_points(self, interaction: discord.Interaction) -> None:
        await self._circle_point_adapter.export_current(interaction)
