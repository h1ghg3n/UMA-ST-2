import logging
from pathlib import Path

import discord

from umacircle_bot.logging_safety import log_sanitized_exception

logger = logging.getLogger(__name__)


async def send_initial_response_safely(
    interaction: discord.Interaction,
    command_name: str,
    content: str,
) -> bool:
    try:
        await interaction.response.send_message(
            content,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True
    except Exception:
        log_sanitized_exception(
            logger,
            "discord initial response failed correlation_id=%s command=%s actor_id=%s",
            correlation_id(interaction),
            command_name,
            interaction_user_id(interaction),
        )
        return False


async def send_followup_safely(
    interaction: discord.Interaction,
    command_name: str,
    content: str,
    *,
    response_kind: str = "success",
    file_path: Path | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> bool:
    try:
        send_options: dict[str, object] = {
            "ephemeral": True,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if file_path is not None:
            send_options["file"] = discord.File(file_path)
        if embed is not None:
            send_options["embed"] = embed
        if view is not None:
            send_options["view"] = view
        await interaction.followup.send(content, **send_options)
        return True
    except Exception:
        log_sanitized_exception(
            logger,
            "discord followup failed correlation_id=%s command=%s actor_id=%s response_kind=%s",
            correlation_id(interaction),
            command_name,
            interaction_user_id(interaction),
            response_kind,
        )
        return False


def correlation_id(interaction: object) -> str:
    interaction_id = getattr(interaction, "id", None)
    return str(interaction_id) if isinstance(interaction_id, int) and interaction_id > 0 else "unavailable"


def interaction_user_id(interaction: object) -> str:
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    return str(user_id) if isinstance(user_id, int) and user_id > 0 else "unavailable"
