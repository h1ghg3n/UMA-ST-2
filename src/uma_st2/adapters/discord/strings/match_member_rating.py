"""User-facing copy and rendering for current Match Rating standings."""

from __future__ import annotations

from decimal import Decimal

import discord

from uma_st2.application.rating import MAX_RATING_STANDING_ROWS, MatchRatingStanding

from ..common import safe_discord_text

RATING_EMPTY = "조건에 맞는 룸매치 Rating 기록이 없습니다."
RATING_FILTER_CONFLICT = "순위와 Persona 검색은 하나만 입력해 주세요."
RATING_FILTER_INVALID = "순위는 1 이상의 정수이고 Persona 검색어는 1~100자로 입력해 주세요."
RATING_RESULT_TOO_LARGE = "표시할 결과가 너무 많습니다. 순위 또는 Persona 검색 범위를 좁혀 주세요."

_EMBED_FIELD_VALUE_LIMIT = 1024


def _rating_row(standing: MatchRatingStanding) -> tuple[str, str, str]:
    owner_name = standing.current_owner_display_name or "연결 없음"
    owner_name = owner_name.replace("\r", " ").replace("\n", " ")
    return (
        safe_discord_text(owner_name, limit=100),
        format(Decimal(standing.current_rating), ".1f"),
        str(standing.competition_rank),
    )


def _rows_fit(rows: list[tuple[str, str, str]]) -> bool:
    return all(len("\n".join(row[column] for row in rows)) <= _EMBED_FIELD_VALUE_LIMIT for column in range(3))


def format_match_rating_pages(
    standings: tuple[MatchRatingStanding, ...],
) -> tuple[discord.Embed, ...]:
    """Render all standings as bounded three-column Discord embeds."""

    if not isinstance(standings, tuple) or any(not isinstance(row, MatchRatingStanding) for row in standings):
        raise ValueError("standings must contain MatchRatingStanding values.")
    if len(standings) > MAX_RATING_STANDING_ROWS:
        raise ValueError("Rating standings exceed the complete delivery capacity.")
    if not standings:
        return ()

    pages: list[list[tuple[str, str, str]]] = []
    current: list[tuple[str, str, str]] = []
    for row in map(_rating_row, standings):
        candidate = [*current, row]
        if current and not _rows_fit(candidate):
            pages.append(current)
            current = [row]
        else:
            current = candidate
        if not _rows_fit(current):
            raise ValueError("One Rating standing row exceeds the Discord embed bound.")
    pages.append(current)

    page_count = len(pages)
    embeds: list[discord.Embed] = []
    for page_number, rows in enumerate(pages, start=1):
        embed = discord.Embed(title=f"룸매치 Rating 순위표 · {page_number}/{page_count}")
        for field_name, column in (("이름", 0), ("Rating", 1), ("순위", 2)):
            embed.add_field(
                name=field_name,
                value="\n".join(row[column] for row in rows),
                inline=True,
            )
        embeds.append(embed)
    return tuple(embeds)
