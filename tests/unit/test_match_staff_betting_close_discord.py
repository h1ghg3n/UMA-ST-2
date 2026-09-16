"""Discord native Match betting-close adapter tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from uma_st2.adapters.discord import (
    MatchBettingCloseDiscordAdapter,
    MatchBettingClosePreview,
    MatchBettingClosePreviewView,
    MatchStaffInteractionContext,
    format_match_betting_close_preview,
    match_betting_close_autocomplete_choices,
)
from uma_st2.application.match import (
    ClosedMatchBetting,
    CloseMatchBetting,
    MatchBettingClosePreviewTarget,
    MatchBettingCloseTargetChoice,
    StoredMatchBettingClosePublication,
)
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus
from uma_st2.domain.publication import PublicationStatus

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)


def _target(*, active_bet_count: int = 2, active_stake_total: int = 40) -> MatchBettingClosePreviewTarget:
    return MatchBettingClosePreviewTarget(
        match_id=71,
        match_name="제12회 @everyone 정기전",
        source_kind=MatchSourceKind.NATIVE_V2,
        status=MatchStatus.BETTING_OPEN,
        grade=MatchGrade.G1,
        scheduled_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        entry_count=3,
        active_bet_count=active_bet_count,
        active_stake_total=active_stake_total,
    )


class RecordingQueries:
    def __init__(self, target: MatchBettingClosePreviewTarget) -> None:
        self.target = target
        self.calls: list[tuple[object, ...]] = []

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchBettingCloseTargetChoice, ...]:
        self.calls.append(("search", search, limit))
        return (
            MatchBettingCloseTargetChoice(
                match_id=self.target.match_id,
                match_name=self.target.match_name,
                entry_count=self.target.entry_count,
                active_bet_count=self.target.active_bet_count,
            ),
        )

    def get_target(self, *, match_id: int) -> MatchBettingClosePreviewTarget:
        self.calls.append(("get", match_id))
        return self.target


class RecordingCommands:
    def __init__(self, target: MatchBettingClosePreviewTarget) -> None:
        self.target = target
        self.calls: list[CloseMatchBetting] = []

    def close_betting(self, command: CloseMatchBetting) -> ClosedMatchBetting:
        self.calls.append(command)
        return ClosedMatchBetting(
            match_id=self.target.match_id,
            match_name=self.target.match_name,
            status=MatchStatus.BETTING_CLOSED,
            closed_at=NOW,
            entry_count=self.target.entry_count,
            active_bet_count=self.target.active_bet_count,
            active_stake_total=self.target.active_stake_total,
            publication=StoredMatchBettingClosePublication(
                publication_id=91,
                event_key=f"match:{self.target.match_id}:betting-close:v1",
                payload_fingerprint="a" * 64,
                status=PublicationStatus.READY,
                target_channel_id="777777777",
            ),
        )


class RecordingAuthorization:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []

    async def __call__(self, interaction: object, command_name: str) -> bool:
        self.calls.append((interaction, command_name))
        return True


class RecordingSourceView:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


@dataclass
class RecordingResponse:
    messages: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    edits: list[dict[str, object]] = field(default_factory=list)
    defers: list[dict[str, object]] = field(default_factory=list)

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))

    async def edit_message(self, **kwargs: object) -> None:
        self.edits.append(kwargs)

    async def defer(self, **kwargs: object) -> None:
        self.defers.append(kwargs)


class RecordingInteraction:
    def __init__(
        self,
        *,
        interaction_id: int = 555,
        user_id: int = 123,
        guild_id: int = 987,
        channel_id: int = 654,
    ) -> None:
        self.id = interaction_id
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.response = RecordingResponse()
        self.edits: list[dict[str, object]] = []

    async def edit_original_response(self, **kwargs: object) -> None:
        self.edits.append(kwargs)


async def _inline[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    return operation()


def _adapter(
    target: MatchBettingClosePreviewTarget,
) -> tuple[
    MatchBettingCloseDiscordAdapter,
    RecordingQueries,
    RecordingCommands,
    RecordingAuthorization,
]:
    queries = RecordingQueries(target)
    commands = RecordingCommands(target)
    authorization = RecordingAuthorization()
    return (
        MatchBettingCloseDiscordAdapter(
            queries=queries,  # type: ignore[arg-type]
            commands=commands,  # type: ignore[arg-type]
            authorize_autocomplete=authorization,
            authorize_interaction=authorization,
            blocking_runner=_inline,  # type: ignore[arg-type]
        ),
        queries,
        commands,
        authorization,
    )


def _layout_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def _layout_button(view: discord.ui.LayoutView, *, label: str) -> discord.ui.Button:
    return next(item for item in view.walk_children() if isinstance(item, discord.ui.Button) and item.label == label)


def test_target_handoff_builds_private_zero_write_preview_in_same_message() -> None:
    target = _target(active_bet_count=0, active_stake_total=0)
    adapter, queries, commands, _ = _adapter(target)
    interaction = RecordingInteraction()
    context = MatchStaffInteractionContext.from_interaction(interaction)
    source_view = RecordingSourceView()

    asyncio.run(
        adapter.preview_close(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            context=context,
            source_view=source_view,
        )
    )

    assert interaction.response.defers == [{"thinking": False}]
    assert source_view.stopped
    assert queries.calls == [("get", 71)]
    assert commands.calls == []
    view = interaction.edits[0]["view"]
    assert isinstance(view, MatchBettingClosePreviewView)
    rendered = _layout_text(view)
    assert "베팅 마감 Preview" in rendered
    assert "Bet 0건 상태에서도 마감" in rendered
    assert "complete 최종 배당률 공지 intent" in rendered
    assert "@everyone" not in rendered


def test_autocomplete_is_bounded_and_mention_safe() -> None:
    targets = tuple(
        MatchBettingCloseTargetChoice(
            match_id=index,
            match_name="@everyone " + "긴이름" * 40,
            entry_count=3,
            active_bet_count=2,
        )
        for index in (1, 2)
    )

    choices = match_betting_close_autocomplete_choices(targets)

    assert len(choices) == 2
    assert all(len(choice.name) <= 100 for choice in choices)
    assert all("@everyone" not in choice.name for choice in choices)
    assert all("ID" in choice.name for choice in choices)


def test_final_confirm_uses_final_interaction_key_and_committed_aggregate_once() -> None:
    target = _target()
    adapter, _, commands, _ = _adapter(target)
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    view = MatchBettingClosePreviewView(
        adapter=adapter,
        context=context,
        preview=MatchBettingClosePreview(target),
    )
    interaction = RecordingInteraction(interaction_id=991)

    asyncio.run(_layout_button(view, label="베팅 마감 확정").callback(interaction))  # type: ignore[arg-type]

    assert interaction.response.defers == [{"thinking": False}]
    assert len(commands.calls) == 1
    command = commands.calls[0]
    assert command.idempotency_key == "match-betting-close:991"
    assert command.actor_discord_user_id == "123"
    assert command.guild_id == "987"
    receipt = interaction.edits[0]["view"]
    assert isinstance(receipt, discord.ui.LayoutView)
    assert "베팅 마감 완료" in _layout_text(receipt)
    assert "2건 · 40 Circle Point" in _layout_text(receipt)
    assert "Publication: `ready` · ID `91`" in _layout_text(receipt)
    assert "별도 메시지" in _layout_text(receipt)

    duplicate = RecordingInteraction(interaction_id=992)
    asyncio.run(
        adapter.confirm_close(  # type: ignore[arg-type]
            duplicate,
            context=context,
            preview=view.preview,
            source_view=view,
        )
    )
    duplicate_payload = duplicate.edits[0]
    duplicate_terminal = duplicate_payload["view"]
    assert len(commands.calls) == 1
    assert duplicate_payload["content"] is None
    assert duplicate_payload["embeds"] == []
    assert duplicate_payload["attachments"] == []
    assert isinstance(duplicate_terminal, discord.ui.LayoutView)
    assert "이미 처리 중이거나 완료되었습니다" in _layout_text(duplicate_terminal)
    assert not any(isinstance(item, discord.ui.Button) for item in duplicate_terminal.walk_children())


def test_cancel_and_context_mismatch_are_zero_write() -> None:
    target = _target()
    adapter, _, commands, authorization = _adapter(target)
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    view = MatchBettingClosePreviewView(
        adapter=adapter,
        context=context,
        preview=MatchBettingClosePreview(target),
    )
    cancel = RecordingInteraction(interaction_id=700)

    asyncio.run(_layout_button(view, label="취소").callback(cancel))  # type: ignore[arg-type]

    assert commands.calls == []
    assert authorization.calls == [(cancel, "match.staff.betting-close")]
    assert cancel.response.defers == [{"thinking": False}]
    assert "DB에는 기록되지 않았습니다" in _layout_text(cancel.edits[0]["view"])

    mismatch = RecordingInteraction(interaction_id=701, user_id=999)
    asyncio.run(
        adapter.confirm_close(  # type: ignore[arg-type]
            mismatch,
            context=context,
            preview=view.preview,
            source_view=view,
        )
    )
    assert commands.calls == []
    assert mismatch.response.messages
    assert "@everyone" not in format_match_betting_close_preview(view.preview)
