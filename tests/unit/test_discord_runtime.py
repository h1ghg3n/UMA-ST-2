"""Tests for concrete Discord authorization, preflight, and scheduling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace

import pytest
from discord import app_commands

from uma_st2.adapters.discord import (
    DiscordCommandGate,
    DiscordRuntimePreflight,
    DiscordRuntimePreflightError,
    PublicationDeliveryRun,
    PublicationDeliveryScheduler,
    UmaSt2DiscordClient,
    validate_discord_runtime_settings,
)
from uma_st2.application.discord import DiscordGuildRuntimeSettings


def _guild_settings(
    *,
    channel_id: str | None = "111111111",
    match_channel_id: str | None = "888888888",
    operator_role_id: str | None = "222222222",
    bot_manager_role_id: str | None = "333333333",
) -> DiscordGuildRuntimeSettings:
    return DiscordGuildRuntimeSettings(
        guild_id="987654321",
        win5_announcement_channel_id=channel_id,
        match_announcement_channel_id=match_channel_id,
        operator_role_id=operator_role_id,
        bot_manager_role_id=bot_manager_role_id,
        win5_announcements_enabled=True,
    )


class RecordingSettingsQueries:
    def __init__(self, values: list[DiscordGuildRuntimeSettings]) -> None:
        self.values = values
        self.calls: list[str] = []

    def get_guild_settings(self, *, guild_id: str) -> DiscordGuildRuntimeSettings:
        self.calls.append(guild_id)
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0]


async def _direct_application(operation):
    return operation()


class FakeResponse:
    def __init__(self) -> None:
        self.done = False
        self.deferred: list[tuple[bool, bool]] = []
        self.messages: list[tuple[str, bool, object]] = []

    def is_done(self) -> bool:
        return self.done

    async def defer(self, *, ephemeral: bool, thinking: bool) -> None:
        self.done = True
        self.deferred.append((ephemeral, thinking))

    async def send_message(
        self,
        content: str,
        *,
        ephemeral: bool,
        allowed_mentions: object,
    ) -> None:
        self.done = True
        self.messages.append((content, ephemeral, allowed_mentions))


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool, object]] = []

    async def send(
        self,
        content: str,
        *,
        ephemeral: bool,
        allowed_mentions: object,
    ) -> None:
        self.messages.append((content, ephemeral, allowed_mentions))


def _interaction(
    *,
    channel_id: int,
    role_ids: tuple[int, ...] = (),
    user_id: int = 444444444,
    owner_id: int = 555555555,
    guild_id: int = 987654321,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=999999999,
        guild_id=guild_id,
        channel_id=channel_id,
        guild=SimpleNamespace(owner_id=owner_id),
        user=SimpleNamespace(
            id=user_id,
            roles=tuple(SimpleNamespace(id=role_id) for role_id in role_ids),
        ),
        response=FakeResponse(),
        followup=FakeFollowup(),
    )


def _gate(queries: RecordingSettingsQueries) -> DiscordCommandGate:
    return DiscordCommandGate(
        settings_queries=queries,  # type: ignore[arg-type]
        configured_guild_id=987654321,
        run_application=_direct_application,
    )


def test_member_prepare_accepts_any_configured_guild_channel_and_preserves_visibility() -> None:
    queries = RecordingSettingsQueries([_guild_settings(channel_id=None)])
    gate = _gate(queries)
    announcement_channel = _interaction(channel_id=111111111)
    another_channel = _interaction(channel_id=777777777)

    assert asyncio.run(gate.prepare_command(announcement_channel, "win5.rounds", ephemeral=False))
    assert asyncio.run(gate.prepare_command(another_channel, "win5.submit", ephemeral=True))

    assert announcement_channel.response.deferred == [(False, True)]
    assert another_channel.response.deferred == [(True, True)]
    assert queries.calls == ["987654321", "987654321"]


def test_match_member_prepare_does_not_require_an_announcement_destination() -> None:
    queries = RecordingSettingsQueries(
        [
            _guild_settings(match_channel_id=None),
            _guild_settings(match_channel_id=None),
            _guild_settings(match_channel_id=None),
            _guild_settings(match_channel_id=None),
        ]
    )
    gate = _gate(queries)
    bet = _interaction(channel_id=444444444)
    public_list = _interaction(channel_id=555555555)
    private_list = _interaction(channel_id=666666666)
    private_ratings = _interaction(channel_id=777777777)

    assert asyncio.run(gate.prepare_command(bet, "match.bet", ephemeral=True))
    assert asyncio.run(gate.prepare_command(public_list, "match.races", ephemeral=False))
    assert asyncio.run(gate.prepare_command(private_list, "match.bets", ephemeral=True))
    assert asyncio.run(gate.prepare_command(private_ratings, "match.ratings", ephemeral=True))

    assert bet.response.deferred == [(True, True)]
    assert public_list.response.deferred == [(False, True)]
    assert private_list.response.deferred == [(True, True)]
    assert private_ratings.response.deferred == [(True, True)]


def test_account_commands_are_private_and_allowed_in_any_configured_guild_channel() -> None:
    gate = _gate(RecordingSettingsQueries([_guild_settings(), _guild_settings()]))
    status = _interaction(channel_id=777777777)
    register = _interaction(channel_id=888888888)

    assert asyncio.run(gate.prepare_command(status, "account.status", ephemeral=True))
    assert asyncio.run(gate.authorize_interaction(register, "account.register"))

    assert status.response.deferred == [(True, True)]
    assert register.response.deferred == []


def test_staff_authorization_accepts_current_role_or_current_guild_owner() -> None:
    gate = _gate(RecordingSettingsQueries([_guild_settings()]))
    operator = _interaction(channel_id=777777777, role_ids=(222222222,))
    owner = _interaction(
        channel_id=777777777,
        user_id=555555555,
        owner_id=555555555,
    )
    member = _interaction(channel_id=777777777)

    assert asyncio.run(gate.authorize_interaction(operator, "win5.staff.round"))
    assert asyncio.run(gate.authorize_interaction(owner, "win5.staff.season"))
    assert asyncio.run(gate.authorize_interaction(operator, "match.staff.race-create"))
    assert asyncio.run(gate.authorize_interaction(operator, "staff.persona"))
    assert not asyncio.run(gate.authorize_interaction(member, "win5.staff.round"))
    assert "역할 권한" in member.response.messages[0][0]


def test_settings_is_private_staff_only_in_any_configured_guild_channel() -> None:
    gate = _gate(RecordingSettingsQueries([_guild_settings(), _guild_settings()]))
    operator = _interaction(channel_id=777777777, role_ids=(222222222,))
    member = _interaction(channel_id=888888888)

    assert asyncio.run(gate.prepare_command(operator, "settings", ephemeral=True))
    assert not asyncio.run(gate.prepare_command(member, "settings", ephemeral=True))

    assert operator.response.deferred == [(True, True)]
    assert "역할 권한" in member.response.messages[0][0]


def test_export_authorization_accepts_any_guild_channel_and_requires_bot_manager_or_owner() -> None:
    gate = _gate(RecordingSettingsQueries([_guild_settings()]))
    manager = _interaction(channel_id=444444444, role_ids=(333333333,))
    operator = _interaction(channel_id=555555555, role_ids=(222222222,))
    another_channel = _interaction(channel_id=666666666, role_ids=(333333333,))
    owner = _interaction(
        channel_id=888888888,
        user_id=555555555,
        owner_id=555555555,
    )

    assert asyncio.run(gate.prepare_command(manager, "export.win5.season", ephemeral=True))
    assert not asyncio.run(gate.prepare_command(operator, "export.win5.season", ephemeral=True))
    assert asyncio.run(gate.authorize_autocomplete(another_channel, "export.win5.season"))
    assert asyncio.run(gate.authorize_autocomplete(owner, "export.win5.season"))
    assert manager.response.deferred == [(True, True)]
    assert "Bot Manager" in operator.response.messages[0][0]
    assert another_channel.response.messages == []


def test_component_authorization_reloads_role_settings_and_rejects_revoked_role() -> None:
    queries = RecordingSettingsQueries(
        [
            _guild_settings(operator_role_id="222222222"),
            _guild_settings(operator_role_id="666666666"),
        ]
    )
    gate = _gate(queries)
    interaction = _interaction(channel_id=777777777, role_ids=(222222222,))

    assert asyncio.run(gate.authorize_interaction(interaction, "win5.staff.round"))
    assert not asyncio.run(gate.authorize_interaction(interaction, "win5.staff.round"))
    assert queries.calls == ["987654321", "987654321"]


def test_autocomplete_denial_does_not_acknowledge_interaction() -> None:
    gate = _gate(RecordingSettingsQueries([_guild_settings()]))
    interaction = _interaction(channel_id=111111111, guild_id=123456789)

    assert not asyncio.run(gate.authorize_autocomplete(interaction, "win5.submit"))
    assert interaction.response.messages == []
    assert interaction.followup.messages == []


@pytest.mark.parametrize(
    "settings",
    [
        _guild_settings(operator_role_id=None, bot_manager_role_id=None),
        _guild_settings(operator_role_id="987654321", bot_manager_role_id=None),
    ],
)
def test_runtime_settings_preflight_rejects_incomplete_or_everyone_role(
    settings: DiscordGuildRuntimeSettings,
) -> None:
    with pytest.raises(DiscordRuntimePreflightError):
        validate_discord_runtime_settings(settings, configured_guild_id=987654321)


def test_runtime_settings_preflight_allows_missing_publication_destinations() -> None:
    validate_discord_runtime_settings(
        _guild_settings(channel_id=None, match_channel_id=None),
        configured_guild_id=987654321,
    )


@dataclass(frozen=True)
class FakePermissions:
    view_channel: bool = True
    send_messages: bool = True
    use_application_commands: bool = True


class FakeChannel:
    def __init__(
        self,
        channel_id: int,
        *,
        guild_id: int = 987654321,
        permissions: FakePermissions | None = None,
    ) -> None:
        self.id = channel_id
        self.guild = SimpleNamespace(id=guild_id)
        self.permissions = permissions or FakePermissions()
        self.permission_members: list[object] = []

    async def send(self, *args, **kwargs) -> None:
        return None

    def permissions_for(self, member: object) -> FakePermissions:
        self.permission_members.append(member)
        return self.permissions


class FakePreflightClient:
    def __init__(self, channels: dict[int, FakeChannel]) -> None:
        self.bot_member = object()
        self.guild = SimpleNamespace(id=987654321, me=self.bot_member)
        self.channels = channels
        self.fetched: list[int] = []

    def get_guild(self, guild_id: int) -> object | None:
        return self.guild if guild_id == self.guild.id else None

    def get_channel(self, channel_id: int) -> object | None:
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> object:
        self.fetched.append(channel_id)
        return self.channels[channel_id]


def test_runtime_preflight_validates_only_configured_publication_destinations() -> None:
    channels = {
        111111111: FakeChannel(
            111111111,
            permissions=FakePermissions(use_application_commands=False),
        ),
        888888888: FakeChannel(888888888),
    }
    client = FakePreflightClient(channels)
    queries = RecordingSettingsQueries([_guild_settings()])
    preflight = DiscordRuntimePreflight(
        client=client,
        settings_queries=queries,  # type: ignore[arg-type]
        configured_guild_id=987654321,
        run_application=_direct_application,
    )

    asyncio.run(preflight.run())

    assert all(channel.permission_members == [client.bot_member] for channel in channels.values())


def test_runtime_preflight_rejects_missing_channel_permission() -> None:
    client = FakePreflightClient(
        {
            111111111: FakeChannel(
                111111111,
                permissions=FakePermissions(send_messages=False),
            ),
        }
    )
    preflight = DiscordRuntimePreflight(
        client=client,
        settings_queries=RecordingSettingsQueries([_guild_settings(match_channel_id=None)]),  # type: ignore[arg-type]
        configured_guild_id=987654321,
        run_application=_direct_application,
    )

    with pytest.raises(DiscordRuntimePreflightError, match="permissions"):
        asyncio.run(preflight.run())


def test_runtime_preflight_accepts_no_configured_publication_destination() -> None:
    client = FakePreflightClient({})
    preflight = DiscordRuntimePreflight(
        client=client,
        settings_queries=RecordingSettingsQueries(  # type: ignore[arg-type]
            [_guild_settings(channel_id=None, match_channel_id=None)]
        ),
        configured_guild_id=987654321,
        run_application=_direct_application,
    )

    asyncio.run(preflight.run())

    assert client.fetched == []


def _delivery_run() -> PublicationDeliveryRun:
    return PublicationDeliveryRun(
        stale_recovered=0,
        claimed=0,
        sent=0,
        failed=0,
        delivery_unknown=0,
    )


class BlockingWorker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def run_once(self) -> PublicationDeliveryRun:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        await self.release.wait()
        self.active -= 1
        return _delivery_run()


def test_scheduler_waits_for_current_non_overlapping_pass_on_shutdown() -> None:
    async def scenario() -> tuple[int, int, bool]:
        worker = BlockingWorker()
        scheduler = PublicationDeliveryScheduler(
            worker=worker,
            poll_interval=timedelta(milliseconds=1),
        )
        scheduler.start()
        await worker.started.wait()
        stop_task = asyncio.create_task(scheduler.stop())
        await asyncio.sleep(0)
        stopped_before_release = stop_task.done()
        worker.release.set()
        await stop_task
        return worker.calls, worker.max_active, stopped_before_release

    calls, max_active, stopped_before_release = asyncio.run(scenario())

    assert calls == 1
    assert max_active == 1
    assert not stopped_before_release


class RecoveringWorker:
    def __init__(self) -> None:
        self.calls = 0
        self.recovered = asyncio.Event()

    async def run_once(self) -> PublicationDeliveryRun:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient worker failure")
        self.recovered.set()
        return _delivery_run()


def test_scheduler_survives_one_worker_exception_and_runs_next_tick() -> None:
    async def scenario() -> int:
        worker = RecoveringWorker()
        scheduler = PublicationDeliveryScheduler(
            worker=worker,
            poll_interval=timedelta(milliseconds=1),
        )
        scheduler.start()
        await asyncio.wait_for(worker.recovered.wait(), timeout=1)
        await scheduler.stop()
        return worker.calls

    assert asyncio.run(scenario()) == 2


class FakeRuntimePreflight:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls = 0

    async def run(self) -> None:
        self.calls += 1
        if self.failure is not None:
            raise self.failure


class FakeRuntimeScheduler:
    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    async def stop(self) -> None:
        self.stop_calls += 1


def test_client_starts_scheduler_only_after_successful_ready_preflight() -> None:
    async def scenario() -> tuple[int, int, int]:
        client = UmaSt2DiscordClient(
            configured_guild_id=987654321,
            command_groups=(app_commands.Group(name="win5", description="WIN5"),),
        )
        preflight = FakeRuntimePreflight()
        scheduler = FakeRuntimeScheduler()
        client.bind_runtime(
            preflight=preflight,  # type: ignore[arg-type]
            scheduler=scheduler,
        )
        await client.on_ready()
        await client.on_ready()
        await client.close()
        return preflight.calls, scheduler.start_calls, scheduler.stop_calls

    assert asyncio.run(scenario()) == (1, 1, 1)


def test_client_closes_without_starting_scheduler_after_preflight_failure() -> None:
    async def scenario() -> tuple[int, int, int, type[Exception] | None]:
        client = UmaSt2DiscordClient(
            configured_guild_id=987654321,
            command_groups=(app_commands.Group(name="win5", description="WIN5"),),
        )
        preflight = FakeRuntimePreflight(DiscordRuntimePreflightError("not ready"))
        scheduler = FakeRuntimeScheduler()
        client.bind_runtime(
            preflight=preflight,  # type: ignore[arg-type]
            scheduler=scheduler,
        )
        await client.on_ready()
        return (
            preflight.calls,
            scheduler.start_calls,
            scheduler.stop_calls,
            None if client.startup_error is None else type(client.startup_error),
        )

    assert asyncio.run(scenario()) == (1, 0, 1, DiscordRuntimePreflightError)
