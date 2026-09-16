"""Discord native whole-Match cancellation adapter tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from uma_st2.adapters.discord import (
    MatchCancellationDiscordAdapter,
    MatchCancellationPreview,
    MatchCancellationPreviewView,
    MatchStaffInteractionContext,
    format_match_cancellation_preview,
    match_cancellation_autocomplete_choices,
)
from uma_st2.application.match import (
    CancelledMatch,
    CancelMatch,
    MatchCancellationPreviewTarget,
    MatchCancellationRefund,
    MatchCancellationTargetChoice,
    StoredMatchRefundPublication,
)
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus
from uma_st2.domain.publication import PublicationStatus

NOW = datetime(2026, 8, 28, 11, 0, tzinfo=UTC)


def _target() -> MatchCancellationPreviewTarget:
    return MatchCancellationPreviewTarget(
        match_id=71,
        match_name="제12회 @everyone 정기전",
        source_kind=MatchSourceKind.NATIVE_V2,
        status=MatchStatus.BETTING_CLOSED,
        terminal_reason=None,
        grade=MatchGrade.G1,
        scheduled_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        entry_count=3,
        active_bet_count=2,
        active_stake_total=40,
        affected_persona_count=1,
    )


class RecordingQueries:
    def __init__(self, target: MatchCancellationPreviewTarget) -> None:
        self.target = target
        self.calls: list[tuple[object, ...]] = []

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchCancellationTargetChoice, ...]:
        self.calls.append(("search", search, limit))
        return (
            MatchCancellationTargetChoice(
                match_id=self.target.match_id,
                match_name=self.target.match_name,
                status=self.target.status,
                active_bet_count=self.target.active_bet_count,
            ),
        )

    def get_target(self, *, match_id: int) -> MatchCancellationPreviewTarget:
        self.calls.append(("get", match_id))
        return self.target


class RecordingCommands:
    def __init__(self, target: MatchCancellationPreviewTarget) -> None:
        self.target = target
        self.calls: list[CancelMatch] = []

    def cancel_match(self, command: CancelMatch) -> CancelledMatch:
        self.calls.append(command)
        return CancelledMatch(
            match_id=self.target.match_id,
            match_name=self.target.match_name,
            previous_status=self.target.status,
            status=MatchStatus.CANCELLED,
            reason=command.reason,
            cancelled_at=NOW,
            entry_count=self.target.entry_count,
            cancelled_bet_count=2,
            refund_total=40,
            refunds=(
                MatchCancellationRefund(
                    persona_id="persona-a",
                    bet_ids=(101, 102),
                    amount=40,
                    balance_before=100,
                    balance_after=140,
                    point_transaction_id=901,
                ),
            ),
            publication=StoredMatchRefundPublication(
                publication_id=777,
                event_key=f"match:{self.target.match_id}:bet-refund:v1",
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


def _adapter() -> tuple[
    MatchCancellationDiscordAdapter,
    RecordingQueries,
    RecordingCommands,
    RecordingAuthorization,
]:
    target = _target()
    queries = RecordingQueries(target)
    commands = RecordingCommands(target)
    authorization = RecordingAuthorization()
    return (
        MatchCancellationDiscordAdapter(
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
    adapter, queries, commands, _ = _adapter()
    interaction = RecordingInteraction()
    context = MatchStaffInteractionContext.from_interaction(interaction)
    source_view = RecordingSourceView()

    asyncio.run(
        adapter.preview_cancellation(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            reason=" 공식 경기 취소 ",
            context=context,
            source_view=source_view,
        )
    )

    assert interaction.response.defers == [{"thinking": False}]
    assert source_view.stopped
    assert queries.calls == [("get", 71)]
    assert commands.calls == []
    view = interaction.edits[0]["view"]
    assert isinstance(view, MatchCancellationPreviewView)
    rendered = _layout_text(view)
    assert "전체 취소 Preview" in rendered
    assert "40 Circle Point" in rendered
    assert "기존 Entry, ResultSubmission과 확정 Result는 삭제하지 않습니다" in rendered
    assert "@everyone" not in rendered


def test_autocomplete_is_bounded_and_mention_safe() -> None:
    targets = tuple(
        MatchCancellationTargetChoice(
            match_id=index,
            match_name="@everyone " + "긴이름" * 40,
            status=MatchStatus.BETTING_CLOSED,
            active_bet_count=2,
        )
        for index in (1, 2)
    )

    choices = match_cancellation_autocomplete_choices(targets)

    assert len(choices) == 2
    assert all(len(choice.name) <= 100 for choice in choices)
    assert all("@everyone" not in choice.name for choice in choices)
    assert all("ID" in choice.name for choice in choices)


def test_final_confirm_uses_final_interaction_key_and_committed_receipt_once() -> None:
    adapter, _, commands, _ = _adapter()
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    view = MatchCancellationPreviewView(
        adapter=adapter,
        context=context,
        preview=MatchCancellationPreview(_target(), "공식 경기 취소"),
    )
    interaction = RecordingInteraction(interaction_id=991)

    asyncio.run(_layout_button(view, label="전체 취소 확정").callback(interaction))  # type: ignore[arg-type]

    assert interaction.response.defers == [{"thinking": False}]
    assert len(commands.calls) == 1
    command = commands.calls[0]
    assert command.idempotency_key == "match-cancel:991"
    assert command.reason == "공식 경기 취소"
    assert command.actor_discord_user_id == "123"
    receipt = interaction.edits[0]["view"]
    assert isinstance(receipt, discord.ui.LayoutView)
    assert "전체 취소 완료" in _layout_text(receipt)
    assert "2건" in _layout_text(receipt)

    duplicate = RecordingInteraction(interaction_id=992)
    asyncio.run(
        adapter.confirm_cancellation(  # type: ignore[arg-type]
            duplicate,
            context=context,
            preview=view.preview,
            source_view=view,
        )
    )
    assert len(commands.calls) == 1


def test_cancel_and_context_mismatch_are_zero_write() -> None:
    adapter, _, commands, authorization = _adapter()
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    view = MatchCancellationPreviewView(
        adapter=adapter,
        context=context,
        preview=MatchCancellationPreview(_target(), "공식 경기 취소"),
    )
    cancel = RecordingInteraction(interaction_id=700)

    asyncio.run(_layout_button(view, label="취소").callback(cancel))  # type: ignore[arg-type]

    assert commands.calls == []
    assert authorization.calls == [(cancel, "match.staff.race-cancel")]
    assert cancel.response.defers == [{"thinking": False}]
    assert "DB에는 기록되지 않았습니다" in _layout_text(cancel.edits[0]["view"])

    mismatch = RecordingInteraction(interaction_id=701, user_id=999)
    asyncio.run(
        adapter.confirm_cancellation(  # type: ignore[arg-type]
            mismatch,
            context=context,
            preview=view.preview,
            source_view=view,
        )
    )
    assert commands.calls == []
    assert mismatch.response.messages
    assert "@everyone" not in format_match_cancellation_preview(view.preview)


def test_empty_reason_is_treated_as_omitted_and_builds_preview() -> None:
    adapter, queries, commands, _ = _adapter()
    interaction = RecordingInteraction()
    context = MatchStaffInteractionContext.from_interaction(interaction)
    source_view = RecordingSourceView()

    asyncio.run(
        adapter.preview_cancellation(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            reason="   ",
            context=context,
            source_view=source_view,
        )
    )

    assert queries.calls == [("get", 71)]
    assert commands.calls == []
    rendered = _layout_text(interaction.edits[0]["view"])
    assert "환불 사유: 미입력 (선택)" in rendered
    assert "사유란을 표시하지 않습니다" in rendered
