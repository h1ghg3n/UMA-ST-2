"""Private Discord Account status dashboard."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import discord
from discord import app_commands

from uma_st2.application.identity import (
    AccountIdentityDetails,
    AccountMatchHistoryPage,
    AccountRegistrationAlreadyLinkedError,
    AccountRegistrationAlreadyPendingError,
    AccountRegistrationAuditError,
    AccountRegistrationCommands,
    AccountRegistrationIdempotencyConflictError,
    AccountRegistrationPidUnavailableError,
    AccountStatusInvalidSourceError,
    AccountStatusOverview,
    AccountStatusQueries,
    AccountWin5HistoryPage,
    SubmitAccountRegistrationRequest,
)
from uma_st2.domain.identity import GameRegion

from .common import (
    AuthorizeDiscordInteraction,
    BlockingApplicationRunner,
    PrepareDiscordCommand,
    buttonless_terminal_layout,
    correlation_id,
    run_blocking_application,
    send_ephemeral_internal_error_after_defer_safely,
)
from .localization import korean_command_name, korean_parameter_name
from .strings import account as copy

logger = logging.getLogger(__name__)

_STATUS_COMMAND_NAME = "account.status"
_REGISTER_COMMAND_NAME = "account.register"
_COMPONENT_TIMEOUT_SECONDS = 600.0

REGION_OPTION_NAME = korean_parameter_name("region", "리전")

AccountStatusProjection = (
    AccountStatusOverview | AccountMatchHistoryPage | AccountWin5HistoryPage | AccountIdentityDetails
)


def _required_snowflake(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive Discord snowflake.")
    return value


@dataclass(frozen=True, slots=True)
class AccountInteractionContext:
    """Opener and guild/channel binding for one private Account dashboard."""

    user_id: int
    guild_id: int
    channel_id: int

    @classmethod
    def from_interaction(cls, interaction: object) -> AccountInteractionContext:
        return cls(
            user_id=_required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            ),
            guild_id=_required_snowflake(getattr(interaction, "guild_id", None), field_name="guild ID"),
            channel_id=_required_snowflake(getattr(interaction, "channel_id", None), field_name="channel ID"),
        )

    def matches(self, interaction: object) -> bool:
        return (
            getattr(getattr(interaction, "user", None), "id", None) == self.user_id
            and getattr(interaction, "guild_id", None) == self.guild_id
            and getattr(interaction, "channel_id", None) == self.channel_id
        )


def _display_name(interaction: object) -> str:
    user = getattr(interaction, "user", None)
    for attribute in ("display_name", "global_name", "name"):
        value = getattr(user, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise RuntimeError("Discord display name is unavailable.")


class AccountRegistrationModal(discord.ui.Modal):
    """Direct-final private registration request input."""

    def __init__(
        self,
        *,
        adapter: AccountRegistrationDiscordAdapter,
        context: AccountInteractionContext,
        game_region: GameRegion,
    ) -> None:
        super().__init__(title=copy.REGISTER_MODAL_TITLE, timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._game_region = game_region
        self.uma_pid = discord.ui.TextInput(
            label=copy.REGISTER_PID_LABEL,
            placeholder=copy.REGISTER_PID_PLACEHOLDER,
            min_length=1,
            max_length=32,
        )
        self.nickname = discord.ui.TextInput(
            label=copy.REGISTER_NICKNAME_LABEL,
            min_length=1,
            max_length=100,
        )
        self.affiliation = discord.ui.TextInput(
            label=copy.REGISTER_AFFILIATION_LABEL,
            required=False,
            max_length=100,
        )
        for item in (self.uma_pid, self.nickname, self.affiliation):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.submit_request(
            interaction,
            context=self._context,
            game_region=self._game_region,
            uma_pid=str(self.uma_pid.value),
            nickname=str(self.nickname.value),
            affiliation=str(self.affiliation.value),
        )


@dataclass(frozen=True, slots=True)
class AccountRegistrationDiscordAdapter:
    """Authorize a request-only Account registration Modal and private receipt."""

    commands: AccountRegistrationCommands
    prepare_command: PrepareDiscordCommand
    authorize_interaction: AuthorizeDiscordInteraction
    run_application: BlockingApplicationRunner = run_blocking_application

    async def open_registration(self, interaction: discord.Interaction, *, game_region: str) -> None:
        try:
            if not await self.authorize_interaction(interaction, _REGISTER_COMMAND_NAME):
                return
            context = AccountInteractionContext.from_interaction(interaction)
            await interaction.response.send_modal(
                AccountRegistrationModal(
                    adapter=self,
                    context=context,
                    game_region=GameRegion(game_region),
                )
            )
        except Exception:
            self._log_failure(interaction)
            await self._send_initial_error(interaction)

    async def submit_request(
        self,
        interaction: discord.Interaction,
        *,
        context: AccountInteractionContext,
        game_region: GameRegion,
        uma_pid: str,
        nickname: str,
        affiliation: str | None,
    ) -> None:
        if not context.matches(interaction):
            await self._send_initial_error(interaction, copy.REGISTER_BOUND_INTERACTION_ERROR)
            return
        if not await self.prepare_command(interaction, _REGISTER_COMMAND_NAME, ephemeral=True):
            return
        try:
            result = await self.run_application(
                lambda: self.commands.submit_registration_request(
                    SubmitAccountRegistrationRequest(
                        guild_id=str(context.guild_id),
                        actor_discord_user_id=str(context.user_id),
                        discord_display_name_snapshot=_display_name(interaction),
                        game_region=game_region,
                        uma_pid=uma_pid,
                        nickname=nickname,
                        affiliation=affiliation,
                        idempotency_key=correlation_id(interaction),
                        correlation_id=correlation_id(interaction),
                    )
                )
            )
        except AccountRegistrationAlreadyLinkedError:
            content = copy.REGISTER_ALREADY_LINKED
        except AccountRegistrationAlreadyPendingError as error:
            content = copy.registration_pending(error.request_id)
        except AccountRegistrationPidUnavailableError:
            content = copy.REGISTER_PID_UNAVAILABLE
        except AccountRegistrationIdempotencyConflictError:
            content = copy.REGISTER_IDEMPOTENCY_CONFLICT
        except AccountRegistrationAuditError:
            self._log_failure(interaction)
            content = copy.registration_internal_error(correlation_id(interaction))
        except ValueError:
            content = copy.REGISTER_INPUT_INVALID
        except Exception:
            self._log_failure(interaction)
            content = copy.registration_internal_error(correlation_id(interaction))
        else:
            snapshot = result.snapshot
            content = copy.format_registration_receipt(
                request_id=snapshot.request_id,
                region=snapshot.game_region.value,
                pid=snapshot.uma_pid,
                nickname=snapshot.nickname,
                affiliation=snapshot.affiliation,
                exact_retry=result.exact_retry,
            )
        try:
            await interaction.edit_original_response(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            self._log_failure(interaction)

    @staticmethod
    async def _send_initial_error(interaction: discord.Interaction, content: str | None = None) -> None:
        message = content or copy.registration_internal_error(correlation_id(interaction))
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except Exception:
            logger.error(
                "Discord Account registration response failed correlation_id=%s",
                correlation_id(interaction),
            )

    @staticmethod
    def _log_failure(interaction: object) -> None:
        logger.error(
            "Discord Account registration failed correlation_id=%s command=%s",
            correlation_id(interaction),
            _REGISTER_COMMAND_NAME,
        )


class AccountStatusTab(StrEnum):
    OVERVIEW = "overview"
    MATCH = "match"
    WIN5 = "win5"
    IDENTITY = "identity"


_TAB_LABELS = {
    AccountStatusTab.OVERVIEW: copy.OVERVIEW_LABEL,
    AccountStatusTab.MATCH: copy.MATCH_LABEL,
    AccountStatusTab.WIN5: copy.WIN5_LABEL,
    AccountStatusTab.IDENTITY: copy.IDENTITY_LABEL,
}


class AccountStatusTabButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: AccountStatusDiscordAdapter,
        context: AccountInteractionContext,
        tab: AccountStatusTab,
        current_tab: AccountStatusTab,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._tab = tab
        super().__init__(
            label=_TAB_LABELS[tab],
            style=discord.ButtonStyle.primary if tab is current_tab else discord.ButtonStyle.secondary,
            custom_id=f"account-status-tab-{tab.value}",
            disabled=tab is current_tab,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.navigate(
            interaction,
            context=self._context,
            tab=self._tab,
            page=0,
            source_view=self.view,
        )


class AccountStatusPageButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: AccountStatusDiscordAdapter,
        context: AccountInteractionContext,
        tab: AccountStatusTab,
        page: int,
        direction: int,
        disabled: bool,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._tab = tab
        self._page = page + direction
        super().__init__(
            label=copy.PREVIOUS_LABEL if direction < 0 else copy.NEXT_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id=f"account-status-page-{'previous' if direction < 0 else 'next'}",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.navigate(
            interaction,
            context=self._context,
            tab=self._tab,
            page=max(0, self._page),
            source_view=self.view,
        )


class AccountStatusRefreshButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: AccountStatusDiscordAdapter,
        context: AccountInteractionContext,
        tab: AccountStatusTab,
        page: int,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._tab = tab
        self._page = page
        super().__init__(
            label=copy.REFRESH_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="account-status-refresh",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.navigate(
            interaction,
            context=self._context,
            tab=self._tab,
            page=self._page,
            source_view=self.view,
        )


class AccountStatusCloseButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(self, *, adapter: AccountStatusDiscordAdapter, context: AccountInteractionContext) -> None:
        self._adapter = adapter
        self._context = context
        super().__init__(
            label=copy.CLOSE_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="account-status-close",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.close(interaction, context=self._context, source_view=self.view)


class AccountStatusView(discord.ui.LayoutView):
    """Detached Account status data plus navigation cursor only."""

    def __init__(
        self,
        *,
        adapter: AccountStatusDiscordAdapter,
        context: AccountInteractionContext,
        tab: AccountStatusTab,
        projection: AccountStatusProjection,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.tab = AccountStatusTab(tab)
        self.projection = projection
        page, has_previous, has_next = self._page_state(projection)
        container = discord.ui.Container(discord.ui.TextDisplay(self._render(projection)))
        tab_row = discord.ui.ActionRow()
        for candidate in AccountStatusTab:
            tab_row.add_item(
                AccountStatusTabButton(
                    adapter=adapter,
                    context=context,
                    tab=candidate,
                    current_tab=self.tab,
                )
            )
        navigation_row = discord.ui.ActionRow()
        if self.tab is not AccountStatusTab.OVERVIEW:
            navigation_row.add_item(
                AccountStatusPageButton(
                    adapter=adapter,
                    context=context,
                    tab=self.tab,
                    page=page,
                    direction=-1,
                    disabled=not has_previous,
                )
            )
            navigation_row.add_item(
                AccountStatusPageButton(
                    adapter=adapter,
                    context=context,
                    tab=self.tab,
                    page=page,
                    direction=1,
                    disabled=not has_next,
                )
            )
        navigation_row.add_item(
            AccountStatusRefreshButton(
                adapter=adapter,
                context=context,
                tab=self.tab,
                page=page,
            )
        )
        navigation_row.add_item(AccountStatusCloseButton(adapter=adapter, context=context))
        container.add_item(tab_row)
        container.add_item(navigation_row)
        self.add_item(container)

    @staticmethod
    def _page_state(projection: AccountStatusProjection) -> tuple[int, bool, bool]:
        if isinstance(projection, AccountStatusOverview):
            return 0, False, False
        return projection.page, projection.has_previous, projection.has_next

    @staticmethod
    def _render(projection: AccountStatusProjection) -> str:
        if isinstance(projection, AccountStatusOverview):
            return copy.format_account_overview(projection)
        if isinstance(projection, AccountMatchHistoryPage):
            return copy.format_account_match_history(projection)
        if isinstance(projection, AccountWin5HistoryPage):
            return copy.format_account_win5_history(projection)
        if isinstance(projection, AccountIdentityDetails):
            return copy.format_account_identity(projection)
        raise TypeError("Unsupported Account status projection.")


@dataclass(frozen=True, slots=True)
class AccountStatusDiscordAdapter:
    """Authorize and present one actor's private Account status."""

    queries: AccountStatusQueries
    prepare_command: PrepareDiscordCommand
    authorize_interaction: AuthorizeDiscordInteraction
    run_application: BlockingApplicationRunner = run_blocking_application

    async def show_status(self, interaction: discord.Interaction) -> None:
        if not await self.prepare_command(interaction, _STATUS_COMMAND_NAME, ephemeral=True):
            return
        try:
            context = AccountInteractionContext.from_interaction(interaction)
            projection = await self.run_application(
                lambda: self.queries.get_overview(
                    discord_user_id=str(context.user_id),
                    guild_id=str(context.guild_id),
                )
            )
            view = AccountStatusView(
                adapter=self,
                context=context,
                tab=AccountStatusTab.OVERVIEW,
                projection=projection,
            )
            await interaction.edit_original_response(
                content=None,
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except AccountStatusInvalidSourceError:
            self._log_failure(interaction)
            await send_ephemeral_internal_error_after_defer_safely(interaction, _STATUS_COMMAND_NAME)
        except Exception:
            self._log_failure(interaction)
            await send_ephemeral_internal_error_after_defer_safely(interaction, _STATUS_COMMAND_NAME)

    async def navigate(
        self,
        interaction: discord.Interaction,
        *,
        context: AccountInteractionContext,
        tab: AccountStatusTab,
        page: int,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_bound_update(interaction, context=context, response_kind="navigation"):
            return
        try:
            projection = await self._load(context=context, tab=tab, page=page)
        except AccountStatusInvalidSourceError:
            self._log_failure(interaction)
            await self._send_component_error(
                interaction,
                copy.account_status_internal_error(correlation_id(interaction)),
            )
            return
        except Exception:
            self._log_failure(interaction)
            await self._send_component_error(
                interaction,
                copy.account_status_internal_error(correlation_id(interaction)),
            )
            return
        replacement = AccountStatusView(
            adapter=self,
            context=context,
            tab=tab,
            projection=projection,
        )
        if source_view is not None:
            source_view.stop()
        await self._edit_layout(interaction, view=replacement, response_kind="navigation")

    async def close(
        self,
        interaction: discord.Interaction,
        *,
        context: AccountInteractionContext,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_bound_update(interaction, context=context, response_kind="close"):
            return
        terminal = buttonless_terminal_layout(
            copy.CLOSED,
            timeout_seconds=_COMPONENT_TIMEOUT_SECONDS,
        )
        if source_view is not None:
            source_view.stop()
        await self._edit_layout(interaction, view=terminal, response_kind="close")

    async def _prepare_bound_update(
        self,
        interaction: discord.Interaction,
        *,
        context: AccountInteractionContext,
        response_kind: str,
    ) -> bool:
        if not context.matches(interaction):
            await self._send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
            return False
        if not await self._defer_message_update(interaction, response_kind=response_kind):
            return False
        return await self.authorize_interaction(interaction, _STATUS_COMMAND_NAME)

    @staticmethod
    async def _defer_message_update(
        interaction: discord.Interaction,
        *,
        response_kind: str,
    ) -> bool:
        try:
            await interaction.response.defer(thinking=False)
        except Exception as error:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=%s-defer error_type=%s",
                correlation_id(interaction),
                _STATUS_COMMAND_NAME,
                response_kind,
                type(error).__name__,
            )
            return False
        return True

    @classmethod
    async def _edit_layout(
        cls,
        interaction: discord.Interaction,
        *,
        view: discord.ui.LayoutView,
        response_kind: str,
    ) -> None:
        try:
            await interaction.edit_original_response(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception as error:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=%s error_type=%s",
                correlation_id(interaction),
                _STATUS_COMMAND_NAME,
                response_kind,
                type(error).__name__,
            )
            await cls._send_component_error(interaction, copy.STATUS_TRANSITION_ERROR)

    async def _load(
        self,
        *,
        context: AccountInteractionContext,
        tab: AccountStatusTab,
        page: int,
    ) -> AccountStatusProjection:
        if tab is AccountStatusTab.OVERVIEW:
            return await self.run_application(
                lambda: self.queries.get_overview(
                    discord_user_id=str(context.user_id),
                    guild_id=str(context.guild_id),
                )
            )
        if tab is AccountStatusTab.MATCH:
            return await self.run_application(
                lambda: self.queries.get_match_history(
                    discord_user_id=str(context.user_id),
                    page=page,
                )
            )
        if tab is AccountStatusTab.WIN5:
            return await self.run_application(
                lambda: self.queries.get_win5_history(
                    discord_user_id=str(context.user_id),
                    page=page,
                )
            )
        return await self.run_application(
            lambda: self.queries.get_identity_details(
                discord_user_id=str(context.user_id),
                guild_id=str(context.guild_id),
                page=page,
            )
        )

    @staticmethod
    async def _send_component_error(interaction: discord.Interaction, content: str) -> None:
        try:
            payload = {
                "ephemeral": True,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if interaction.response.is_done():
                await interaction.followup.send(content, **payload)
            else:
                await interaction.response.send_message(content, **payload)
        except Exception:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=component-error",
                correlation_id(interaction),
                _STATUS_COMMAND_NAME,
            )

    @staticmethod
    def _log_failure(interaction: object) -> None:
        logger.error(
            "Discord Account status failed correlation_id=%s command=%s",
            correlation_id(interaction),
            _STATUS_COMMAND_NAME,
        )


class AccountStatusHandler(Protocol):
    async def show_status(self, interaction: discord.Interaction) -> None: ...


class AccountRegistrationHandler(Protocol):
    async def open_registration(self, interaction: discord.Interaction, *, game_region: str) -> None: ...


class AccountCommandGroup(app_commands.Group):
    """V2 `/account` root with reviewed Korean native localization."""

    def __init__(
        self,
        *,
        status_adapter: AccountStatusHandler,
        registration_adapter: AccountRegistrationHandler,
    ) -> None:
        super().__init__(
            name=korean_command_name("account", "계정"),
            description=copy.ACCOUNT_ROOT_DESCRIPTION,
        )
        self._status_adapter = status_adapter
        self._registration_adapter = registration_adapter

    @app_commands.command(
        name=korean_command_name("register", "등록"),
        description=copy.ACCOUNT_REGISTER_DESCRIPTION,
    )
    @app_commands.choices(
        game_region=[
            app_commands.Choice(name="한국 (KR)", value=GameRegion.KR.value),
            app_commands.Choice(name="일본 (JP)", value=GameRegion.JP.value),
        ]
    )
    @app_commands.rename(game_region=REGION_OPTION_NAME)
    @app_commands.describe(game_region="등록할 GameAccount의 리전")
    async def register(
        self,
        interaction: discord.Interaction,
        game_region: app_commands.Choice[str],
    ) -> None:
        await self._registration_adapter.open_registration(
            interaction,
            game_region=game_region.value,
        )

    @app_commands.command(
        name=korean_command_name("status", "상태"),
        description=copy.ACCOUNT_STATUS_DESCRIPTION,
    )
    async def status(self, interaction: discord.Interaction) -> None:
        await self._status_adapter.show_status(interaction)
