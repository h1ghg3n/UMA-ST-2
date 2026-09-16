"""Discord adapter tests for private Room Match Rating standings."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from decimal import Decimal
from types import SimpleNamespace

from uma_st2.adapters.discord import MatchRatingDiscordAdapter, format_match_rating_pages
from uma_st2.application.rating import (
    MatchRatingFilterConflictError,
    MatchRatingInvalidSourceError,
    MatchRatingResultTooLargeError,
    MatchRatingStanding,
)


def _standing(
    index: int = 1,
    *,
    rank: int = 1,
    name: str | None = "테스트 Persona",
    rating: str = "1500.150000000000000000",
) -> MatchRatingStanding:
    return MatchRatingStanding(
        game_account_id=index,
        current_owner_persona_id=(f"00000000-0000-0000-0000-{index:012d}" if name is not None else None),
        current_owner_display_name=name,
        current_rating=Decimal(rating),
        competition_rank=rank,
    )


class RecordingQueries:
    def __init__(
        self,
        *,
        rows: tuple[MatchRatingStanding, ...] = (_standing(),),
        error: Exception | None = None,
    ) -> None:
        self.rows = rows
        self.error = error
        self.calls: list[tuple[int | None, str | None]] = []

    def list_ratings(
        self,
        *,
        rank: int | None = None,
        persona: str | None = None,
    ) -> tuple[MatchRatingStanding, ...]:
        self.calls.append((rank, persona))
        if self.error is not None:
            raise self.error
        return self.rows


class RecordingPreparation:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[object, str, bool]] = []

    async def __call__(self, interaction: object, command_name: str, *, ephemeral: bool) -> bool:
        self.calls.append((interaction, command_name, ephemeral))
        return self.allowed


class RecordingFollowup:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send(self, content: str, **kwargs: object) -> None:
        self.messages.append({"content": content, **kwargs})


class RecordingInteraction:
    def __init__(self) -> None:
        self.id = 555
        self.user = SimpleNamespace(id=123)
        self.guild_id = 987
        self.channel_id = 654
        self.edits: list[dict[str, object]] = []
        self.followup = RecordingFollowup()

    async def edit_original_response(self, **kwargs: object) -> None:
        self.edits.append(kwargs)


async def _inline[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    return operation()


def _adapter(
    *,
    rows: tuple[MatchRatingStanding, ...] = (_standing(),),
    error: Exception | None = None,
    allowed: bool = True,
) -> tuple[MatchRatingDiscordAdapter, RecordingQueries, RecordingPreparation]:
    queries = RecordingQueries(rows=rows, error=error)
    preparation = RecordingPreparation(allowed=allowed)
    return (
        MatchRatingDiscordAdapter(
            queries=queries,  # type: ignore[arg-type]
            prepare_command=preparation,
            blocking_runner=_inline,  # type: ignore[arg-type]
        ),
        queries,
        preparation,
    )


def test_renderer_uses_exact_three_fields_preserved_ranks_and_safe_names() -> None:
    standings = (
        _standing(1, rank=1, name="첫째\n@everyone", rating="1600.150000000000000000"),
        _standing(2, rank=1, name=None, rating="1600.150000000000000000"),
        _standing(3, rank=3, name="셋째", rating="1499.940000000000000000"),
    )

    pages = format_match_rating_pages(standings)

    assert len(pages) == 1
    embed = pages[0]
    assert embed.title == "룸매치 Rating 순위표 · 1/1"
    assert [field.name for field in embed.fields] == ["이름", "Rating", "순위"]
    assert "@everyone" not in embed.fields[0].value
    assert "첫째" in embed.fields[0].value
    assert "\n@" not in embed.fields[0].value
    assert "연결 없음" in embed.fields[0].value
    assert embed.fields[1].value.splitlines() == ["1600.2", "1600.2", "1499.9"]
    assert embed.fields[2].value.splitlines() == ["1", "1", "3"]


def test_renderer_pages_all_long_rows_without_omission() -> None:
    standings = tuple(_standing(index, rank=index, name=f"Persona {index} " + "긴이름" * 28) for index in range(1, 31))

    pages = format_match_rating_pages(standings)

    assert len(pages) > 1
    names = "\n".join(embed.fields[0].value for embed in pages)
    ranks = "\n".join(embed.fields[2].value for embed in pages).splitlines()
    assert all(len(field.value) <= 1024 for embed in pages for field in embed.fields)
    assert all(f"Persona {index}" in names for index in range(1, 31))
    assert ranks == [str(index) for index in range(1, 31)]


def test_command_is_private_and_forwards_one_filter() -> None:
    adapter, queries, preparation = _adapter(rows=(_standing(rank=37),))
    interaction = RecordingInteraction()

    asyncio.run(adapter.list_ratings(interaction, persona="테스트"))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "match.ratings", True)]
    assert queries.calls == [(None, "테스트")]
    assert interaction.edits[0]["content"] == "\u200b"
    embed = interaction.edits[0]["embeds"][0]  # type: ignore[index]
    assert embed.fields[2].value == "37"


def test_command_delivers_every_page_as_private_followup() -> None:
    rows = tuple(_standing(index, rank=index, name="긴이름" * 33) for index in range(1, 31))
    adapter, _, _ = _adapter(rows=rows)
    interaction = RecordingInteraction()

    asyncio.run(adapter.list_ratings(interaction, rank=1))  # type: ignore[arg-type]

    assert len(interaction.edits) == 1
    assert interaction.followup.messages
    assert all(message["ephemeral"] is True for message in interaction.followup.messages)


def test_empty_result_uses_private_empty_copy() -> None:
    adapter, _, _ = _adapter(rows=())
    interaction = RecordingInteraction()

    asyncio.run(adapter.list_ratings(interaction))  # type: ignore[arg-type]

    assert "기록이 없습니다" in interaction.edits[0]["content"]  # type: ignore[operator]


def test_expected_query_errors_use_specific_private_copy() -> None:
    cases = (
        (MatchRatingFilterConflictError("conflict"), "하나만"),
        (MatchRatingResultTooLargeError("large"), "너무 많습니다"),
    )
    for error, expected in cases:
        adapter, _, _ = _adapter(error=error)
        interaction = RecordingInteraction()

        asyncio.run(adapter.list_ratings(interaction))  # type: ignore[arg-type]

        assert expected in interaction.edits[0]["content"]  # type: ignore[operator]


def test_invalid_source_uses_generic_private_error() -> None:
    adapter, _, _ = _adapter(error=MatchRatingInvalidSourceError("malformed"))
    interaction = RecordingInteraction()

    asyncio.run(adapter.list_ratings(interaction))  # type: ignore[arg-type]

    assert "참조 ID: `555`" in interaction.edits[0]["content"]  # type: ignore[operator]


def test_denied_preparation_stops_before_query() -> None:
    adapter, queries, preparation = _adapter(allowed=False)
    interaction = RecordingInteraction()

    asyncio.run(adapter.list_ratings(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "match.ratings", True)]
    assert queries.calls == []
    assert interaction.edits == []
