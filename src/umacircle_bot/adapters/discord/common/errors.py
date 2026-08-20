import logging

import discord

from umacircle_bot.adapters.discord.common.delivery import (
    correlation_id,
    interaction_user_id,
    send_followup_safely,
    send_initial_response_safely,
)
from umacircle_bot.logging_safety import log_sanitized_exception

logger = logging.getLogger(__name__)


async def send_user_error(
    interaction: discord.Interaction,
    command_name: str,
    prefix: str,
    error: Exception,
) -> None:
    message = " ".join(str(error).split())[:300] or "요청 값이 올바르지 않습니다."
    await send_followup_safely(
        interaction,
        command_name,
        f"{prefix}: {message}",
        response_kind="domain-error",
    )


async def send_internal_error(interaction: discord.Interaction, command_name: str) -> None:
    request_id = correlation_id(interaction)
    log_sanitized_exception(
        logger,
        "discord command failed correlation_id=%s command=%s actor_id=%s guild_id=%s channel_id=%s",
        request_id,
        command_name,
        interaction_user_id(interaction),
        getattr(interaction, "guild_id", None),
        getattr(interaction, "channel_id", None),
    )
    await send_followup_safely(
        interaction,
        command_name,
        f"요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요. 참조 ID: `{request_id}`",
        response_kind="internal-error",
    )


async def send_pre_modal_user_error(
    interaction: discord.Interaction,
    command_name: str,
    prefix: str,
    error: Exception,
) -> None:
    message = " ".join(str(error).split())[:300] or "요청 값이 올바르지 않습니다."
    await send_initial_response_safely(
        interaction,
        command_name,
        f"{prefix}: {message}",
    )


async def handle_modal_open_error(
    interaction: discord.Interaction,
    command_name: str,
) -> None:
    log_sanitized_exception(
        logger,
        "discord modal open failed correlation_id=%s command=%s actor_id=%s",
        correlation_id(interaction),
        command_name,
        interaction_user_id(interaction),
    )
    if not interaction.response.is_done():
        await send_initial_response_safely(
            interaction,
            command_name,
            f"입력 창을 열지 못했습니다. 요청 ID: `{correlation_id(interaction)}`",
        )
