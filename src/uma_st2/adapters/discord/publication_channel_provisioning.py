"""Automatic Discord channel create-or-find before publication delivery."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

import discord

from uma_st2.application.discord import (
    AwaitingDiscordPublicationChannel,
    DiscordPublicationChannelProvisioningCommands,
    DiscordPublicationChannelProvisioningError,
    DiscordPublicationChannelProvisioningQueries,
    ProvisionDiscordPublicationChannel,
)
from uma_st2.shared import normalize_utc_datetime, utc_now

from .common import BlockingApplicationRunner, run_blocking_application
from .publication_delivery import PublicationDeliveryRun, PublicationRunOnceWorker

logger = logging.getLogger(__name__)

_AUDIT_REASON = "UMA-ST2 automatic publication announcement channel provisioning"


class DiscordPublicationChannelProviderError(RuntimeError):
    """Current Discord guild/channel authority cannot safely provision."""


class DiscordPublicationChannelClient(Protocol):
    def get_guild(self, guild_id: int) -> object | None: ...


@dataclass(slots=True)
class DiscordPublicationChannelProvisioningWorker:
    """Provision at most one channel target, then run existing delivery."""

    queries: DiscordPublicationChannelProvisioningQueries
    commands: DiscordPublicationChannelProvisioningCommands
    client: DiscordPublicationChannelClient
    guild_id: str
    delivery_worker: PublicationRunOnceWorker
    retry_delay: timedelta
    run_application: BlockingApplicationRunner = run_blocking_application
    clock: Callable[[], datetime] = utc_now
    _guild_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)
    _retry_not_before: dict[str, datetime] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        if not self.guild_id.isascii() or not self.guild_id.isdecimal() or int(self.guild_id) <= 0:
            raise ValueError("guild_id must be a positive Discord snowflake.")
        if not isinstance(self.retry_delay, timedelta) or self.retry_delay <= timedelta(0):
            raise ValueError("retry_delay must be a positive duration.")

    async def run_once(self) -> PublicationDeliveryRun:
        try:
            await self._provision_once()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "Automatic publication channel provisioning failed guild_id=%s error_type=%s",
                self.guild_id,
                type(error).__name__,
            )
        return await self.delivery_worker.run_once()

    async def _provision_once(self) -> None:
        target = await self.run_application(lambda: self.queries.find_target(guild_id=self.guild_id))
        if target is None or not self._retry_due(target):
            return

        async with self._guild_lock:
            target = await self.run_application(lambda: self.queries.find_target(guild_id=self.guild_id))
            if target is None or not self._retry_due(target):
                return
            try:
                channel_id, bot_user_id = await self._provision_provider_channel(target)
                result = await self.run_application(
                    lambda: self.commands.provision(
                        ProvisionDiscordPublicationChannel(
                            target=target,
                            target_channel_id=channel_id,
                            actor_discord_user_id=bot_user_id,
                            idempotency_key=self._idempotency_key(target, channel_id),
                            correlation_id=self._idempotency_key(target, channel_id),
                        )
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._retry_not_before[target.destination_kind] = self._now() + self.retry_delay
                logger.error(
                    "Automatic publication channel target failed guild_id=%s "
                    "destination=%s publication_id=%s error_type=%s",
                    target.guild_id,
                    target.destination_kind,
                    target.publication_id,
                    type(error).__name__,
                )
                return

            self._retry_not_before.pop(target.destination_kind, None)
            logger.info(
                "Automatic publication channel provisioned guild_id=%s destination=%s "
                "publication_id=%s channel_id=%s operation_id=%s exact_retry=%s",
                result.guild_id,
                result.destination_kind,
                result.publication_id,
                result.target_channel_id,
                result.operation_id,
                result.exact_retry,
            )

    async def _provision_provider_channel(
        self,
        target: AwaitingDiscordPublicationChannel,
    ) -> tuple[str, str]:
        guild = self.client.get_guild(int(target.guild_id))
        if guild is None or getattr(guild, "id", None) != int(target.guild_id):
            raise DiscordPublicationChannelProviderError("Configured Discord guild is unavailable.")
        bot_member = getattr(guild, "me", None)
        default_role = getattr(guild, "default_role", None)
        if bot_member is None or default_role is None:
            raise DiscordPublicationChannelProviderError("Discord guild membership or default Role is unavailable.")
        bot_user_id = getattr(bot_member, "id", None)
        if isinstance(bot_user_id, bool) or not isinstance(bot_user_id, int) or bot_user_id <= 0:
            raise DiscordPublicationChannelProviderError("Discord bot identity is unavailable.")
        guild_permissions = getattr(bot_member, "guild_permissions", None)
        if not bool(getattr(guild_permissions, "manage_channels", False)):
            raise DiscordPublicationChannelProviderError("Discord bot lacks Manage Channels.")

        fetch_channels = getattr(guild, "fetch_channels", None)
        create_text_channel = getattr(guild, "create_text_channel", None)
        if not callable(fetch_channels) or not callable(create_text_channel):
            raise DiscordPublicationChannelProviderError("Discord guild channel provider API is unavailable.")
        channels = await fetch_channels()
        matches = self._exact_text_channels(channels, target=target)
        if len(matches) > 1:
            raise DiscordPublicationChannelProviderError("Duplicate canonical Discord channel names exist.")

        overwrites = {
            default_role: discord.PermissionOverwrite(view_channel=True),
            bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if matches:
            edit = getattr(matches[0], "edit", None)
            if not callable(edit):
                raise DiscordPublicationChannelProviderError("Existing Discord channel cannot be normalized.")
            channel = await edit(overwrites=overwrites, reason=_AUDIT_REASON)
        else:
            channel = await create_text_channel(
                target.channel_name,
                overwrites=overwrites,
                reason=_AUDIT_REASON,
            )
        channel_id = self._validate_channel(
            channel,
            target=target,
            default_role=default_role,
            bot_member=bot_member,
        )
        return str(channel_id), str(bot_user_id)

    @staticmethod
    def _exact_text_channels(
        channels: object,
        *,
        target: AwaitingDiscordPublicationChannel,
    ) -> tuple[object, ...]:
        if not isinstance(channels, Sequence):
            raise DiscordPublicationChannelProviderError("Discord channel list is malformed.")
        return tuple(
            channel
            for channel in channels
            if getattr(channel, "name", None) == target.channel_name
            and getattr(channel, "type", None) == discord.ChannelType.text
        )

    @staticmethod
    def _validate_channel(
        channel: object,
        *,
        target: AwaitingDiscordPublicationChannel,
        default_role: object,
        bot_member: object,
    ) -> int:
        channel_id = getattr(channel, "id", None)
        if (
            isinstance(channel_id, bool)
            or not isinstance(channel_id, int)
            or channel_id <= 0
            or getattr(getattr(channel, "guild", None), "id", None) != int(target.guild_id)
            or getattr(channel, "name", None) != target.channel_name
            or getattr(channel, "type", None) != discord.ChannelType.text
        ):
            raise DiscordPublicationChannelProviderError("Provisioned Discord channel identity is malformed.")
        permissions_for = getattr(channel, "permissions_for", None)
        if not callable(permissions_for) or not callable(getattr(channel, "send", None)):
            raise DiscordPublicationChannelProviderError("Provisioned Discord channel is not messageable.")
        everyone_permissions = permissions_for(default_role)
        bot_permissions = permissions_for(bot_member)
        if not bool(getattr(everyone_permissions, "view_channel", False)) or not all(
            bool(getattr(bot_permissions, permission, False)) for permission in ("view_channel", "send_messages")
        ):
            raise DiscordPublicationChannelProviderError("Provisioned Discord channel permissions are unsafe.")
        return channel_id

    def _retry_due(self, target: AwaitingDiscordPublicationChannel) -> bool:
        retry_at = self._retry_not_before.get(target.destination_kind)
        return retry_at is None or self._now() >= retry_at

    def _now(self) -> datetime:
        return normalize_utc_datetime(self.clock(), field_name="clock result")

    @staticmethod
    def _idempotency_key(target: AwaitingDiscordPublicationChannel, channel_id: str) -> str:
        key = f"auto-channel:{target.publication_id}:{channel_id}"
        if len(key) > 128:
            raise DiscordPublicationChannelProvisioningError("Automatic channel idempotency key is too long.")
        return key
