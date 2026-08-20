from __future__ import annotations

import asyncio
import logging
from enum import StrEnum

import discord

from umacircle_bot.logging_safety import log_sanitized_exception

logger = logging.getLogger(__name__)


class ManagedChannelKind(StrEnum):
    WIN5_ANNOUNCEMENT = "win5_announcement"
    ROOM_MATCH_ANNOUNCEMENT = "room_match_announcement"
    LOG = "log"


DEFAULT_CHANNEL_NAMES = {
    ManagedChannelKind.WIN5_ANNOUNCEMENT: "umabot-win5",
    ManagedChannelKind.ROOM_MATCH_ANNOUNCEMENT: "umabot-room-match",
    ManagedChannelKind.LOG: "umabot-log",
}
_CHANNEL_PROVISION_LOCKS: dict[tuple[int, ManagedChannelKind], asyncio.Lock] = {}


async def resolve_or_create_default_channel(
    guild: discord.Guild,
    *,
    configured_channel_id: str | None,
    kind: ManagedChannelKind,
    staff_role_ids: tuple[int, ...] = (),
) -> discord.TextChannel | None:
    """Resolve a configured/default text channel, or best-effort create it.

    This adapter deliberately owns no database transaction. Callers decide whether
    and when a created channel ID is persisted through the settings service.
    """

    try:
        lock_key = (guild.id, kind)
        lock = _CHANNEL_PROVISION_LOCKS.setdefault(lock_key, asyncio.Lock())
        async with lock:
            configured = _configured_text_channel(guild, configured_channel_id)
            if configured is not None:
                return await _prepare_resolved_channel(
                    guild,
                    configured,
                    kind=kind,
                    staff_role_ids=staff_role_ids,
                )
            if configured_channel_id is not None:
                logger.warning(
                    "configured managed Discord channel is unavailable guild_id=%s kind=%s",
                    guild.id,
                    kind.value,
                )
                return None

            default_name = DEFAULT_CHANNEL_NAMES[kind]
            for channel in sorted(guild.text_channels, key=lambda item: item.id):
                if channel.name == default_name:
                    return await _prepare_resolved_channel(
                        guild,
                        channel,
                        kind=kind,
                        staff_role_ids=staff_role_ids,
                    )

            if kind is ManagedChannelKind.LOG:
                overwrites = _private_log_overwrites(guild, staff_role_ids)
                created = await guild.create_text_channel(
                    default_name,
                    overwrites=overwrites,
                    reason="UMA-ST-2 bot managed log channel provisioning",
                )
                if not _has_private_log_permissions(created, overwrites):
                    return None
                return created
            return await guild.create_text_channel(
                default_name,
                reason="UMA-ST-2 bot managed announcement channel provisioning",
            )
    except Exception:
        log_sanitized_exception(
            logger,
            "managed Discord channel provisioning failed guild_id=%s kind=%s",
            getattr(guild, "id", "unavailable"),
            kind.value,
        )
        return None


def _configured_text_channel(
    guild: discord.Guild,
    configured_channel_id: str | None,
) -> discord.TextChannel | None:
    if configured_channel_id is None:
        return None
    try:
        channel_id = int(configured_channel_id)
    except (TypeError, ValueError):
        return None
    channel = guild.get_channel(channel_id)
    if channel is None or getattr(channel, "type", None) is not discord.ChannelType.text:
        return None
    return channel


async def _prepare_resolved_channel(
    guild: discord.Guild,
    channel: discord.TextChannel,
    *,
    kind: ManagedChannelKind,
    staff_role_ids: tuple[int, ...],
) -> discord.TextChannel | None:
    if kind is not ManagedChannelKind.LOG:
        return channel
    overwrites = _private_log_overwrites(guild, staff_role_ids)
    edited = await channel.edit(
        overwrites=overwrites,
        reason="UMA-ST-2 bot managed log channel permission enforcement",
    )
    resolved = edited or channel
    return resolved if _has_private_log_permissions(resolved, overwrites) else None


def _has_private_log_permissions(
    channel: discord.TextChannel,
    desired: dict[discord.abc.Snowflake, discord.PermissionOverwrite],
) -> bool:
    if set(channel.overwrites) != set(desired):
        return False
    for target, expected in desired.items():
        actual = channel.overwrites_for(target)
        for permission_name in (
            "view_channel",
            "send_messages",
            "read_message_history",
            "manage_channels",
        ):
            if getattr(actual, permission_name) is not getattr(expected, permission_name):
                return False
    return True


def _private_log_overwrites(
    guild: discord.Guild,
    staff_role_ids: tuple[int, ...],
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    for role_id in dict.fromkeys(staff_role_ids):
        role = guild.get_role(role_id)
        if role is not None:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )
    bot_member = guild.me
    if bot_member is not None:
        overwrites[bot_member] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
        )
    return overwrites
