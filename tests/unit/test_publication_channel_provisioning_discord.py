"""Discord provider tests for automatic publication-channel provisioning."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord

from uma_st2.adapters.discord import (
    DiscordPublicationChannelProvisioningWorker,
    MatchOddsPublicationRuntimeWorker,
    PublicationDeliveryRun,
)
from uma_st2.application.discord import (
    AwaitingDiscordPublicationChannel,
    DiscordPublicationChannelProvisioningStaleError,
    ProvisionDiscordPublicationChannel,
)
from uma_st2.application.publication import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    MATCH_BETTING_OPENED_EVENT_TYPE,
)

NOW = datetime(2026, 9, 5, 6, 0, tzinfo=UTC)
GUILD_ID = "100000000000000001"
BOT_ID = 400000000000000001
CHANNEL_ID = 200000000000000001


def _target() -> AwaitingDiscordPublicationChannel:
    return AwaitingDiscordPublicationChannel(
        publication_id=71,
        guild_id=GUILD_ID,
        destination_kind=MATCH_ANNOUNCEMENT_DESTINATION_KIND,
        event_type=MATCH_BETTING_OPENED_EVENT_TYPE,
        payload_fingerprint="a" * 64,
        expected_settings_fingerprint="b" * 64,
    )


async def _direct_application(operation: Callable[[], object]) -> object:
    return operation()


@dataclass(frozen=True)
class FakePermissions:
    view_channel: bool = False
    send_messages: bool = False
    manage_channels: bool = False


@dataclass(frozen=True)
class FakeRole:
    id: int


@dataclass(frozen=True)
class FakeMember:
    id: int
    guild_permissions: FakePermissions


class FakeChannel:
    def __init__(
        self,
        *,
        channel_id: int,
        name: str,
        guild: FakeGuild,
    ) -> None:
        self.id = channel_id
        self.name = name
        self.guild = guild
        self.type = discord.ChannelType.text
        self.edit_calls: list[tuple[dict[object, discord.PermissionOverwrite], str]] = []
        self._permissions: dict[int, FakePermissions] = {}

    async def edit(
        self,
        *,
        overwrites: dict[object, discord.PermissionOverwrite],
        reason: str,
    ) -> FakeChannel:
        self.edit_calls.append((overwrites, reason))
        self._apply(overwrites)
        return self

    async def send(self, *_args: object, **_kwargs: object) -> None:
        return None

    def permissions_for(self, target: object) -> FakePermissions:
        return self._permissions.get(target.id, FakePermissions())  # type: ignore[attr-defined]

    def _apply(self, overwrites: dict[object, discord.PermissionOverwrite]) -> None:
        for target, overwrite in overwrites.items():
            self._permissions[target.id] = FakePermissions(  # type: ignore[attr-defined]
                view_channel=overwrite.view_channel is True,
                send_messages=overwrite.send_messages is True,
            )


class FakeGuild:
    def __init__(self, *, channels: list[FakeChannel] | None = None, manage_channels: bool = True) -> None:
        self.id = int(GUILD_ID)
        self.default_role = FakeRole(self.id)
        self.me = FakeMember(
            BOT_ID,
            FakePermissions(manage_channels=manage_channels),
        )
        self.channels = channels or []
        for channel in self.channels:
            channel.guild = self
        self.fetch_calls = 0
        self.create_calls: list[tuple[str, dict[object, discord.PermissionOverwrite], str]] = []

    async def fetch_channels(self) -> list[FakeChannel]:
        self.fetch_calls += 1
        return list(self.channels)

    async def create_text_channel(
        self,
        name: str,
        *,
        overwrites: dict[object, discord.PermissionOverwrite],
        reason: str,
    ) -> FakeChannel:
        self.create_calls.append((name, overwrites, reason))
        channel = FakeChannel(channel_id=CHANNEL_ID, name=name, guild=self)
        channel._apply(overwrites)
        self.channels.append(channel)
        return channel


class FakeClient:
    def __init__(self, guild: FakeGuild | None) -> None:
        self.guild = guild

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        if self.guild is None or guild_id != self.guild.id:
            return None
        return self.guild


class RecordingQueries:
    def __init__(self, target: AwaitingDiscordPublicationChannel | None, events: list[str]) -> None:
        self.target = target
        self.events = events
        self.calls = 0

    def find_target(self, *, guild_id: str) -> AwaitingDiscordPublicationChannel | None:
        assert guild_id == GUILD_ID
        self.calls += 1
        self.events.append("query")
        return self.target


class RecordingCommands:
    def __init__(self, events: list[str], *, error: Exception | None = None) -> None:
        self.events = events
        self.error = error
        self.commands: list[ProvisionDiscordPublicationChannel] = []

    def provision(self, command: ProvisionDiscordPublicationChannel) -> object:
        self.events.append("bind")
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            guild_id=command.target.guild_id,
            destination_kind=command.target.destination_kind,
            publication_id=command.target.publication_id,
            target_channel_id=command.target_channel_id,
            operation_id=91,
            exact_retry=False,
        )


class RecordingDelivery:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.result = PublicationDeliveryRun(0, 0, 0, 0, 0)

    async def run_once(self) -> PublicationDeliveryRun:
        self.events.append("delivery")
        return self.result


class RecordingOddsCommands:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def refresh_due(self, *, guild_id: str) -> object:
        self.events.append(f"refresh:{guild_id}")
        return SimpleNamespace(publication=None)


class MutableClock:
    def __init__(self) -> None:
        self.current = NOW

    def __call__(self) -> datetime:
        return self.current


def _worker(
    *,
    guild: FakeGuild | None,
    target: AwaitingDiscordPublicationChannel | None,
    events: list[str],
    command_error: Exception | None = None,
    clock: MutableClock | None = None,
) -> tuple[
    DiscordPublicationChannelProvisioningWorker,
    RecordingQueries,
    RecordingCommands,
    RecordingDelivery,
]:
    queries = RecordingQueries(target, events)
    commands = RecordingCommands(events, error=command_error)
    delivery = RecordingDelivery(events)
    worker = DiscordPublicationChannelProvisioningWorker(
        queries=queries,  # type: ignore[arg-type]
        commands=commands,  # type: ignore[arg-type]
        client=FakeClient(guild),
        guild_id=GUILD_ID,
        delivery_worker=delivery,
        retry_delay=timedelta(minutes=5),
        run_application=_direct_application,  # type: ignore[arg-type]
        clock=clock or MutableClock(),
    )
    return worker, queries, commands, delivery


def test_worker_creates_public_channel_binds_then_runs_delivery() -> None:
    events: list[str] = []
    guild = FakeGuild()
    worker, queries, commands, delivery = _worker(guild=guild, target=_target(), events=events)

    result = asyncio.run(worker.run_once())

    assert result == delivery.result
    assert events == ["query", "query", "bind", "delivery"]
    assert queries.calls == 2
    assert len(guild.create_calls) == 1
    name, overwrites, reason = guild.create_calls[0]
    assert name == "umabot-room-match"
    assert "automatic publication" in reason
    assert overwrites[guild.default_role].view_channel is True
    assert overwrites[guild.me].view_channel is True
    assert overwrites[guild.me].send_messages is True
    assert len(commands.commands) == 1
    command = commands.commands[0]
    assert command.target_channel_id == str(CHANNEL_ID)
    assert command.actor_discord_user_id == str(BOT_ID)
    assert command.idempotency_key == f"auto-channel:71:{CHANNEL_ID}"


def test_runtime_orders_odds_generation_then_provisioning_then_delivery() -> None:
    events: list[str] = []
    guild = FakeGuild()
    provisioning, _queries, _commands, delivery = _worker(
        guild=guild,
        target=_target(),
        events=events,
    )
    worker = MatchOddsPublicationRuntimeWorker(
        commands=RecordingOddsCommands(events),  # type: ignore[arg-type]
        guild_id=GUILD_ID,
        delivery_worker=provisioning,
        run_application=_direct_application,  # type: ignore[arg-type]
    )

    result = asyncio.run(worker.run_once())

    assert result == delivery.result
    assert events == [f"refresh:{GUILD_ID}", "query", "query", "bind", "delivery"]


def test_worker_normalizes_and_reuses_one_exact_name_channel() -> None:
    events: list[str] = []
    guild = FakeGuild()
    existing = FakeChannel(channel_id=CHANNEL_ID, name="umabot-room-match", guild=guild)
    guild.channels.append(existing)
    worker, _queries, commands, _delivery = _worker(guild=guild, target=_target(), events=events)

    asyncio.run(worker.run_once())

    assert guild.create_calls == []
    assert len(existing.edit_calls) == 1
    assert commands.commands[0].target_channel_id == str(CHANNEL_ID)


def test_duplicate_name_failure_is_isolated_and_respects_retry_cooldown() -> None:
    events: list[str] = []
    clock = MutableClock()
    guild = FakeGuild()
    guild.channels.extend(
        [
            FakeChannel(channel_id=CHANNEL_ID, name="umabot-room-match", guild=guild),
            FakeChannel(channel_id=CHANNEL_ID + 1, name="umabot-room-match", guild=guild),
        ]
    )
    worker, queries, commands, delivery = _worker(
        guild=guild,
        target=_target(),
        events=events,
        clock=clock,
    )

    first = asyncio.run(worker.run_once())
    second = asyncio.run(worker.run_once())
    clock.current += timedelta(minutes=5)
    third = asyncio.run(worker.run_once())

    assert first == second == third == delivery.result
    assert guild.fetch_calls == 2
    assert queries.calls == 5
    assert commands.commands == []
    assert events.count("delivery") == 3


def test_missing_manage_channels_isolated_without_provider_mutation() -> None:
    events: list[str] = []
    guild = FakeGuild(manage_channels=False)
    worker, _queries, commands, delivery = _worker(guild=guild, target=_target(), events=events)

    result = asyncio.run(worker.run_once())

    assert result == delivery.result
    assert guild.fetch_calls == 0
    assert guild.create_calls == []
    assert commands.commands == []
    assert events[-1] == "delivery"


def test_canonical_stale_after_provider_success_leaves_channel_and_runs_delivery() -> None:
    events: list[str] = []
    guild = FakeGuild()
    worker, _queries, commands, delivery = _worker(
        guild=guild,
        target=_target(),
        events=events,
        command_error=DiscordPublicationChannelProvisioningStaleError("stale"),
    )

    result = asyncio.run(worker.run_once())

    assert result == delivery.result
    assert len(guild.channels) == 1
    assert len(commands.commands) == 1
    assert events[-1] == "delivery"


def test_no_eligible_target_skips_provider_and_runs_delivery() -> None:
    events: list[str] = []
    guild = FakeGuild()
    worker, queries, commands, delivery = _worker(guild=guild, target=None, events=events)

    result = asyncio.run(worker.run_once())

    assert result == delivery.result
    assert queries.calls == 1
    assert guild.fetch_calls == 0
    assert commands.commands == []
    assert events == ["query", "delivery"]
