"""Safe delivery for deferred Discord interaction responses."""

from __future__ import annotations

import logging

import discord

from ..strings.common import internal_error_message

logger = logging.getLogger(__name__)


def _safe_snowflake(value: object) -> str:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else "unavailable"


def correlation_id(interaction: object) -> str:
    """Return a bounded interaction identifier safe for user-facing errors."""

    return _safe_snowflake(getattr(interaction, "id", None))


def _interaction_id(interaction: object, attribute: str) -> str:
    return _safe_snowflake(getattr(interaction, attribute, None))


def _actor_id(interaction: object) -> str:
    return _safe_snowflake(getattr(getattr(interaction, "user", None), "id", None))


def _log_delivery_failure(interaction: object, command_name: str, response_kind: str) -> None:
    logger.error(
        "Discord response failed correlation_id=%s command=%s response_kind=%s actor_id=%s guild_id=%s channel_id=%s",
        correlation_id(interaction),
        command_name,
        response_kind,
        _actor_id(interaction),
        _interaction_id(interaction, "guild_id"),
        _interaction_id(interaction, "channel_id"),
    )


def _log_application_failure(interaction: object, command_name: str) -> None:
    logger.error(
        "Discord application call failed correlation_id=%s command=%s actor_id=%s guild_id=%s channel_id=%s",
        correlation_id(interaction),
        command_name,
        _actor_id(interaction),
        _interaction_id(interaction, "guild_id"),
        _interaction_id(interaction, "channel_id"),
    )


async def send_deferred_response_safely(
    interaction: discord.Interaction,
    command_name: str,
    content: str,
) -> bool:
    """Complete a previously deferred response without exposing mentions."""

    try:
        await interaction.edit_original_response(
            content=content,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        _log_delivery_failure(interaction, command_name, "success")
        return False
    return True


async def send_deferred_attachment_safely(
    interaction: discord.Interaction,
    command_name: str,
    content: str,
    attachment: discord.File,
) -> bool:
    """Complete an ephemeral deferred response with one caller-owned file."""

    try:
        await interaction.edit_original_response(
            content=content,
            attachments=[attachment],
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        _log_delivery_failure(interaction, command_name, "attachment")
        return False
    return True


async def send_private_error_after_defer_safely(
    interaction: discord.Interaction,
    command_name: str,
) -> bool:
    """Replace a deferred public placeholder with a private generic error."""

    request_id = correlation_id(interaction)
    _log_application_failure(interaction, command_name)
    delivered = True
    try:
        await interaction.delete_original_response()
    except Exception:
        _log_delivery_failure(interaction, command_name, "public-placeholder-delete")
        delivered = False
    try:
        await interaction.followup.send(
            internal_error_message(request_id),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        _log_delivery_failure(interaction, command_name, "internal-error")
        return False
    return delivered


async def send_private_response_after_public_defer_safely(
    interaction: discord.Interaction,
    command_name: str,
    content: str,
) -> bool:
    """Replace a deferred public placeholder with an expected private response."""

    delivered = True
    try:
        await interaction.delete_original_response()
    except Exception:
        _log_delivery_failure(interaction, command_name, "public-placeholder-delete")
        delivered = False
    try:
        await interaction.followup.send(
            content,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        _log_delivery_failure(interaction, command_name, "private-response")
        return False
    return delivered


async def send_ephemeral_internal_error_after_defer_safely(
    interaction: discord.Interaction,
    command_name: str,
) -> bool:
    """Replace an ephemeral deferred response with a generic internal error."""

    request_id = correlation_id(interaction)
    _log_application_failure(interaction, command_name)
    try:
        await interaction.edit_original_response(
            content=internal_error_message(request_id),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        _log_delivery_failure(interaction, command_name, "internal-error")
        return False
    return True
