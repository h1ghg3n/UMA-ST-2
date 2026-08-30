from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

import discord
from discord import app_commands

from umacircle_bot.adapters.discord.common import run_blocking_application
from umacircle_bot.domain.errors import DomainError
from umacircle_bot.logging_safety import log_sanitized_exception
from umacircle_bot.services.autocomplete_queries import AutocompleteChoice
from umacircle_bot.services.dtos import MatchBetDTO
from umacircle_bot.services.match_reporting import GameAccountRatingDTO
from umacircle_bot.services.race_queries import OpenMatchRace

_RATING_SCOREBOARD_FIELD_LIMIT = 1024
_RATING_SCOREBOARD_ROW_LIMIT = 25


class PrepareCommandPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        *,
        defer: bool = True,
    ) -> object | None: ...


class AutocompleteAuthorizedPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
    ) -> bool: ...


class PlaceMatchBetPort(Protocol):
    def __call__(
        self,
        discord_user_id: str,
        game_account_id: int | None,
        race_id: int,
        bet_type: str,
        numbers: tuple[int, ...],
        amount: int,
        interaction_id: str,
    ) -> MatchBetDTO: ...


class SendUserErrorPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        prefix: str,
        error: Exception,
    ) -> None: ...


class SendInteractionCommandErrorPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
    ) -> None: ...


class SendFollowupPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        content: str,
        *,
        response_kind: str = "success",
        embed: discord.Embed | None = None,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class MatchMemberAdapterPorts:
    prepare_command: PrepareCommandPort
    autocomplete_authorized: AutocompleteAuthorizedPort
    query_races: Callable[[], Sequence[OpenMatchRace]]
    query_ratings: Callable[[], Sequence[GameAccountRatingDTO]]
    query_bet_races: Callable[[str], tuple[AutocompleteChoice, ...]]
    query_bet_accounts: Callable[[str, str], tuple[AutocompleteChoice, ...]]
    place_bet: PlaceMatchBetPort
    send_user_error: SendUserErrorPort
    send_internal_error: SendInteractionCommandErrorPort
    send_followup: SendFollowupPort
    correlation_id: Callable[[object], str]
    interaction_user_id: Callable[[object], str]
    safe_text: Callable[[str], str]
    bounded_message: Callable[[list[str]], str]
    logger: logging.Logger


