from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal

import discord
from discord import app_commands
from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.adapters.discord import account as account_adapter
from umacircle_bot.adapters.discord import match_member as match_member_adapter
from umacircle_bot.adapters.discord import match_staff_lifecycle as match_staff_lifecycle_adapter
from umacircle_bot.adapters.discord import match_staff_results as match_staff_result_adapter
from umacircle_bot.adapters.discord import match_staff_settlement as match_staff_settlement_adapter
from umacircle_bot.adapters.discord import settings as settings_adapter
from umacircle_bot.adapters.discord import staff_account as staff_account_adapter
from umacircle_bot.adapters.discord.account import AccountAdapter, AccountAdapterPorts
from umacircle_bot.adapters.discord.common import InteractionContext
from umacircle_bot.adapters.discord.common import permissions as discord_permissions
from umacircle_bot.adapters.discord.common.delivery import (
    correlation_id as _correlation_id,
)
from umacircle_bot.adapters.discord.common.delivery import (
    interaction_user_id as _interaction_user_id,
)
from umacircle_bot.adapters.discord.common.delivery import (
    send_followup_safely as _send_followup_safely,
)
from umacircle_bot.adapters.discord.common.delivery import (
    send_initial_response_safely as _send_initial_response_safely,
)
from umacircle_bot.adapters.discord.common.errors import (
    handle_modal_open_error as _handle_modal_open_error,
)
from umacircle_bot.adapters.discord.common.errors import (
    send_internal_error as _send_internal_error,
)
from umacircle_bot.adapters.discord.common.errors import (
    send_pre_modal_user_error as _send_pre_modal_user_error,
)
from umacircle_bot.adapters.discord.common.errors import (
    send_user_error as _send_user_error,
)
from umacircle_bot.adapters.discord.common.permissions import (
    COMMAND_ACCESS_MATRIX,
    COMMAND_CHANNEL_SCOPE_MATRIX,
    CommandAccess,
    CommandChannelScope,
    GuildRoleAuthorization,
)
from umacircle_bot.adapters.discord.common.preparation import (
    CommandPreparation,
    CommandPreparationPorts,
)
from umacircle_bot.adapters.discord.common.responses import (
    bounded_discord_message as _bounded_discord_message,
)
from umacircle_bot.adapters.discord.common.responses import (
    safe_discord_text as _safe_discord_text,
)
from umacircle_bot.adapters.discord.export import (
    ExportAdapterPorts,
    ExportCommandGroup,
)
from umacircle_bot.adapters.discord.export import (
    create_export_command_group as _create_export_command_group,
)
from umacircle_bot.adapters.discord.match_member import (
    MatchMemberAdapter,
    MatchMemberAdapterPorts,
)
from umacircle_bot.adapters.discord.match_staff import (
    MatchStaffAdapter,
    MatchStaffAdapterPorts,
)
from umacircle_bot.adapters.discord.player_link import (
    PlayerLinkAdapter,
    PlayerLinkAdapterPorts,
)
from umacircle_bot.adapters.discord.player_link import (
    close_view as _close_extracted_player_link_view,
)
from umacircle_bot.adapters.discord.settings import (
    SettingsAdapter,
    SettingsAdapterPorts,
    SettingsCommandGroup,
)
from umacircle_bot.adapters.discord.settings import (
    create_settings_command_group as _create_settings_command_group,
)
from umacircle_bot.adapters.discord.staff_account import (
    StaffAccountAdapter,
    StaffAccountAdapterPorts,
)
from umacircle_bot.adapters.discord.staff_persona import (
    StaffPersonaAdapter,
    StaffPersonaAdapterPorts,
)
from umacircle_bot.adapters.discord.staff_player_link import (
    StaffPlayerLinkAdapter,
    StaffPlayerLinkAdapterPorts,
)
from umacircle_bot.adapters.discord.staff_player_link import (
    format_resolution_history as _format_extracted_player_link_resolution_history,
)
from umacircle_bot.adapters.discord.staff_player_link import (
    interaction_context as _extracted_player_link_interaction_context,
)
from umacircle_bot.adapters.discord.staff_player_link import (
    publish_resolution_history as _publish_extracted_player_link_resolution_history,
)
from umacircle_bot.config import Settings, get_settings
from umacircle_bot.db.models import (
    Race,
    Win5Result,
    Win5Round,
    Win5RoundRace,
    Win5Season,
)
from umacircle_bot.discord_delivery import deliver_discord_publication_once
from umacircle_bot.discord_player_link_ui import (
    PlayerLinkInteractionContext,
    PlayerLinkNoCandidateView,
)
from umacircle_bot.domain.errors import DomainError, MatchResultError
from umacircle_bot.domain.guild_discord_settings import GuildDiscordSettingsValues
from umacircle_bot.domain.match_results import MatchResultInput
from umacircle_bot.domain.win5 import Win5RoundStatus, Win5RoundType, Win5SeasonStatus
from umacircle_bot.logging_safety import log_sanitized_exception
from umacircle_bot.services import (
    match_lifecycle_application,
    match_result_application,
    match_settlement_application,
)
from umacircle_bot.services.account_application import (
    StaffAccountPointCommand,
    execute_account_registration_approve,
    execute_account_registration_cancel,
    execute_account_registration_reject,
    execute_account_registration_submit,
    execute_discord_nickname_refresh,
    execute_staff_ingame_name_update,
    execute_staff_room_point_adjustment,
    execute_staff_room_point_grant,
    query_account_overview,
    query_owned_account_registration_request,
    query_staff_registered_accounts,
)
from umacircle_bot.services.accounts import resolve_owned_game_account
from umacircle_bot.services.application import (
    run_application_command,
    run_application_query,
    run_application_transaction,
)
from umacircle_bot.services.autocomplete_queries import (
    AutocompleteChoice,
    RoomRaceChoicePurpose,
    Win5RoundChoicePurpose,
    autocomplete_active_win5_rounds,
    autocomplete_member_cancellable_win5_submissions,
    autocomplete_room_races,
    autocomplete_staff_win5_seasons,
    autocomplete_win5_rounds,
)
from umacircle_bot.services.circle_point_export import export_circle_point_snapshot, remove_circle_point_export_artifact
from umacircle_bot.services.dtos import (
    AccountOverviewDTO,
    AccountRegistrationMutationDTO,
    AccountRegistrationRequestDTO,
    CirclePointTransactionDTO,
    GameAccountDTO,
    PlayerLinkApprovalDTO,
    PlayerLinkCandidateDTO,
    PlayerLinkMutationDTO,
    PlayerLinkRequestDTO,
    RaceOperationResultDTO,
    Win5EntryDTO,
    Win5MutationResultDTO,
    Win5ResultMutationDTO,
    Win5RoundDTO,
    Win5ScoringResultDTO,
    Win5SeasonInfoDTO,
    Win5StandingDTO,
    Win5SubmissionRoundDTO,
)
from umacircle_bot.services.guild_discord_settings import (
    GuildDiscordSettingsDTO,
    GuildDiscordSettingsMutationDTO,
    UpdateGuildDiscordSettingsCommand,
)
from umacircle_bot.services.match_member_application import (
    PlaceMemberMatchBetCommand,
    execute_member_match_bet,
    query_member_match_bet_accounts,
    query_member_match_bet_races,
    query_open_member_match_races,
)
from umacircle_bot.services.match_odds_snapshots import MatchOddsInput
from umacircle_bot.services.match_races import (
    RegisteredFinalEntrySnapshotInput,
)
from umacircle_bot.services.match_reporting import GameAccountRatingDTO
from umacircle_bot.services.match_reporting_application import query_game_account_rating_report
from umacircle_bot.services.match_result_notifications import MatchResultNotificationDTO
from umacircle_bot.services.match_results import MatchResultOperationDTO
from umacircle_bot.services.match_settlement import MatchSettlementOperationDTO
from umacircle_bot.services.match_settlement_rollback import MatchSettlementRollbackDTO
from umacircle_bot.services.persona_discord_link_application import (
    StaffDiscordPersonaAttachCommand,
    StaffDiscordPersonaAttachPreviewDTO,
    StaffDiscordPersonaAttachResultDTO,
    StaffPersonaChoiceDTO,
    execute_staff_discord_persona_attach,
    preview_staff_discord_persona_attach,
    query_staff_persona_choices,
)
from umacircle_bot.services.persona_game_account_claim_application import (
    StaffSourceGameAccountChoiceDTO,
    StaffSourceGameAccountClaimCommand,
    StaffSourceGameAccountClaimPreviewDTO,
    StaffSourceGameAccountClaimResultDTO,
    execute_staff_source_game_account_claim,
    preview_staff_source_game_account_claim,
    query_staff_source_game_account_choices,
)
from umacircle_bot.services.player_link_application import (
    execute_player_link_approve,
    execute_player_link_cancel,
    execute_player_link_reject,
    execute_player_link_review,
    execute_player_link_revise,
    execute_player_link_submit,
    query_active_player_link_request,
    query_owned_player_link_request,
    query_player_link_candidates,
    query_player_link_candidates_for_terms,
    query_staff_player_link_queue,
    query_staff_player_link_request,
)
from umacircle_bot.services.player_link_requests import (
    ApprovePlayerLinkRequestCommand,
    CancelPlayerLinkRequestCommand,
    RejectPlayerLinkRequestCommand,
    ReviewPlayerLinkRequestCommand,
    RevisePlayerLinkRequestCommand,
    SubmitPlayerLinkRequestCommand,
)
from umacircle_bot.services.race_queries import OpenMatchRace
from umacircle_bot.services.registration_requests import (
    ApproveAccountRegistrationRequestCommand,
    CancelAccountRegistrationRequestCommand,
    RejectAccountRegistrationRequestCommand,
    SubmitAccountRegistrationRequestCommand,
)
from umacircle_bot.services.settings_application import (
    execute_guild_discord_settings_update,
    query_guild_discord_settings,
)
from umacircle_bot.services.win5 import (
    CancelWin5SubmissionCommand,
    CorrectWin5ResultCommand,
    CreateWin5RoundCommand,
    CreateWin5SeasonCommand,
    CreateWin5SpecialRoundCommand,
    EnterWin5ResultCommand,
    ScoreWin5RoundCommand,
    Win5RoundEntryInput,
    Win5RoundTransitionCommand,
    Win5SeasonTransitionCommand,
    Win5SpecialRaceInput,
    activate_win5_season,
    cancel_win5_season,
    cancel_win5_submission,
    close_win5_round,
    close_win5_season,
    correct_win5_result,
    create_win5_round,
    create_win5_season,
    create_win5_special_round,
    enter_win5_result,
    get_active_win5_season_info,
    list_active_win5_submission_rounds,
    list_open_win5_rounds,
    list_win5_standings,
    list_win5_submissions,
    open_win5_round,
    score_win5_round,
    submit_win5_prediction,
    submit_win5_special_prediction,
)
from umacircle_bot.services.win5_export import execute_win5_season_export

logger = logging.getLogger(__name__)
SettingsInteractionContext = InteractionContext
GeneralSettingsModal = settings_adapter.GeneralSettingsModal
SettingsChannelReasonModal = settings_adapter.SettingsChannelReasonModal
SettingsChannelSelect = settings_adapter.SettingsChannelSelect
SettingsConfirmView = settings_adapter.SettingsConfirmView
SettingsGeneralPanelView = settings_adapter.SettingsGeneralPanelView
SettingsRolePanelView = settings_adapter.SettingsRolePanelView
SettingsRoleReasonModal = settings_adapter.SettingsRoleReasonModal
SettingsRoleSelect = settings_adapter.SettingsRoleSelect
_allowed_channel_ids_for_scope = discord_permissions.allowed_channel_ids_for_scope
_interaction_access_error = discord_permissions.interaction_access_error
_member_role_ids = discord_permissions.member_role_ids
can_operate_member = discord_permissions.can_operate_member


