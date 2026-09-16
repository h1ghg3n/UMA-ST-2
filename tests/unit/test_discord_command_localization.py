"""Discord-native Korean slash-command parameter localization tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord import app_commands

from uma_st2.adapters.discord.account import AccountCommandGroup
from uma_st2.adapters.discord.export import (
    ExportCommandGroup,
    ExportMatchCommandGroup,
    ExportWin5CommandGroup,
)
from uma_st2.adapters.discord.localization import KoreanParameterNameTranslator
from uma_st2.adapters.discord.match_staff import MatchCommandGroup
from uma_st2.adapters.discord.match_staff_workflows import MatchStaffCommandGroup
from uma_st2.adapters.discord.runtime import UmaSt2DiscordClient
from uma_st2.adapters.discord.settings import create_settings_command
from uma_st2.adapters.discord.staff_persona import StaffCommandGroup
from uma_st2.adapters.discord.win5_member import Win5MemberCommandGroup
from uma_st2.adapters.discord.win5_staff import Win5StaffCommandGroup

_SUBCOMMAND_TYPES = {1, 2}


def _command_groups() -> tuple[app_commands.Command | app_commands.Group, ...]:
    account = AccountCommandGroup(
        status_adapter=SimpleNamespace(),  # type: ignore[arg-type]
        registration_adapter=SimpleNamespace(),  # type: ignore[arg-type]
    )
    staff = StaffCommandGroup(  # type: ignore[arg-type]
        persona_adapter=SimpleNamespace(),
        circle_point_adapter=SimpleNamespace(),
    )
    match = MatchCommandGroup(
        staff_group=MatchStaffCommandGroup(adapter=SimpleNamespace()),  # type: ignore[arg-type]
        betting_adapter=SimpleNamespace(),  # type: ignore[arg-type]
        rating_adapter=SimpleNamespace(),  # type: ignore[arg-type]
    )
    win5 = Win5MemberCommandGroup(adapter=SimpleNamespace())  # type: ignore[arg-type]
    win5.add_command(Win5StaffCommandGroup(adapter=SimpleNamespace()))  # type: ignore[arg-type]
    export = ExportCommandGroup(
        circle_point_adapter=SimpleNamespace(),  # type: ignore[arg-type]
        win5_group=ExportWin5CommandGroup(adapter=SimpleNamespace()),  # type: ignore[arg-type]
        match_group=ExportMatchCommandGroup(adapter=SimpleNamespace()),  # type: ignore[arg-type]
    )
    settings = create_settings_command(SimpleNamespace())  # type: ignore[arg-type]
    return account, staff, settings, match, win5, export


async def _translated_payloads() -> tuple[dict[str, object], ...]:
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    translator = KoreanParameterNameTranslator()
    try:
        return tuple([await group.get_translated_payload(tree, translator) for group in _command_groups()])
    finally:
        await client.close()


def _canonical_leaf_paths(payload: dict[str, object]) -> set[str]:
    paths: set[str] = set()

    def visit(
        node: dict[str, object],
        *,
        default_parent: tuple[str, ...] = (),
    ) -> None:
        default_path = (*default_parent, str(node["name"]))
        localizations = node.get("name_localizations", {})
        expected_localizations = {
            ("account",): {"ko": "계정"},
            ("account", "register"): {"ko": "등록"},
            ("account", "status"): {"ko": "상태"},
            ("staff", "persona"): {"ko": "계정"},
        }.get(default_path, {})
        assert localizations == expected_localizations
        options = node.get("options", [])
        assert isinstance(options, list)
        children = [
            option for option in options if isinstance(option, dict) and option.get("type") in _SUBCOMMAND_TYPES
        ]
        if not children:
            paths.add("/" + " ".join(default_path))
            return
        for child in children:
            visit(child, default_parent=default_path)

    visit(payload)
    return paths


def _localized_parameter_names(payload: dict[str, object]) -> dict[str, dict[str, str]]:
    names: dict[str, dict[str, str]] = {}

    def visit(node: dict[str, object], *, parent: tuple[str, ...] = ()) -> None:
        path = (*parent, str(node["name"]))
        options = node.get("options", [])
        assert isinstance(options, list)
        children = [
            option for option in options if isinstance(option, dict) and option.get("type") in _SUBCOMMAND_TYPES
        ]
        if children:
            for child in children:
                visit(child, parent=path)
            return
        parameters = {}
        for option in options:
            assert isinstance(option, dict)
            localizations = option.get("name_localizations", {})
            assert isinstance(localizations, dict)
            assert set(localizations) <= {"ko"}
            if "ko" in localizations:
                parameters[str(option["name"])] = str(localizations["ko"])
        if parameters:
            names["/" + " ".join(path)] = parameters

    visit(payload)
    return names


def test_registered_command_tree_keeps_exact_default_paths_and_reviewed_identity_localization() -> None:
    payloads = asyncio.run(_translated_payloads())

    paths = set()
    for payload in payloads:
        paths.update(_canonical_leaf_paths(payload))

    assert paths == {
        "/account register",
        "/account status",
        "/staff persona",
        "/staff grant-circle-points",
        "/staff adjust-circle-points",
        "/settings",
        "/match races",
        "/match ratings",
        "/match bets",
        "/match bet",
        "/match bet-change",
        "/match staff race",
        "/match staff result",
        "/match staff settlement",
        "/win5 info",
        "/win5 rounds",
        "/win5 submit",
        "/win5 special-submit",
        "/win5 submissions",
        "/win5 cancel",
        "/win5 standings",
        "/win5 staff round",
        "/win5 staff season",
        "/export circle-points",
        "/export win5 season",
        "/export match season",
    }


def test_registered_options_have_exact_reviewed_korean_names() -> None:
    payloads = asyncio.run(_translated_payloads())

    localized = {}
    for payload in payloads:
        localized.update(_localized_parameter_names(payload))

    assert localized == {
        "/account register": {"region": "리전"},
        "/staff persona": {"persona": "페르소나"},
        "/staff grant-circle-points": {"persona": "페르소나"},
        "/staff adjust-circle-points": {"persona": "페르소나"},
        "/match races": {"match_id": "경기"},
        "/match ratings": {"rank": "순위", "persona": "페르소나"},
        "/match bet": {
            "match_id": "경기",
            "bet_type": "유형",
            "numbers": "번호",
            "amount": "금액",
        },
        "/match bet-change": {
            "bet_id": "베팅",
            "bet_type": "유형",
            "numbers": "번호",
            "amount": "금액",
        },
        "/win5 submit": {"round_id": "회차"},
        "/win5 special-submit": {"round_id": "회차"},
        "/win5 cancel": {"submission_id": "제출", "reason": "사유"},
        "/win5 standings": {"season_id": "시즌", "ranking": "집계"},
        "/export win5 season": {"season": "시즌"},
        "/export match season": {"season": "시즌"},
    }


def test_runtime_installs_parameter_name_translator_before_guild_sync() -> None:
    async def scenario() -> tuple[list[tuple[object, int]], int]:
        client = UmaSt2DiscordClient(
            configured_guild_id=987654321,
            command_groups=(app_commands.Group(name="example", description="Example"),),
        )
        observed: list[tuple[object, int]] = []

        async def sync_side_effect(*, guild: discord.Object) -> list[object]:
            observed.append((client.tree.translator, guild.id))
            return []

        sync = AsyncMock(side_effect=sync_side_effect)
        client.tree.sync = sync
        try:
            await client.setup_hook()
            return observed, sync.await_count
        finally:
            await client.close()

    observed, sync_count = asyncio.run(scenario())

    assert sync_count == 1
    assert len(observed) == 1
    assert isinstance(observed[0][0], KoreanParameterNameTranslator)
    assert observed[0][1] == 987654321