class MatchMemberAdapter:
    def __init__(self, ports: MatchMemberAdapterPorts) -> None:
        self.ports = ports

    def create_command_group(
        self,
        *,
        staff_group: app_commands.Group | None = None,
    ) -> MatchMemberCommandGroup:
        return MatchMemberCommandGroup(adapter=self, staff_group=staff_group)

    async def list_races(self, interaction: discord.Interaction) -> None:
        command_name = "match.races"
        if await self.ports.prepare_command(interaction, command_name) is None:
            return
        try:
            races = await run_blocking_application(self.ports.query_races)
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        content = self.format_races(races) if races else "현재 베팅 가능한 룸매치 레이스가 없습니다."
        await self.ports.send_followup(interaction, command_name, content)

    async def list_ratings(self, interaction: discord.Interaction) -> None:
        command_name = "match.ratings"
        if await self.ports.prepare_command(interaction, command_name) is None:
            return
        try:
            ratings = await run_blocking_application(self.ports.query_ratings)
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return

        if not ratings:
            await self.ports.send_followup(
                interaction,
                command_name,
                "현재 표시할 룸매치 Rating 기록이 없습니다.",
            )
            return

        for embed in self.format_rating_scoreboard_pages(ratings):
            delivered = await self.ports.send_followup(
                interaction,
                command_name,
                "\u200b",
                embed=embed,
            )
            if not delivered:
                return

    async def place_bet(
        self,
        interaction: discord.Interaction,
        *,
        race_id: int,
        bet_type: str,
        numbers: str,
        amount: int,
        game_account_id: int | None = None,
    ) -> None:
        command_name = "match.bet"
        if await self.ports.prepare_command(interaction, command_name) is None:
            return

        discord_user_id = str(interaction.user.id)
        interaction_id = str(interaction.id)
        try:
            parsed_numbers = tuple(parse_match_numbers(numbers))
            bet = await run_blocking_application(
                lambda: self.ports.place_bet(
                    discord_user_id,
                    game_account_id,
                    race_id,
                    bet_type,
                    parsed_numbers,
                    amount,
                    interaction_id,
                )
            )
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "베팅하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return

        normalized_numbers = ",".join(str(number) for number in bet.numbers)
        await self.ports.send_followup(
            interaction,
            command_name,
            f"베팅 #{bet.id}: {bet.bet_type} {normalized_numbers} / 서클 포인트 {bet.amount}",
        )

    async def autocomplete_bet_race(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        command_name = "match.bet"
        if not await self.ports.autocomplete_authorized(interaction, command_name):
            return []
        try:
            choices = await run_blocking_application(lambda: self.ports.query_bet_races(current))
        except Exception:
            log_sanitized_exception(
                self.ports.logger,
                "discord autocomplete failed correlation_id=%s command=%s actor_id=%s",
                self.ports.correlation_id(interaction),
                command_name,
                self.ports.interaction_user_id(interaction),
            )
            return []
        return [
            app_commands.Choice(
                name=choice.label[:100],
                value=choice.value,
            )
            for choice in choices[:25]
        ]

    async def autocomplete_bet_account(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        command_name = "match.bet"
        if not await self.ports.autocomplete_authorized(interaction, command_name):
            return []
        discord_user_id = str(interaction.user.id)
        try:
            choices = await run_blocking_application(lambda: self.ports.query_bet_accounts(discord_user_id, current))
        except Exception:
            log_sanitized_exception(
                self.ports.logger,
                "discord autocomplete failed correlation_id=%s command=%s actor_id=%s",
                self.ports.correlation_id(interaction),
                command_name,
                self.ports.interaction_user_id(interaction),
            )
            return []
        return [app_commands.Choice(name=choice.label[:100], value=choice.value) for choice in choices[:25]]

    def format_races(self, races: Sequence[OpenMatchRace]) -> str:
        lines = ["베팅 가능한 룸매치 레이스"]
        for race in races:
            starts_at = race.starts_at.isoformat() if race.starts_at is not None else "시간 미정"
            participant_count = f" / {race.participant_count}명" if race.participant_count is not None else ""
            name = self.ports.safe_text(race.name[:100])
            lines.append(f"#{race.race_id} {name} / {starts_at}{participant_count}")
        return self.ports.bounded_message(lines)

    def format_rating_scoreboard_pages(
        self,
        ratings: Sequence[GameAccountRatingDTO],
    ) -> tuple[discord.Embed, ...]:
        rows = tuple(self._format_rating_scoreboard_row(rating) for rating in ratings)
        page_rows: list[list[tuple[str, str, str]]] = []
        current: list[tuple[str, str, str]] = []
        for row in rows:
            candidate = [*current, row]
            if current and not _rating_scoreboard_rows_fit(candidate):
                page_rows.append(current)
                current = [row]
            else:
                current = candidate
        if current:
            page_rows.append(current)

        page_count = len(page_rows)
        embeds: list[discord.Embed] = []
        for page_number, page in enumerate(page_rows, start=1):
            embed = discord.Embed(title=f"룸매치 Rating 순위표 · {page_number}/{page_count}")
            embed.add_field(
                name="이름",
                value="\n".join(row[0] for row in page),
                inline=True,
            )
            embed.add_field(
                name="Rating",
                value="\n".join(row[1] for row in page),
                inline=True,
            )
            embed.add_field(
                name="순위",
                value="\n".join(row[2] for row in page),
                inline=True,
            )
            embeds.append(embed)
        return tuple(embeds)

    def _format_rating_scoreboard_row(
        self,
        rating: GameAccountRatingDTO,
    ) -> tuple[str, str, str]:
        owner_name = rating.current_owner_display_name or "연결 없음"
        normalized_owner_name = owner_name.replace("\r", " ").replace("\n", " ")
        safe_owner_name = self.ports.safe_text(normalized_owner_name[:100])
        return (
            safe_owner_name,
            f"{rating.current_rating:.1f}",
            str(rating.competition_rank),
        )


class MatchMemberCommandGroup(app_commands.Group):
    def __init__(
        self,
        *,
        adapter: MatchMemberAdapter,
        staff_group: app_commands.Group | None = None,
    ) -> None:
        super().__init__(name="match", description="룸매치 레이스와 베팅 기능입니다.")
        self._adapter = adapter
        if staff_group is not None:
            self.add_command(staff_group)

    async def bet_race_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        return await self._adapter.autocomplete_bet_race(interaction, current)

    async def bet_account_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        return await self._adapter.autocomplete_bet_account(interaction, current)

    @app_commands.command(name="races", description="베팅 가능한 룸매치 레이스를 조회합니다.")
    async def races(self, interaction: discord.Interaction) -> None:
        await self._adapter.list_races(interaction)

    @app_commands.command(name="ratings", description="현재 룸매치 Rating 순위표를 조회합니다.")
    async def ratings(self, interaction: discord.Interaction) -> None:
        await self._adapter.list_ratings(interaction)

    @app_commands.command(name="bet", description="등록 PID로 서클 포인트를 베팅합니다.")
    @app_commands.describe(
        race_id="/match races에 표시된 레이스 ID",
        bet_type="win, quinella, trio 중 하나",
        numbers="번호를 하이픈 또는 쉼표로 구분 (예: 1-2-3)",
        amount="베팅할 서클 포인트",
        game_account_id="복수 PID가 연결된 경우 베팅 provenance로 사용할 계정",
    )
    @app_commands.autocomplete(
        race_id=bet_race_autocomplete,
        game_account_id=bet_account_autocomplete,
    )
    async def bet(
        self,
        interaction: discord.Interaction,
        race_id: int,
        bet_type: str,
        numbers: str,
        amount: int,
        game_account_id: int | None = None,
    ) -> None:
        await self._adapter.place_bet(
            interaction,
            game_account_id=game_account_id,
            race_id=race_id,
            bet_type=bet_type,
            numbers=numbers,
            amount=amount,
        )


def create_match_member_command_group(
    ports: MatchMemberAdapterPorts,
    *,
    staff_group: app_commands.Group | None = None,
) -> MatchMemberCommandGroup:
    return MatchMemberAdapter(ports).create_command_group(staff_group=staff_group)


def parse_match_numbers(value: str) -> list[int]:
    if not isinstance(value, str):
        raise ValueError("bet numbers must be text")
    parts = [part.strip() for part in value.replace(",", "-").split("-")]
    if not parts or any(not part or not part.isascii() or not part.isdigit() for part in parts):
        raise ValueError("bet numbers must be ASCII positive integers separated by commas or hyphens")
    return [int(part) for part in parts]


def _rating_scoreboard_rows_fit(rows: Sequence[tuple[str, str, str]]) -> bool:
    if len(rows) > _RATING_SCOREBOARD_ROW_LIMIT:
        return False
    return all(len("\n".join(row[column] for row in rows)) <= _RATING_SCOREBOARD_FIELD_LIMIT for column in range(3))