async def _autocomplete_season_activate(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_staff_seasons(
        interaction,
        current,
        command_name="win5.staff.season",
        allowed_statuses=(Win5SeasonStatus.DRAFT.value,),
    )


async def _autocomplete_season_close(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_staff_seasons(
        interaction,
        current,
        command_name="win5.staff.season",
        allowed_statuses=(Win5SeasonStatus.ACTIVE.value,),
    )


async def _autocomplete_season_for_round_create(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_staff_seasons(
        interaction,
        current,
        command_name="win5.staff.round",
        allowed_statuses=(
            Win5SeasonStatus.DRAFT.value,
            Win5SeasonStatus.ACTIVE.value,
        ),
    )


async def _autocomplete_season_for_special_round_create(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_staff_seasons(
        interaction,
        current,
        command_name="win5.staff.round",
        allowed_statuses=(
            Win5SeasonStatus.DRAFT.value,
            Win5SeasonStatus.ACTIVE.value,
        ),
    )


async def _autocomplete_member_active_season(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    command_name = _member_win5_submission_command_name(interaction)
    return await _autocomplete_staff_seasons(
        interaction,
        current,
        command_name=command_name,
        allowed_statuses=(Win5SeasonStatus.ACTIVE.value,),
    )


async def _autocomplete_member_standings_season(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_staff_seasons(
        interaction,
        current,
        command_name="win5.standings",
        allowed_statuses=(
            Win5SeasonStatus.ACTIVE.value,
            Win5SeasonStatus.CLOSED.value,
        ),
    )


async def _autocomplete_export_win5_season(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_staff_seasons(
        interaction,
        current,
        command_name="export.win5.season",
        allowed_statuses=(
            Win5SeasonStatus.ACTIVE.value,
            Win5SeasonStatus.CLOSED.value,
        ),
    )


async def _autocomplete_member_open_round(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    command_name = _member_win5_submission_command_name(interaction)
    if not await _autocomplete_interaction_authorized(interaction, command_name):
        return []
    season_id = getattr(getattr(interaction, "namespace", None), "season_id", None)
    if not isinstance(season_id, int) or isinstance(season_id, bool) or season_id <= 0:
        return []
    return await _run_autocomplete_query(
        interaction,
        command_name=command_name,
        loader=lambda: run_application_query(
            lambda session: autocomplete_win5_rounds(
                session,
                season_id=season_id,
                purpose=Win5RoundChoicePurpose.SUBMIT,
                query=current,
            )
        ),
    )


async def _autocomplete_member_cancellable_submission(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    command_name = "win5.cancel"
    if not await _autocomplete_interaction_authorized(interaction, command_name):
        return []
    return await _run_autocomplete_query(
        interaction,
        command_name=command_name,
        loader=lambda: run_application_query(
            lambda session: autocomplete_member_cancellable_win5_submissions(
                session,
                discord_user_id=str(interaction.user.id),
                query=current,
            )
        ),
    )


async def _autocomplete_room_race_edit(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_room_race(
        interaction,
        current,
        command_name="match.staff.race-edit",
        purpose=RoomRaceChoicePurpose.EDIT,
    )


async def _autocomplete_room_race_condition(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_room_race(
        interaction,
        current,
        command_name="match.staff.race-condition-set",
        purpose=RoomRaceChoicePurpose.EDIT,
    )


async def _autocomplete_room_race_entries(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_room_race(
        interaction,
        current,
        command_name="match.staff.entries-set",
        purpose=RoomRaceChoicePurpose.ENTRIES,
    )


async def _autocomplete_room_race_open(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_room_race(
        interaction,
        current,
        command_name="match.staff.betting-open",
        purpose=RoomRaceChoicePurpose.OPEN,
    )


async def _autocomplete_room_race_close(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_room_race(
        interaction,
        current,
        command_name="match.staff.betting-close",
        purpose=RoomRaceChoicePurpose.CLOSE,
    )


async def _autocomplete_room_race_show(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_room_race(
        interaction,
        current,
        command_name="match.staff.race-show",
        purpose=RoomRaceChoicePurpose.SHOW,
    )


async def _autocomplete_room_race_bet(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    """Compatibility shim for the extracted Match member autocomplete."""

    return await _MATCH_MEMBER_ADAPTER.autocomplete_bet_race(interaction, current)


async def _autocomplete_room_result_submit(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_room_race(
        interaction,
        current,
        command_name="match.staff.result-submit",
        purpose=RoomRaceChoicePurpose.RESULT,
        allowed_statuses=("betting_closed",),
    )


async def _autocomplete_room_result_review(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_room_race(
        interaction,
        current,
        command_name="match.staff.result-review",
        purpose=RoomRaceChoicePurpose.RESULT,
        allowed_statuses=("result_review",),
    )


async def _autocomplete_room_result_correct(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_room_race(
        interaction,
        current,
        command_name="match.staff.result-correct",
        purpose=RoomRaceChoicePurpose.RESULT,
        allowed_statuses=("result_review",),
    )


async def _autocomplete_room_result_reject(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_room_race(
        interaction,
        current,
        command_name="match.staff.result-reject",
        purpose=RoomRaceChoicePurpose.RESULT,
        allowed_statuses=("result_review",),
    )


async def _autocomplete_room_result_confirm(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_room_race(
        interaction,
        current,
        command_name="match.staff.result-confirm",
        purpose=RoomRaceChoicePurpose.RESULT,
        allowed_statuses=("result_review",),
    )


async def _autocomplete_room_result_show(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_room_race(
        interaction,
        current,
        command_name="match.staff.result-show",
        purpose=RoomRaceChoicePurpose.SHOW,
    )


async def _autocomplete_room_settlement(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_room_race(
        interaction,
        current,
        command_name="match.staff.settlement",
        purpose=RoomRaceChoicePurpose.RESULT,
        allowed_statuses=("result_confirmed",),
    )


async def _autocomplete_room_publish(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    return await _autocomplete_room_race(
        interaction,
        current,
        command_name="match.staff.publish",
        purpose=RoomRaceChoicePurpose.RESULT,
        allowed_statuses=("settled",),
    )


async def _autocomplete_staff_seasons(
    interaction: discord.Interaction,
    current: str,
    *,
    command_name: str,
    allowed_statuses: tuple[str, ...],
) -> list[app_commands.Choice[int]]:
    if not await _autocomplete_interaction_authorized(interaction, command_name):
        return []
    return await _run_autocomplete_query(
        interaction,
        command_name=command_name,
        loader=lambda: run_application_query(
            lambda session: autocomplete_staff_win5_seasons(
                session,
                allowed_statuses=allowed_statuses,
                query=current,
            )
        ),
    )


async def _autocomplete_room_race(
    interaction: discord.Interaction,
    current: str,
    *,
    command_name: str,
    purpose: RoomRaceChoicePurpose,
    allowed_statuses: tuple[str, ...] | None = None,
) -> list[app_commands.Choice[int]]:
    if not await _autocomplete_interaction_authorized(interaction, command_name):
        return []
    return await _run_autocomplete_query(
        interaction,
        command_name=command_name,
        loader=lambda: _room_race_autocomplete_query(
            purpose=purpose,
            query=current,
            allowed_statuses=allowed_statuses,
        ),
    )


async def _autocomplete_circle_point_account(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    command_name = "staff.adjust-circle-points"
    if not await _autocomplete_interaction_authorized(interaction, command_name):
        return []
    return await _run_autocomplete_query(
        interaction,
        command_name=command_name,
        loader=lambda: _staff_account_query_registered_accounts(query=current),
    )


async def _autocomplete_staff_change_name_account(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    command_name = "staff.change-name"
    if not await _autocomplete_interaction_authorized(interaction, command_name):
        return []
    return await _run_autocomplete_query(
        interaction,
        command_name=command_name,
        loader=lambda: _staff_account_query_registered_accounts(query=current),
    )


def _room_race_autocomplete_query(
    *,
    purpose: RoomRaceChoicePurpose,
    query: str,
    allowed_statuses: tuple[str, ...] | None,
) -> tuple[AutocompleteChoice, ...]:
    return match_lifecycle_application.query_match_race_choices(
        purpose=purpose,
        query=query,
        allowed_statuses=allowed_statuses,
    )


async def _run_autocomplete_query(
    interaction: discord.Interaction,
    *,
    command_name: str,
    loader: Callable[[], tuple[AutocompleteChoice, ...]],
) -> list[app_commands.Choice[int]]:
    try:
        choices = await asyncio.to_thread(loader)
    except Exception:
        log_sanitized_exception(
            logger,
            "discord autocomplete failed correlation_id=%s command=%s actor_id=%s",
            _correlation_id(interaction),
            command_name,
            _interaction_user_id(interaction),
        )
        return []
    return [app_commands.Choice(name=choice.label, value=choice.value) for choice in choices[:25]]


async def _autocomplete_interaction_authorized(
    interaction: discord.Interaction,
    command_name: str,
) -> bool:
    guild_id = getattr(interaction, "guild_id", None)
    if not isinstance(guild_id, int) or isinstance(guild_id, bool) or guild_id <= 0:
        return False
    try:
        settings = get_settings()
        access = COMMAND_ACCESS_MATRIX[command_name]
        channel_scope = COMMAND_CHANNEL_SCOPE_MATRIX[command_name]
        authorization = await _interaction_role_authorization(
            interaction,
            settings=settings,
            access=access,
        )
        channel_settings = await _interaction_channel_settings(
            interaction,
            settings=settings,
            channel_scope=channel_scope,
        )
        return (
            _interaction_access_error(
                interaction,
                settings=settings,
                access=access,
                channel_scope=channel_scope,
                channel_settings=channel_settings,
                role_authorization=authorization,
            )
            is None
        )
    except Exception:
        return False


def _member_win5_submission_command_name(interaction: discord.Interaction) -> str:
    command_name = getattr(getattr(interaction, "command", None), "name", None)
    return "win5.special-submit" if command_name == "special-submit" else "win5.submit"


def _optional_modal_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _selected_modal_id(
    selection: discord.ui.Select,
    *,
    allowed_ids: frozenset[int],
    field_name: str,
) -> int:
    if len(selection.values) != 1:
        raise ValueError(f"{field_name}을(를) 하나 선택해야 합니다.")
    try:
        selected_id = int(selection.values[0])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 선택값이 올바르지 않습니다.") from exc
    if selected_id not in allowed_ids:
        raise ValueError(f"{field_name} 선택값이 현재 허용되지 않습니다.")
    return selected_id


def _selected_modal_ids(
    selection: discord.ui.Select,
    *,
    allowed_ids: frozenset[int],
    field_name: str,
) -> tuple[int, ...]:
    if not selection.values:
        raise ValueError(f"{field_name}을(를) 하나 이상 선택해야 합니다.")
    try:
        selected_ids = tuple(sorted({int(value) for value in selection.values}))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 선택값이 올바르지 않습니다.") from exc
    if len(selected_ids) != len(selection.values):
        raise ValueError(f"{field_name}을(를) 중복 선택할 수 없습니다.")
    if not set(selected_ids).issubset(allowed_ids):
        raise ValueError(f"{field_name} 선택값이 현재 허용되지 않습니다.")
    return selected_ids


class Win5SeasonCreateModal(discord.ui.Modal, title="WIN5 시즌 생성"):
    def __init__(self) -> None:
        super().__init__(timeout=600)
        self.name = discord.ui.TextInput(
            custom_id="win5-season-create-name",
            placeholder="시즌 표시명",
            required=True,
            min_length=1,
            max_length=100,
        )
        self.starts_at = discord.ui.TextInput(
            custom_id="win5-season-create-starts-at",
            placeholder="선택: 2026-08-01 00:00 (KST)",
            required=False,
            max_length=16,
        )
        self.ends_at = discord.ui.TextInput(
            custom_id="win5-season-create-ends-at",
            placeholder="선택: 2026-12-31 23:59 (KST)",
            required=False,
            max_length=16,
        )
        self.reason = discord.ui.TextInput(
            custom_id="win5-season-create-reason",
            style=discord.TextStyle.paragraph,
            placeholder="선택: 특이사항 또는 운영 메모",
            required=False,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="시즌명", component=self.name))
        self.add_item(
            discord.ui.Label(
                text="시작 시각",
                description="선택 입력이며 기본 시간대는 KST입니다.",
                component=self.starts_at,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="종료 시각",
                description="선택 입력이며 기본 시간대는 KST입니다.",
                component=self.ends_at,
            )
        )
        self.add_item(discord.ui.Label(text="운영 메모 (선택)", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        command_name = "win5.staff.season"
        if await _prepare_command(interaction, command_name) is None:
            return
        try:
            result = await asyncio.to_thread(
                _create_win5_season,
                str(self.name.value),
                _optional_modal_kst_datetime_iso(self.starts_at.value),
                _optional_modal_kst_datetime_iso(self.ends_at.value),
                _optional_modal_text(self.reason.value),
                str(interaction.user.id),
                str(interaction.id),
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(interaction, command_name, "시즌을 생성하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_win5_mutation_result(interaction, command_name, result)


class Win5SeasonTransitionModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        choices: tuple[AutocompleteChoice, ...],
        action: Literal["activate", "close", "cancel"],
    ) -> None:
        action_label = {
            "activate": "활성화",
            "close": "종료",
            "cancel": "삭제",
        }[action]
        super().__init__(title=f"WIN5 시즌 {action_label}", timeout=600)
        self._action = action
        self._allowed_ids = frozenset(choice.value for choice in choices)
        self.target = discord.ui.Select(
            custom_id=f"win5-season-{action}-target",
            placeholder=f"{action_label}할 시즌 선택",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=choice.label, value=str(choice.value)) for choice in choices],
            required=True,
        )
        self.reason = discord.ui.TextInput(
            custom_id=f"win5-season-{action}-reason",
            style=discord.TextStyle.paragraph,
            placeholder=(
                "삭제 사유를 입력해 주세요." if action == "cancel" else f"선택: 시즌 {action_label} 관련 특이사항"
            ),
            required=action == "cancel",
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="대상 시즌", component=self.target))
        self.add_item(
            discord.ui.Label(
                text="삭제 사유" if action == "cancel" else "운영 메모 (선택)",
                component=self.reason,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        command_name = "win5.staff.season"
        if await _prepare_command(interaction, command_name) is None:
            return
        try:
            season_id = _selected_modal_id(
                self.target,
                allowed_ids=self._allowed_ids,
                field_name="대상 시즌",
            )
            operation = {
                "activate": _activate_win5_season,
                "close": _close_win5_season,
                "cancel": _cancel_win5_season,
            }[self._action]
            result = await asyncio.to_thread(
                operation,
                season_id,
                _optional_modal_text(self.reason.value),
                str(interaction.user.id),
                str(interaction.id),
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(
                interaction,
                command_name,
                "시즌 상태를 변경하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_win5_mutation_result(interaction, command_name, result)


class Win5RoundEntriesModal(discord.ui.Modal, title="WIN5 엔트리 입력"):
    def __init__(
        self,
        *,
        season_id: int,
        round_number: int,
        race_name: str,
        timezone_name: Literal["KST", "UTC"],
        round_label: str | None,
        event_id: int | None,
        opens_at: str | None,
        closes_at: str | None,
        reason: str | None,
    ) -> None:
        super().__init__(timeout=600)
        self._season_id = season_id
        self._round_number = round_number
        self._race_name = race_name
        self._timezone_name = timezone_name
        self._round_label = round_label
        self._event_id = event_id
        self._opens_at = opens_at
        self._closes_at = closes_at
        self._reason = reason
        self.race_date = discord.ui.TextInput(
            label="경기 날짜",
            placeholder="2026-07-27",
            style=discord.TextStyle.short,
            required=True,
            min_length=10,
            max_length=10,
        )
        self.race_time = discord.ui.TextInput(
            label="경기 시간",
            placeholder="18:00",
            style=discord.TextStyle.short,
            required=True,
            min_length=5,
            max_length=5,
        )
        self.entries = discord.ui.TextInput(
            label="엔트리",
            placeholder="스페셜 위크\n사일런스 스즈카\n토카이 테이오",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.add_item(self.race_date)
        self.add_item(self.race_time)
        self.add_item(self.entries)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if await _prepare_command(interaction, "win5.staff.round") is None:
            return
        try:
            starts_at = parse_operator_date_and_time(
                str(self.race_date.value),
                str(self.race_time.value),
                self._timezone_name,
            ).isoformat()
            result = await asyncio.to_thread(
                _create_win5_round,
                self._season_id,
                self._round_number,
                self._race_name,
                starts_at,
                str(self.entries.value),
                self._round_label,
                self._event_id,
                self._opens_at,
                self._closes_at,
                self._reason,
                str(interaction.user.id),
                str(interaction.id),
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(
                interaction,
                "win5.staff.round",
                "라운드를 생성하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await _send_internal_error(interaction, "win5.staff.round")
            return
        await _send_win5_mutation_result(interaction, "win5.staff.round", result)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_sanitized_exception(
            logger,
            "discord modal failed correlation_id=%s command=%s actor_id=%s",
            _correlation_id(interaction),
            "win5.staff.round",
            _interaction_user_id(interaction),
        )
        if not interaction.response.is_done():
            await _send_initial_response_safely(
                interaction,
                "win5.staff.round",
                f"내부 오류가 발생했습니다. 요청 ID: `{_correlation_id(interaction)}`",
            )


@dataclass(frozen=True, slots=True)
class Win5RoundCreateSeasonOption:
    season_id: int
    label: str
    next_round_number: int


class Win5RoundCreateModal(discord.ui.Modal, title="WIN5 일반 라운드 생성"):
    def __init__(self, *, season: Win5RoundCreateSeasonOption) -> None:
        super().__init__(timeout=600)
        self._season = season
        self.round_number = discord.ui.TextInput(
            custom_id="win5-round-create-number",
            default=str(season.next_round_number),
            placeholder=f"예: {season.next_round_number}",
            required=True,
            max_length=9,
        )
        self.race_name = discord.ui.TextInput(
            custom_id="win5-round-create-race-name",
            placeholder="예: 우마 스테이크스",
            required=True,
            max_length=240,
        )
        self.starts_at = discord.ui.TextInput(
            custom_id="win5-round-create-starts-at",
            placeholder="2026-07-27 18:00 (KST)",
            required=True,
            min_length=16,
            max_length=16,
        )
        self.entries = discord.ui.TextInput(
            custom_id="win5-round-create-entries",
            placeholder="출주 번호 순서대로 말 이름\n스페셜 위크\n사일런스 스즈카",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.add_item(
            discord.ui.Label(
                text=f"라운드 번호 (생성 가능 라운드 번호: {season.next_round_number})",
                component=self.round_number,
            )
        )
        self.add_item(discord.ui.Label(text="경기명", component=self.race_name))
        self.add_item(
            discord.ui.Label(
                text="경기 시작 시각",
                description="기본 시간대는 KST이며 DB에는 UTC로 저장됩니다.",
                component=self.starts_at,
            )
        )
        self.add_item(discord.ui.Label(text="엔트리", component=self.entries))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        command_name = "win5.staff.round"
        if await _prepare_command(interaction, command_name) is None:
            return
        try:
            round_number = parse_win5_round_number(str(self.round_number.value))
            race_name = _optional_modal_text(self.race_name.value)
            if race_name is None:
                raise ValueError("경기명은 비워둘 수 없습니다.")
            result = await asyncio.to_thread(
                _create_win5_round,
                self._season.season_id,
                round_number,
                race_name,
                parse_operator_kst_datetime(str(self.starts_at.value)).isoformat(),
                str(self.entries.value),
                None,
                None,
                None,
                None,
                None,
                str(interaction.user.id),
                str(interaction.id),
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(interaction, command_name, "라운드를 생성하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_win5_mutation_result(interaction, command_name, result)


class Win5SpecialRoundCreateModal(discord.ui.Modal, title="WIN5 특별 라운드 생성"):
    def __init__(
        self,
        *,
        season_choices: tuple[AutocompleteChoice, ...],
    ) -> None:
        super().__init__(timeout=600)
        self._allowed_season_ids = frozenset(choice.value for choice in season_choices)
        self.season = discord.ui.Select(
            custom_id="win5-special-create-season",
            placeholder="라운드를 생성할 시즌 선택",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=choice.label, value=str(choice.value)) for choice in season_choices],
            required=True,
        )
        self.races = discord.ui.TextInput(
            custom_id="win5-special-create-races",
            style=discord.TextStyle.paragraph,
            placeholder="경기명 (여러 경기면 줄바꿈)",
            required=True,
            max_length=4000,
        )
        self.round_info = discord.ui.TextInput(
            custom_id="win5-special-create-info",
            placeholder="특별 이벤트 이름",
            required=True,
            max_length=64,
        )
        self.reason = discord.ui.TextInput(
            custom_id="win5-special-create-reason",
            style=discord.TextStyle.paragraph,
            placeholder="선택: 특이사항 또는 운영 메모",
            required=False,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="대상 시즌", component=self.season))
        self.add_item(
            discord.ui.Label(
                text="독립 경기",
                description="한 줄에 경기명 하나씩 입력하세요.",
                component=self.races,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="라운드명",
                description="라운드 번호는 시즌 내 다음 번호로 자동 배정됩니다.",
                component=self.round_info,
            )
        )
        self.add_item(discord.ui.Label(text="운영 메모 (선택)", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        command_name = "win5.staff.round"
        if await _prepare_command(interaction, command_name) is None:
            return
        try:
            season_id = _selected_modal_id(
                self.season,
                allowed_ids=self._allowed_season_ids,
                field_name="대상 시즌",
            )
            round_label = _optional_modal_text(self.round_info.value)
            if round_label is None:
                raise ValueError("라운드명을 입력해야 합니다.")
            result = await asyncio.to_thread(
                _create_win5_special_round,
                season_id,
                None,
                str(self.races.value),
                round_label,
                None,
                None,
                _optional_modal_text(self.reason.value),
                str(interaction.user.id),
                str(interaction.id),
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(
                interaction,
                command_name,
                "특별 라운드를 생성하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_win5_mutation_result(interaction, command_name, result)


@dataclass(frozen=True, slots=True)
class Win5ResultTargetOption:
    token: int
    round_id: int
    race_id: int
    label: str


@dataclass(frozen=True, slots=True)
class Win5SpecialResultRaceOption:
    race_id: int
    display_order: int
    race_name: str


@dataclass(frozen=True, slots=True)
class Win5SpecialResultTargetOption:
    round_id: int
    label: str
    races: tuple[Win5SpecialResultRaceOption, ...]


class Win5ResultEnterModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        targets: tuple[Win5ResultTargetOption, ...],
        correcting: bool = False,
    ) -> None:
        super().__init__(title="WIN5 결과 정정" if correcting else "WIN5 결과 입력", timeout=600)
        self._target_by_token = {target.token: target for target in targets}
        self._correcting = correcting
        self.target = discord.ui.Select(
            custom_id="win5-result-enter-target",
            placeholder="결과를 입력할 라운드·경기 선택",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=target.label, value=str(target.token)) for target in targets],
            required=True,
        )
        self.result_order = discord.ui.TextInput(
            custom_id="win5-result-enter-order",
            placeholder="일반: 1착부터 5착 (1-3-5-2-4)\n특별: 1착 번호 하나 (3)",
            required=True,
            max_length=100,
        )
        self.reason = discord.ui.TextInput(
            custom_id="win5-result-enter-reason",
            style=discord.TextStyle.paragraph,
            placeholder="정정 사유" if correcting else "선택: 결과 입력 사유",
            required=correcting,
            min_length=1 if correcting else None,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="대상 라운드·경기", component=self.target))
        self.add_item(discord.ui.Label(text="정정 결과" if correcting else "확정 결과", component=self.result_order))
        self.add_item(
            discord.ui.Label(
                text="정정 사유 (필수)" if correcting else "운영 메모 (선택)",
                component=self.reason,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        command_name = "win5.staff.round"
        if await _prepare_command(interaction, command_name) is None:
            return
        try:
            if len(self.target.values) != 1:
                raise ValueError("결과를 입력할 경기를 하나 선택해야 합니다.")
            token = int(self.target.values[0])
            target = self._target_by_token.get(token)
            if target is None:
                raise ValueError("선택한 결과 대상이 현재 허용되지 않습니다.")
            operation = _correct_win5_result if self._correcting else _enter_win5_result
            result = await asyncio.to_thread(
                operation,
                target.round_id,
                str(self.result_order.value),
                target.race_id,
                str(self.reason.value) if self._correcting else _optional_modal_text(self.reason.value),
                str(interaction.user.id),
                str(interaction.id),
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(
                interaction,
                command_name,
                "결과를 정정하지 못했습니다" if self._correcting else "결과를 입력하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_win5_result_mutation(interaction, command_name, result)


class Win5SpecialResultScoreModal(discord.ui.Modal, title="WIN5 특별 라운드 결과·채점"):
    def __init__(self, *, targets: tuple[Win5SpecialResultTargetOption, ...]) -> None:
        super().__init__(timeout=600)
        self._target_by_round_id = {target.round_id: target for target in targets}
        self.target = discord.ui.Select(
            custom_id="win5-special-result-score-target",
            placeholder="결과를 입력하고 채점할 특별 라운드 선택",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=target.label,
                    value=str(target.round_id),
                    description=_modal_option_label(
                        " / ".join(f"{race.display_order}. {race.race_name}" for race in target.races)
                        or "점수에서 제외되지 않은 경기가 없습니다."
                    ),
                )
                for target in targets
            ],
            required=True,
        )
        self.winners = discord.ui.TextInput(
            custom_id="win5-special-result-score-winners",
            placeholder="1경기: 3 / 여러 경기: 1-2-3-4-5",
            required=False,
            max_length=100,
        )
        self.reason = discord.ui.TextInput(
            custom_id="win5-special-result-score-reason",
            style=discord.TextStyle.paragraph,
            placeholder="선택: 결과·채점 관련 특이사항",
            required=False,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="대상 특별 라운드", component=self.target))
        self.add_item(
            discord.ui.Label(
                text="경기별 1착 번호",
                description="드롭다운 설명의 경기 순서와 같은 순서로 입력하세요.",
                component=self.winners,
            )
        )
        self.add_item(discord.ui.Label(text="운영 메모 (선택)", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        command_name = "win5.staff.round"
        if await _prepare_command(interaction, command_name) is None:
            return
        try:
            round_id = _selected_modal_id(
                self.target,
                allowed_ids=frozenset(self._target_by_round_id),
                field_name="대상 특별 라운드",
            )
            target = self._target_by_round_id[round_id]
            result = await asyncio.to_thread(
                _enter_and_score_win5_special_round,
                round_id,
                tuple(race.race_id for race in target.races),
                str(self.winners.value),
                _optional_modal_text(self.reason.value),
                str(interaction.user.id),
                str(interaction.id),
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(interaction, command_name, "특별 라운드 결과·채점을 완료하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_win5_scoring_result(interaction, command_name, result)


class Win5ScoreModal(discord.ui.Modal, title="WIN5 라운드 채점"):
    def __init__(self, *, choices: tuple[AutocompleteChoice, ...]) -> None:
        super().__init__(timeout=600)
        self._allowed_ids = frozenset(choice.value for choice in choices)
        self.target = discord.ui.Select(
            custom_id="win5-score-target",
            placeholder="채점할 라운드 선택 (복수 선택 가능)",
            min_values=1,
            max_values=min(len(choices), 25),
            options=[discord.SelectOption(label=choice.label, value=str(choice.value)) for choice in choices],
            required=True,
        )
        self.reason = discord.ui.TextInput(
            custom_id="win5-score-reason",
            style=discord.TextStyle.paragraph,
            placeholder="선택: 채점 관련 특이사항",
            required=False,
            max_length=255,
        )
        self.add_item(
            discord.ui.Label(
                text="대상 라운드",
                description="여러 라운드를 한 번에 채점합니다. 점수와 서클 포인트 지급은 모두 원자적으로 처리됩니다.",
                component=self.target,
            )
        )
        self.add_item(discord.ui.Label(text="운영 메모 (선택)", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        command_name = "win5.staff.round"
        if await _prepare_command(interaction, command_name) is None:
            return
        try:
            round_ids = _selected_modal_ids(
                self.target,
                allowed_ids=self._allowed_ids,
                field_name="대상 라운드",
            )
            results = await asyncio.to_thread(
                _score_win5_round_batch,
                round_ids,
                _optional_modal_text(self.reason.value),
                str(interaction.user.id),
                str(interaction.id),
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(interaction, command_name, "라운드를 채점하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_win5_scoring_batch_result(interaction, command_name, results)


class RoomRaceCreateModal(discord.ui.Modal, title="룸매치 경기 생성"):
    def __init__(self) -> None:
        super().__init__(timeout=600)
        self.name = discord.ui.TextInput(
            custom_id="room-race-create-name",
            placeholder="경기 표시명",
            required=True,
            min_length=1,
            max_length=200,
        )
        self.starts_at = discord.ui.TextInput(
            custom_id="room-race-create-starts-at",
            placeholder="2026-07-27 18:00 (KST)",
            required=True,
            min_length=16,
            max_length=16,
        )
        self.description = discord.ui.TextInput(
            custom_id="room-race-create-description",
            style=discord.TextStyle.paragraph,
            placeholder="선택 입력",
            required=False,
            max_length=1000,
        )
        self.reason = discord.ui.TextInput(
            custom_id="room-race-create-reason",
            style=discord.TextStyle.paragraph,
            placeholder="선택: 특이사항 또는 운영 메모",
            required=False,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="경기명", component=self.name))
        self.add_item(
            discord.ui.Label(
                text="경기 시작 시각",
                description="기본 시간대는 KST이며 DB에는 UTC로 저장됩니다.",
                component=self.starts_at,
            )
        )
        self.add_item(discord.ui.Label(text="경기 설명", component=self.description))
        self.add_item(discord.ui.Label(text="운영 메모 (선택)", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        command_name = "match.staff.race-create"
        if await _prepare_command(interaction, command_name) is None:
            return
        try:
            result = await asyncio.to_thread(
                _create_match_race,
                str(self.name.value),
                parse_operator_kst_datetime(str(self.starts_at.value)).isoformat(),
                _optional_modal_text(self.description.value),
                _optional_modal_text(self.reason.value),
                str(interaction.user.id),
                str(interaction.id),
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(interaction, command_name, "레이스를 생성하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_race_operation_result(interaction, command_name, result)


class RoomRaceEditModal(discord.ui.Modal, title="룸매치 경기 수정"):
    def __init__(self, *, choices: tuple[AutocompleteChoice, ...]) -> None:
        super().__init__(timeout=600)
        self._allowed_ids = frozenset(choice.value for choice in choices)
        self.target = discord.ui.Select(
            custom_id="room-race-edit-target",
            placeholder="수정할 경기 선택",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=choice.label, value=str(choice.value)) for choice in choices],
            required=True,
        )
        self.name = discord.ui.TextInput(
            custom_id="room-race-edit-name",
            placeholder="변경 후 경기 표시명",
            required=True,
            min_length=1,
            max_length=200,
        )
        self.starts_at = discord.ui.TextInput(
            custom_id="room-race-edit-starts-at",
            placeholder="2026-07-27 18:00 (KST)",
            required=True,
            min_length=16,
            max_length=16,
        )
        self.description = discord.ui.TextInput(
            custom_id="room-race-edit-description",
            style=discord.TextStyle.paragraph,
            placeholder="빈 값이면 설명을 제거합니다.",
            required=False,
            max_length=1000,
        )
        self.reason = discord.ui.TextInput(
            custom_id="room-race-edit-reason",
            style=discord.TextStyle.paragraph,
            placeholder="선택: 수정 관련 특이사항",
            required=False,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="대상 경기", component=self.target))
        self.add_item(discord.ui.Label(text="경기명", component=self.name))
        self.add_item(
            discord.ui.Label(
                text="경기 시작 시각",
                description="기본 시간대는 KST이며 DB에는 UTC로 저장됩니다.",
                component=self.starts_at,
            )
        )
        self.add_item(discord.ui.Label(text="경기 설명", component=self.description))
        self.add_item(discord.ui.Label(text="운영 메모 (선택)", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        command_name = "match.staff.race-edit"
        if await _prepare_command(interaction, command_name) is None:
            return
        try:
            race_id = _selected_modal_id(
                self.target,
                allowed_ids=self._allowed_ids,
                field_name="대상 경기",
            )
            result = await asyncio.to_thread(
                _edit_match_race_preserving_event,
                race_id,
                str(self.name.value),
                parse_operator_kst_datetime(str(self.starts_at.value)).isoformat(),
                _optional_modal_text(self.description.value),
                _optional_modal_text(self.reason.value),
                str(interaction.user.id),
                str(interaction.id),
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(interaction, command_name, "레이스를 수정하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_race_operation_result(interaction, command_name, result)


class RoomRaceConditionModal(discord.ui.Modal, title="룸매치 경기 조건 설정"):
    def __init__(self, *, choices: tuple[AutocompleteChoice, ...]) -> None:
        super().__init__(timeout=600)
        self._allowed_ids = frozenset(choice.value for choice in choices)
        self.target = discord.ui.Select(
            custom_id="room-condition-target",
            placeholder="조건을 설정할 경기 선택",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=choice.label, value=str(choice.value)) for choice in choices],
            required=True,
        )
        self.race_profile = discord.ui.TextInput(
            custom_id="room-condition-race-profile",
            placeholder="G1 | 도쿄 | turf | 2400 | left",
            required=True,
            max_length=300,
        )
        self.environment = discord.ui.TextInput(
            custom_id="room-condition-environment",
            placeholder="summer | clear | good",
            required=True,
            max_length=200,
        )
        self.condition_label = discord.ui.TextInput(
            custom_id="room-condition-label",
            placeholder="표시용 조건명",
            required=True,
            min_length=1,
            max_length=200,
        )
        self.reason = discord.ui.TextInput(
            custom_id="room-condition-reason",
            style=discord.TextStyle.paragraph,
            placeholder="선택: 조건 관련 특이사항",
            required=False,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="대상 경기", component=self.target))
        self.add_item(
            discord.ui.Label(
                text="등급 | 경기장 | 트랙 | 거리 | 방향",
                component=self.race_profile,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="계절 | 날씨 | 주로 상태",
                component=self.environment,
            )
        )
        self.add_item(discord.ui.Label(text="조건 표시명", component=self.condition_label))
        self.add_item(discord.ui.Label(text="운영 메모 (선택)", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        command_name = "match.staff.race-condition-set"
        if await _prepare_command(interaction, command_name) is None:
            return
        try:
            race_id = _selected_modal_id(
                self.target,
                allowed_ids=self._allowed_ids,
                field_name="대상 경기",
            )
            grade, venue, track_surface, distance, direction = parse_room_condition_profile(
                str(self.race_profile.value)
            )
            season, weather, track_condition = parse_room_condition_environment(str(self.environment.value))
            result = await asyncio.to_thread(
                _set_match_condition,
                race_id,
                grade,
                venue,
                track_surface,
                distance,
                direction,
                season,
                weather,
                track_condition,
                str(self.condition_label.value),
                _optional_modal_text(self.reason.value),
                str(interaction.user.id),
                str(interaction.id),
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(interaction, command_name, "조건을 설정하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_race_operation_result(interaction, command_name, result)


class RoomEntriesModal(discord.ui.Modal, title="룸매치 엔트리 입력"):
    def __init__(self, *, choices: tuple[AutocompleteChoice, ...]) -> None:
        super().__init__(timeout=600)
        self._allowed_ids = frozenset(choice.value for choice in choices)
        self.target = discord.ui.Select(
            custom_id="room-entries-target",
            placeholder="엔트리를 교체할 경기 선택",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=choice.label, value=str(choice.value)) for choice in choices],
            required=True,
        )
        self.entries = discord.ui.TextInput(
            custom_id="room-entries-values",
            placeholder="PID | 말 이름\n966621167959 | 스페셜 위크\n123456789012 | 사일런스 스즈카",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.reason = discord.ui.TextInput(
            custom_id="room-entries-reason",
            style=discord.TextStyle.paragraph,
            placeholder="선택: 엔트리 관련 특이사항",
            required=False,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="대상 경기", component=self.target))
        self.add_item(discord.ui.Label(text="PID | 말 이름", component=self.entries))
        self.add_item(discord.ui.Label(text="운영 메모 (선택)", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if await _prepare_command(interaction, "match.staff.entries-set") is None:
            return
        try:
            race_id = _selected_modal_id(
                self.target,
                allowed_ids=self._allowed_ids,
                field_name="대상 경기",
            )
            result = await asyncio.to_thread(
                _set_match_entries,
                race_id,
                str(self.entries.value),
                _optional_modal_text(self.reason.value),
                str(interaction.user.id),
                str(interaction.id),
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(
                interaction,
                "match.staff.entries-set",
                "확정 엔트리를 설정하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await _send_internal_error(interaction, "match.staff.entries-set")
            return
        await _send_race_operation_result(interaction, "match.staff.entries-set", result)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_sanitized_exception(
            logger,
            "discord modal failed correlation_id=%s command=%s actor_id=%s",
            _correlation_id(interaction),
            "match.staff.entries-set",
            _interaction_user_id(interaction),
        )
        if not interaction.response.is_done():
            await _send_initial_response_safely(
                interaction,
                "match.staff.entries-set",
                f"내부 오류가 발생했습니다. 요청 ID: `{_correlation_id(interaction)}`",
            )


class LifecycleBatchModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        command_name: str,
        choices: tuple[AutocompleteChoice, ...],
        domain: Literal["room", "win5"],
        opening: bool,
    ) -> None:
        action_label = "열기" if opening else "마감"
        domain_label = "룸매치 경기" if domain == "room" else "WIN5 라운드"
        super().__init__(title=f"{domain_label} 일괄 {action_label}", timeout=600)
        self._command_name = command_name
        self._domain = domain
        self._opening = opening
        self._allowed_ids = frozenset(choice.value for choice in choices)
        self.targets = discord.ui.Select(
            custom_id=f"{domain}-lifecycle-targets",
            placeholder=f"{action_label}할 {domain_label} 선택",
            min_values=1,
            max_values=len(choices),
            options=[
                discord.SelectOption(
                    label=choice.label,
                    value=str(choice.value),
                )
                for choice in choices
            ],
            required=True,
        )
        self.reason = discord.ui.TextInput(
            custom_id=f"{domain}-lifecycle-reason",
            style=discord.TextStyle.paragraph,
            placeholder="선택: 특이사항을 모든 항목에 공통 기록",
            required=False,
            max_length=255,
        )
        self.add_item(
            discord.ui.Label(
                text=f"{domain_label} 선택",
                description="여러 항목을 동시에 선택할 수 있습니다.",
                component=self.targets,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="운영 메모 (선택)",
                description="입력하면 모든 항목의 감사 기록에 공통 적용됩니다.",
                component=self.reason,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if await _prepare_command(interaction, self._command_name) is None:
            return
        try:
            selected_ids = tuple(sorted({int(value) for value in self.targets.values}))
            if not selected_ids or not set(selected_ids).issubset(self._allowed_ids):
                raise ValueError("선택한 경기 목록이 유효하지 않습니다.")
            reason = _optional_modal_text(self.reason.value)
            if self._domain == "room":
                results = await asyncio.to_thread(
                    _transition_match_betting_batch,
                    selected_ids,
                    self._opening,
                    reason,
                    str(interaction.user.id),
                    str(interaction.id),
                )
                await _send_room_lifecycle_batch_result(
                    interaction,
                    self._command_name,
                    results,
                )
            else:
                results = await asyncio.to_thread(
                    _transition_win5_round_batch,
                    selected_ids,
                    self._opening,
                    reason,
                    str(interaction.user.id),
                    str(interaction.id),
                )
                await _send_win5_lifecycle_batch_result(
                    interaction,
                    self._command_name,
                    results,
                )
        except (DomainError, ValueError) as exc:
            failure_prefix = (
                "베팅 상태를 변경하지 못했습니다" if self._domain == "room" else "라운드 상태를 변경하지 못했습니다"
            )
            await _send_user_error(
                interaction,
                self._command_name,
                failure_prefix,
                exc,
            )
        except Exception:
            await _send_internal_error(interaction, self._command_name)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_sanitized_exception(
            logger,
            "discord lifecycle modal failed correlation_id=%s command=%s actor_id=%s",
            _correlation_id(interaction),
            self._command_name,
            _interaction_user_id(interaction),
        )
        if not interaction.response.is_done():
            await _send_initial_response_safely(
                interaction,
                self._command_name,
                f"내부 오류가 발생했습니다. 요청 ID: `{_correlation_id(interaction)}`",
            )


async def _show_lifecycle_batch_modal(
    interaction: discord.Interaction,
    *,
    command_name: str,
    domain: Literal["room", "win5"],
    opening: bool,
    prepared: bool = False,
) -> None:
    if not prepared and await _prepare_command(interaction, command_name, defer=False) is None:
        return
    try:
        choices = await asyncio.to_thread(
            _load_lifecycle_batch_choices,
            domain,
            opening,
        )
        if not choices:
            action_label = "열 수 있는" if opening else "마감할"
            await _send_initial_response_safely(
                interaction,
                command_name,
                f"현재 {action_label} 경기 또는 라운드가 없습니다.",
            )
            return
        await interaction.response.send_modal(
            LifecycleBatchModal(
                command_name=command_name,
                choices=choices,
                domain=domain,
                opening=opening,
            )
        )
    except (DomainError, ValueError) as exc:
        await _send_pre_modal_user_error(
            interaction,
            command_name,
            "경기 목록을 불러오지 못했습니다",
            exc,
        )
    except Exception:
        await _handle_modal_open_error(interaction, command_name)


def _load_lifecycle_batch_choices(
    domain: Literal["room", "win5"],
    opening: bool,
) -> tuple[AutocompleteChoice, ...]:
    if domain == "room":
        purpose = RoomRaceChoicePurpose.OPEN if opening else RoomRaceChoicePurpose.CLOSE
        return run_application_query(
            lambda session: autocomplete_room_races(
                session,
                purpose=purpose,
            )
        )
    purpose = Win5RoundChoicePurpose.OPEN if opening else Win5RoundChoicePurpose.CLOSE
    return run_application_query(
        lambda session: autocomplete_active_win5_rounds(
            session,
            purpose=purpose,
        )
    )


async def _show_room_target_modal(
    interaction: discord.Interaction,
    *,
    command_name: str,
    purpose: RoomRaceChoicePurpose,
    modal_factory: Callable[[tuple[AutocompleteChoice, ...]], discord.ui.Modal],
) -> None:
    if await _prepare_command(interaction, command_name, defer=False) is None:
        return
    try:
        choices = await asyncio.to_thread(
            lambda: run_application_query(
                lambda session: autocomplete_room_races(
                    session,
                    purpose=purpose,
                )
            )
        )
        if not choices:
            await _send_initial_response_safely(
                interaction,
                command_name,
                "현재 이 작업을 적용할 수 있는 룸매치 경기가 없습니다.",
            )
            return
        await interaction.response.send_modal(modal_factory(choices))
    except (DomainError, ValueError) as exc:
        await _send_pre_modal_user_error(
            interaction,
            command_name,
            "경기 목록을 불러오지 못했습니다",
            exc,
        )
    except Exception:
        await _handle_modal_open_error(interaction, command_name)


async def _show_room_result_modal(
    interaction: discord.Interaction,
    *,
    command_name: str,
    allowed_statuses: tuple[str, ...],
    modal_factory: Callable[
        [tuple[AutocompleteChoice, ...], SettingsInteractionContext],
        discord.ui.Modal,
    ],
) -> None:
    if await _prepare_command(interaction, command_name, defer=False) is None:
        return
    try:
        choices = await asyncio.to_thread(
            lambda: run_application_query(
                lambda session: autocomplete_room_races(
                    session,
                    purpose=RoomRaceChoicePurpose.RESULT,
                    allowed_statuses=allowed_statuses,
                )
            )
        )
        if not choices:
            await _send_initial_response_safely(
                interaction,
                command_name,
                "현재 이 결과 작업을 적용할 수 있는 룸매치 경기가 없습니다.",
            )
            return
        context = _settings_interaction_context(interaction)
        await interaction.response.send_modal(modal_factory(choices, context))
    except (DomainError, ValueError) as exc:
        await _send_pre_modal_user_error(
            interaction,
            command_name,
            "경기 목록을 불러오지 못했습니다",
            exc,
        )
    except Exception:
        await _handle_modal_open_error(interaction, command_name)


async def _show_win5_season_create_modal(
    interaction: discord.Interaction,
    *,
    prepared: bool = False,
) -> None:
    command_name = "win5.staff.season"
    if not prepared and await _prepare_command(interaction, command_name, defer=False) is None:
        return
    try:
        await interaction.response.send_modal(Win5SeasonCreateModal())
    except Exception:
        await _handle_modal_open_error(interaction, command_name)


async def _show_win5_staff_control_panel(
    interaction: discord.Interaction,
    *,
    domain: Literal["season", "round"],
) -> None:
    command_name = f"win5.staff.{domain}"
    if await _prepare_command(interaction, command_name, defer=False) is None:
        return
    label = "시즌" if domain == "season" else "라운드"
    try:
        await interaction.response.send_message(
            f"관리할 WIN5 {label} 작업을 선택해 주세요.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
            view=Win5StaffControlView(
                domain=domain,
                context=_settings_interaction_context(interaction),
            ),
        )
    except Exception:
        await _handle_modal_open_error(interaction, command_name)


async def _show_win5_season_transition_modal(
    interaction: discord.Interaction,
    *,
    action: Literal["activate", "close", "cancel"],
    prepared: bool = False,
) -> None:
    command_name = "win5.staff.season"
    if not prepared and await _prepare_command(interaction, command_name, defer=False) is None:
        return
    try:
        allowed_statuses = (Win5SeasonStatus.ACTIVE.value,) if action == "close" else (Win5SeasonStatus.DRAFT.value,)
        choices = await asyncio.to_thread(
            _load_win5_season_choices,
            allowed_statuses,
        )
        if not choices:
            action_label = {
                "activate": "활성화할",
                "close": "종료할",
                "cancel": "삭제할",
            }[action]
            await _send_initial_response_safely(
                interaction,
                command_name,
                f"현재 {action_label} WIN5 시즌이 없습니다.",
            )
            return
        await interaction.response.send_modal(
            Win5SeasonTransitionModal(
                choices=choices,
                action=action,
            )
        )
    except (DomainError, ValueError) as exc:
        await _send_pre_modal_user_error(
            interaction,
            command_name,
            "시즌 목록을 불러오지 못했습니다",
            exc,
        )
    except Exception:
        await _handle_modal_open_error(interaction, command_name)


async def _show_win5_round_create_modal(
    interaction: discord.Interaction,
    *,
    prepared: bool = False,
) -> None:
    command_name = "win5.staff.round"
    if not prepared and await _prepare_command(interaction, command_name, defer=False) is None:
        return
    try:
        seasons = await asyncio.to_thread(_load_win5_round_create_seasons)
        if not seasons:
            await _send_initial_response_safely(
                interaction,
                command_name,
                "라운드를 생성할 수 있는 WIN5 시즌이 없습니다.",
            )
            return
        if len(seasons) == 1:
            await interaction.response.send_modal(Win5RoundCreateModal(season=seasons[0]))
            return
        await interaction.response.send_message(
            "일반 라운드를 생성할 시즌을 선택해 주세요.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
            view=Win5RoundCreateSeasonView(
                seasons=seasons,
                context=_settings_interaction_context(interaction),
            ),
        )
    except (DomainError, ValueError) as exc:
        await _send_pre_modal_user_error(
            interaction,
            command_name,
            "시즌 목록을 불러오지 못했습니다",
            exc,
        )
    except Exception:
        await _handle_modal_open_error(interaction, command_name)


async def _show_win5_special_round_create_modal(
    interaction: discord.Interaction,
    *,
    prepared: bool = False,
) -> None:
    command_name = "win5.staff.round"
    if not prepared and await _prepare_command(interaction, command_name, defer=False) is None:
        return
    try:
        season_choices = await asyncio.to_thread(
            _load_win5_season_choices,
            (
                Win5SeasonStatus.DRAFT.value,
                Win5SeasonStatus.ACTIVE.value,
            ),
        )
        if not season_choices:
            await _send_initial_response_safely(
                interaction,
                command_name,
                "특별 라운드를 생성할 수 있는 WIN5 시즌이 없습니다.",
            )
            return
        await interaction.response.send_modal(Win5SpecialRoundCreateModal(season_choices=season_choices))
    except (DomainError, ValueError) as exc:
        await _send_pre_modal_user_error(
            interaction,
            command_name,
            "시즌 목록을 불러오지 못했습니다",
            exc,
        )
    except Exception:
        await _handle_modal_open_error(interaction, command_name)


async def _show_win5_result_enter_modal(
    interaction: discord.Interaction,
    *,
    prepared: bool = False,
) -> None:
    command_name = "win5.staff.round"
    if not prepared and await _prepare_command(interaction, command_name, defer=False) is None:
        return
    try:
        targets = await asyncio.to_thread(_load_win5_result_targets)
        if not targets:
            await _send_initial_response_safely(
                interaction,
                command_name,
                "현재 결과를 입력할 수 있는 WIN5 라운드가 없습니다.",
            )
            return
        await interaction.response.send_modal(Win5ResultEnterModal(targets=targets))
    except (DomainError, ValueError) as exc:
        await _send_pre_modal_user_error(
            interaction,
            command_name,
            "결과 입력 대상을 불러오지 못했습니다",
            exc,
        )
    except Exception:
        await _handle_modal_open_error(interaction, command_name)


async def _show_win5_result_correct_modal(
    interaction: discord.Interaction,
    *,
    prepared: bool = False,
) -> None:
    command_name = "win5.staff.round"
    if not prepared and await _prepare_command(interaction, command_name, defer=False) is None:
        return
    try:
        targets = await asyncio.to_thread(_load_win5_result_correction_targets)
        if not targets:
            await _send_initial_response_safely(
                interaction,
                command_name,
                "현재 채점 전 결과를 정정할 수 있는 일반 WIN5 라운드가 없습니다.",
            )
            return
        await interaction.response.send_modal(Win5ResultEnterModal(targets=targets, correcting=True))
    except (DomainError, ValueError) as exc:
        await _send_pre_modal_user_error(
            interaction,
            command_name,
            "결과 정정 대상을 불러오지 못했습니다",
            exc,
        )
    except Exception:
        await _handle_modal_open_error(interaction, command_name)


async def _show_win5_special_result_score_modal(
    interaction: discord.Interaction,
    *,
    prepared: bool = False,
) -> None:
    command_name = "win5.staff.round"
    if not prepared and await _prepare_command(interaction, command_name, defer=False) is None:
        return
    try:
        targets = await asyncio.to_thread(_load_win5_special_result_targets)
        if not targets:
            await _send_initial_response_safely(
                interaction,
                command_name,
                "현재 결과를 입력하고 채점할 수 있는 WIN5 특별 라운드가 없습니다.",
            )
            return
        await interaction.response.send_modal(Win5SpecialResultScoreModal(targets=targets))
    except (DomainError, ValueError) as exc:
        await _send_pre_modal_user_error(
            interaction,
            command_name,
            "특별 라운드 대상을 불러오지 못했습니다",
            exc,
        )
    except Exception:
        await _handle_modal_open_error(interaction, command_name)


async def _show_win5_score_modal(
    interaction: discord.Interaction,
    *,
    prepared: bool = False,
) -> None:
    command_name = "win5.staff.round"
    if not prepared and await _prepare_command(interaction, command_name, defer=False) is None:
        return
    try:
        choices = await asyncio.to_thread(_load_win5_score_choices)
        if not choices:
            await _send_initial_response_safely(
                interaction,
                command_name,
                "현재 채점할 수 있는 WIN5 라운드가 없습니다.",
            )
            return
        await interaction.response.send_modal(Win5ScoreModal(choices=choices))
    except (DomainError, ValueError) as exc:
        await _send_pre_modal_user_error(
            interaction,
            command_name,
            "채점 대상을 불러오지 못했습니다",
            exc,
        )
    except Exception:
        await _handle_modal_open_error(interaction, command_name)


def _load_win5_season_choices(
    allowed_statuses: tuple[str, ...],
) -> tuple[AutocompleteChoice, ...]:
    return run_application_query(
        lambda session: autocomplete_staff_win5_seasons(
            session,
            allowed_statuses=allowed_statuses,
        )
    )


def _load_win5_round_create_seasons() -> tuple[Win5RoundCreateSeasonOption, ...]:
    allowed_statuses = (
        Win5SeasonStatus.DRAFT.value,
        Win5SeasonStatus.ACTIVE.value,
    )

    def operation(session: Session) -> tuple[Win5RoundCreateSeasonOption, ...]:
        choices = autocomplete_staff_win5_seasons(
            session,
            allowed_statuses=allowed_statuses,
        )
        if not choices:
            return ()
        round_rows = session.execute(
            select(Win5Round.season_id, Win5Round.round_number).where(
                Win5Round.season_id.in_([choice.value for choice in choices])
            )
        ).all()
        used_numbers: dict[int, set[int]] = {choice.value: set() for choice in choices}
        for row in round_rows:
            used_numbers[row.season_id].add(row.round_number)
        return tuple(
            Win5RoundCreateSeasonOption(
                season_id=choice.value,
                label=choice.label,
                next_round_number=_next_available_win5_round_number(used_numbers[choice.value]),
            )
            for choice in choices
        )

    return run_application_query(operation)


def _next_available_win5_round_number(used_numbers: set[int]) -> int:
    candidate = 1
    for number in sorted(number for number in used_numbers if number > 0):
        if number > candidate:
            break
        if number == candidate:
            candidate += 1
    return candidate


def _load_win5_result_targets() -> tuple[Win5ResultTargetOption, ...]:
    def operation(session: Session) -> tuple[Win5ResultTargetOption, ...]:
        normal_rows = session.execute(
            select(
                Win5Round.id.label("round_id"),
                Win5Round.round_number,
                Race.id.label("race_id"),
                Race.name.label("race_name"),
            )
            .join(Win5Season, Win5Season.id == Win5Round.season_id)
            .join(Race, Race.id == Win5Round.race_id)
            .where(
                Win5Season.status == Win5SeasonStatus.ACTIVE.value,
                Win5Round.status == Win5RoundStatus.CLOSED.value,
                Win5Round.round_type == Win5RoundType.NORMAL.value,
                Race.id.not_in(select(Win5Result.race_id)),
            )
            .order_by(Win5Round.round_number, Win5Round.id)
        ).all()
        return tuple(
            Win5ResultTargetOption(
                token=index,
                round_id=row.round_id,
                race_id=row.race_id,
                label=_modal_option_label(f"{row.round_number}R {row.race_name}"),
            )
            for index, row in enumerate(normal_rows[:25], start=1)
        )

    return run_application_query(operation)


def _load_win5_result_correction_targets() -> tuple[Win5ResultTargetOption, ...]:
    def operation(session: Session) -> tuple[Win5ResultTargetOption, ...]:
        rows = session.execute(
            select(
                Win5Round.id.label("round_id"),
                Win5Round.round_number,
                Race.id.label("race_id"),
                Race.name.label("race_name"),
            )
            .join(Win5Season, Win5Season.id == Win5Round.season_id)
            .join(Race, Race.id == Win5Round.race_id)
            .join(Win5Result, Win5Result.race_id == Race.id)
            .where(
                Win5Season.status == Win5SeasonStatus.ACTIVE.value,
                Win5Round.status == Win5RoundStatus.RESULT_ENTERED.value,
                Win5Round.round_type == Win5RoundType.NORMAL.value,
            )
            .order_by(Win5Round.round_number, Win5Round.id)
            .limit(25)
        ).all()
        return tuple(
            Win5ResultTargetOption(
                token=index,
                round_id=row.round_id,
                race_id=row.race_id,
                label=_modal_option_label(f"{row.round_number}R {row.race_name}"),
            )
            for index, row in enumerate(rows, start=1)
        )

    return run_application_query(operation)


def _load_win5_special_result_targets() -> tuple[Win5SpecialResultTargetOption, ...]:
    def operation(session: Session) -> tuple[Win5SpecialResultTargetOption, ...]:
        rounds = session.execute(
            select(
                Win5Round.id,
                Win5Round.round_number,
                Win5Round.round_label,
            )
            .join(Win5Season, Win5Season.id == Win5Round.season_id)
            .where(
                Win5Season.status == Win5SeasonStatus.ACTIVE.value,
                Win5Round.round_type == Win5RoundType.SPECIAL.value,
                Win5Round.status.in_(
                    (
                        Win5RoundStatus.CLOSED.value,
                        Win5RoundStatus.RESULT_ENTERED.value,
                    )
                ),
            )
            .order_by(Win5Round.round_number, Win5Round.id)
            .limit(25)
        ).all()

        targets: list[Win5SpecialResultTargetOption] = []
        for round_ in rounds:
            races = session.execute(
                select(
                    Race.id,
                    Win5RoundRace.display_order,
                    Race.name,
                )
                .join(Win5RoundRace, Win5RoundRace.race_id == Race.id)
                .where(
                    Win5RoundRace.round_id == round_.id,
                    Race.status != "voided",
                )
                .order_by(Win5RoundRace.display_order, Race.id)
            ).all()
            targets.append(
                Win5SpecialResultTargetOption(
                    round_id=round_.id,
                    label=_modal_option_label(
                        f"{round_.round_number}R [{round_.round_label or '특별 라운드'}] · {len(races)}경기"
                    ),
                    races=tuple(
                        Win5SpecialResultRaceOption(
                            race_id=race.id,
                            display_order=race.display_order,
                            race_name=race.name,
                        )
                        for race in races
                    ),
                )
            )
        return tuple(targets)

    return run_application_query(operation)


def _load_win5_score_choices() -> tuple[AutocompleteChoice, ...]:
    def operation(session: Session) -> tuple[AutocompleteChoice, ...]:
        rows = session.execute(
            select(
                Win5Round.id,
                Win5Round.round_number,
                Win5Round.round_label,
                Win5Round.round_type,
                Race.name.label("race_name"),
            )
            .join(Win5Season, Win5Season.id == Win5Round.season_id)
            .outerjoin(Race, Race.id == Win5Round.race_id)
            .where(
                Win5Season.status == Win5SeasonStatus.ACTIVE.value,
                Win5Round.status == Win5RoundStatus.RESULT_ENTERED.value,
                Win5Round.round_type == Win5RoundType.NORMAL.value,
            )
            .order_by(Win5Round.round_number, Win5Round.id)
            .limit(25)
        ).all()
        return tuple(
            AutocompleteChoice(
                value=row.id,
                label=_modal_option_label(f"{row.round_number}R {row.race_name or row.round_label or '이름 없음'}"),
                state=Win5RoundStatus.RESULT_ENTERED.value,
            )
            for row in rows
        )

    return run_application_query(operation)


def _modal_option_label(value: object) -> str:
    normalized = " ".join(str(value).split()).strip()
    return (normalized or "이름 없음")[:100]


class Win5StaffActionSelect(discord.ui.Select):
    def __init__(
        self,
        *,
        domain: Literal["season", "round"],
        context: SettingsInteractionContext,
    ) -> None:
        self._domain = domain
        self._context = context
        if domain == "season":
            options = (
                ("create", "시즌 생성", "새 draft 시즌을 생성합니다."),
                ("activate", "시즌 활성화", "draft 시즌 하나를 활성화합니다."),
                ("close", "시즌 종료", "현재 active 시즌을 종료합니다."),
                ("cancel", "시즌 삭제", "draft 시즌을 이력을 남기고 취소합니다."),
            )
        else:
            options = (
                ("create", "일반 라운드 생성", "경기와 엔트리를 한 번에 생성합니다."),
                ("special-create", "특별 라운드 생성", "독립 경기 여러 개와 엔트리를 함께 생성합니다."),
                ("open", "라운드 열기", "여러 라운드를 선택해 제출을 시작합니다."),
                ("close", "라운드 마감", "여러 라운드를 선택해 제출을 마감합니다."),
                ("result", "일반 결과 입력", "일반 라운드의 1~5착 결과를 입력합니다."),
                ("result-correct", "일반 결과 정정", "채점 전 일반 라운드 결과를 정정합니다."),
                ("special-score", "특별 결과 입력·채점", "모든 경기의 1착 결과를 입력하고 즉시 채점합니다."),
                ("score", "일반 라운드 채점", "결과 입력을 마친 일반 라운드를 채점합니다."),
            )
        super().__init__(
            custom_id=f"win5-staff-{domain}-action",
            placeholder=f"{'시즌' if domain == 'season' else '라운드'} 작업 선택",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(value=value, label=label, description=description)
                for value, label, description in options
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        command_name = f"win5.staff.{self._domain}"
        if (
            await _prepare_bound_component(
                interaction,
                self._context,
                command_name=command_name,
                defer=False,
            )
            is None
        ):
            return
        action = self.values[0]
        try:
            if self._domain == "season":
                if action == "create":
                    await _show_win5_season_create_modal(interaction, prepared=True)
                else:
                    await _show_win5_season_transition_modal(
                        interaction,
                        action=action,
                        prepared=True,
                    )
                return
            if action == "create":
                await _show_win5_round_create_modal(interaction, prepared=True)
            elif action == "special-create":
                await _show_win5_special_round_create_modal(interaction, prepared=True)
            elif action in {"open", "close"}:
                await _show_lifecycle_batch_modal(
                    interaction,
                    command_name=command_name,
                    domain="win5",
                    opening=action == "open",
                    prepared=True,
                )
            elif action == "result":
                await _show_win5_result_enter_modal(interaction, prepared=True)
            elif action == "result-correct":
                await _show_win5_result_correct_modal(interaction, prepared=True)
            elif action == "special-score":
                await _show_win5_special_result_score_modal(interaction, prepared=True)
            elif action == "score":
                await _show_win5_score_modal(interaction, prepared=True)
            else:
                raise ValueError("지원하지 않는 WIN5 작업입니다.")
        except (DomainError, ValueError) as exc:
            await _send_pre_modal_user_error(
                interaction,
                command_name,
                "WIN5 작업 화면을 열지 못했습니다",
                exc,
            )
        except Exception:
            await _handle_modal_open_error(interaction, command_name)


class Win5StaffControlView(discord.ui.View):
    def __init__(
        self,
        *,
        domain: Literal["season", "round"],
        context: SettingsInteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        self.add_item(Win5StaffActionSelect(domain=domain, context=context))


class Win5RoundCreateSeasonSelect(discord.ui.Select):
    def __init__(
        self,
        *,
        seasons: tuple[Win5RoundCreateSeasonOption, ...],
        context: SettingsInteractionContext,
    ) -> None:
        self._seasons = {season.season_id: season for season in seasons}
        self._context = context
        super().__init__(
            custom_id="win5-round-create-season",
            placeholder="라운드를 생성할 시즌 선택",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=season.label,
                    value=str(season.season_id),
                    description=f"생성 가능 라운드 번호: {season.next_round_number}",
                )
                for season in seasons
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        command_name = "win5.staff.round"
        if (
            await _prepare_bound_component(
                interaction,
                self._context,
                command_name=command_name,
                defer=False,
            )
            is None
        ):
            return
        try:
            season_id = int(self.values[0])
            season = self._seasons.get(season_id)
            if season is None:
                raise ValueError("선택한 시즌을 찾을 수 없습니다.")
            await interaction.response.send_modal(Win5RoundCreateModal(season=season))
        except (DomainError, ValueError) as exc:
            await _send_pre_modal_user_error(
                interaction,
                command_name,
                "일반 라운드 생성 화면을 열지 못했습니다",
                exc,
            )
        except Exception:
            await _handle_modal_open_error(interaction, command_name)


class Win5RoundCreateSeasonView(discord.ui.View):
    def __init__(
        self,
        *,
        seasons: tuple[Win5RoundCreateSeasonOption, ...],
        context: SettingsInteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        self.add_item(Win5RoundCreateSeasonSelect(seasons=seasons, context=context))


@app_commands.command(name="help", description="사용 가능한 주요 명령과 등록 절차를 안내합니다.")
async def help_command(interaction: discord.Interaction) -> None:
    if await _prepare_command(interaction, "help") is None:
        return
    await _send_followup_safely(
        interaction,
        "help",
        "\n".join(
            [
                "[사용자] `/account register` · `/account info` · `/match races` · `/match ratings` · `/match bet`",
                "[기존 기록 연결] `/account link-request` 후 운영자가 `/staff link-player`로 승인합니다.",
                "[신규 등록] `/account register`는 승인 대기 요청만 생성하며, "
                "`/account cancel-registration confirm:취소`로 대기 요청을 취소할 수 있습니다.",
                "[운영자] `/staff account-registration-approve`, `/staff account-registration-reject`, "
                "`/match staff ...`, `/staff link-player`, `/settings general`, `/settings role`",
                "세부 선택지는 Discord의 `/` 명령 자동완성에서 확인하세요.",
            ]
        ),
    )


async def _settings_prepare_command(
    interaction: discord.Interaction,
    command_name: str,
    *,
    defer: bool = True,
) -> Settings | None:
    return await _prepare_command(interaction, command_name, defer=defer)


async def _settings_prepare_component(
    interaction: discord.Interaction,
    context: SettingsInteractionContext,
    *,
    command_name: str,
    defer: bool = True,
) -> Settings | None:
    return await _prepare_settings_component(
        interaction,
        context,
        command_name=command_name,
        defer=defer,
    )


def _settings_get_guild_settings(guild_id: str) -> GuildDiscordSettingsDTO:
    return _get_guild_settings(guild_id)


def _settings_update_guild_settings(
    current: GuildDiscordSettingsDTO,
    proposed: GuildDiscordSettingsValues,
    reason: str,
    actor_discord_user_id: str,
    interaction_id: str,
) -> GuildDiscordSettingsMutationDTO:
    return _update_guild_settings(
        current,
        proposed,
        reason,
        actor_discord_user_id,
        interaction_id,
    )


async def _settings_send_user_error(
    interaction: discord.Interaction,
    command_name: str,
    prefix: str,
    error: Exception,
) -> None:
    await _send_user_error(interaction, command_name, prefix, error)


async def _settings_send_internal_error(
    interaction: discord.Interaction,
    command_name: str,
) -> None:
    await _send_internal_error(interaction, command_name)


async def _settings_send_followup(
    interaction: discord.Interaction,
    command_name: str,
    content: str,
    *,
    response_kind: str = "success",
    view: discord.ui.View | None = None,
) -> bool:
    return await _send_followup_safely(
        interaction,
        command_name,
        content,
        response_kind=response_kind,
        view=view,
    )


def _settings_build_context(interaction: discord.Interaction) -> SettingsInteractionContext:
    return _settings_interaction_context(interaction)


def _settings_correlation_id(interaction: object) -> str:
    return _correlation_id(interaction)


def _settings_bounded_message(lines: list[str]) -> str:
    return _bounded_discord_message(lines)


def _settings_safe_text(value: str) -> str:
    return _safe_discord_text(value)


_SETTINGS_ADAPTER = SettingsAdapter(
    SettingsAdapterPorts(
        prepare_command=_settings_prepare_command,
        prepare_component=_settings_prepare_component,
        get_guild_settings=_settings_get_guild_settings,
        update_guild_settings=_settings_update_guild_settings,
        build_context=_settings_build_context,
        send_user_error=_settings_send_user_error,
        send_internal_error=_settings_send_internal_error,
        send_followup=_settings_send_followup,
        correlation_id=_settings_correlation_id,
        bounded_message=_settings_bounded_message,
        safe_text=_settings_safe_text,
    )
)


def create_settings_command_group() -> SettingsCommandGroup:
    return _create_settings_command_group(_SETTINGS_ADAPTER.ports)


async def _show_settings_general(interaction: discord.Interaction) -> None:
    await _SETTINGS_ADAPTER.show_general(interaction)


async def _show_settings_role(interaction: discord.Interaction) -> None:
    await _SETTINGS_ADAPTER.show_role(interaction)


class MatchResultOrderModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        command_name: str,
        race_id: int,
        match_type: str,
        reason: str | None,
        context: SettingsInteractionContext,
        revision_number: int | None = None,
    ) -> None:
        title = "룸매치 결과 정정" if revision_number is not None else "룸매치 결과 제출"
        super().__init__(title=title, timeout=600)
        self._command_name = command_name
        self._race_id = race_id
        self._match_type = match_type
        self._reason = reason
        self._context = context
        self._revision_number = revision_number
        self.entry_order = discord.ui.TextInput(
            label="도착 순서와 결과 상세",
            placeholder="번호 | 평가 | 인기 | 기록ms | 착차\n3 | UE5 | 1 | 121234 | -",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.add_item(self.entry_order)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (
            await _prepare_bound_component(
                interaction,
                self._context,
                command_name=self._command_name,
            )
            is None
        ):
            return
        try:
            results = parse_match_result_order(str(self.entry_order.value))
            if self._revision_number is None:
                result = await asyncio.to_thread(
                    _submit_match_result,
                    self._race_id,
                    self._match_type,
                    results,
                    self._reason,
                    str(self._context.guild_id),
                    str(interaction.user.id),
                    str(interaction.id),
                )
            else:
                result = await asyncio.to_thread(
                    _correct_match_result,
                    self._race_id,
                    self._revision_number,
                    self._match_type,
                    results,
                    self._reason,
                    str(self._context.guild_id),
                    str(interaction.user.id),
                    str(interaction.id),
                )
        except (DomainError, ValueError) as exc:
            await _send_user_error(
                interaction,
                self._command_name,
                "룸매치 결과를 저장하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await _send_internal_error(interaction, self._command_name)
            return
        await _send_match_result_notification(interaction, self._command_name, result)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_sanitized_exception(
            logger,
            "discord modal failed correlation_id=%s command=%s actor_id=%s",
            _correlation_id(interaction),
            self._command_name,
            _interaction_user_id(interaction),
        )
        if not interaction.response.is_done():
            await _send_initial_response_safely(
                interaction,
                self._command_name,
                f"내부 오류가 발생했습니다. 요청 ID: `{_correlation_id(interaction)}`",
            )


class MatchResultEntryModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        command_name: str,
        choices: tuple[AutocompleteChoice, ...],
        context: SettingsInteractionContext,
        correcting: bool,
    ) -> None:
        super().__init__(
            title="룸매치 결과 정정" if correcting else "룸매치 결과 제출",
            timeout=600,
        )
        self._command_name = command_name
        self._context = context
        self._correcting = correcting
        self._allowed_ids = frozenset(choice.value for choice in choices)
        self.target = discord.ui.Select(
            custom_id="room-result-entry-target",
            placeholder="결과를 입력할 경기 선택",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=choice.label, value=str(choice.value)) for choice in choices],
            required=True,
        )
        self.match_type = discord.ui.Select(
            custom_id="room-result-entry-match-type",
            placeholder="경기 유형 선택",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="정기 룸매치", value="regular_room_match"),
                discord.SelectOption(label="비정기 룸매치", value="irregular_room_match"),
            ],
            required=True,
        )
        self.entry_order = discord.ui.TextInput(
            custom_id="room-result-entry-order",
            placeholder="번호 | 평가 | 인기 | 기록ms | 착차\n3 | UE5 | 1 | 121234 | -",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.reason = discord.ui.TextInput(
            custom_id="room-result-entry-reason",
            style=discord.TextStyle.paragraph,
            placeholder=("선택: 정정 관련 특이사항" if correcting else "선택: 결과 입력 관련 특이사항"),
            required=False,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="대상 경기", component=self.target))
        self.add_item(discord.ui.Label(text="경기 유형", component=self.match_type))
        self.add_item(discord.ui.Label(text="도착 순서와 결과 상세", component=self.entry_order))
        self.add_item(
            discord.ui.Label(
                text="운영 메모 (선택)",
                component=self.reason,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (
            await _prepare_bound_component(
                interaction,
                self._context,
                command_name=self._command_name,
            )
            is None
        ):
            return
        try:
            race_id = _selected_modal_id(
                self.target,
                allowed_ids=self._allowed_ids,
                field_name="대상 경기",
            )
            if len(self.match_type.values) != 1:
                raise ValueError("경기 유형을 하나 선택해야 합니다.")
            match_type = self.match_type.values[0]
            results = parse_match_result_order(str(self.entry_order.value))
            reason = _optional_modal_text(self.reason.value)
            if self._correcting:
                current = await asyncio.to_thread(_get_match_result, race_id, None)
                if current.submission is None:
                    raise MatchResultError("정정할 룸매치 결과 revision이 없습니다.")
                result = await asyncio.to_thread(
                    _correct_match_result,
                    race_id,
                    current.submission.revision_number,
                    match_type,
                    results,
                    reason,
                    str(self._context.guild_id),
                    str(interaction.user.id),
                    str(interaction.id),
                )
            else:
                result = await asyncio.to_thread(
                    _submit_match_result,
                    race_id,
                    match_type,
                    results,
                    reason,
                    str(self._context.guild_id),
                    str(interaction.user.id),
                    str(interaction.id),
                )
        except (DomainError, ValueError) as exc:
            await _send_user_error(
                interaction,
                self._command_name,
                "룸매치 결과를 저장하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await _send_internal_error(interaction, self._command_name)
            return
        await _send_match_result_notification(interaction, self._command_name, result)


class MatchResultDecisionModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        action: Literal["review", "reject", "confirm"],
        choices: tuple[AutocompleteChoice, ...],
        context: SettingsInteractionContext,
    ) -> None:
        action_label = {
            "review": "검토 완료",
            "reject": "반려",
            "confirm": "확정 미리보기",
        }[action]
        super().__init__(title=f"룸매치 결과 {action_label}", timeout=600)
        self._action = action
        self._command_name = f"match.staff.result-{action}"
        self._context = context
        self._allowed_ids = frozenset(choice.value for choice in choices)
        self.target = discord.ui.Select(
            custom_id=f"room-result-{action}-target",
            placeholder=f"{action_label}할 경기 선택",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=choice.label, value=str(choice.value)) for choice in choices],
            required=True,
        )
        self.reason = discord.ui.TextInput(
            custom_id=f"room-result-{action}-reason",
            style=discord.TextStyle.paragraph,
            placeholder=("반려 사유" if action == "reject" else f"선택: {action_label} 관련 특이사항"),
            required=action == "reject",
            min_length=1 if action == "reject" else None,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="대상 경기", component=self.target))
        self.add_item(
            discord.ui.Label(
                text=("반려 사유 (필수)" if action == "reject" else "운영 메모 (선택)"),
                component=self.reason,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (
            await _prepare_bound_component(
                interaction,
                self._context,
                command_name=self._command_name,
            )
            is None
        ):
            return
        try:
            race_id = _selected_modal_id(
                self.target,
                allowed_ids=self._allowed_ids,
                field_name="대상 경기",
            )
            reason = str(self.reason.value) if self._action == "reject" else _optional_modal_text(self.reason.value)
            current = await asyncio.to_thread(_get_match_result, race_id, None)
            if current.submission is None:
                raise MatchResultError("처리할 룸매치 결과 revision이 없습니다.")
            revision_number = current.submission.revision_number
            if self._action == "review":
                result = await asyncio.to_thread(
                    _review_match_result,
                    race_id,
                    revision_number,
                    reason,
                    str(self._context.guild_id),
                    str(interaction.user.id),
                    str(interaction.id),
                )
            elif self._action == "reject":
                result = await asyncio.to_thread(
                    _reject_match_result,
                    race_id,
                    revision_number,
                    reason,
                    str(self._context.guild_id),
                    str(interaction.user.id),
                    str(interaction.id),
                )
            else:
                view = MatchResultConfirmView(
                    result=current,
                    reason=reason,
                    context=self._context,
                )
                await _send_followup_safely(
                    interaction,
                    self._command_name,
                    _format_match_result(
                        current,
                        heading="룸매치 결과 확정 미리보기",
                    ),
                    view=view,
                )
                return
        except (DomainError, ValueError) as exc:
            await _send_user_error(
                interaction,
                self._command_name,
                "룸매치 결과를 처리하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await _send_internal_error(interaction, self._command_name)
            return
        await _send_match_result_notification(interaction, self._command_name, result)


class MatchResultConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        result: MatchResultOperationDTO,
        reason: str | None,
        context: SettingsInteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        if result.submission is None:
            raise ValueError("확인할 결과 revision이 없습니다.")
        self._race_id = result.race_id
        self._revision_number = result.submission.revision_number
        self._reason = reason
        self._context = context

    @discord.ui.button(label="결과 확정", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        command_name = "match.staff.result-confirm"
        if (
            await _prepare_bound_component(
                interaction,
                self._context,
                command_name=command_name,
            )
            is None
        ):
            return
        try:
            result = await asyncio.to_thread(
                _confirm_match_result,
                self._race_id,
                self._revision_number,
                self._reason,
                str(self._context.guild_id),
                str(interaction.user.id),
                str(interaction.id),
            )
        except DomainError as exc:
            await _send_user_error(interaction, command_name, "룸매치 결과를 확정하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_match_result_notification(interaction, command_name, result)
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        command_name = "match.staff.result-confirm"
        if (
            await _prepare_bound_component(
                interaction,
                self._context,
                command_name=command_name,
            )
            is None
        ):
            return
        await _send_followup_safely(interaction, command_name, "룸매치 결과 확정을 취소했습니다.")
        self.stop()


class MatchSettlementModal(discord.ui.Modal, title="룸매치 정산 확정 미리보기"):
    def __init__(
        self,
        *,
        choices: tuple[AutocompleteChoice, ...],
        context: SettingsInteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        self._context = context
        self._allowed_ids = frozenset(choice.value for choice in choices)
        self.target = discord.ui.Select(
            custom_id="room-settlement-target",
            placeholder="정산할 결과 확정 경기 선택",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=choice.label, value=str(choice.value)) for choice in choices],
            required=True,
        )
        self.win_rate = discord.ui.TextInput(
            custom_id="room-settlement-win-rate",
            label="단승 배율",
            placeholder="예: 4.00 (해당 승식에 베팅이 없으면 비움)",
            required=False,
            max_length=32,
        )
        self.quinella_rate = discord.ui.TextInput(
            custom_id="room-settlement-quinella-rate",
            label="연승 배율",
            placeholder="예: 8.50 (해당 승식에 베팅이 없으면 비움)",
            required=False,
            max_length=32,
        )
        self.trio_rate = discord.ui.TextInput(
            custom_id="room-settlement-trio-rate",
            label="삼쌍 배율",
            placeholder="예: 25.00 (해당 승식에 베팅이 없으면 비움)",
            required=False,
            max_length=32,
        )
        self.reason = discord.ui.TextInput(
            custom_id="room-settlement-reason",
            label="운영 메모 (선택)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="대상 경기", component=self.target))
        self.add_item(self.win_rate)
        self.add_item(self.quinella_rate)
        self.add_item(self.trio_rate)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        command_name = "match.staff.settlement"
        if (
            await _prepare_bound_component(
                interaction,
                self._context,
                command_name=command_name,
            )
            is None
        ):
            return
        try:
            race_id = _selected_modal_id(
                self.target,
                allowed_ids=self._allowed_ids,
                field_name="대상 경기",
            )
            odds = _parse_room_match_odds(
                win=self.win_rate.value,
                quinella=self.quinella_rate.value,
                trio=self.trio_rate.value,
            )
            view = MatchSettlementConfirmView(
                race_id=race_id,
                odds=odds,
                reason=_optional_modal_text(self.reason.value),
                context=self._context,
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(interaction, command_name, "정산 미리보기를 만들지 못했습니다", exc)
            return
        await _send_followup_safely(
            interaction,
            command_name,
            _format_match_settlement_preview(race_id=race_id, odds=odds),
            view=view,
        )


class MatchSettlementConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        race_id: int,
        odds: tuple[MatchOddsInput, ...],
        reason: str | None,
        context: SettingsInteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        self._race_id = race_id
        self._odds = odds
        self._reason = reason
        self._context = context

    @discord.ui.button(label="정산 확정", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        command_name = "match.staff.settlement"
        if (
            await _prepare_bound_component(
                interaction,
                self._context,
                command_name=command_name,
            )
            is None
        ):
            return
        try:
            result = await asyncio.to_thread(
                _confirm_match_settlement,
                self._race_id,
                self._odds,
                self._reason,
                str(interaction.user.id),
                str(interaction.id),
            )
        except DomainError as exc:
            await _send_user_error(interaction, command_name, "룸매치 정산을 확정하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_match_settlement_result(interaction, command_name, result)
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        command_name = "match.staff.settlement"
        if (
            await _prepare_bound_component(
                interaction,
                self._context,
                command_name=command_name,
            )
            is None
        ):
            return
        await _send_followup_safely(interaction, command_name, "룸매치 정산 확정을 취소했습니다.")
        self.stop()


class MatchSettlementRollbackConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        race_id: int,
        reason: str,
        context: SettingsInteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        self._race_id = race_id
        self._reason = reason
        self._context = context

    @discord.ui.button(label="정산 롤백 확정", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        command_name = "match.staff.settlement-rollback"
        if (
            await _prepare_bound_component(
                interaction,
                self._context,
                command_name=command_name,
            )
            is None
        ):
            return
        try:
            result = await asyncio.to_thread(
                _rollback_match_settlement,
                self._race_id,
                self._reason,
                str(interaction.user.id),
                str(interaction.id),
            )
        except DomainError as exc:
            await _send_user_error(interaction, command_name, "룸매치 정산을 롤백하지 못했습니다", exc)
        except Exception:
            await _send_internal_error(interaction, command_name)
        else:
            await _send_match_settlement_rollback_result(interaction, command_name, result)
        finally:
            await _close_interaction_view(interaction, command_name)
            self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        command_name = "match.staff.settlement-rollback"
        if (
            await _prepare_bound_component(
                interaction,
                self._context,
                command_name=command_name,
            )
            is None
        ):
            return
        await _send_followup_safely(interaction, command_name, "룸매치 정산 롤백을 취소했습니다.")
        await _close_interaction_view(interaction, command_name)
        self.stop()


class MatchPublishConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        race_id: int,
        revision_number: int,
        authorize_delivery_unknown_retry: bool,
        context: SettingsInteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        self._race_id = race_id
        self._revision_number = revision_number
        self._authorize_delivery_unknown_retry = authorize_delivery_unknown_retry
        self._context = context

    @discord.ui.button(label="결과 공개", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        command_name = "match.staff.publish"
        if (
            await _prepare_bound_component(
                interaction,
                self._context,
                command_name=command_name,
            )
            is None
        ):
            return
        try:
            result = await asyncio.to_thread(
                _publish_match_result,
                self._race_id,
                self._revision_number,
                str(self._context.guild_id),
                str(interaction.user.id),
                str(interaction.id),
                self._authorize_delivery_unknown_retry,
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(interaction, command_name, "룸매치 결과를 공개하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_match_result_notification(interaction, command_name, result)
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        command_name = "match.staff.publish"
        if (
            await _prepare_bound_component(
                interaction,
                self._context,
                command_name=command_name,
            )
            is None
        ):
            return
        await _send_followup_safely(interaction, command_name, "룸매치 결과 공개를 취소했습니다.")
        self.stop()


async def _player_link_prepare_command(
    interaction: discord.Interaction,
    command_name: str,
    *,
    defer: bool = True,
) -> Settings | None:
    return await _prepare_command(interaction, command_name, defer=defer)


async def _player_link_prepare_component(
    interaction: discord.Interaction,
    context: PlayerLinkInteractionContext,
    *,
    command_name: str,
    defer: bool = True,
) -> Settings | None:
    return await _prepare_bound_component(
        interaction,
        context,
        command_name=command_name,
        defer=defer,
    )


def _player_link_query_owned_request(
    guild_id: str,
    requester_discord_user_id: str,
) -> PlayerLinkRequestDTO | None:
    return _get_owned_player_link_request(guild_id, requester_discord_user_id)


def _player_link_submit_request(
    *,
    guild_id: str,
    requester_discord_user_id: str,
    discord_nickname_snapshot: str,
    submitted_ingame_name: str,
    submitted_uma_pid: str,
    submitted_nickname_chunk: str | None,
    submitted_participation_hint: str | None,
    requester_note: str | None,
    interaction_id: str,
) -> PlayerLinkMutationDTO:
    return _submit_player_link_request(
        guild_id=guild_id,
        requester_discord_user_id=requester_discord_user_id,
        discord_nickname_snapshot=discord_nickname_snapshot,
        submitted_ingame_name=submitted_ingame_name,
        submitted_uma_pid=submitted_uma_pid,
        submitted_nickname_chunk=submitted_nickname_chunk,
        submitted_participation_hint=submitted_participation_hint,
        requester_note=requester_note,
        interaction_id=interaction_id,
    )


def _player_link_cancel_request(
    *,
    request_id: int,
    guild_id: str,
    requester_discord_user_id: str,
    reason: str | None,
    interaction_id: str,
) -> PlayerLinkMutationDTO:
    return _cancel_player_link_request(
        request_id=request_id,
        guild_id=guild_id,
        requester_discord_user_id=requester_discord_user_id,
        reason=reason,
        interaction_id=interaction_id,
    )


def _player_link_revise_request(
    *,
    request_id: int,
    guild_id: str,
    requester_discord_user_id: str,
    discord_nickname_snapshot: str,
    submitted_ingame_name: str,
    submitted_uma_pid: str,
    submitted_nickname_chunk: str | None,
    submitted_participation_hint: str | None,
    requester_note: str | None,
    interaction_id: str,
) -> PlayerLinkMutationDTO:
    return _revise_player_link_request(
        request_id=request_id,
        guild_id=guild_id,
        requester_discord_user_id=requester_discord_user_id,
        discord_nickname_snapshot=discord_nickname_snapshot,
        submitted_ingame_name=submitted_ingame_name,
        submitted_uma_pid=submitted_uma_pid,
        submitted_nickname_chunk=submitted_nickname_chunk,
        submitted_participation_hint=submitted_participation_hint,
        requester_note=requester_note,
        interaction_id=interaction_id,
    )


async def _player_link_send_user_error(
    interaction: discord.Interaction,
    command_name: str,
    prefix: str,
    error: Exception,
) -> None:
    await _send_user_error(interaction, command_name, prefix, error)


async def _player_link_send_pre_modal_user_error(
    interaction: discord.Interaction,
    command_name: str,
    prefix: str,
    error: Exception,
) -> None:
    await _send_pre_modal_user_error(interaction, command_name, prefix, error)


async def _player_link_send_internal_error(
    interaction: discord.Interaction,
    command_name: str,
) -> None:
    await _send_internal_error(interaction, command_name)


async def _player_link_handle_modal_open_error(
    interaction: discord.Interaction,
    command_name: str,
) -> None:
    await _handle_modal_open_error(interaction, command_name)


async def _player_link_send_initial_response(
    interaction: discord.Interaction,
    command_name: str,
    content: str,
) -> bool:
    return await _send_initial_response_safely(interaction, command_name, content)


async def _player_link_send_followup(
    interaction: discord.Interaction,
    command_name: str,
    content: str,
    *,
    response_kind: str = "success",
    view: discord.ui.View | None = None,
) -> bool:
    return await _send_followup_safely(
        interaction,
        command_name,
        content,
        response_kind=response_kind,
        view=view,
    )


def _player_link_display_name(user: object) -> str:
    return _display_name(user)


_PLAYER_LINK_ADAPTER = PlayerLinkAdapter(
    PlayerLinkAdapterPorts(
        prepare_command=_player_link_prepare_command,
        prepare_component=_player_link_prepare_component,
        query_owned_request=_player_link_query_owned_request,
        submit_request=_player_link_submit_request,
        cancel_request=_player_link_cancel_request,
        revise_request=_player_link_revise_request,
        send_user_error=_player_link_send_user_error,
        send_pre_modal_user_error=_player_link_send_pre_modal_user_error,
        send_internal_error=_player_link_send_internal_error,
        handle_modal_open_error=_player_link_handle_modal_open_error,
        send_initial_response=_player_link_send_initial_response,
        send_followup=_player_link_send_followup,
        correlation_id=_correlation_id,
        display_name=_player_link_display_name,
        bounded_message=_bounded_discord_message,
        safe_text=_safe_discord_text,
        logger=logger,
    )
)


def _staff_player_link_query_queue(guild_id: str) -> tuple[PlayerLinkRequestDTO, ...]:
    return _list_player_link_requests(guild_id)


def _staff_player_link_query_active_request(
    guild_id: str,
    requester_discord_user_id: str,
) -> PlayerLinkRequestDTO | None:
    return _get_active_player_link_request(guild_id, requester_discord_user_id)


def _staff_player_link_query_request(
    guild_id: str,
    request_id: int,
) -> PlayerLinkRequestDTO:
    return _get_staff_player_link_request(guild_id, request_id)


def _staff_player_link_query_candidates(
    request_id: int,
) -> tuple[PlayerLinkCandidateDTO, ...]:
    return _search_staff_player_link_candidates(request_id)


def _staff_player_link_query_candidates_for_terms(
    submitted_ingame_name: str,
    discord_nickname_snapshot: str,
    submitted_nickname_chunk: str | None,
) -> tuple[PlayerLinkCandidateDTO, ...]:
    return _search_staff_player_link_candidates_for_terms(
        submitted_ingame_name,
        discord_nickname_snapshot,
        submitted_nickname_chunk,
    )


def _staff_player_link_submit_request(
    *,
    guild_id: str,
    requester_discord_user_id: str,
    discord_nickname_snapshot: str,
    submitted_ingame_name: str,
    submitted_uma_pid: str,
    submitted_nickname_chunk: str | None,
    submitted_participation_hint: str | None,
    requester_note: str | None,
    interaction_id: str,
    submitted_by_discord_user_id: str,
) -> PlayerLinkMutationDTO:
    return _submit_player_link_request(
        guild_id=guild_id,
        requester_discord_user_id=requester_discord_user_id,
        discord_nickname_snapshot=discord_nickname_snapshot,
        submitted_ingame_name=submitted_ingame_name,
        submitted_uma_pid=submitted_uma_pid,
        submitted_nickname_chunk=submitted_nickname_chunk,
        submitted_participation_hint=submitted_participation_hint,
        requester_note=requester_note,
        interaction_id=interaction_id,
        submitted_by_discord_user_id=submitted_by_discord_user_id,
    )


def _staff_player_link_approve_request(
    *,
    request_id: int,
    candidate: PlayerLinkCandidateDTO,
    actor_discord_user_id: str,
    interaction_id: str,
) -> PlayerLinkApprovalDTO | PlayerLinkMutationDTO:
    return _approve_player_link_request(
        request_id=request_id,
        candidate=candidate,
        actor_discord_user_id=actor_discord_user_id,
        interaction_id=interaction_id,
    )


def _staff_player_link_review_request(
    *,
    request_id: int,
    actor_discord_user_id: str,
    note: str,
    interaction_id: str,
) -> PlayerLinkMutationDTO:
    return _review_player_link_request(
        request_id=request_id,
        actor_discord_user_id=actor_discord_user_id,
        note=note,
        interaction_id=interaction_id,
    )


def _staff_player_link_reject_request(
    *,
    request_id: int,
    actor_discord_user_id: str,
    note: str,
    interaction_id: str,
) -> PlayerLinkMutationDTO:
    return _reject_player_link_request(
        request_id=request_id,
        actor_discord_user_id=actor_discord_user_id,
        note=note,
        interaction_id=interaction_id,
    )


async def _staff_player_link_publish_resolution_history(
    interaction: discord.Interaction,
    *,
    request: PlayerLinkRequestDTO,
    resolution: str,
) -> None:
    await _post_player_link_resolution_history(
        interaction,
        request=request,
        resolution=resolution,
    )


async def _staff_player_link_close_view(source_message: object | None) -> None:
    await _close_player_link_view(source_message)


_STAFF_PLAYER_LINK_ADAPTER = StaffPlayerLinkAdapter(
    StaffPlayerLinkAdapterPorts(
        prepare_command=_player_link_prepare_command,
        prepare_component=_player_link_prepare_component,
        query_active_request=_staff_player_link_query_active_request,
        query_queue=_staff_player_link_query_queue,
        query_request=_staff_player_link_query_request,
        query_candidates=_staff_player_link_query_candidates,
        query_candidates_for_terms=_staff_player_link_query_candidates_for_terms,
        submit_request=_staff_player_link_submit_request,
        approve_request=_staff_player_link_approve_request,
        review_request=_staff_player_link_review_request,
        reject_request=_staff_player_link_reject_request,
        publish_resolution_history=_staff_player_link_publish_resolution_history,
        close_view=_staff_player_link_close_view,
        send_user_error=_player_link_send_user_error,
        send_pre_modal_user_error=_player_link_send_pre_modal_user_error,
        send_internal_error=_player_link_send_internal_error,
        handle_modal_open_error=_player_link_handle_modal_open_error,
        send_followup=_player_link_send_followup,
        correlation_id=_correlation_id,
        display_name=_player_link_display_name,
        bounded_message=_bounded_discord_message,
        safe_text=_safe_discord_text,
        logger=logger,
    )
)


async def _legacy_account_link_request(interaction: discord.Interaction) -> None:
    await _PLAYER_LINK_ADAPTER.open_member_request(interaction)


async def _legacy_account_link_status(interaction: discord.Interaction) -> None:
    await _PLAYER_LINK_ADAPTER.show_member_status(interaction)


async def _legacy_account_link_cancel(interaction: discord.Interaction) -> None:
    await _PLAYER_LINK_ADAPTER.open_member_cancel(interaction)


async def _account_prepare_command(
    interaction: discord.Interaction,
    command_name: str,
    *,
    defer: bool = True,
) -> Settings | None:
    return await _prepare_command(interaction, command_name, defer=defer)


async def _account_require_guild_id(
    interaction: discord.Interaction,
    command_name: str,
) -> str | None:
    return await _require_guild_id(interaction, command_name)


def _account_registration_modal_factory(_adapter: AccountAdapter) -> discord.ui.Modal:
    return AccountRegistrationModal()


def _account_submit_registration(
    *,
    guild_id: str,
    requester_discord_user_id: str,
    discord_nickname_snapshot: str,
    submitted_uma_pid: str,
    submitted_nickname: str | None,
    submitted_ingame_name: str | None,
    interaction_id: str,
) -> AccountRegistrationMutationDTO:
    return _submit_account_registration_request(
        guild_id=guild_id,
        requester_discord_user_id=requester_discord_user_id,
        discord_nickname_snapshot=discord_nickname_snapshot,
        submitted_uma_pid=submitted_uma_pid,
        submitted_nickname=submitted_nickname,
        submitted_ingame_name=submitted_ingame_name,
        interaction_id=interaction_id,
    )


def _account_query_owned_registration(
    *,
    guild_id: str,
    requester_discord_user_id: str,
) -> AccountRegistrationRequestDTO | None:
    return _get_owned_account_registration_request(
        guild_id=guild_id,
        requester_discord_user_id=requester_discord_user_id,
    )


def _account_cancel_registration(
    *,
    request_id: int,
    guild_id: str,
    requester_discord_user_id: str,
    reason: str | None,
    interaction_id: str,
) -> AccountRegistrationMutationDTO:
    return _cancel_account_registration_request(
        request_id=request_id,
        guild_id=guild_id,
        requester_discord_user_id=requester_discord_user_id,
        reason=reason,
        interaction_id=interaction_id,
    )


def _account_query_overview(discord_user_id: str) -> AccountOverviewDTO:
    return _account_info(discord_user_id)


async def _account_send_user_error(
    interaction: discord.Interaction,
    command_name: str,
    prefix: str,
    error: Exception,
) -> None:
    await _send_user_error(interaction, command_name, prefix, error)


async def _account_send_internal_error(
    interaction: discord.Interaction,
    command_name: str,
) -> None:
    await _send_internal_error(interaction, command_name)


async def _account_handle_modal_open_error(
    interaction: discord.Interaction,
    command_name: str,
) -> None:
    await _handle_modal_open_error(interaction, command_name)


async def _account_send_initial_response(
    interaction: discord.Interaction,
    command_name: str,
    content: str,
) -> bool:
    return await _send_initial_response_safely(interaction, command_name, content)


async def _account_send_followup(
    interaction: discord.Interaction,
    command_name: str,
    content: str,
    *,
    response_kind: str = "success",
) -> bool:
    return await _send_followup_safely(
        interaction,
        command_name,
        content,
        response_kind=response_kind,
    )


def _account_correlation_id(interaction: object) -> str:
    return _correlation_id(interaction)


def _account_interaction_user_id(interaction: object) -> str:
    return _interaction_user_id(interaction)


def _account_display_name(user: object) -> str:
    return _display_name(user)


def _account_bounded_message(lines: list[str]) -> str:
    return _bounded_discord_message(lines)


def _account_safe_text(value: str) -> str:
    return _safe_discord_text(value)


_ACCOUNT_ADAPTER = AccountAdapter(
    AccountAdapterPorts(
        prepare_command=_account_prepare_command,
        require_guild_id=_account_require_guild_id,
        submit_registration=_account_submit_registration,
        query_owned_registration=_account_query_owned_registration,
        cancel_registration=_account_cancel_registration,
        query_account_overview=_account_query_overview,
        registration_modal_factory=_account_registration_modal_factory,
        legacy_link_request=_legacy_account_link_request,
        legacy_link_status=_legacy_account_link_status,
        legacy_link_cancel=_legacy_account_link_cancel,
        send_user_error=_account_send_user_error,
        send_internal_error=_account_send_internal_error,
        handle_modal_open_error=_account_handle_modal_open_error,
        send_initial_response=_account_send_initial_response,
        send_followup=_account_send_followup,
        correlation_id=_account_correlation_id,
        interaction_user_id=_account_interaction_user_id,
        display_name=_account_display_name,
        bounded_message=_account_bounded_message,
        safe_text=_account_safe_text,
        logger=logger,
    )
)


class AccountRegistrationModal(account_adapter.AccountRegistrationModal):
    def __init__(self) -> None:
        super().__init__(adapter=_ACCOUNT_ADAPTER)


class AccountCommandGroup(account_adapter.AccountCommandGroup):
    def __init__(self) -> None:
        super().__init__(adapter=_ACCOUNT_ADAPTER)


def create_account_command_group() -> AccountCommandGroup:
    return AccountCommandGroup()


async def _staff_account_prepare_command(
    interaction: discord.Interaction,
    command_name: str,
    *,
    defer: bool = True,
) -> Settings | None:
    return await _prepare_command(interaction, command_name, defer=defer)


async def _staff_account_prepare_component(
    interaction: discord.Interaction,
    context: InteractionContext,
    *,
    command_name: str,
    defer: bool = True,
) -> Settings | None:
    return await _prepare_bound_component(
        interaction,
        context,
        command_name=command_name,
        defer=defer,
    )


def _staff_account_build_context(interaction: discord.Interaction) -> InteractionContext:
    return _settings_interaction_context(interaction)


def _staff_account_query_registered_accounts(
    *,
    query: str = "",
) -> tuple[AutocompleteChoice, ...]:
    return query_staff_registered_accounts(query=query)


def _staff_account_query_registered_accounts_port() -> tuple[AutocompleteChoice, ...]:
    return _staff_account_query_registered_accounts()


def _staff_account_grant_circle_points(
    game_account_id: int,
    amount: int,
    reason: str,
    actor_discord_user_id: str,
    interaction_id: str,
) -> tuple[CirclePointTransactionDTO, str]:
    return _grant_circle_points_to_account(
        game_account_id,
        amount,
        reason,
        actor_discord_user_id,
        interaction_id,
    )


def _staff_account_adjust_circle_points(
    game_account_id: int,
    amount: int,
    reason: str,
    actor_discord_user_id: str,
    interaction_id: str,
) -> tuple[CirclePointTransactionDTO, str]:
    return _adjust_circle_points_to_account(
        game_account_id,
        amount,
        reason,
        actor_discord_user_id,
        interaction_id,
    )


def _staff_account_update_ingame_name(
    game_account_id: int,
    ingame_name: str,
) -> GameAccountDTO:
    return _change_game_account_ingame_name(game_account_id, ingame_name)


def _staff_account_circle_point_grant_modal_factory(
    _adapter: StaffAccountAdapter,
    choices: tuple[AutocompleteChoice, ...],
    context: InteractionContext,
) -> discord.ui.Modal:
    return CirclePointGrantModal(choices=choices, context=context)


def _staff_account_circle_point_adjustment_modal_factory(
    _adapter: StaffAccountAdapter,
    game_account_id: int,
    target_label: str,
    context: InteractionContext,
) -> discord.ui.Modal:
    return CirclePointAdjustmentModal(
        game_account_id=game_account_id,
        target_label=target_label,
        context=context,
    )


def _staff_account_ingame_name_modal_factory(
    _adapter: StaffAccountAdapter,
    game_account_id: int,
    target_label: str,
    context: InteractionContext,
) -> discord.ui.Modal:
    return IngameNameChangeModal(
        game_account_id=game_account_id,
        target_label=target_label,
        context=context,
    )


async def _staff_account_send_user_error(
    interaction: discord.Interaction,
    command_name: str,
    prefix: str,
    error: Exception,
) -> None:
    await _send_user_error(interaction, command_name, prefix, error)


async def _staff_account_send_pre_modal_user_error(
    interaction: discord.Interaction,
    command_name: str,
    prefix: str,
    error: Exception,
) -> None:
    await _send_pre_modal_user_error(interaction, command_name, prefix, error)


async def _staff_account_send_internal_error(
    interaction: discord.Interaction,
    command_name: str,
) -> None:
    await _send_internal_error(interaction, command_name)


async def _staff_account_handle_modal_open_error(
    interaction: discord.Interaction,
    command_name: str,
) -> None:
    await _handle_modal_open_error(interaction, command_name)


async def _staff_account_send_initial_response(
    interaction: discord.Interaction,
    command_name: str,
    content: str,
) -> bool:
    return await _send_initial_response_safely(interaction, command_name, content)


async def _staff_account_send_followup(
    interaction: discord.Interaction,
    command_name: str,
    content: str,
    *,
    response_kind: str = "success",
) -> bool:
    return await _send_followup_safely(
        interaction,
        command_name,
        content,
        response_kind=response_kind,
    )


def _staff_account_correlation_id(interaction: object) -> str:
    return _correlation_id(interaction)


def _staff_account_interaction_user_id(interaction: object) -> str:
    return _interaction_user_id(interaction)


def _staff_account_safe_text(value: str) -> str:
    return _safe_discord_text(value)


_STAFF_ACCOUNT_ADAPTER = StaffAccountAdapter(
    StaffAccountAdapterPorts(
        prepare_command=_staff_account_prepare_command,
        prepare_component=_staff_account_prepare_component,
        build_context=_staff_account_build_context,
        query_registered_accounts=_staff_account_query_registered_accounts_port,
        grant_circle_points=_staff_account_grant_circle_points,
        adjust_circle_points=_staff_account_adjust_circle_points,
        update_ingame_name=_staff_account_update_ingame_name,
        circle_point_grant_modal_factory=_staff_account_circle_point_grant_modal_factory,
        circle_point_adjustment_modal_factory=_staff_account_circle_point_adjustment_modal_factory,
        ingame_name_modal_factory=_staff_account_ingame_name_modal_factory,
        send_user_error=_staff_account_send_user_error,
        send_pre_modal_user_error=_staff_account_send_pre_modal_user_error,
        send_internal_error=_staff_account_send_internal_error,
        handle_modal_open_error=_staff_account_handle_modal_open_error,
        send_initial_response=_staff_account_send_initial_response,
        send_followup=_staff_account_send_followup,
        correlation_id=_staff_account_correlation_id,
        interaction_user_id=_staff_account_interaction_user_id,
        safe_text=_staff_account_safe_text,
        logger=logger,
    )
)


async def _staff_persona_prepare_command(
    interaction: discord.Interaction,
    command_name: str,
    *,
    defer: bool = True,
) -> Settings | None:
    return await _prepare_command(interaction, command_name, defer=defer)


async def _staff_persona_prepare_component(
    interaction: discord.Interaction,
    context: InteractionContext,
    *,
    command_name: str,
    defer: bool = True,
) -> Settings | None:
    return await _prepare_bound_component(
        interaction,
        context,
        command_name=command_name,
        defer=defer,
    )


async def _staff_persona_autocomplete_authorized(
    interaction: discord.Interaction,
    command_name: str,
) -> bool:
    return await _autocomplete_interaction_authorized(interaction, command_name)


def _staff_persona_build_context(interaction: discord.Interaction) -> InteractionContext:
    return _settings_interaction_context(interaction)


def _staff_persona_query_personas(query: str) -> tuple[StaffPersonaChoiceDTO, ...]:
    return query_staff_persona_choices(query=query)


def _staff_persona_query_source_game_accounts(
    query: str,
) -> tuple[StaffSourceGameAccountChoiceDTO, ...]:
    return query_staff_source_game_account_choices(query=query)


def _staff_persona_preview_attach(
    command: StaffDiscordPersonaAttachCommand,
) -> StaffDiscordPersonaAttachPreviewDTO:
    return preview_staff_discord_persona_attach(command)


def _staff_persona_apply_attach(
    command: StaffDiscordPersonaAttachCommand,
) -> StaffDiscordPersonaAttachResultDTO:
    return execute_staff_discord_persona_attach(command)


def _staff_persona_preview_claim(
    command: StaffSourceGameAccountClaimCommand,
) -> StaffSourceGameAccountClaimPreviewDTO:
    return preview_staff_source_game_account_claim(command)


def _staff_persona_apply_claim(
    command: StaffSourceGameAccountClaimCommand,
) -> StaffSourceGameAccountClaimResultDTO:
    return execute_staff_source_game_account_claim(command)


async def _staff_persona_send_user_error(
    interaction: discord.Interaction,
    command_name: str,
    prefix: str,
    error: Exception,
) -> None:
    await _send_user_error(interaction, command_name, prefix, error)


async def _staff_persona_send_internal_error(
    interaction: discord.Interaction,
    command_name: str,
) -> None:
    await _send_internal_error(interaction, command_name)


async def _staff_persona_send_followup(
    interaction: discord.Interaction,
    command_name: str,
    content: str,
    *,
    response_kind: str = "success",
    view: discord.ui.View | None = None,
) -> bool:
    return await _send_followup_safely(
        interaction,
        command_name,
        content,
        response_kind=response_kind,
        view=view,
    )


async def _staff_persona_close_view(source_message: object | None) -> None:
    await _close_extracted_player_link_view(source_message, logger=logger)


_STAFF_PERSONA_ADAPTER = StaffPersonaAdapter(
    StaffPersonaAdapterPorts(
        prepare_command=_staff_persona_prepare_command,
        prepare_component=_staff_persona_prepare_component,
        autocomplete_authorized=_staff_persona_autocomplete_authorized,
        build_context=_staff_persona_build_context,
        query_personas=_staff_persona_query_personas,
        query_source_game_accounts=_staff_persona_query_source_game_accounts,
        preview_attach=_staff_persona_preview_attach,
        apply_attach=_staff_persona_apply_attach,
        preview_claim=_staff_persona_preview_claim,
        apply_claim=_staff_persona_apply_claim,
        send_user_error=_staff_persona_send_user_error,
        send_internal_error=_staff_persona_send_internal_error,
        send_followup=_staff_persona_send_followup,
        close_view=_staff_persona_close_view,
        correlation_id=_correlation_id,
        safe_text=_safe_discord_text,
        logger=logger,
    )
)


class CirclePointGrantModal(staff_account_adapter.CirclePointGrantModal):
    def __init__(
        self,
        *,
        choices: tuple[AutocompleteChoice, ...],
        context: InteractionContext,
    ) -> None:
        super().__init__(
            adapter=_STAFF_ACCOUNT_ADAPTER,
            choices=choices,
            context=context,
        )


class CirclePointAdjustmentModal(staff_account_adapter.CirclePointAdjustmentModal):
    def __init__(
        self,
        *,
        game_account_id: int,
        target_label: str,
        context: InteractionContext,
    ) -> None:
        super().__init__(
            adapter=_STAFF_ACCOUNT_ADAPTER,
            game_account_id=game_account_id,
            target_label=target_label,
            context=context,
        )


class IngameNameChangeModal(staff_account_adapter.IngameNameChangeModal):
    def __init__(
        self,
        *,
        game_account_id: int,
        target_label: str,
        context: InteractionContext,
    ) -> None:
        super().__init__(
            adapter=_STAFF_ACCOUNT_ADAPTER,
            game_account_id=game_account_id,
            target_label=target_label,
            context=context,
        )


class StaffCommandGroup(app_commands.Group):
    def __init__(self) -> None:
        super().__init__(name="staff", description="스태프 전용 공통 기능입니다.")
        for command_name in (
            "room-race-create",
            "room-race-edit",
            "room-race-condition-set",
            "room-race-entries-set",
            "room-race-betting-open",
            "room-race-betting-close",
            "room-race-show",
            "win5-season-create",
            "win5-season-activate",
            "win5-season-close",
            "win5-round-create",
            "win5-special-round-create",
            "win5-round-open",
            "win5-round-close",
            "win5-result-enter",
            "win5-score",
        ):
            self.remove_command(command_name)
        self.add_command(_STAFF_PERSONA_ADAPTER.create_command_group())

    @app_commands.command(name="grant-circle-points", description="등록 PID에 서클 포인트를 지급합니다.")
    async def grant_circle_points(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _STAFF_ACCOUNT_ADAPTER.grant_circle_points(interaction)

    @app_commands.command(name="adjust-circle-points", description="등록 PID의 서클 포인트를 증감 조정합니다.")
    @app_commands.describe(target_account_id="PID 또는 표시명으로 검색할 대상")
    @app_commands.autocomplete(target_account_id=_autocomplete_circle_point_account)
    async def adjust_circle_points(
        self,
        interaction: discord.Interaction,
        target_account_id: int,
    ) -> None:
        await _STAFF_ACCOUNT_ADAPTER.adjust_circle_points(
            interaction,
            target_account_id=target_account_id,
        )

    @app_commands.command(name="change-name", description="등록 PID의 인게임명을 변경합니다.")
    @app_commands.describe(target_account_id="PID 또는 표시명으로 검색할 대상")
    @app_commands.autocomplete(target_account_id=_autocomplete_staff_change_name_account)
    async def change_name(
        self,
        interaction: discord.Interaction,
        target_account_id: int,
    ) -> None:
        await _STAFF_ACCOUNT_ADAPTER.change_name(
            interaction,
            target_account_id=target_account_id,
        )

    @app_commands.command(name="link-player", description="기존 기록 연결을 검색·승인합니다.")
    @app_commands.describe(target="직접 연결할 Discord 사용자 (생략하면 기존 요청 목록을 엽니다)")
    async def link_player(
        self,
        interaction: discord.Interaction,
        target: discord.Member | None = None,
    ) -> None:
        await _STAFF_PLAYER_LINK_ADAPTER.link_player(
            interaction,
            target=target,
        )

    @app_commands.command(name="account-registration-approve", description="계정 등록 요청을 승인합니다.")
    @app_commands.describe(
        request_id="승인할 계정 등록 요청 ID",
        review_note="승인 메모 (선택)",
        persona_id="기존 Persona UUID (다른 Discord 계정 연결 시 선택)",
    )
    async def account_registration_approve(
        self,
        interaction: discord.Interaction,
        request_id: int,
        review_note: str | None = None,
        persona_id: str | None = None,
    ) -> None:
        command_name = "staff.account-registration-approve"
        if await _prepare_command(interaction, command_name) is None:
            return
        try:
            result = await asyncio.to_thread(
                _approve_account_registration_request,
                request_id=request_id,
                reviewed_by_discord_user_id=str(interaction.user.id),
                review_note=review_note,
                target_persona_id=persona_id,
                interaction_id=_correlation_id(interaction),
            )
        except DomainError as exc:
            await _send_user_error(interaction, command_name, "계정 등록 요청을 승인하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_followup_safely(
            interaction,
            command_name,
            f"계정 등록 요청 #{result.request.id}을(를) 승인했습니다. "
            f"참가자: {_safe_discord_text(result.request.accepted_persona_display_name or '-')} "
            f"(`{result.request.accepted_persona_short_id or '-'}`), "
            f"초기 서클 포인트 지급량: {result.request.initial_grant_amount}",
        )
        await _notify_account_registration_completion(interaction, request=result.request)

    @app_commands.command(name="account-registration-reject", description="계정 등록 요청을 반려합니다.")
    @app_commands.describe(
        request_id="반려할 계정 등록 요청 ID",
        review_note="반려 사유",
    )
    async def account_registration_reject(
        self,
        interaction: discord.Interaction,
        request_id: int,
        review_note: str,
    ) -> None:
        command_name = "staff.account-registration-reject"
        if await _prepare_command(interaction, command_name) is None:
            return
        try:
            result = await asyncio.to_thread(
                _reject_account_registration_request,
                request_id=request_id,
                reviewed_by_discord_user_id=str(interaction.user.id),
                review_note=review_note,
                interaction_id=_correlation_id(interaction),
            )
        except DomainError as exc:
            await _send_user_error(interaction, command_name, "계정 등록 요청을 반려하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_followup_safely(
            interaction,
            command_name,
            f"계정 등록 요청 #{result.request.id}을(를) 반려했습니다.",
        )

    @app_commands.command(name="room-race-create", description="룸매치 레이스를 setup 상태로 생성합니다.")
    async def room_race_create(
        self,
        interaction: discord.Interaction,
    ) -> None:
        command_name = "match.staff.race-create"
        if await _prepare_command(interaction, command_name, defer=False) is None:
            return
        try:
            await interaction.response.send_modal(RoomRaceCreateModal())
        except Exception:
            await _handle_modal_open_error(interaction, command_name)

    @app_commands.command(name="room-race-edit", description="setup 레이스의 metadata를 수정합니다.")
    async def room_race_edit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_room_target_modal(
            interaction,
            command_name="match.staff.race-edit",
            purpose=RoomRaceChoicePurpose.EDIT,
            modal_factory=lambda choices: RoomRaceEditModal(choices=choices),
        )

    @app_commands.command(name="room-race-condition-set", description="setup 레이스의 조건 snapshot을 설정합니다.")
    async def room_race_condition_set(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_room_target_modal(
            interaction,
            command_name="match.staff.race-condition-set",
            purpose=RoomRaceChoicePurpose.EDIT,
            modal_factory=lambda choices: RoomRaceConditionModal(choices=choices),
        )

    @app_commands.command(name="room-race-entries-set", description="외부 게임의 확정 엔트리를 전체 교체합니다.")
    async def room_race_entries_set(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_room_target_modal(
            interaction,
            command_name="match.staff.entries-set",
            purpose=RoomRaceChoicePurpose.ENTRIES,
            modal_factory=lambda choices: RoomEntriesModal(choices=choices),
        )

    @app_commands.command(name="room-race-betting-open", description="setup 레이스의 베팅을 시작합니다.")
    async def room_race_betting_open(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_lifecycle_batch_modal(
            interaction,
            command_name="match.staff.betting-open",
            domain="room",
            opening=True,
        )

    @app_commands.command(name="room-race-betting-close", description="열린 룸매치 베팅을 마감합니다.")
    async def room_race_betting_close(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_lifecycle_batch_modal(
            interaction,
            command_name="match.staff.betting-close",
            domain="room",
            opening=False,
        )

    @app_commands.command(name="room-race-show", description="룸매치 레이스 control-plane snapshot을 조회합니다.")
    async def room_race_show(self, interaction: discord.Interaction, race_id: int) -> None:
        if await _prepare_command(interaction, "match.staff.race-show") is None:
            return
        try:
            result = await asyncio.to_thread(_show_match_race, race_id)
        except (DomainError, ValueError) as exc:
            await _send_user_error(interaction, "match.staff.race-show", "레이스를 조회하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, "match.staff.race-show")
            return
        await _send_race_operation_result(interaction, "match.staff.race-show", result)

    @app_commands.command(name="win5-season-create", description="WIN5 시즌을 setup 상태로 생성합니다.")
    async def win5_season_create(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_win5_season_create_modal(interaction)

    @app_commands.command(name="win5-season-activate", description="WIN5 시즌을 활성화합니다.")
    async def win5_season_activate(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_win5_season_transition_modal(
            interaction,
            action="activate",
        )

    @app_commands.command(name="win5-season-close", description="WIN5 시즌을 종료합니다.")
    async def win5_season_close(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_win5_season_transition_modal(
            interaction,
            action="close",
        )

    async def _run_win5_season_transition(
        self,
        interaction: discord.Interaction,
        *,
        command_name: str,
        season_id: int,
        reason: str | None,
        operation,
        failure_prefix: str,
    ) -> None:
        if await _prepare_command(interaction, command_name) is None:
            return
        try:
            result = await asyncio.to_thread(
                operation,
                season_id,
                reason,
                str(interaction.user.id),
                str(interaction.id),
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(interaction, command_name, failure_prefix, exc)
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_win5_mutation_result(interaction, command_name, result)

    @app_commands.command(name="win5-round-create", description="일반 WIN5 라운드와 경기를 생성합니다.")
    async def win5_round_create(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_win5_round_create_modal(interaction)

    @app_commands.command(name="win5-special-round-create", description="특별 WIN5 라운드를 생성합니다.")
    async def win5_special_round_create(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_win5_special_round_create_modal(interaction)

    @app_commands.command(name="win5-round-open", description="WIN5 라운드 제출을 시작합니다.")
    async def win5_round_open(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_lifecycle_batch_modal(
            interaction,
            command_name="win5.staff.round",
            domain="win5",
            opening=True,
        )

    @app_commands.command(name="win5-round-close", description="WIN5 라운드 제출을 마감합니다.")
    async def win5_round_close(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_lifecycle_batch_modal(
            interaction,
            command_name="win5.staff.round",
            domain="win5",
            opening=False,
        )

    @app_commands.command(name="win5-result-enter", description="WIN5 경기의 확정 결과를 입력합니다.")
    async def win5_result_enter(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_win5_result_enter_modal(interaction)

    @app_commands.command(name="win5-score", description="WIN5 라운드를 채점합니다.")
    async def win5_score(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_win5_score_modal(interaction)


class Win5StaffCommandGroup(app_commands.Group):
    def __init__(self) -> None:
        super().__init__(name="staff", description="WIN5 스태프 전용 기능입니다.")

    @app_commands.command(name="season", description="WIN5 시즌 생성·활성화·종료·삭제를 관리합니다.")
    async def season(self, interaction: discord.Interaction) -> None:
        await _show_win5_staff_control_panel(interaction, domain="season")

    @app_commands.command(name="round", description="WIN5 라운드 생성·상태·결과·채점을 관리합니다.")
    async def round(self, interaction: discord.Interaction) -> None:
        await _show_win5_staff_control_panel(interaction, domain="round")


class RoomStaffCommandGroup(app_commands.Group):
    def __init__(self) -> None:
        super().__init__(name="staff", description="룸매치 스태프 전용 기능입니다.")

    @app_commands.command(name="race-create", description="룸매치 레이스를 생성합니다.")
    async def race_create(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await StaffCommandGroup.room_race_create.callback(self, interaction)

    @app_commands.command(name="race-edit", description="setup 룸매치 레이스 정보를 수정합니다.")
    async def race_edit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await StaffCommandGroup.room_race_edit.callback(self, interaction)

    @app_commands.command(name="race-condition-set", description="setup 레이스의 조건을 설정합니다.")
    async def race_condition_set(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await StaffCommandGroup.room_race_condition_set.callback(self, interaction)

    @app_commands.command(name="entries-set", description="룸매치 확정 엔트리를 전체 교체합니다.")
    async def entries_set(self, interaction: discord.Interaction) -> None:
        await StaffCommandGroup.room_race_entries_set.callback(self, interaction)

    @app_commands.command(name="betting-open", description="setup 레이스의 베팅을 시작합니다.")
    async def betting_open(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await StaffCommandGroup.room_race_betting_open.callback(self, interaction)

    @app_commands.command(name="betting-close", description="열린 룸매치 베팅을 마감합니다.")
    async def betting_close(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await StaffCommandGroup.room_race_betting_close.callback(self, interaction)

    @app_commands.command(name="race-show", description="룸매치 레이스 snapshot을 조회합니다.")
    @app_commands.autocomplete(race_id=_autocomplete_room_race_show)
    async def race_show(self, interaction: discord.Interaction, race_id: int) -> None:
        await StaffCommandGroup.room_race_show.callback(self, interaction, race_id)

    @app_commands.command(name="result-submit", description="룸매치 도착 순서를 제출합니다.")
    async def result_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_room_result_modal(
            interaction,
            command_name="match.staff.result-submit",
            allowed_statuses=("betting_closed",),
            modal_factory=lambda choices, context: MatchResultEntryModal(
                command_name="match.staff.result-submit",
                choices=choices,
                context=context,
                correcting=False,
            ),
        )

    @app_commands.command(name="result-review", description="제출된 룸매치 결과를 검토 완료 처리합니다.")
    async def result_review(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_room_result_modal(
            interaction,
            command_name="match.staff.result-review",
            allowed_statuses=("result_review",),
            modal_factory=lambda choices, context: MatchResultDecisionModal(
                action="review",
                choices=choices,
                context=context,
            ),
        )

    @app_commands.command(name="result-correct", description="현재 룸매치 결과 revision을 정정합니다.")
    async def result_correct(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_room_result_modal(
            interaction,
            command_name="match.staff.result-correct",
            allowed_statuses=("result_review",),
            modal_factory=lambda choices, context: MatchResultEntryModal(
                command_name="match.staff.result-correct",
                choices=choices,
                context=context,
                correcting=True,
            ),
        )

    @app_commands.command(name="result-reject", description="현재 룸매치 결과 revision을 반려합니다.")
    async def result_reject(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_room_result_modal(
            interaction,
            command_name="match.staff.result-reject",
            allowed_statuses=("result_review",),
            modal_factory=lambda choices, context: MatchResultDecisionModal(
                action="reject",
                choices=choices,
                context=context,
            ),
        )

    @app_commands.command(name="result-confirm", description="검토된 룸매치 결과를 미리보고 확정합니다.")
    async def result_confirm(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_room_result_modal(
            interaction,
            command_name="match.staff.result-confirm",
            allowed_statuses=("result_review",),
            modal_factory=lambda choices, context: MatchResultDecisionModal(
                action="confirm",
                choices=choices,
                context=context,
            ),
        )

    @app_commands.command(name="settlement", description="결과 확정 룸매치의 배율을 확인하고 정산합니다.")
    async def settlement(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await _show_room_result_modal(
            interaction,
            command_name="match.staff.settlement",
            allowed_statuses=("result_confirmed",),
            modal_factory=lambda choices, context: MatchSettlementModal(
                choices=choices,
                context=context,
            ),
        )

    @app_commands.command(name="settlement-rollback", description="정산 완료 룸매치를 사유와 함께 롤백합니다.")
    @app_commands.describe(race_id="롤백할 정산 완료 레이스 ID", reason="필수 롤백 사유")
    async def settlement_rollback(
        self,
        interaction: discord.Interaction,
        race_id: int,
        reason: str,
    ) -> None:
        command_name = "match.staff.settlement-rollback"
        if await _prepare_command(interaction, command_name) is None:
            return
        normalized_reason = reason.strip()
        try:
            if race_id <= 0:
                raise ValueError("레이스 ID는 양의 정수여야 합니다.")
            if not normalized_reason:
                raise ValueError("롤백 사유를 입력해야 합니다.")
            if len(normalized_reason) > 255:
                raise ValueError("롤백 사유는 255자 이하여야 합니다.")
            context = _settings_interaction_context(interaction)
        except ValueError as exc:
            await _send_user_error(interaction, command_name, "롤백 미리보기를 만들지 못했습니다", exc)
            return
        await _send_followup_safely(
            interaction,
            command_name,
            _bounded_discord_message(
                [
                    "룸매치 정산 롤백 미리보기",
                    f"레이스: #{race_id}",
                    f"사유: {_safe_discord_text(normalized_reason)}",
                    "확정하면 서클 포인트와 Rating을 보상 기록으로 되돌리고 레이스를 voided 처리합니다.",
                    "이미 공개한 Discord 결과는 자동 삭제되지 않으므로 필요하면 정정 공지를 게시하세요.",
                ]
            ),
            view=MatchSettlementRollbackConfirmView(
                race_id=race_id,
                reason=normalized_reason,
                context=context,
            ),
        )

    @app_commands.command(name="publish", description="정산 완료 룸매치 결과를 공개합니다.")
    @app_commands.autocomplete(race_id=_autocomplete_room_publish)
    async def publish(self, interaction: discord.Interaction, race_id: int) -> None:
        command_name = "match.staff.publish"
        if await _prepare_command(interaction, command_name) is None:
            return
        try:
            result = await asyncio.to_thread(_get_match_result, race_id, None)
            if result.race_status != "settled" or result.submission is None:
                raise MatchResultError("정산 완료된 현재 결과 revision이 필요합니다.")
            publication_status = result.publication.status if result.publication is not None else None
            if publication_status not in {None, "failed", "delivery_unknown"}:
                raise MatchResultError("현재 결과 공개가 이미 진행 중이거나 완료되었습니다.")
            context = _settings_interaction_context(interaction)
        except (DomainError, ValueError) as exc:
            await _send_user_error(interaction, command_name, "공개 미리보기를 만들지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_followup_safely(
            interaction,
            command_name,
            _format_match_result(result, heading="룸매치 결과 공개 미리보기"),
            view=MatchPublishConfirmView(
                race_id=result.race_id,
                revision_number=result.submission.revision_number,
                authorize_delivery_unknown_retry=publication_status == "delivery_unknown",
                context=context,
            ),
        )

    @app_commands.command(name="result-show", description="룸매치 결과 revision을 조회합니다.")
    @app_commands.autocomplete(race_id=_autocomplete_room_result_show)
    async def result_show(
        self,
        interaction: discord.Interaction,
        race_id: int,
        revision_number: int | None = None,
    ) -> None:
        command_name = "match.staff.result-show"
        if await _prepare_command(interaction, command_name) is None:
            return
        try:
            result = await asyncio.to_thread(_get_match_result, race_id, revision_number)
        except DomainError as exc:
            await _send_user_error(interaction, command_name, "룸매치 결과를 조회하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        logger.info(
            "discord command succeeded correlation_id=%s command=%s race_id=%s submission_id=%s publication_id=%s",
            _correlation_id(interaction),
            command_name,
            result.race_id,
            result.submission.id if result.submission is not None else None,
            result.publication.id if result.publication is not None else None,
        )
        await _send_followup_safely(
            interaction,
            command_name,
            _format_match_result(result),
        )


async def _match_member_prepare_command(
    interaction: discord.Interaction,
    command_name: str,
    *,
    defer: bool = True,
) -> Settings | None:
    return await _prepare_command(interaction, command_name, defer=defer)


async def _match_member_autocomplete_authorized(
    interaction: discord.Interaction,
    command_name: str,
) -> bool:
    return await _autocomplete_interaction_authorized(interaction, command_name)


def _match_member_query_races() -> tuple[OpenMatchRace, ...]:
    return _list_room_match_races()


def _match_member_query_ratings() -> tuple[GameAccountRatingDTO, ...]:
    return query_game_account_rating_report()


def _match_member_query_bet_races(current: str) -> tuple[AutocompleteChoice, ...]:
    return query_member_match_bet_races(query=current)


def _match_member_query_bet_accounts(
    discord_user_id: str,
    current: str,
) -> tuple[AutocompleteChoice, ...]:
    return query_member_match_bet_accounts(
        discord_user_id=discord_user_id,
        query=current,
    )


def _match_member_place_bet(
    discord_user_id: str,
    game_account_id: int | None,
    race_id: int,
    bet_type: str,
    numbers: tuple[int, ...],
    amount: int,
    interaction_id: str,
):
    return _place_match_bet(
        discord_user_id,
        race_id,
        bet_type,
        numbers,
        amount,
        interaction_id,
        game_account_id=game_account_id,
    )


async def _match_member_send_user_error(
    interaction: discord.Interaction,
    command_name: str,
    prefix: str,
    error: Exception,
) -> None:
    await _send_user_error(interaction, command_name, prefix, error)


async def _match_member_send_internal_error(
    interaction: discord.Interaction,
    command_name: str,
) -> None:
    await _send_internal_error(interaction, command_name)


async def _match_member_send_followup(
    interaction: discord.Interaction,
    command_name: str,
    content: str,
    *,
    response_kind: str = "success",
    embed: discord.Embed | None = None,
) -> bool:
    return await _send_followup_safely(
        interaction,
        command_name,
        content,
        response_kind=response_kind,
        embed=embed,
    )


_MATCH_MEMBER_ADAPTER = MatchMemberAdapter(
    MatchMemberAdapterPorts(
        prepare_command=_match_member_prepare_command,
        autocomplete_authorized=_match_member_autocomplete_authorized,
        query_races=_match_member_query_races,
        query_ratings=_match_member_query_ratings,
        query_bet_races=_match_member_query_bet_races,
        query_bet_accounts=_match_member_query_bet_accounts,
        place_bet=_match_member_place_bet,
        send_user_error=_match_member_send_user_error,
        send_internal_error=_match_member_send_internal_error,
        send_followup=_match_member_send_followup,
        correlation_id=_correlation_id,
        interaction_user_id=_interaction_user_id,
        safe_text=_safe_discord_text,
        bounded_message=_bounded_discord_message,
        logger=logger,
    )
)


async def _match_staff_prepare_command(
    interaction: discord.Interaction,
    command_name: str,
    *,
    defer: bool = True,
) -> Settings | None:
    return await _prepare_command(interaction, command_name, defer=defer)


async def _match_staff_prepare_component(
    interaction: discord.Interaction,
    context: InteractionContext,
    *,
    command_name: str,
    defer: bool = True,
) -> Settings | None:
    return await _prepare_bound_component(
        interaction,
        context,
        command_name=command_name,
        defer=defer,
    )


def _match_staff_build_context(interaction: discord.Interaction) -> InteractionContext:
    return _settings_interaction_context(interaction)


def _match_staff_parse_operator_kst_datetime(value: str) -> datetime:
    return parse_operator_kst_datetime(value)


def _match_staff_parse_condition_profile(value: str) -> tuple[str, str, str, int, str]:
    return parse_room_condition_profile(value)


def _match_staff_parse_condition_environment(value: str) -> tuple[str, str, str]:
    return parse_room_condition_environment(value)


def _match_staff_parse_entries(
    value: str,
) -> tuple[RegisteredFinalEntrySnapshotInput, ...]:
    return parse_final_entry_snapshot(value)


async def _match_staff_send_race_result(
    interaction: discord.Interaction,
    command_name: str,
    result: RaceOperationResultDTO,
) -> None:
    await _send_race_operation_result(interaction, command_name, result)


async def _match_staff_send_race_batch_result(
    interaction: discord.Interaction,
    command_name: str,
    results: tuple[RaceOperationResultDTO, ...],
) -> None:
    await _send_room_lifecycle_batch_result(interaction, command_name, results)


async def _match_staff_send_result_notification(
    interaction: discord.Interaction,
    command_name: str,
    result: MatchResultNotificationDTO,
) -> None:
    await _send_match_result_notification(interaction, command_name, result)


def _match_staff_lifecycle_race_choices(
    purpose: RoomRaceChoicePurpose,
) -> tuple[AutocompleteChoice, ...]:
    return match_lifecycle_application.query_match_race_choices(purpose=purpose)


def _match_staff_result_race_choices(
    allowed_statuses: tuple[str, ...],
) -> tuple[AutocompleteChoice, ...]:
    return match_lifecycle_application.query_match_race_choices(
        purpose=RoomRaceChoicePurpose.RESULT,
        allowed_statuses=allowed_statuses,
    )


def _match_staff_settlement_race_choices(
    purpose: RoomRaceChoicePurpose,
    query: str,
    allowed_statuses: tuple[str, ...] | None,
) -> tuple[AutocompleteChoice, ...]:
    return match_lifecycle_application.query_match_race_choices(
        purpose=purpose,
        query=query,
        allowed_statuses=allowed_statuses,
    )


def _match_staff_settlement_result_query(
    race_id: int,
    revision_number: int | None,
) -> MatchResultOperationDTO:
    return match_settlement_application.query_match_result(
        match_settlement_application.MatchResultQuery(
            race_id=race_id,
            revision_number=revision_number,
        )
    )


_MATCH_STAFF_LIFECYCLE_ADAPTER = match_staff_lifecycle_adapter.MatchStaffLifecycleAdapter(
    match_staff_lifecycle_adapter.MatchStaffLifecycleAdapterPorts(
        prepare_command=_match_staff_prepare_command,
        prepare_bound_component=_match_staff_prepare_component,
        build_context=_match_staff_build_context,
        query_race_choices=_match_staff_lifecycle_race_choices,
        execute_match_race_create=match_lifecycle_application.execute_match_race_create,
        execute_match_race_edit_preserving_event=(match_lifecycle_application.execute_match_race_edit_preserving_event),
        execute_match_race_condition_set=(match_lifecycle_application.execute_match_race_condition_set),
        execute_match_race_entries_replacement=(match_lifecycle_application.execute_match_race_entries_replacement),
        execute_match_betting_batch_transition=(match_lifecycle_application.execute_match_betting_batch_transition),
        query_match_race=match_lifecycle_application.query_match_race,
        parse_operator_kst_datetime=_match_staff_parse_operator_kst_datetime,
        parse_room_condition_profile=_match_staff_parse_condition_profile,
        parse_room_condition_environment=_match_staff_parse_condition_environment,
        parse_final_entry_snapshot=_match_staff_parse_entries,
        send_user_error=_send_user_error,
        send_pre_modal_user_error=_send_pre_modal_user_error,
        send_internal_error=_send_internal_error,
        handle_modal_open_error=_handle_modal_open_error,
        send_initial_response=_send_initial_response_safely,
        send_race_result=_match_staff_send_race_result,
        send_race_batch_result=_match_staff_send_race_batch_result,
        correlation_id=_correlation_id,
        interaction_user_id=_interaction_user_id,
        logger=logger,
    )
)


_MATCH_STAFF_RESULT_ADAPTER = match_staff_result_adapter.MatchStaffResultAdapter(
    match_staff_result_adapter.MatchStaffResultAdapterPorts(
        prepare_command=_match_staff_prepare_command,
        prepare_bound_component=_match_staff_prepare_component,
        build_context=_match_staff_build_context,
        query_race_choices=_match_staff_result_race_choices,
        submit_result=match_result_application.execute_match_result_submit,
        review_result=match_result_application.execute_match_result_review,
        correct_result=match_result_application.execute_match_result_correct,
        reject_result=match_result_application.execute_match_result_reject,
        confirm_result=match_result_application.execute_match_result_confirm,
        query_result=match_result_application.query_match_result_show,
        send_notification=_match_staff_send_result_notification,
        send_user_error=_send_user_error,
        send_pre_modal_user_error=_send_pre_modal_user_error,
        send_internal_error=_send_internal_error,
        handle_modal_open_error=_handle_modal_open_error,
        send_initial_response=_send_initial_response_safely,
        send_followup=_send_followup_safely,
        correlation_id=_correlation_id,
        interaction_user_id=_interaction_user_id,
        safe_text=_safe_discord_text,
        bounded_message=_bounded_discord_message,
        logger=logger,
    )
)


_MATCH_STAFF_SETTLEMENT_ADAPTER = match_staff_settlement_adapter.MatchStaffSettlementAdapter(
    match_staff_settlement_adapter.MatchStaffSettlementAdapterPorts(
        prepare_command=_match_staff_prepare_command,
        prepare_component=_match_staff_prepare_component,
        autocomplete_authorized=_autocomplete_interaction_authorized,
        build_context=_match_staff_build_context,
        query_race_choices=_match_staff_settlement_race_choices,
        query_result=_match_staff_settlement_result_query,
        confirm_settlement=match_settlement_application.execute_match_settlement_confirmation,
        rollback_settlement=match_settlement_application.execute_match_settlement_rollback,
        publish_result=match_settlement_application.execute_match_result_publication,
        send_user_error=_send_user_error,
        send_pre_modal_user_error=_send_pre_modal_user_error,
        send_internal_error=_send_internal_error,
        handle_modal_open_error=_handle_modal_open_error,
        send_initial_response=_send_initial_response_safely,
        send_followup=_send_followup_safely,
        send_notification=_match_staff_send_result_notification,
        correlation_id=_correlation_id,
        interaction_user_id=_interaction_user_id,
        safe_text=_safe_discord_text,
        bounded_message=_bounded_discord_message,
        logger=logger,
    )
)


_MATCH_STAFF_ADAPTER = MatchStaffAdapter(
    MatchStaffAdapterPorts(
        race_create=_MATCH_STAFF_LIFECYCLE_ADAPTER.race_create,
        race_edit=_MATCH_STAFF_LIFECYCLE_ADAPTER.race_edit,
        race_condition_set=_MATCH_STAFF_LIFECYCLE_ADAPTER.race_condition_set,
        entries_set=_MATCH_STAFF_LIFECYCLE_ADAPTER.entries_set,
        betting_open=_MATCH_STAFF_LIFECYCLE_ADAPTER.betting_open,
        betting_close=_MATCH_STAFF_LIFECYCLE_ADAPTER.betting_close,
        race_show=_MATCH_STAFF_LIFECYCLE_ADAPTER.race_show,
        result_submit=_MATCH_STAFF_RESULT_ADAPTER.result_submit,
        result_review=_MATCH_STAFF_RESULT_ADAPTER.result_review,
        result_correct=_MATCH_STAFF_RESULT_ADAPTER.result_correct,
        result_reject=_MATCH_STAFF_RESULT_ADAPTER.result_reject,
        result_confirm=_MATCH_STAFF_RESULT_ADAPTER.result_confirm,
        settlement=_MATCH_STAFF_SETTLEMENT_ADAPTER.open_settlement,
        settlement_rollback=_MATCH_STAFF_SETTLEMENT_ADAPTER.show_rollback_preview,
        publish=_MATCH_STAFF_SETTLEMENT_ADAPTER.show_publish_preview,
        result_show=_MATCH_STAFF_RESULT_ADAPTER.result_show,
        autocomplete_race_show=_autocomplete_room_race_show,
        autocomplete_publish=_MATCH_STAFF_SETTLEMENT_ADAPTER.autocomplete_publish,
        autocomplete_result_show=_autocomplete_room_result_show,
    )
)


class MatchCommandGroup(match_member_adapter.MatchMemberCommandGroup):
    def __init__(self) -> None:
        super().__init__(
            adapter=_MATCH_MEMBER_ADAPTER,
            staff_group=_MATCH_STAFF_ADAPTER.create_command_group(),
        )


def _member_win5_round_label(round_: Win5RoundDTO) -> str:
    name = round_.race_name or round_.round_label
    if name is None and round_.special_races:
        name = " · ".join(race.race_name for race in round_.special_races)
    return _modal_option_label(f"{round_.round_number}R [{name or '이름 없음'}]")


class Win5SubmissionModal(discord.ui.Modal, title="WIN5 예측 제출"):
    def __init__(self, *, rounds: tuple[Win5RoundDTO, ...]) -> None:
        super().__init__(timeout=600)
        self._round_by_id = {round_.id: round_ for round_ in rounds}
        self.round = discord.ui.Select(
            custom_id="win5-submit-round",
            placeholder="현재 열린 일반 라운드 선택",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=_member_win5_round_label(round_), value=str(round_.id)) for round_ in rounds
            ],
            required=True,
        )
        self.tier = discord.ui.Select(
            custom_id="win5-submit-tier",
            placeholder="예측 범위 선택",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="TOP1", value="top1"),
                discord.SelectOption(label="TOP3", value="top3"),
                discord.SelectOption(label="TOP5", value="top5"),
            ],
            required=True,
        )
        self.picks = discord.ui.TextInput(
            custom_id="win5-submit-picks",
            placeholder="순서대로 번호 입력: 3-6-1-9-4",
            required=True,
            max_length=100,
        )
        self.add_item(discord.ui.Label(text="대상 라운드", component=self.round))
        self.add_item(discord.ui.Label(text="예측 범위", component=self.tier))
        self.add_item(discord.ui.Label(text="순위 예측", component=self.picks))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        command_name = "win5.submit"
        if await _prepare_command(interaction, command_name) is None:
            return
        try:
            round_id = _selected_modal_id(
                self.round,
                allowed_ids=frozenset(self._round_by_id),
                field_name="대상 라운드",
            )
            if len(self.tier.values) != 1 or self.tier.values[0] not in {"top1", "top3", "top5"}:
                raise ValueError("예측 범위를 하나 선택해야 합니다.")
            round_ = self._round_by_id[round_id]
            entry = await asyncio.to_thread(
                _submit_win5_prediction,
                str(interaction.user.id),
                round_.season_id,
                round_.id,
                parse_match_numbers(str(self.picks.value)),
                str(interaction.id),
                self.tier.values[0],
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(interaction, command_name, "WIN5를 제출하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_followup_safely(
            interaction,
            command_name,
            f"WIN5 제출 #{entry.id}을 저장했습니다. {_member_win5_round_label(round_)}",
        )


class Win5SpecialSubmissionModal(discord.ui.Modal, title="특별 WIN5 예측 제출"):
    def __init__(self, *, rounds: tuple[Win5RoundDTO, ...]) -> None:
        super().__init__(timeout=600)
        self._round_by_id = {round_.id: round_ for round_ in rounds}
        self.round = discord.ui.Select(
            custom_id="win5-special-submit-round",
            placeholder="현재 열린 특별 라운드 선택",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=_member_win5_round_label(round_),
                    value=str(round_.id),
                    description=_modal_option_label(
                        " / ".join(f"{race.display_order}. {race.race_name}" for race in round_.special_races)
                    ),
                )
                for round_ in rounds
            ],
            required=True,
        )
        self.entry_numbers = discord.ui.TextInput(
            custom_id="win5-special-submit-entry-numbers",
            placeholder="1경기: 3 / 여러 경기: 3-7-2",
            required=True,
            max_length=100,
        )
        self.add_item(discord.ui.Label(text="대상 특별 라운드", component=self.round))
        self.add_item(
            discord.ui.Label(
                text="경기별 예상 1착",
                description="경기 순서대로 번호 하나씩 입력합니다. 1경기면 숫자 하나만 입력하세요.",
                component=self.entry_numbers,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        command_name = "win5.special-submit"
        if await _prepare_command(interaction, command_name) is None:
            return
        try:
            round_id = _selected_modal_id(
                self.round,
                allowed_ids=frozenset(self._round_by_id),
                field_name="대상 특별 라운드",
            )
            round_ = self._round_by_id[round_id]
            predicted_numbers = parse_match_numbers(str(self.entry_numbers.value))
            if len(predicted_numbers) != len(round_.special_races):
                raise ValueError("특별 라운드의 각 경기에 대해 예상 1착 번호를 하나씩 입력해야 합니다.")
            predictions = tuple(
                (race.race_id, predicted_number)
                for race, predicted_number in zip(
                    round_.special_races,
                    predicted_numbers,
                    strict=True,
                )
            )
            entries = await asyncio.to_thread(
                _submit_win5_special_predictions,
                str(interaction.user.id),
                round_.season_id,
                round_.id,
                predictions,
                str(interaction.id),
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(interaction, command_name, "WIN5를 제출하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_followup_safely(
            interaction,
            command_name,
            f"{_member_win5_round_label(round_)} 특별 예측 {len(entries)}건을 저장했습니다.",
        )


def _format_win5_submission_round(summary: Win5SubmissionRoundDTO) -> str:
    round_ = summary.round
    lines = [
        _member_win5_round_label(round_),
        f"상태: `{round_.status}`",
    ]
    if not summary.submissions:
        lines.append("내 제출: 없음")
        return "\n".join(lines)

    judgement_by_entry = {judgement.win5_entry_id: judgement for judgement in summary.judgements}
    special_race_name_by_id = {race.race_id: race.race_name for race in round_.special_races}
    for index, entry in enumerate(summary.submissions, start=1):
        picks = "-".join(str(number) for number in entry.picks)
        tier = "특별 1착" if entry.prediction_tier == "special_winner" else entry.prediction_tier.upper()
        race_suffix = (
            f" · {_safe_discord_text(special_race_name_by_id.get(entry.special_race_id, '알 수 없는 경기'))}"
            if entry.special_race_id is not None
            else ""
        )
        lines.append(f"제출 {index}: {tier}{race_suffix} / `{picks}` / `{entry.status}`")
        judgement = judgement_by_entry.get(entry.id)
        if judgement is None:
            continue
        exact = tuple(
            pick
            for position, pick in enumerate(judgement.picks)
            if position < len(judgement.result_order) and judgement.result_order[position] == pick
        )
        board_wrong = tuple(
            pick
            for position, pick in enumerate(judgement.picks)
            if pick in judgement.result_order
            and (position >= len(judgement.result_order) or judgement.result_order[position] != pick)
        )
        lines.extend(
            (
                f"확정 결과: `{'-'.join(str(number) for number in judgement.result_order)}`",
                f"정확 순위 적중: {judgement.exact_position_count}개"
                + (f" (`{'-'.join(str(number) for number in exact)}`)" if exact else ""),
                f"게시판 내 적중: {judgement.on_board_wrong_position_count}개"
                + (f" (`{'-'.join(str(number) for number in board_wrong)}`)" if board_wrong else ""),
                f"획득 승점: {judgement.season_score_delta}점",
            )
        )
    return _bounded_discord_message(lines)


class Win5SubmissionRoundSelect(discord.ui.Select):
    def __init__(
        self,
        *,
        summaries: tuple[Win5SubmissionRoundDTO, ...],
        owner: Win5SubmissionsView,
    ) -> None:
        self._summary_by_round_id = {summary.round.id: summary for summary in summaries}
        self._owner = owner
        super().__init__(
            custom_id=f"win5-submissions-{owner.mode}-round",
            placeholder="조회할 라운드 선택",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=_member_win5_round_label(summary.round),
                    value=str(summary.round.id),
                    default=summary.round.id == owner.selected_round_id,
                )
                for summary in summaries
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if (
            await _prepare_bound_component(
                interaction,
                self._owner.context,
                command_name="win5.submissions",
                defer=False,
            )
            is None
        ):
            return
        try:
            round_id = int(self.values[0])
            summary = self._summary_by_round_id.get(round_id)
            if summary is None:
                raise ValueError("선택한 라운드를 더 이상 조회할 수 없습니다.")
            self._owner.select_round(round_id)
            await interaction.response.edit_message(
                content=_format_win5_submission_round(summary),
                view=self._owner,
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(interaction, "win5.submissions", "제출 내역을 조회하지 못했습니다", exc)
        except Exception:
            await _send_internal_error(interaction, "win5.submissions")


class Win5SubmissionsView(discord.ui.View):
    def __init__(
        self,
        *,
        open_rounds: tuple[Win5SubmissionRoundDTO, ...],
        scored_rounds: tuple[Win5SubmissionRoundDTO, ...],
        context: SettingsInteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        self._summaries = {
            "open": open_rounds,
            "scored": scored_rounds,
        }
        self.context = context
        self.mode: Literal["open", "scored"] = "open"
        self.selected_round_id: int | None = None
        self._round_select: Win5SubmissionRoundSelect | None = None
        self._activate("open")

    def _activate(self, mode: Literal["open", "scored"]) -> None:
        self.mode = mode
        summaries = self._summaries[mode]
        self.selected_round_id = summaries[0].round.id if summaries else None
        self.open_button.style = discord.ButtonStyle.primary if mode == "open" else discord.ButtonStyle.secondary
        self.scored_button.style = discord.ButtonStyle.primary if mode == "scored" else discord.ButtonStyle.secondary
        if self._round_select is not None:
            self.remove_item(self._round_select)
            self._round_select = None
        if summaries:
            self._round_select = Win5SubmissionRoundSelect(summaries=summaries, owner=self)
            self.add_item(self._round_select)

    def select_round(self, round_id: int) -> None:
        summaries = self._summaries[self.mode]
        if round_id not in {summary.round.id for summary in summaries}:
            raise ValueError("선택한 라운드를 조회할 수 없습니다.")
        self.selected_round_id = round_id
        if self._round_select is not None:
            for option in self._round_select.options:
                option.default = option.value == str(round_id)

    def content(self) -> str:
        summaries = self._summaries[self.mode]
        if not summaries:
            return (
                "현재 열린 라운드가 없습니다."
                if self.mode == "open"
                else "현재 활성 시즌에 채점 완료된 내 제출이 없습니다."
            )
        selected = next(summary for summary in summaries if summary.round.id == self.selected_round_id)
        return _format_win5_submission_round(selected)

    async def _switch(
        self,
        interaction: discord.Interaction,
        mode: Literal["open", "scored"],
    ) -> None:
        if (
            await _prepare_bound_component(
                interaction,
                self.context,
                command_name="win5.submissions",
                defer=False,
            )
            is None
        ):
            return
        try:
            self._activate(mode)
            await interaction.response.edit_message(content=self.content(), view=self)
        except Exception:
            await _send_internal_error(interaction, "win5.submissions")

    @discord.ui.button(label="진행 중", style=discord.ButtonStyle.primary)
    async def open_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self._switch(interaction, "open")

    @discord.ui.button(label="채점 완료", style=discord.ButtonStyle.secondary)
    async def scored_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self._switch(interaction, "scored")


class Win5CommandGroup(app_commands.Group):
    def __init__(self) -> None:
        super().__init__(name="win5", description="WIN5 예측과 순위를 관리합니다.")
        self.add_command(Win5StaffCommandGroup())

    @app_commands.command(name="info", description="현재 활성 WIN5 시즌 정보를 조회합니다.")
    async def info(self, interaction: discord.Interaction) -> None:
        if await _prepare_command(interaction, "win5.info") is None:
            return
        try:
            info = await asyncio.to_thread(_get_active_win5_season_info, str(interaction.user.id))
        except DomainError as exc:
            await _send_user_error(interaction, "win5.info", "시즌 정보를 조회하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, "win5.info")
            return
        await _send_followup_safely(
            interaction,
            "win5.info",
            _format_win5_season_info(info),
        )

    @app_commands.command(name="rounds", description="현재 열린 WIN5 라운드를 조회합니다.")
    async def rounds(self, interaction: discord.Interaction) -> None:
        if await _prepare_command(interaction, "win5.rounds") is None:
            return
        try:
            rounds = await asyncio.to_thread(_list_open_win5_rounds)
        except Exception:
            await _send_internal_error(interaction, "win5.rounds")
            return
        await _send_followup_safely(
            interaction,
            "win5.rounds",
            _format_win5_rounds(rounds),
        )

    @app_commands.command(name="submit", description="일반 WIN5 라운드에 순위 예측을 제출합니다.")
    async def submit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        command_name = "win5.submit"
        if await _prepare_command(interaction, command_name, defer=False) is None:
            return
        try:
            rounds = tuple(
                round_
                for round_ in await asyncio.to_thread(_list_open_win5_rounds)
                if round_.round_type == Win5RoundType.NORMAL.value
            )
            if not rounds:
                raise ValueError("현재 제출 가능한 일반 WIN5 라운드가 없습니다.")
            await interaction.response.send_modal(Win5SubmissionModal(rounds=rounds))
        except (DomainError, ValueError) as exc:
            await _send_pre_modal_user_error(interaction, command_name, "제출 화면을 열지 못했습니다", exc)
        except Exception:
            await _handle_modal_open_error(interaction, command_name)

    @app_commands.command(name="special-submit", description="특별 WIN5 경기의 1착 예측을 제출합니다.")
    async def special_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        command_name = "win5.special-submit"
        if await _prepare_command(interaction, command_name, defer=False) is None:
            return
        try:
            special_rounds = tuple(
                round_
                for round_ in await asyncio.to_thread(_list_open_win5_rounds)
                if round_.round_type == Win5RoundType.SPECIAL.value and round_.special_races
            )
            if not special_rounds:
                raise ValueError("현재 제출 가능한 특별 WIN5 경기가 없습니다.")
            await interaction.response.send_modal(Win5SpecialSubmissionModal(rounds=special_rounds))
        except (DomainError, ValueError) as exc:
            await _send_pre_modal_user_error(interaction, command_name, "제출 화면을 열지 못했습니다", exc)
        except Exception:
            await _handle_modal_open_error(interaction, command_name)

    @app_commands.command(name="submissions", description="현재 시즌의 WIN5 제출과 채점 결과를 조회합니다.")
    async def submissions(self, interaction: discord.Interaction) -> None:
        command_name = "win5.submissions"
        if await _prepare_command(interaction, command_name) is None:
            return
        try:
            open_rounds, scored_rounds = await asyncio.to_thread(
                _load_win5_submission_dashboard,
                str(interaction.user.id),
            )
            view = Win5SubmissionsView(
                open_rounds=open_rounds,
                scored_rounds=scored_rounds,
                context=_settings_interaction_context(interaction),
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(interaction, command_name, "제출 이력을 조회하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, command_name)
            return
        await _send_followup_safely(
            interaction,
            command_name,
            view.content(),
            view=view,
        )

    @app_commands.command(name="cancel", description="열린 라운드의 내 WIN5 제출을 취소합니다.")
    @app_commands.autocomplete(submission_id=_autocomplete_member_cancellable_submission)
    async def cancel(
        self,
        interaction: discord.Interaction,
        submission_id: int,
        reason: str | None = None,
    ) -> None:
        if await _prepare_command(interaction, "win5.cancel") is None:
            return
        try:
            entry = await asyncio.to_thread(
                _cancel_win5_submission,
                str(interaction.user.id),
                submission_id,
                reason,
                str(interaction.id),
            )
        except (DomainError, ValueError) as exc:
            await _send_user_error(interaction, "win5.cancel", "WIN5 제출을 취소하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, "win5.cancel")
            return
        logger.info(
            "discord command succeeded correlation_id=%s command=%s submission_id=%s",
            _correlation_id(interaction),
            "win5.cancel",
            entry.id,
        )
        await _send_followup_safely(
            interaction,
            "win5.cancel",
            f"WIN5 제출 #{entry.id}을 취소했습니다.",
        )

    @app_commands.command(name="standings", description="WIN5 시즌 순위를 조회합니다.")
    @app_commands.autocomplete(season_id=_autocomplete_member_standings_season)
    async def standings(
        self,
        interaction: discord.Interaction,
        season_id: int,
        ranking: Literal["season", "top1"] = "season",
    ) -> None:
        if await _prepare_command(interaction, "win5.standings") is None:
            return
        try:
            standings = await asyncio.to_thread(_list_win5_standings, season_id, ranking)
        except (DomainError, ValueError) as exc:
            await _send_user_error(interaction, "win5.standings", "순위를 조회하지 못했습니다", exc)
            return
        except Exception:
            await _send_internal_error(interaction, "win5.standings")
            return
        await _send_followup_safely(
            interaction,
            "win5.standings",
            _format_win5_standings(standings, ranking=ranking),
        )


def _create_match_race(
    name: str,
    starts_at: str,
    description: str | None,
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> RaceOperationResultDTO:
    return match_lifecycle_application.execute_match_race_create(
        match_lifecycle_application.MatchRaceCreateCommand(
            name=name,
            starts_at=parse_operator_datetime(starts_at),
            description=description,
            reason=reason,
            actor_discord_user_id=actor_discord_user_id,
            interaction_id=interaction_id,
        )
    )


def _edit_match_race_preserving_event(
    race_id: int,
    name: str,
    starts_at: str,
    description: str | None,
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> RaceOperationResultDTO:
    return match_lifecycle_application.execute_match_race_edit_preserving_event(
        match_lifecycle_application.MatchRaceEditCommand(
            race_id=race_id,
            name=name,
            starts_at=parse_operator_datetime(starts_at),
            description=description,
            reason=reason,
            actor_discord_user_id=actor_discord_user_id,
            interaction_id=interaction_id,
        )
    )


def _set_match_condition(
    race_id: int,
    grade: str,
    venue: str,
    track_surface: str,
    distance: int,
    direction: str,
    season: str,
    weather: str,
    track_condition: str,
    condition_label: str,
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> RaceOperationResultDTO:
    return match_lifecycle_application.execute_match_race_condition_set(
        match_lifecycle_application.MatchRaceConditionCommand(
            race_id=race_id,
            grade=grade,
            venue=venue,
            track_surface=track_surface,
            distance=distance,
            direction=direction,
            season=season,
            weather=weather,
            track_condition=track_condition,
            condition_label=condition_label,
            reason=reason,
            actor_discord_user_id=actor_discord_user_id,
            interaction_id=interaction_id,
        )
    )


def _set_match_entries(
    race_id: int,
    entries: str,
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> RaceOperationResultDTO:
    return match_lifecycle_application.execute_match_race_entries_replacement(
        match_lifecycle_application.MatchRaceEntriesCommand(
            race_id=race_id,
            entries=parse_final_entry_snapshot(entries),
            reason=reason,
            actor_discord_user_id=actor_discord_user_id,
            interaction_id=interaction_id,
        )
    )


def _open_match_betting(
    race_id: int,
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> RaceOperationResultDTO:
    return match_lifecycle_application.execute_match_betting_open(
        match_lifecycle_application.MatchRaceBettingCommand(
            race_id=race_id,
            reason=reason,
            actor_discord_user_id=actor_discord_user_id,
            interaction_id=interaction_id,
        )
    )


def _close_match_betting(
    race_id: int,
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> RaceOperationResultDTO:
    return match_lifecycle_application.execute_match_betting_close(
        match_lifecycle_application.MatchRaceBettingCommand(
            race_id=race_id,
            reason=reason,
            actor_discord_user_id=actor_discord_user_id,
            interaction_id=interaction_id,
        )
    )


def _transition_match_betting_batch(
    race_ids: tuple[int, ...],
    opening: bool,
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> tuple[RaceOperationResultDTO, ...]:
    return match_lifecycle_application.execute_match_betting_batch_transition(
        match_lifecycle_application.MatchRaceBettingBatchCommand(
            race_ids=race_ids,
            opening=opening,
            reason=reason,
            actor_discord_user_id=actor_discord_user_id,
            interaction_id=interaction_id,
        )
    )


def _show_match_race(race_id: int) -> RaceOperationResultDTO:
    return match_lifecycle_application.query_match_race(race_id=race_id)


def _submit_match_result(
    race_id: int,
    match_type: str,
    results: tuple[MatchResultInput, ...],
    reason: str | None,
    guild_id: str,
    actor_discord_user_id: str,
    interaction_id: str,
) -> MatchResultNotificationDTO:
    return match_result_application.execute_match_result_submit(
        race_id=race_id,
        match_type=match_type,
        results=results,
        reason=reason,
        guild_id=guild_id,
        actor_discord_user_id=actor_discord_user_id,
        interaction_id=interaction_id,
    )


def _review_match_result(
    race_id: int,
    revision_number: int,
    reason: str | None,
    guild_id: str,
    actor_discord_user_id: str,
    interaction_id: str,
) -> MatchResultNotificationDTO:
    return match_result_application.execute_match_result_review(
        race_id=race_id,
        revision_number=revision_number,
        reason=reason,
        guild_id=guild_id,
        actor_discord_user_id=actor_discord_user_id,
        interaction_id=interaction_id,
    )


def _correct_match_result(
    race_id: int,
    revision_number: int,
    match_type: str,
    results: tuple[MatchResultInput, ...],
    reason: str | None,
    guild_id: str,
    actor_discord_user_id: str,
    interaction_id: str,
) -> MatchResultNotificationDTO:
    return match_result_application.execute_match_result_correct(
        race_id=race_id,
        revision_number=revision_number,
        match_type=match_type,
        results=results,
        reason=reason,
        guild_id=guild_id,
        actor_discord_user_id=actor_discord_user_id,
        interaction_id=interaction_id,
    )


def _reject_match_result(
    race_id: int,
    revision_number: int,
    reason: str,
    guild_id: str,
    actor_discord_user_id: str,
    interaction_id: str,
) -> MatchResultNotificationDTO:
    return match_result_application.execute_match_result_reject(
        race_id=race_id,
        revision_number=revision_number,
        reason=reason,
        guild_id=guild_id,
        actor_discord_user_id=actor_discord_user_id,
        interaction_id=interaction_id,
    )


def _confirm_match_result(
    race_id: int,
    revision_number: int,
    reason: str | None,
    guild_id: str,
    actor_discord_user_id: str,
    interaction_id: str,
) -> MatchResultNotificationDTO:
    return match_result_application.execute_match_result_confirm(
        race_id=race_id,
        revision_number=revision_number,
        reason=reason,
        guild_id=guild_id,
        actor_discord_user_id=actor_discord_user_id,
        interaction_id=interaction_id,
    )


def _confirm_match_settlement(
    race_id: int,
    odds: tuple[MatchOddsInput, ...],
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> MatchSettlementOperationDTO:
    return match_settlement_application.execute_match_settlement_confirmation(
        match_settlement_application.ConfirmMatchSettlementApplicationCommand(
            race_id=race_id,
            odds=odds,
            reason=reason,
            actor_discord_user_id=actor_discord_user_id,
            interaction_id=interaction_id,
        )
    )


def _rollback_match_settlement(
    race_id: int,
    reason: str,
    actor_discord_user_id: str,
    interaction_id: str,
) -> MatchSettlementRollbackDTO:
    return match_settlement_application.execute_match_settlement_rollback(
        match_settlement_application.RollbackMatchSettlementApplicationCommand(
            race_id=race_id,
            reason=reason,
            actor_discord_user_id=actor_discord_user_id,
            interaction_id=interaction_id,
        )
    )


def _publish_match_result(
    race_id: int,
    revision_number: int,
    guild_id: str,
    actor_discord_user_id: str,
    interaction_id: str,
    authorize_delivery_unknown_retry: bool,
) -> MatchResultNotificationDTO:
    return match_settlement_application.execute_match_result_publication(
        match_settlement_application.PublishMatchResultApplicationCommand(
            race_id=race_id,
            revision_number=revision_number,
            guild_id=guild_id,
            actor_discord_user_id=actor_discord_user_id,
            interaction_id=interaction_id,
            authorize_delivery_unknown_retry=authorize_delivery_unknown_retry,
        )
    )


def _get_match_result(
    race_id: int,
    revision_number: int | None,
) -> MatchResultOperationDTO:
    return match_result_application.query_match_result_show(
        race_id=race_id,
        revision_number=revision_number,
    )


def parse_operator_datetime(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("시작 시각은 ISO 8601 형식이어야 합니다.")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError("시작 시각은 ISO 8601 형식이어야 합니다.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("시작 시각에는 UTC offset이 필요합니다.")
    return parsed.astimezone(UTC)


def parse_operator_date_and_time(
    date_value: str,
    time_value: str,
    timezone_name: str,
) -> datetime:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date_value):
        raise ValueError("경기 날짜는 YYYY-MM-DD 형식이어야 합니다.")
    if not re.fullmatch(r"[0-9]{2}:[0-9]{2}", time_value):
        raise ValueError("경기 시간은 HH:MM 형식이어야 합니다.")
    if timezone_name not in {"KST", "UTC"}:
        raise ValueError("시간대는 KST 또는 UTC여야 합니다.")
    try:
        local_datetime = datetime.strptime(f"{date_value} {time_value}", "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError("유효한 경기 날짜와 시간을 입력해야 합니다.") from exc
    selected_timezone = timezone(timedelta(hours=9), name="KST") if timezone_name == "KST" else UTC
    return local_datetime.replace(tzinfo=selected_timezone).astimezone(UTC)


def parse_operator_kst_datetime(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("날짜와 시간은 YYYY-MM-DD HH:MM 형식이어야 합니다.")
    parts = value.strip().split()
    if len(parts) != 2:
        raise ValueError("날짜와 시간은 YYYY-MM-DD HH:MM 형식이어야 합니다.")
    return parse_operator_date_and_time(parts[0], parts[1], "KST")


def _optional_modal_kst_datetime_iso(value: object) -> str | None:
    normalized = _optional_modal_text(value)
    return parse_operator_kst_datetime(normalized).isoformat() if normalized is not None else None


def parse_win5_round_number(value: str) -> int:
    if not isinstance(value, str):
        raise ValueError("라운드 번호는 정수여야 합니다.")
    try:
        round_number = int(value.strip())
    except ValueError as exc:
        raise ValueError("라운드 번호는 정수여야 합니다.") from exc
    if round_number <= 0:
        raise ValueError("라운드 번호는 양수여야 합니다.")
    return round_number


def parse_room_condition_profile(value: str) -> tuple[str, str, str, int, str]:
    parts = _pipe_delimited_modal_values(value, expected=5, field_name="경기 조건")
    try:
        distance = int(parts[3])
    except ValueError as exc:
        raise ValueError("경기 거리는 정수로 입력해야 합니다.") from exc
    return parts[0], parts[1], parts[2], distance, parts[4]


def parse_room_condition_environment(value: str) -> tuple[str, str, str]:
    parts = _pipe_delimited_modal_values(value, expected=3, field_name="환경 조건")
    return parts[0], parts[1], parts[2]


def _pipe_delimited_modal_values(
    value: str,
    *,
    expected: int,
    field_name: str,
) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError(f"{field_name}은(는) | 구분 텍스트여야 합니다.")
    parts = tuple(part.strip() for part in value.split("|"))
    if len(parts) != expected or any(not part for part in parts):
        raise ValueError(f"{field_name}은(는) {expected}개 값을 | 문자로 구분해야 합니다.")
    return parts


def parse_final_entry_snapshot(value: str) -> tuple[RegisteredFinalEntrySnapshotInput, ...]:
    if not isinstance(value, str):
        raise ValueError("엔트리는 줄 단위 텍스트여야 합니다.")
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        raise ValueError("확정 엔트리를 한 개 이상 입력해야 합니다.")
    entries: list[RegisteredFinalEntrySnapshotInput] = []
    seen_pids: set[str] = set()
    for line in lines:
        parts = line.split("|")
        if len(parts) != 2:
            raise ValueError("엔트리는 'PID | 말 이름' 형식이어야 합니다.")
        uma_pid, character_name = (part.strip() for part in parts)
        if not re.fullmatch(r"[1-9][0-9]{0,31}", uma_pid):
            raise ValueError("PID는 0으로 시작하지 않는 1~32자리 숫자여야 합니다.")
        if not character_name:
            raise ValueError("말 이름을 입력해야 합니다.")
        if uma_pid in seen_pids:
            raise ValueError("엔트리에 중복 PID가 있습니다.")
        seen_pids.add(uma_pid)
        entries.append(RegisteredFinalEntrySnapshotInput(uma_pid=uma_pid, character_name=character_name))
    return tuple(entries)


def parse_match_result_order(value: str) -> tuple[MatchResultInput, ...]:
    if not isinstance(value, str):
        raise ValueError("결과 순서는 줄 단위 텍스트여야 합니다.")
    raw_lines = value.splitlines()
    if not raw_lines or any(not line.strip() for line in raw_lines):
        raise ValueError("1위부터 엔트리 번호를 빈 줄 없이 입력해 주세요.")
    results: list[MatchResultInput] = []
    for rank, line in enumerate(raw_lines, start=1):
        parts = tuple(part.strip() for part in line.split("|"))
        if len(parts) not in {1, 5}:
            raise ValueError("결과는 '번호 | 평가 | 인기 | 기록ms | 착차' 형식이어야 합니다.")
        if not re.fullmatch(r"[1-9][0-9]*", parts[0]):
            raise ValueError("엔트리 번호는 0으로 시작하지 않는 양의 정수여야 합니다.")
        if len(parts) == 1:
            details: tuple[str, str, str, str] = ("", "", "", "")
        else:
            details = parts[1:]
        evaluation, popularity, finish_time, margin = (None if part in {"", "-"} else part for part in details)
        if popularity is not None and not re.fullmatch(r"[1-9][0-9]*", popularity):
            raise ValueError("인기 순위는 양의 정수 또는 - 이어야 합니다.")
        if finish_time is not None and not re.fullmatch(r"[1-9][0-9]*", finish_time):
            raise ValueError("기록은 양의 정수 millisecond 또는 - 이어야 합니다.")
        results.append(
            MatchResultInput(
                entry_number=int(parts[0]),
                rank=rank,
                character_evaluation_rank=evaluation,
                popularity_rank=int(popularity) if popularity is not None else None,
                finish_time_ms=int(finish_time) if finish_time is not None else None,
                finish_margin_text=margin,
            )
        )
    entry_numbers = [item.entry_number for item in results]
    if len(entry_numbers) != len(set(entry_numbers)):
        raise ValueError("결과 순서에 중복 엔트리 번호가 있습니다.")
    return tuple(results)


def _parse_room_match_odds(
    *,
    win: object,
    quinella: object,
    trio: object,
) -> tuple[MatchOddsInput, ...]:
    values = (("win", win), ("quinella", quinella), ("trio", trio))
    odds: list[MatchOddsInput] = []
    for bet_type, value in values:
        text = _optional_modal_text(value)
        if text is None:
            continue
        try:
            rate = Decimal(text)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{bet_type} 배율은 0 이상의 Decimal 숫자여야 합니다.") from exc
        if not rate.is_finite() or rate < 0:
            raise ValueError(f"{bet_type} 배율은 0 이상의 유한 Decimal 숫자여야 합니다.")
        odds.append(MatchOddsInput(bet_type=bet_type, declared_payout_rate=rate))
    return tuple(odds)


def parse_win5_round_entries(value: str) -> tuple[Win5RoundEntryInput, ...]:
    if not isinstance(value, str):
        raise ValueError("WIN5 엔트리는 줄 단위 텍스트여야 합니다.")
    lines = [line.strip(" ") for line in value.splitlines() if line.strip(" ")]
    if not lines:
        raise ValueError("WIN5 엔트리를 한 개 이상 입력해야 합니다.")
    entries: list[Win5RoundEntryInput] = []
    for entry_number, line in enumerate(lines, start=1):
        if any(character.isspace() and character != " " for character in line):
            raise ValueError("WIN5 엔트리 이름의 공백은 일반 space 문자만 사용할 수 있습니다.")
        display_name = line.strip(" ")
        entries.append(
            Win5RoundEntryInput(
                entry_number=entry_number,
                display_name=display_name,
            )
        )
    return tuple(entries)


def parse_win5_special_round_races(value: str) -> tuple[Win5SpecialRaceInput, ...]:
    if not isinstance(value, str):
        raise ValueError("특별 라운드 경기는 줄 단위 텍스트여야 합니다.")
    lines = [line.strip(" ") for line in value.splitlines() if line.strip(" ")]
    if not lines:
        raise ValueError("특별 라운드는 경기를 한 개 이상 입력해야 합니다.")
    races: list[Win5SpecialRaceInput] = []
    seen_names: set[str] = set()
    for line in lines:
        if "|" in line:
            raise ValueError("특별 라운드는 한 줄에 경기명 하나만 입력해야 합니다.")
        if any(character.isspace() and character != " " for character in line):
            raise ValueError("특별 경기 이름의 공백은 일반 space 문자만 사용할 수 있습니다.")
        race_name = line
        if race_name in seen_names:
            raise ValueError("특별 라운드에 같은 경기명을 중복 입력할 수 없습니다.")
        seen_names.add(race_name)
        races.append(Win5SpecialRaceInput(race_name=race_name))
    return tuple(races)


async def _send_race_operation_result(
    interaction: discord.Interaction,
    command_name: str,
    result: RaceOperationResultDTO,
) -> None:
    logger.info(
        "discord command succeeded correlation_id=%s command=%s audit_id=%s race_id=%s",
        _correlation_id(interaction),
        command_name,
        result.audit_id,
        result.race.id,
    )
    await _send_followup_safely(
        interaction,
        command_name,
        (
            f"레이스 #{result.race.id} `{result.race.name}` / 상태: `{result.race.status}` / "
            f"엔트리: {result.race.entry_count}건"
        ),
    )


async def _send_room_lifecycle_batch_result(
    interaction: discord.Interaction,
    command_name: str,
    results: tuple[RaceOperationResultDTO, ...],
) -> None:
    logger.info(
        "discord batch command succeeded correlation_id=%s command=%s audit_ids=%s race_ids=%s",
        _correlation_id(interaction),
        command_name,
        ",".join(str(result.audit_id) for result in results),
        ",".join(str(result.race.id) for result in results),
    )
    lines = [
        (f"• #{result.race.id} {_safe_discord_text(result.race.name)[:48]} → `{result.race.status}`")
        for result in results
    ]
    await _send_followup_safely(
        interaction,
        command_name,
        f"{len(results)}개 룸매치 경기 상태를 변경했습니다.\n" + "\n".join(lines),
    )


async def _send_match_result_operation(
    interaction: discord.Interaction,
    command_name: str,
    result: MatchResultOperationDTO,
) -> None:
    submission_id = result.submission.id if result.submission is not None else None
    publication_id = result.publication.id if result.publication is not None else None
    logger.info(
        "discord command succeeded correlation_id=%s command=%s audit_id=%s race_id=%s "
        "submission_id=%s publication_id=%s",
        _correlation_id(interaction),
        command_name,
        result.audit_id,
        result.race_id,
        submission_id,
        publication_id,
    )
    lines = [
        f"레이스 #{result.race_id} / 상태 `{result.race_status}`",
    ]
    if result.submission is not None:
        lines.append(
            f"결과 제출 #{result.submission.id} / revision {result.submission.revision_number} / "
            f"상태 `{result.submission.status}`"
        )
    if result.publication is not None:
        lines.append(f"게시 intent #{result.publication.id} / 상태 `{result.publication.status}`")
    await _send_followup_safely(
        interaction,
        command_name,
        _bounded_discord_message(lines),
    )


async def _send_match_result_notification(
    interaction: discord.Interaction,
    command_name: str,
    result: MatchResultNotificationDTO,
) -> None:
    await _send_match_result_operation(
        interaction,
        command_name,
        result.operation,
    )
    if not result.publications:
        return
    guild = interaction.guild
    if guild is None:
        logger.error(
            "discord publication skipped correlation_id=%s command=%s reason=missing_guild",
            _correlation_id(interaction),
            command_name,
        )
        return
    try:
        settings = await asyncio.to_thread(
            _get_guild_settings,
            str(guild.id),
        )
        configured_channels = {
            "win5_announcement": settings.win5_announcement_channel_id,
            "room_match_announcement": settings.room_match_announcement_channel_id,
            "log_mirror": settings.log_channel_id,
        }
        config = get_settings()
        staff_role_ids = tuple(
            role_id
            for role_id in (
                int(settings.operator_role_id or 0),
                config.owner_role_id,
                int(settings.bot_manager_role_id or 0),
            )
            if role_id
        )
        for publication in result.publications:
            try:
                delivered = await deliver_discord_publication_once(
                    publication,
                    guild=guild,
                    configured_channel_id=configured_channels[publication.destination_kind],
                    staff_role_ids=staff_role_ids,
                )
                logger.info(
                    "discord publication completed correlation_id=%s command=%s "
                    "publication_id=%s destination=%s status=%s",
                    _correlation_id(interaction),
                    command_name,
                    delivered.id,
                    delivered.destination_kind,
                    delivered.status,
                )
                if command_name == "match.staff.publish" and publication.destination_kind == "room_match_announcement":
                    await asyncio.to_thread(
                        _record_match_publication_delivery,
                        result.operation,
                        delivered.status,
                        delivered.discord_message_id,
                        delivered.last_error_code,
                        str(interaction.user.id),
                        str(interaction.id),
                        delivered.attempt_count,
                    )
            except Exception:
                log_sanitized_exception(
                    logger,
                    "discord publication failed correlation_id=%s command=%s publication_id=%s destination=%s",
                    _correlation_id(interaction),
                    command_name,
                    publication.id,
                    publication.destination_kind,
                )
    except Exception:
        log_sanitized_exception(
            logger,
            "discord publication failed correlation_id=%s command=%s actor_id=%s",
            _correlation_id(interaction),
            command_name,
            _interaction_user_id(interaction),
        )


def _record_match_publication_delivery(
    operation: MatchResultOperationDTO,
    delivery_status: str,
    discord_message_id: str | None,
    error_code: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
    delivery_attempt_count: int,
) -> None:
    publication = operation.publication
    if publication is None:
        raise MatchResultError("결과 공개 전달 상태에 publication 정보가 없습니다.")
    match_settlement_application.record_match_publication_delivery(
        match_settlement_application.RecordMatchPublicationDeliveryCommand(
            race_id=operation.race_id,
            publication_id=publication.id,
            delivery_status=delivery_status,
            discord_message_id=discord_message_id,
            error_code=error_code,
            actor_discord_user_id=actor_discord_user_id,
            interaction_id=interaction_id,
            delivery_attempt_count=delivery_attempt_count,
        )
    )


async def _send_win5_mutation_result(
    interaction: discord.Interaction,
    command_name: str,
    result: Win5MutationResultDTO,
) -> None:
    if result.round is not None:
        subject = (
            f"라운드 #{result.round.id} / 시즌 #{result.round.season_id} / "
            f"{result.round.round_type} / 상태 `{result.round.status}`"
        )
    elif result.season is not None:
        subject = (
            f"시즌 {result.season.season_number} "
            f"{_safe_discord_text(result.season.name)} / 상태 `{result.season.status}`"
        )
    elif result.submission is not None:
        subject = f"제출 #{result.submission.id} / 상태 `{result.submission.status}`"
    else:
        subject = "WIN5 작업"
    logger.info(
        "discord command succeeded correlation_id=%s command=%s audit_id=%s",
        _correlation_id(interaction),
        command_name,
        result.audit_id,
    )
    await _send_followup_safely(
        interaction,
        command_name,
        subject,
    )


async def _send_win5_lifecycle_batch_result(
    interaction: discord.Interaction,
    command_name: str,
    results: tuple[Win5MutationResultDTO, ...],
) -> None:
    logger.info(
        "discord batch command succeeded correlation_id=%s command=%s audit_ids=%s round_ids=%s",
        _correlation_id(interaction),
        command_name,
        ",".join(str(result.audit_id) for result in results),
        ",".join(str(result.round.id) for result in results if result.round is not None),
    )
    lines = [
        (
            f"• 라운드 #{result.round.id} "
            f"{_safe_discord_text(result.round.round_label or result.round.race_name or '이름 없음')[:40]} "
            f"→ `{result.round.status}`"
        )
        for result in results
        if result.round is not None
    ]
    await _send_followup_safely(
        interaction,
        command_name,
        f"{len(lines)}개 WIN5 라운드 상태를 변경했습니다.\n" + "\n".join(lines),
    )


async def _send_win5_result_mutation(
    interaction: discord.Interaction,
    command_name: str,
    result: Win5ResultMutationDTO,
) -> None:
    order = ",".join(str(number) for number in result.result.result_order)
    logger.info(
        "discord command succeeded correlation_id=%s command=%s audit_id=%s round_id=%s race_id=%s",
        _correlation_id(interaction),
        command_name,
        result.audit_id,
        result.round.id,
        result.result.race_id,
    )
    await _send_followup_safely(
        interaction,
        command_name,
        f"라운드 #{result.round.id} / 경기 #{result.result.race_id} 결과 [{order}]",
    )


async def _send_win5_scoring_result(
    interaction: discord.Interaction,
    command_name: str,
    result: Win5ScoringResultDTO,
) -> None:
    logger.info(
        "discord command succeeded correlation_id=%s command=%s audit_id=%s round_id=%s",
        _correlation_id(interaction),
        command_name,
        result.audit_id,
        result.round.id,
    )
    await _send_followup_safely(
        interaction,
        command_name,
        (f"라운드 #{result.round.id} 채점 완료 / 판정 {len(result.judgements)}건 / 점수 {len(result.scores)}건"),
    )


async def _send_win5_scoring_batch_result(
    interaction: discord.Interaction,
    command_name: str,
    results: tuple[Win5ScoringResultDTO, ...],
) -> None:
    logger.info(
        "discord batch command succeeded correlation_id=%s command=%s audit_ids=%s round_ids=%s",
        _correlation_id(interaction),
        command_name,
        ",".join(str(result.audit_id) for result in results),
        ",".join(str(result.round.id) for result in results),
    )
    lines = [
        f"• 라운드 #{result.round.id}: 판정 {len(result.judgements)}건 / 점수 {len(result.scores)}건"
        for result in results
    ]
    await _send_followup_safely(
        interaction,
        command_name,
        f"{len(results)}개 WIN5 라운드 채점을 완료했습니다.\n" + "\n".join(lines),
    )


async def _open_staff_direct_player_link_modal(
    interaction: discord.Interaction,
    *,
    target: discord.Member,
) -> None:
    await _STAFF_PLAYER_LINK_ADAPTER.open_direct_modal(
        interaction,
        context=_player_link_interaction_context(interaction),
        target_discord_user_id=str(target.id),
        target_discord_nickname=_display_name(target),
    )


async def _handle_staff_direct_player_link_modal(
    interaction: discord.Interaction,
    *,
    target_discord_user_id: str,
    target_discord_nickname: str,
    expected_existing_request_id: int | None,
    values: dict[str, str | None],
) -> None:
    await _STAFF_PLAYER_LINK_ADAPTER.submit_direct(
        interaction,
        context=_player_link_interaction_context(interaction),
        target_discord_user_id=target_discord_user_id,
        target_discord_nickname=target_discord_nickname,
        expected_existing_request_id=expected_existing_request_id,
        source_message=None,
        values=values,
    )


async def _handle_staff_player_link_request_selected(
    interaction: discord.Interaction,
    *,
    context: PlayerLinkInteractionContext,
    request_id: int,
) -> None:
    await _STAFF_PLAYER_LINK_ADAPTER.select_request(
        interaction,
        context=context,
        request_id=request_id,
    )


async def _handle_staff_player_link_candidate_selected(
    interaction: discord.Interaction,
    *,
    context: PlayerLinkInteractionContext,
    request: PlayerLinkRequestDTO,
    candidates: tuple[PlayerLinkCandidateDTO, ...],
    candidate_id: int,
) -> None:
    await _STAFF_PLAYER_LINK_ADAPTER.select_candidate(
        interaction,
        context=context,
        request=request,
        candidates=candidates,
        candidate_id=candidate_id,
    )


async def _handle_staff_player_link_approve(
    interaction: discord.Interaction,
    *,
    context: PlayerLinkInteractionContext,
    request: PlayerLinkRequestDTO,
    candidate: PlayerLinkCandidateDTO,
) -> None:
    await _STAFF_PLAYER_LINK_ADAPTER.approve(
        interaction,
        context=context,
        request=request,
        candidate=candidate,
    )


async def _open_staff_player_link_search_modal(
    interaction: discord.Interaction,
    *,
    context: PlayerLinkInteractionContext,
    request: PlayerLinkRequestDTO,
) -> None:
    await _STAFF_PLAYER_LINK_ADAPTER.open_search_modal(
        interaction,
        context=context,
        request=request,
    )


async def _open_staff_player_link_note_modal(
    interaction: discord.Interaction,
    *,
    context: PlayerLinkInteractionContext,
    request_id: int,
    action: str,
) -> None:
    await _STAFF_PLAYER_LINK_ADAPTER.open_note_modal(
        interaction,
        context=context,
        request_id=request_id,
        action=action,
    )


async def _handle_staff_player_link_note_modal(
    interaction: discord.Interaction,
    *,
    context: PlayerLinkInteractionContext,
    request_id: int,
    source_message: object | None,
    values: dict[str, str | None],
) -> None:
    await _STAFF_PLAYER_LINK_ADAPTER.submit_note(
        interaction,
        context=context,
        request_id=request_id,
        source_message=source_message,
        values=values,
    )


def _player_link_interaction_context(interaction: discord.Interaction) -> PlayerLinkInteractionContext:
    return _extracted_player_link_interaction_context(interaction)


def _player_link_no_candidate_view(
    *,
    context: PlayerLinkInteractionContext,
    request: PlayerLinkRequestDTO,
) -> PlayerLinkNoCandidateView:
    return _STAFF_PLAYER_LINK_ADAPTER.no_candidate_view(
        context=context,
        request=request,
    )


async def _close_player_link_view(source_message: object | None) -> None:
    await _close_extracted_player_link_view(source_message, logger=logger)


async def _close_interaction_view(interaction: discord.Interaction, command_name: str) -> None:
    edit = getattr(getattr(interaction, "message", None), "edit", None)
    if not callable(edit):
        return
    try:
        await edit(view=None)
    except Exception:
        log_sanitized_exception(
            logger,
            "discord interaction view close failed correlation_id=%s command=%s actor_id=%s",
            _correlation_id(interaction),
            command_name,
            _interaction_user_id(interaction),
        )


def _format_player_link_resolution_history(
    request: PlayerLinkRequestDTO,
    *,
    resolution: str,
) -> str:
    return _format_extracted_player_link_resolution_history(
        request,
        resolution=resolution,
        bounded_message=_bounded_discord_message,
        safe_text=_safe_discord_text,
    )


async def _post_player_link_resolution_history(
    interaction: discord.Interaction,
    *,
    request: PlayerLinkRequestDTO,
    resolution: str,
) -> None:
    await _publish_extracted_player_link_resolution_history(
        interaction,
        request=request,
        resolution=resolution,
        correlation_id=_correlation_id,
        bounded_message=_bounded_discord_message,
        safe_text=_safe_discord_text,
        logger=logger,
    )


def _format_staff_player_link_evidence(request: PlayerLinkRequestDTO) -> str:
    return _STAFF_PLAYER_LINK_ADAPTER.format_evidence(request)


def _format_staff_player_link_candidate_detail(
    request: PlayerLinkRequestDTO,
    candidate: PlayerLinkCandidateDTO,
) -> str:
    return _STAFF_PLAYER_LINK_ADAPTER.format_candidate_detail(request, candidate)


def _submit_account_registration_request(
    *,
    guild_id: str,
    requester_discord_user_id: str,
    discord_nickname_snapshot: str,
    submitted_uma_pid: str,
    submitted_nickname: str | None,
    submitted_ingame_name: str | None,
    interaction_id: str,
) -> AccountRegistrationMutationDTO:
    return execute_account_registration_submit(
        SubmitAccountRegistrationRequestCommand(
            guild_id=guild_id,
            requester_discord_user_id=requester_discord_user_id,
            discord_nickname_snapshot=discord_nickname_snapshot,
            submitted_uma_pid=submitted_uma_pid,
            submitted_nickname=submitted_nickname,
            submitted_ingame_name=submitted_ingame_name,
            idempotency_key=f"account-registration-submit:{interaction_id}",
        )
    )


def _get_owned_account_registration_request(
    *,
    guild_id: str,
    requester_discord_user_id: str,
) -> AccountRegistrationRequestDTO | None:
    return query_owned_account_registration_request(
        guild_id=guild_id,
        requester_discord_user_id=requester_discord_user_id,
    )


def _cancel_account_registration_request(
    *,
    request_id: int,
    guild_id: str,
    requester_discord_user_id: str,
    reason: str | None,
    interaction_id: str,
) -> AccountRegistrationMutationDTO:
    return execute_account_registration_cancel(
        CancelAccountRegistrationRequestCommand(
            request_id=request_id,
            guild_id=guild_id,
            requester_discord_user_id=requester_discord_user_id,
            reason=reason,
            idempotency_key=f"account-registration-cancel:{interaction_id}",
        )
    )


def _approve_account_registration_request(
    *,
    request_id: int,
    reviewed_by_discord_user_id: str,
    review_note: str | None,
    target_persona_id: str | None,
    interaction_id: str,
) -> AccountRegistrationMutationDTO:
    return execute_account_registration_approve(
        ApproveAccountRegistrationRequestCommand(
            request_id=request_id,
            reviewed_by_discord_user_id=reviewed_by_discord_user_id,
            review_note=review_note,
            target_persona_id=target_persona_id,
            idempotency_key=f"account-registration-approve:{interaction_id}",
        )
    )


def _reject_account_registration_request(
    *,
    request_id: int,
    reviewed_by_discord_user_id: str,
    review_note: str,
    interaction_id: str,
) -> AccountRegistrationMutationDTO:
    return execute_account_registration_reject(
        RejectAccountRegistrationRequestCommand(
            request_id=request_id,
            reviewed_by_discord_user_id=reviewed_by_discord_user_id,
            review_note=review_note,
            idempotency_key=f"account-registration-reject:{interaction_id}",
        )
    )


def _submit_player_link_request(
    *,
    guild_id: str,
    requester_discord_user_id: str,
    discord_nickname_snapshot: str,
    submitted_ingame_name: str,
    submitted_uma_pid: str,
    submitted_nickname_chunk: str | None,
    submitted_participation_hint: str | None,
    requester_note: str | None,
    interaction_id: str,
    submitted_by_discord_user_id: str | None = None,
) -> PlayerLinkMutationDTO:
    return execute_player_link_submit(
        SubmitPlayerLinkRequestCommand(
            guild_id=guild_id,
            requester_discord_user_id=requester_discord_user_id,
            discord_nickname_snapshot=discord_nickname_snapshot,
            submitted_ingame_name=submitted_ingame_name,
            submitted_uma_pid=submitted_uma_pid,
            submitted_nickname_chunk=submitted_nickname_chunk,
            submitted_participation_hint=submitted_participation_hint,
            requester_note=requester_note,
            submitted_by_discord_user_id=submitted_by_discord_user_id,
            idempotency_key=(
                f"player-link-staff-submit:{interaction_id}"
                if submitted_by_discord_user_id is not None
                else f"player-link-submit:{interaction_id}"
            ),
        )
    )


def _get_owned_player_link_request(
    guild_id: str,
    requester_discord_user_id: str,
) -> PlayerLinkRequestDTO | None:
    return query_owned_player_link_request(
        guild_id=guild_id,
        requester_discord_user_id=requester_discord_user_id,
    )


def _get_active_player_link_request(
    guild_id: str,
    requester_discord_user_id: str,
) -> PlayerLinkRequestDTO | None:
    return query_active_player_link_request(
        guild_id=guild_id,
        requester_discord_user_id=requester_discord_user_id,
    )


def _cancel_player_link_request(
    *,
    request_id: int,
    guild_id: str,
    requester_discord_user_id: str,
    reason: str | None,
    interaction_id: str,
) -> PlayerLinkMutationDTO:
    return execute_player_link_cancel(
        CancelPlayerLinkRequestCommand(
            request_id=request_id,
            guild_id=guild_id,
            requester_discord_user_id=requester_discord_user_id,
            idempotency_key=f"player-link-cancel:{interaction_id}",
            reason=reason,
        )
    )


def _revise_player_link_request(
    *,
    request_id: int,
    guild_id: str,
    requester_discord_user_id: str,
    discord_nickname_snapshot: str,
    submitted_ingame_name: str,
    submitted_uma_pid: str,
    submitted_nickname_chunk: str | None,
    submitted_participation_hint: str | None,
    requester_note: str | None,
    interaction_id: str,
) -> PlayerLinkMutationDTO:
    return execute_player_link_revise(
        RevisePlayerLinkRequestCommand(
            request_id=request_id,
            guild_id=guild_id,
            requester_discord_user_id=requester_discord_user_id,
            discord_nickname_snapshot=discord_nickname_snapshot,
            submitted_ingame_name=submitted_ingame_name,
            submitted_uma_pid=submitted_uma_pid,
            submitted_nickname_chunk=submitted_nickname_chunk,
            submitted_participation_hint=submitted_participation_hint,
            requester_note=requester_note,
            idempotency_key=f"player-link-revise:{interaction_id}",
        )
    )


def _list_player_link_requests(guild_id: str) -> tuple[PlayerLinkRequestDTO, ...]:
    return query_staff_player_link_queue(guild_id=guild_id)


def _get_staff_player_link_request(guild_id: str, request_id: int) -> PlayerLinkRequestDTO:
    return query_staff_player_link_request(
        guild_id=guild_id,
        request_id=request_id,
    )


def _search_staff_player_link_candidates(request_id: int) -> tuple[PlayerLinkCandidateDTO, ...]:
    return query_player_link_candidates(request_id=request_id)


def _search_staff_player_link_candidates_for_terms(
    submitted_ingame_name: str,
    discord_nickname_snapshot: str,
    submitted_nickname_chunk: str | None,
) -> tuple[PlayerLinkCandidateDTO, ...]:
    return query_player_link_candidates_for_terms(
        submitted_ingame_name=submitted_ingame_name,
        discord_nickname_snapshot=discord_nickname_snapshot,
        submitted_nickname_chunk=submitted_nickname_chunk,
    )


def _approve_player_link_request(
    *,
    request_id: int,
    candidate: PlayerLinkCandidateDTO,
    actor_discord_user_id: str,
    interaction_id: str,
) -> PlayerLinkApprovalDTO | PlayerLinkMutationDTO:
    return execute_player_link_approve(
        ApprovePlayerLinkRequestCommand(
            request_id=request_id,
            selected_game_account_id=candidate.game_account_id,
            expected_candidate_fingerprint=candidate.candidate_fingerprint,
            reviewed_by_discord_user_id=actor_discord_user_id,
            idempotency_key=f"player-link-approve:{interaction_id}",
        )
    )


def _review_player_link_request(
    *,
    request_id: int,
    actor_discord_user_id: str,
    note: str,
    interaction_id: str,
) -> PlayerLinkMutationDTO:
    return execute_player_link_review(
        ReviewPlayerLinkRequestCommand(
            request_id=request_id,
            reviewed_by_discord_user_id=actor_discord_user_id,
            idempotency_key=f"player-link-review:{interaction_id}",
            review_note=note,
        )
    )


def _reject_player_link_request(
    *,
    request_id: int,
    actor_discord_user_id: str,
    note: str,
    interaction_id: str,
) -> PlayerLinkMutationDTO:
    return execute_player_link_reject(
        RejectPlayerLinkRequestCommand(
            request_id=request_id,
            reviewed_by_discord_user_id=actor_discord_user_id,
            idempotency_key=f"player-link-reject:{interaction_id}",
            review_note=note,
        )
    )


def _account_info(discord_user_id: str):
    return query_account_overview(discord_user_id=discord_user_id)


def _change_game_account_ingame_name(game_account_id: int, ingame_name: str):
    return execute_staff_ingame_name_update(
        game_account_id=game_account_id,
        ingame_name=ingame_name,
    )


def _refresh_interaction_discord_nickname(interaction: discord.Interaction) -> bool:
    return execute_discord_nickname_refresh(
        discord_user_id=str(interaction.user.id),
        discord_nickname=_display_name(interaction.user),
    )


def _get_guild_settings(guild_id: str) -> GuildDiscordSettingsDTO:
    return query_guild_discord_settings(guild_id=guild_id)


def _update_guild_settings(
    current: GuildDiscordSettingsDTO,
    proposed: GuildDiscordSettingsValues,
    reason: str,
    actor_discord_user_id: str,
    interaction_id: str,
):
    command = UpdateGuildDiscordSettingsCommand(
        guild_id=current.guild_id,
        expected_revision=current.revision_number,
        values=proposed,
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=f"settings-update:{interaction_id}",
        reason=reason,
    )
    return execute_guild_discord_settings_update(command)


def _export_circle_points(output_dir):
    result = None

    def operation(session: Session):
        nonlocal result
        result = export_circle_point_snapshot(session, output_dir=output_dir)
        return result

    try:
        return _with_session(operation)
    except Exception:
        if result is not None:
            remove_circle_point_export_artifact(result)
        raise


def _export_win5_season(output_dir, season_id: int):
    return execute_win5_season_export(
        season_id=season_id,
        output_dir=output_dir,
    )


def create_export_command_group() -> ExportCommandGroup:
    return _create_export_command_group(
        ExportAdapterPorts(
            prepare_command=_prepare_command,
            autocomplete_win5_season=_autocomplete_export_win5_season,
            export_circle_points=_export_circle_points,
            export_win5_season=_export_win5_season,
            send_user_error=_send_user_error,
            send_internal_error=_send_internal_error,
            send_followup=_send_followup_safely,
            correlation_id=_correlation_id,
            logger=logger,
        )
    )


def _grant_circle_points_to_account(
    game_account_id: int,
    amount: int,
    reason: str,
    granted_by_discord_user_id: str,
    interaction_id: str,
) -> tuple[CirclePointTransactionDTO, str]:
    result = execute_staff_room_point_grant(
        StaffAccountPointCommand(
            game_account_id=game_account_id,
            amount=amount,
            reason=reason,
            actor_discord_user_id=granted_by_discord_user_id,
            idempotency_key=f"operator-grant:{interaction_id}",
        )
    )
    return result.transaction, result.uma_pid


def _adjust_circle_points_to_account(
    game_account_id: int,
    amount: int,
    reason: str,
    adjusted_by_discord_user_id: str,
    interaction_id: str,
) -> tuple[CirclePointTransactionDTO, str]:
    result = execute_staff_room_point_adjustment(
        StaffAccountPointCommand(
            game_account_id=game_account_id,
            amount=amount,
            reason=reason,
            actor_discord_user_id=adjusted_by_discord_user_id,
            idempotency_key=f"operator-adjustment:{interaction_id}",
        )
    )
    return result.transaction, result.uma_pid


def _list_room_match_races() -> tuple[OpenMatchRace, ...]:
    """Compatibility shim for the extracted Match member query port."""

    return query_open_member_match_races()


def _place_match_bet(
    discord_user_id: str,
    race_id: int,
    bet_type: str,
    numbers: tuple[int, ...] | list[int],
    amount: int,
    interaction_id: str,
    game_account_id: int | None = None,
):
    """Compatibility shim for the extracted Match member command port."""

    return execute_member_match_bet(
        PlaceMemberMatchBetCommand(
            discord_user_id=discord_user_id,
            game_account_id=game_account_id,
            race_id=race_id,
            bet_type=bet_type,
            numbers=tuple(numbers),
            amount=amount,
            idempotency_key=f"room-match-bet:{interaction_id}",
        )
    )


def _create_win5_season(
    name: str,
    starts_at: str | None,
    ends_at: str | None,
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> Win5MutationResultDTO:
    command = CreateWin5SeasonCommand(
        name=name,
        starts_at=_optional_operator_datetime(starts_at),
        ends_at=_optional_operator_datetime(ends_at),
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=f"win5-season-create:{interaction_id}",
        reason=reason,
    )
    return _with_session(lambda session: create_win5_season(session, command=command))


def _activate_win5_season(
    season_id: int,
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> Win5MutationResultDTO:
    command = Win5SeasonTransitionCommand(
        season_id=season_id,
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=f"win5-season-activate:{interaction_id}",
        reason=reason,
    )
    return _with_session(lambda session: activate_win5_season(session, command=command))


def _close_win5_season(
    season_id: int,
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> Win5MutationResultDTO:
    command = Win5SeasonTransitionCommand(
        season_id=season_id,
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=f"win5-season-close:{interaction_id}",
        reason=reason,
    )
    return _with_session(lambda session: close_win5_season(session, command=command))


def _cancel_win5_season(
    season_id: int,
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> Win5MutationResultDTO:
    command = Win5SeasonTransitionCommand(
        season_id=season_id,
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=f"win5-season-cancel:{interaction_id}",
        reason=reason,
    )
    return _with_session(lambda session: cancel_win5_season(session, command=command))


def _create_win5_round(
    season_id: int,
    round_number: int,
    race_name: str,
    starts_at: str,
    entries: str,
    round_label: str | None,
    event_id: int | None,
    opens_at: str | None,
    closes_at: str | None,
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> Win5MutationResultDTO:
    command = CreateWin5RoundCommand(
        season_id=season_id,
        round_number=round_number,
        race_name=race_name,
        starts_at=parse_operator_datetime(starts_at),
        entries=parse_win5_round_entries(entries),
        round_label=round_label,
        event_id=event_id,
        opens_at=_optional_operator_datetime(opens_at),
        closes_at=_optional_operator_datetime(closes_at),
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=f"win5-round-create:{interaction_id}",
        reason=reason,
    )
    return _with_session(lambda session: create_win5_round(session, command=command))


def _create_win5_special_round(
    season_id: int,
    round_number: int,
    races: str,
    round_label: str | None,
    opens_at: str | None,
    closes_at: str | None,
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> Win5MutationResultDTO:
    command = CreateWin5SpecialRoundCommand(
        season_id=season_id,
        round_number=round_number,
        races=parse_win5_special_round_races(races),
        round_label=round_label,
        opens_at=_optional_operator_datetime(opens_at),
        closes_at=_optional_operator_datetime(closes_at),
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=f"win5-special-round-create:{interaction_id}",
        reason=reason,
    )
    return _with_session(lambda session: create_win5_special_round(session, command=command))


def _open_win5_round(
    round_id: int,
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> Win5MutationResultDTO:
    command = Win5RoundTransitionCommand(
        round_id=round_id,
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=f"win5-round-open:{interaction_id}",
        reason=reason,
    )
    return _with_session(lambda session: open_win5_round(session, command=command))


def _close_win5_round(
    round_id: int,
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> Win5MutationResultDTO:
    command = Win5RoundTransitionCommand(
        round_id=round_id,
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=f"win5-round-close:{interaction_id}",
        reason=reason,
    )
    return _with_session(lambda session: close_win5_round(session, command=command))


def _transition_win5_round_batch(
    round_ids: tuple[int, ...],
    opening: bool,
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> tuple[Win5MutationResultDTO, ...]:
    ordered_ids = _normalize_lifecycle_batch_ids(round_ids)

    def operation(session: Session) -> tuple[Win5MutationResultDTO, ...]:
        results: list[Win5MutationResultDTO] = []
        for round_id in ordered_ids:
            command = Win5RoundTransitionCommand(
                round_id=round_id,
                actor_discord_user_id=actor_discord_user_id,
                idempotency_key=(f"win5-round-{'open' if opening else 'close'}:{interaction_id}:{round_id}"),
                reason=reason,
            )
            results.append(
                open_win5_round(session, command=command) if opening else close_win5_round(session, command=command)
            )
        return tuple(results)

    return run_application_transaction(operation)


def _normalize_lifecycle_batch_ids(values: tuple[int, ...]) -> tuple[int, ...]:
    if not values or len(values) > 25:
        raise ValueError("경기는 1개 이상 25개 이하로 선택해야 합니다.")
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in values):
        raise ValueError("선택한 경기 ID가 올바르지 않습니다.")
    if len(values) != len(set(values)):
        raise ValueError("같은 경기를 중복 선택할 수 없습니다.")
    return tuple(sorted(values))


def _enter_win5_result(
    round_id: int,
    result_order: str,
    race_id: int | None,
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> Win5ResultMutationDTO:
    command = EnterWin5ResultCommand(
        round_id=round_id,
        race_id=race_id,
        result_order=tuple(parse_match_numbers(result_order)),
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=f"win5-result-enter:{interaction_id}",
        reason=reason,
    )
    return _with_session(lambda session: enter_win5_result(session, command=command))


def _correct_win5_result(
    round_id: int,
    result_order: str,
    race_id: int | None,
    reason: str,
    actor_discord_user_id: str,
    interaction_id: str,
) -> Win5ResultMutationDTO:
    command = CorrectWin5ResultCommand(
        round_id=round_id,
        race_id=race_id,
        result_order=tuple(parse_match_numbers(result_order)),
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=f"win5-result-correct:{interaction_id}",
        reason=reason,
    )
    return _with_session(lambda session: correct_win5_result(session, command=command))


def _enter_and_score_win5_special_round(
    round_id: int,
    race_ids: tuple[int, ...],
    winner_numbers: str,
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> Win5ScoringResultDTO:
    normalized_winners = tuple(parse_match_numbers(winner_numbers)) if winner_numbers.strip() else ()
    if len(normalized_winners) != len(race_ids):
        raise ValueError(f"특별 라운드 경기 {len(race_ids)}개의 1착 번호를 경기 순서대로 입력해야 합니다.")

    def operation(session: Session) -> Win5ScoringResultDTO:
        current_race_ids = tuple(
            session.scalars(
                select(Win5RoundRace.race_id)
                .join(Race, Race.id == Win5RoundRace.race_id)
                .where(
                    Win5RoundRace.round_id == round_id,
                    Race.status != "voided",
                )
                .order_by(Win5RoundRace.display_order, Win5RoundRace.race_id)
            )
        )
        if current_race_ids != race_ids:
            raise ValueError("특별 라운드 경기 구성이 변경되었습니다. 입력 화면을 다시 열어 주세요.")
        for race_id, winner in zip(race_ids, normalized_winners, strict=True):
            enter_win5_result(
                session,
                command=EnterWin5ResultCommand(
                    round_id=round_id,
                    race_id=race_id,
                    result_order=(winner,),
                    actor_discord_user_id=actor_discord_user_id,
                    idempotency_key=(f"win5-special-result-enter:{interaction_id}:{race_id}"),
                    reason=reason,
                ),
            )
        return score_win5_round(
            session,
            command=ScoreWin5RoundCommand(
                round_id=round_id,
                actor_discord_user_id=actor_discord_user_id,
                idempotency_key=f"win5-special-score:{interaction_id}",
                reason=reason,
            ),
        )

    return run_application_transaction(operation)


def _score_win5_round(
    round_id: int,
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> Win5ScoringResultDTO:
    command = ScoreWin5RoundCommand(
        round_id=round_id,
        actor_discord_user_id=actor_discord_user_id,
        idempotency_key=f"win5-score:{interaction_id}",
        reason=reason,
    )
    return _with_session(lambda session: score_win5_round(session, command=command))


def _score_win5_round_batch(
    round_ids: tuple[int, ...],
    reason: str | None,
    actor_discord_user_id: str,
    interaction_id: str,
) -> tuple[Win5ScoringResultDTO, ...]:
    ordered_ids = _normalize_lifecycle_batch_ids(round_ids)

    def operation(session: Session) -> tuple[Win5ScoringResultDTO, ...]:
        return tuple(
            score_win5_round(
                session,
                command=ScoreWin5RoundCommand(
                    round_id=round_id,
                    actor_discord_user_id=actor_discord_user_id,
                    idempotency_key=f"win5-score:{interaction_id}:{round_id}",
                    reason=reason,
                ),
            )
            for round_id in ordered_ids
        )

    return run_application_transaction(operation)


def _optional_operator_datetime(value: str | None) -> datetime | None:
    return parse_operator_datetime(value) if value is not None else None


def _submit_win5_prediction(
    discord_user_id: str,
    season_id: int,
    round_id: int,
    picks: list[int],
    interaction_id: str,
    tier: str = "top5",
):
    def operation(session: Session):
        account = resolve_owned_game_account(session, discord_user_id=discord_user_id)
        return submit_win5_prediction(
            session,
            game_account_id=account.id,
            season_id=season_id,
            round_id=round_id,
            picks=picks,
            prediction_tier=tier,
            actor_discord_user_id=discord_user_id,
            idempotency_key=f"win5-submit:{interaction_id}",
        )

    return _with_session(operation)


def _submit_win5_special_prediction(
    discord_user_id: str,
    season_id: int,
    round_id: int,
    race_id: int,
    entry_number: int,
    interaction_id: str,
):
    def operation(session: Session):
        account = resolve_owned_game_account(session, discord_user_id=discord_user_id)
        return submit_win5_special_prediction(
            session,
            game_account_id=account.id,
            season_id=season_id,
            round_id=round_id,
            race_id=race_id,
            predicted_entry_number=entry_number,
            actor_discord_user_id=discord_user_id,
            idempotency_key=f"win5-special-submit:{interaction_id}",
        )

    return _with_session(operation)


def _submit_win5_special_predictions(
    discord_user_id: str,
    season_id: int,
    round_id: int,
    predictions: tuple[tuple[int, int], ...],
    interaction_id: str,
) -> tuple[Win5EntryDTO, ...]:
    def operation(session: Session) -> tuple[Win5EntryDTO, ...]:
        account = resolve_owned_game_account(session, discord_user_id=discord_user_id)
        return tuple(
            submit_win5_special_prediction(
                session,
                game_account_id=account.id,
                season_id=season_id,
                round_id=round_id,
                race_id=race_id,
                predicted_entry_number=entry_number,
                actor_discord_user_id=discord_user_id,
                idempotency_key=f"win5-special-submit:{interaction_id}:{race_id}",
            )
            for race_id, entry_number in predictions
        )

    return _with_session(operation)


def _list_open_win5_rounds() -> tuple[Win5RoundDTO, ...]:
    return run_application_query(list_open_win5_rounds)


def _get_active_win5_season_info(discord_user_id: str) -> Win5SeasonInfoDTO:
    return run_application_query(lambda session: get_active_win5_season_info(session, discord_user_id=discord_user_id))


def _list_win5_submissions(
    discord_user_id: str,
    round_id: int,
) -> tuple[Win5EntryDTO, ...]:
    def operation(session: Session):
        account = resolve_owned_game_account(session, discord_user_id=discord_user_id)
        return list_win5_submissions(
            session,
            game_account_id=account.id,
            round_id=round_id,
        )

    return run_application_query(operation)


def _load_win5_submission_dashboard(
    discord_user_id: str,
) -> tuple[tuple[Win5SubmissionRoundDTO, ...], tuple[Win5SubmissionRoundDTO, ...]]:
    def operation(
        session: Session,
    ) -> tuple[tuple[Win5SubmissionRoundDTO, ...], tuple[Win5SubmissionRoundDTO, ...]]:
        account = resolve_owned_game_account(session, discord_user_id=discord_user_id)
        return (
            list_active_win5_submission_rounds(
                session,
                game_account_id=account.id,
                scope="open",
            ),
            list_active_win5_submission_rounds(
                session,
                game_account_id=account.id,
                scope="scored",
            ),
        )

    return run_application_query(operation)


def _cancel_win5_submission(
    discord_user_id: str,
    submission_id: int,
    reason: str | None,
    interaction_id: str,
) -> Win5EntryDTO:
    def operation(session: Session):
        account = resolve_owned_game_account(session, discord_user_id=discord_user_id)
        return cancel_win5_submission(
            session,
            command=CancelWin5SubmissionCommand(
                submission_id=submission_id,
                game_account_id=account.id,
                actor_discord_user_id=discord_user_id,
                idempotency_key=f"win5-cancel:{interaction_id}",
                reason=reason,
            ),
        )

    return _with_session(operation)


def _list_win5_standings(
    season_id: int,
    ranking: str,
) -> tuple[Win5StandingDTO, ...]:
    return run_application_query(
        lambda session: list_win5_standings(
            session,
            season_id=season_id,
            ranking=ranking,
        )
    )


def _preparation_get_runtime_settings() -> Settings:
    return get_settings()


def _preparation_get_guild_settings(guild_id: str) -> GuildDiscordSettingsDTO:
    return _get_guild_settings(guild_id)


def _preparation_refresh_discord_nickname(
    discord_user_id: str,
    discord_nickname: str,
) -> bool:
    return execute_discord_nickname_refresh(
        discord_user_id=discord_user_id,
        discord_nickname=discord_nickname,
    )


def _preparation_refresh_discord_nickname_port(
    discord_user_id: str,
    discord_nickname: str,
) -> bool:
    return _preparation_refresh_discord_nickname(discord_user_id, discord_nickname)


def _preparation_display_name(user: object) -> str:
    return _display_name(user)


async def _preparation_send_initial_response(
    interaction: discord.Interaction,
    command_name: str,
    content: str,
) -> bool:
    return await _send_initial_response_safely(interaction, command_name, content)


def _preparation_correlation_id(interaction: object) -> str:
    return _correlation_id(interaction)


def _preparation_interaction_user_id(interaction: object) -> str:
    return _interaction_user_id(interaction)


_COMMAND_PREPARATION = CommandPreparation(
    CommandPreparationPorts(
        get_runtime_settings=_preparation_get_runtime_settings,
        get_guild_settings=_preparation_get_guild_settings,
        refresh_discord_nickname=_preparation_refresh_discord_nickname_port,
        display_name=_preparation_display_name,
        send_initial_response=_preparation_send_initial_response,
        correlation_id=_preparation_correlation_id,
        interaction_user_id=_preparation_interaction_user_id,
        logger=logger,
    )
)


async def _require_guild_id(
    interaction: discord.Interaction,
    command_name: str,
) -> str | None:
    return await _COMMAND_PREPARATION.require_guild_id(interaction, command_name)


async def _prepare_command(
    interaction: discord.Interaction,
    command_name: str,
    *,
    defer: bool = True,
) -> Settings | None:
    return await _COMMAND_PREPARATION.prepare_command(
        interaction,
        command_name,
        defer=defer,
    )


async def _prepare_settings_component(
    interaction: discord.Interaction,
    context: SettingsInteractionContext,
    *,
    command_name: str,
    defer: bool = True,
) -> Settings | None:
    return await _prepare_bound_component(
        interaction,
        context,
        command_name=command_name,
        defer=defer,
    )


async def _prepare_bound_component(
    interaction: discord.Interaction,
    context: SettingsInteractionContext,
    *,
    command_name: str,
    defer: bool = True,
) -> Settings | None:
    return await _COMMAND_PREPARATION.prepare_bound_component(
        interaction,
        context,
        command_name=command_name,
        defer=defer,
    )


def _settings_interaction_context(interaction: discord.Interaction) -> SettingsInteractionContext:
    return _COMMAND_PREPARATION.interaction_context(interaction)


async def _interaction_channel_settings(
    interaction: discord.Interaction,
    *,
    settings: Settings,
    channel_scope: CommandChannelScope,
) -> GuildDiscordSettingsDTO | None:
    return await _COMMAND_PREPARATION.interaction_channel_settings(
        interaction,
        settings=settings,
        channel_scope=channel_scope,
    )


async def _interaction_role_authorization(
    interaction: discord.Interaction,
    *,
    settings: Settings,
    access: CommandAccess,
) -> GuildRoleAuthorization:
    return await _COMMAND_PREPARATION.interaction_role_authorization(
        interaction,
        settings=settings,
        access=access,
    )


def _with_session(operation: Callable[[Session], object]):
    """Compatibility facade for legacy Discord mutations during R3 migration."""

    return run_application_command(operation)


def _display_name(user: discord.abc.User) -> str:
    return str(
        getattr(
            user,
            "display_name",
            getattr(user, "name", getattr(user, "id", "unknown")),
        )
    )


def parse_match_numbers(value: str) -> list[int]:
    """Compatibility shim for the extracted Match member parser."""

    return match_member_adapter.parse_match_numbers(value)


def _format_match_races(races: list[OpenMatchRace] | tuple[OpenMatchRace, ...]) -> str:
    """Compatibility shim for the extracted Match member formatter."""

    return _MATCH_MEMBER_ADAPTER.format_races(races)


def _format_win5_rounds(rounds: tuple[Win5RoundDTO, ...]) -> str:
    if not rounds:
        return "현재 열린 WIN5 라운드가 없습니다."
    lines = ["현재 열린 WIN5 라운드"]
    for round_ in rounds:
        if round_.round_type == "normal":
            race_names = _safe_discord_text(round_.race_name or "이름 없음")
        else:
            race_names = _safe_discord_text(round_.round_label or "이름 없음")
        lines.append(f"{round_.round_number}R [{race_names}]")
    return _bounded_discord_message(lines)


def _format_win5_season_info(info: Win5SeasonInfoDTO) -> str:
    season = info.season
    season_status = {
        "draft": "작성 중",
        "active": "활성",
        "closed": "종료",
    }.get(season.status, "알 수 없음")
    starts_at = season.starts_at.isoformat() if season.starts_at is not None else "-"
    ends_at = season.ends_at.isoformat() if season.ends_at is not None else "-"
    lines = [
        "현재 WIN5 시즌 정보",
        f"시즌: {season.season_number} · {_safe_discord_text(season.name)}",
        f"상태: {season_status}",
        f"시작: {starts_at}",
        f"종료: {ends_at}",
        f"라운드: 전체 {info.total_round_count}개 / 오픈 {info.open_round_count}개",
        f"내 시즌 승점: {info.season_score}점",
        f"내 TOP1 승점: {info.top1_score}점",
    ]
    if info.latest_open_round is None:
        lines.append("최근 오픈 라운드: -")
    else:
        round_ = info.latest_open_round
        label = _safe_discord_text(round_.round_label) if round_.round_label else "-"
        round_type = {
            "normal": "일반",
            "special": "특별 라운드",
        }.get(round_.round_type, "알 수 없음")
        lines.append(f"최근 오픈 라운드: #{round_.id} / {round_.round_number}회 / 라벨 {label} / 유형 {round_type}")
    return _bounded_discord_message(lines)


def _format_win5_submissions(entries: tuple[Win5EntryDTO, ...]) -> str:
    if not entries:
        return "해당 라운드에 제출한 WIN5 예측이 없습니다."
    lines = ["내 WIN5 제출 이력"]
    for entry in entries:
        special = f" / 경기 #{entry.special_race_id}" if entry.special_race_id else ""
        picks = ",".join(str(number) for number in entry.picks)
        lines.append(
            f"제출 #{entry.id} / 라운드 #{entry.round_id}{special} / {entry.prediction_tier} [{picks}] / {entry.status}"
        )
    return _bounded_discord_message(lines)


def _format_win5_standings(
    standings: tuple[Win5StandingDTO, ...],
    *,
    ranking: str,
) -> str:
    title = "WIN5 TOP1 순위" if ranking == "top1" else "WIN5 시즌 종합 순위"
    if not standings:
        return f"{title}\n아직 집계된 점수가 없습니다."
    lines = [title]
    for standing in standings:
        lines.append(f"{standing.rank}위 {_safe_discord_text(standing.display_name)} / {standing.score}점")
    return _bounded_discord_message(lines)


def _format_match_result(
    result: MatchResultOperationDTO,
    *,
    heading: str = "룸매치 결과",
) -> str:
    lines = [
        heading,
        f"레이스: #{result.race_id} {_safe_discord_text(result.race_name)}",
        f"레이스 상태: {result.race_status}",
    ]
    if result.submission is None:
        lines.append("결과 제출: 없음")
    else:
        submission = result.submission
        lines.extend(
            [
                f"결과 제출: #{submission.id}",
                f"revision: {submission.revision_number}",
                f"매치 유형: {submission.match_type}",
                f"제출 상태: {submission.status}",
            ]
        )
    if result.results:
        lines.append("도착 순서:")
        for item in sorted(result.results, key=lambda value: value.rank):
            character = f" / {_safe_discord_text(item.character_name)}" if item.character_name is not None else ""
            lines.append(f"{item.rank}위: 엔트리 #{item.entry_number}{character}")
            details: list[str] = []
            evaluation = getattr(item, "character_evaluation_rank", None)
            popularity = getattr(item, "popularity_rank", None)
            finish_time = getattr(item, "finish_time_ms", None)
            margin = getattr(item, "finish_margin_text", None)
            if evaluation is not None:
                details.append(f"평가 {_safe_discord_text(evaluation)}")
            if popularity is not None:
                details.append(f"인기 {popularity}위")
            if finish_time is not None:
                details.append(f"{finish_time}ms")
            if margin is not None:
                details.append(f"착차 {_safe_discord_text(margin)}")
            if details:
                lines.append("  " + " · ".join(details))
    else:
        lines.append("도착 순서: 없음")
    if result.publication is not None:
        lines.append(f"게시 intent: #{result.publication.id} / 상태 {result.publication.status}")
    return _bounded_discord_message(lines)


def _format_match_settlement_preview(
    *,
    race_id: int,
    odds: tuple[MatchOddsInput, ...],
) -> str:
    lines = [
        "룸매치 정산 확정 미리보기",
        f"레이스: #{race_id}",
        "아래 배율은 확정 시 immutable odds snapshot으로 저장됩니다.",
    ]
    if odds:
        lines.append("입력 배율:")
        lines.extend(f"- {item.bet_type}: {item.declared_payout_rate}" for item in odds)
    else:
        lines.append("입력 배율: 없음 (활성 베팅 승식이 없어야 합니다)")
    lines.append("확정하면 판정·Rating·서클 포인트 정산이 한 트랜잭션으로 처리됩니다.")
    return _bounded_discord_message(lines)


async def _send_match_settlement_result(
    interaction: discord.Interaction,
    command_name: str,
    result: MatchSettlementOperationDTO,
) -> None:
    logger.info(
        "discord command succeeded correlation_id=%s command=%s audit_id=%s race_id=%s transaction_count=%s",
        _correlation_id(interaction),
        command_name,
        result.audit_id,
        result.race_id,
        len(result.transactions),
    )
    lines = [
        f"레이스 #{result.race_id} 정산 완료 / 상태 `{result.race_status}`",
        f"odds snapshot #{result.odds_snapshot.id} / 참가 인원 {result.odds_snapshot.settlement_participant_count}명 / "
        f"배율 {result.odds_snapshot.payout_multiplier}",
        f"Rating: {result.rating.grade} / 이벤트 {result.rating.event_count}건",
        f"서클 포인트 정산 거래: {len(result.transactions)}건",
    ]
    await _send_followup_safely(interaction, command_name, _bounded_discord_message(lines))


async def _send_match_settlement_rollback_result(
    interaction: discord.Interaction,
    command_name: str,
    result: MatchSettlementRollbackDTO,
) -> None:
    logger.info(
        "discord command succeeded correlation_id=%s command=%s audit_id=%s race_id=%s transaction_count=%s",
        _correlation_id(interaction),
        command_name,
        result.audit_id,
        result.race_id,
        len(result.transactions),
    )
    await _send_followup_safely(
        interaction,
        command_name,
        _bounded_discord_message(
            [
                f"레이스 #{result.race_id} 정산 롤백 완료 / 상태 `{result.race_status}`",
                f"서클 포인트 보상 거래: {len(result.transactions)}건",
                "Rating 보상: "
                f"기존 {len(result.reversed_rating_event_ids)}건 / "
                f"보상 {len(result.compensating_rating_event_ids)}건",
                "이미 공개한 결과가 있다면 정정 공지를 확인하세요.",
            ]
        ),
    )


def _settings_values(settings: GuildDiscordSettingsDTO) -> GuildDiscordSettingsValues:
    return _SETTINGS_ADAPTER.settings_values(settings)


def _format_settings_panel(settings: GuildDiscordSettingsDTO) -> str:
    return _SETTINGS_ADAPTER.format_settings_panel(settings)


def _format_role_settings_panel(settings: GuildDiscordSettingsDTO, *, owner_role_id: int) -> str:
    return _SETTINGS_ADAPTER.format_role_settings_panel(settings, owner_role_id=owner_role_id)


def _format_settings_preview(
    current: GuildDiscordSettingsDTO,
    proposed: GuildDiscordSettingsValues,
) -> str:
    return _SETTINGS_ADAPTER.format_settings_preview(current, proposed)


async def _send_settings_preview(
    interaction: discord.Interaction,
    *,
    settings: GuildDiscordSettingsDTO,
    proposed: GuildDiscordSettingsValues,
    reason: str,
    context: SettingsInteractionContext,
    command_name: str,
) -> None:
    await _SETTINGS_ADAPTER.send_preview(
        interaction,
        settings=settings,
        proposed=proposed,
        reason=reason,
        context=context,
        command_name=command_name,
    )


def _parse_settings_boolean(value: str, *, label: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{label} 값은 true 또는 false여야 합니다.")


def _settings_preview_value(field_name: str, value: object) -> str:
    return _SETTINGS_ADAPTER.settings_preview_value(field_name, value)


def _enabled_label(enabled: bool) -> str:
    return "사용" if enabled else "사용 안 함"


def _configured_label(channel_id: str | None) -> str:
    return "설정됨" if channel_id is not None else "미설정"


def _role_mention(role_id: str | None) -> str:
    return f"<@&{role_id}>" if role_id is not None else "미설정"


def _format_account_info(overview: AccountOverviewDTO) -> str:
    return _ACCOUNT_ADAPTER.format_account_info(overview)


def _format_account_registration_request(request: AccountRegistrationRequestDTO | None) -> str:
    return _ACCOUNT_ADAPTER.format_registration_request(request)


async def _notify_account_registration_completion(
    interaction: discord.Interaction,
    *,
    request: AccountRegistrationRequestDTO,
) -> None:
    """Best-effort completion DM; `/account registration-status` remains the fallback."""

    client = getattr(interaction, "client", None)
    get_user = getattr(client, "get_user", None)
    fetch_user = getattr(client, "fetch_user", None)
    recipient = get_user(int(request.requester_discord_user_id)) if callable(get_user) else None
    if recipient is None and callable(fetch_user):
        try:
            recipient = await fetch_user(int(request.requester_discord_user_id))
        except Exception:
            log_sanitized_exception(
                logger,
                "account registration completion recipient lookup failed correlation_id=%s request_id=%s",
                _correlation_id(interaction),
                request.id,
            )
            return
    send = getattr(recipient, "send", None)
    if not callable(send):
        return
    try:
        await send(_format_account_registration_completion(request))
    except Exception:
        log_sanitized_exception(
            logger,
            "account registration completion DM failed correlation_id=%s request_id=%s",
            _correlation_id(interaction),
            request.id,
        )


def _format_account_registration_completion(request: AccountRegistrationRequestDTO) -> str:
    return _bounded_discord_message(
        [
            "게임 계정 등록이 완료되었습니다.",
            f"참가자 이름: {_safe_discord_text(request.accepted_persona_display_name or '-')}",
            f"참가자 ID: `{request.accepted_persona_short_id or '-'}`",
            f"연결 Discord 표시명: {_safe_discord_text(request.discord_nickname_snapshot)}",
            f"연결 게임 계정: {_safe_discord_text(request.accepted_game_account_name or '-')}",
            f"초기 서클 포인트 지급량: {request.initial_grant_amount}",
        ]
    )
