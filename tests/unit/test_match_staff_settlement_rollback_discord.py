"""Discord terminal Match settlement rollback adapter tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from tests.unit.test_match_settlement_rollback import _committed, _target
from uma_st2.adapters.discord import (
    MatchSettlementRollbackDiscordAdapter,
    MatchSettlementRollbackPreview,
    MatchSettlementRollbackPreviewView,
    MatchStaffInteractionContext,
    match_settlement_rollback_autocomplete_choices,
)
from uma_st2.application.match import (
    MatchSettlementRollbackTargetChoice,
    RollbackMatchSettlement,
    build_match_settlement_rollback_plan,
)


class RecordingQueries:
    def __init__(self) -> None:
        self.plan = build_match_settlement_rollback_plan(_target())
        self.calls: list[tuple[object, ...]] = []

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchSettlementRollbackTargetChoice, ...]:
        self.calls.append(("search", search, limit))
        return (
            MatchSettlementRollbackTargetChoice(
                self.plan.target.match_id,
                self.plan.target.match_name,
                datetime(2026, 8, 28, 14, 0, tzinfo=UTC),
                len(self.plan.target.bets),
            ),
        )

    def get_preview(self, *, match_id: int):  # type: ignore[no-untyped-def]
        self.calls.append(("get", match_id))
        return self.plan


class RecordingCommands:
    def __init__(self) -> None:
        self.calls: list[RollbackMatchSettlement] = []

    def rollback_settlement(self, command: RollbackMatchSettlement):  # type: ignore[no-untyped-def]
        self.calls.append(command)
        return _committed(build_match_settlement_rollback_plan(_target()), command)


class RecordingAuthorization:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []

    async def __call__(self, interaction: object, command_name: str) -> bool:
        self.calls.append((interaction, command_name))
        return True


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


@dataclass
class RecordingFollowup:
    messages: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    async def send(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))


class RecordingSourceView:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class RecordingInteraction:
    def __init__(
        self,
        *,
        interaction_id: int = 555,
        user_id: int = 123,
        guild_id: int = 987,
        channel_id: int = 654,
        fail_edit: bool = False,
    ) -> None:
        self.id = interaction_id
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.response = RecordingResponse()
        self.followup = RecordingFollowup()
        self.edits: list[dict[str, object]] = []
        self.fail_edit = fail_edit

    async def edit_original_response(self, **kwargs: object) -> None:
        if self.fail_edit:
            raise RuntimeError("delivery failed")
        self.edits.append(kwargs)


async def _inline[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    return operation()


def _adapter() -> tuple[
    MatchSettlementRollbackDiscordAdapter,
    RecordingQueries,
    RecordingCommands,
    RecordingAuthorization,
]:
    queries = RecordingQueries()
    commands = RecordingCommands()
    authorization = RecordingAuthorization()
    return (
        MatchSettlementRollbackDiscordAdapter(
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


def test_target_handoff_builds_private_zero_write_danger_preview() -> None:
    adapter, queries, commands, authorization = _adapter()
    interaction = RecordingInteraction()
    context = MatchStaffInteractionContext.from_interaction(interaction)
    source_view = RecordingSourceView()

    asyncio.run(
        adapter.preview_rollback(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            reason="공식 결과 오류",
            context=context,
            source_view=source_view,  # type: ignore[arg-type]
        )
    )

    assert interaction.response.defers == [{"thinking": False}]
    assert authorization.calls == [(interaction, "match.staff.settlement-rollback")]
    assert source_view.stopped
    assert queries.calls == [("get", 71)]
    assert commands.calls == []
    view = interaction.edits[0]["view"]
    assert isinstance(view, MatchSettlementRollbackPreviewView)
    rendered = _layout_text(view)
    assert "룸매치 정산 롤백 Preview" in rendered
    assert "original stake" in rendered
    assert "terminal `voided`" in rendered
    assert "자동 삭제되지 않습니다" in rendered


def test_autocomplete_is_bounded_and_mention_safe() -> None:
    targets = tuple(
        MatchSettlementRollbackTargetChoice(
            index,
            "@everyone " + "긴이름" * 40,
            datetime(2026, 8, 28, 14, 0, tzinfo=UTC),
            3,
        )
        for index in (1, 2)
    )

    choices = match_settlement_rollback_autocomplete_choices(targets)

    assert len(choices) == 2
    assert all(len(choice.name) <= 100 for choice in choices)
    assert all("@everyone" not in choice.name for choice in choices)
    assert all("ID" in choice.name for choice in choices)


def test_final_confirm_uses_preview_fingerprint_and_final_interaction_key_once() -> None:
    adapter, queries, commands, authorization = _adapter()
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    view = MatchSettlementRollbackPreviewView(
        adapter=adapter,
        context=context,
        preview=MatchSettlementRollbackPreview(queries.plan, "공식 결과 오류"),
    )
    interaction = RecordingInteraction(interaction_id=991)

    asyncio.run(_layout_button(view, label="정산 롤백 확정").callback(interaction))  # type: ignore[arg-type]

    assert interaction.response.defers == [{"thinking": False}]
    assert authorization.calls == [(interaction, "match.staff.settlement-rollback")]
    assert len(commands.calls) == 1
    command = commands.calls[0]
    assert command.idempotency_key == "match-settlement-rollback:991"
    assert command.expected_rollback_fingerprint == queries.plan.rollback_fingerprint
    assert command.reason == "공식 결과 오류"
    assert command.actor_discord_user_id == "123"
    assert command.guild_id == "987"
    assert "룸매치 정산 롤백 완료" in _layout_text(interaction.edits[0]["view"])

    duplicate = RecordingInteraction(interaction_id=992)
    asyncio.run(view.confirm(duplicate))  # type: ignore[arg-type]
    assert len(commands.calls) == 1
    assert duplicate.response.defers == [{"thinking": False}]


def test_cancel_and_context_mismatch_are_zero_write() -> None:
    adapter, queries, commands, authorization = _adapter()
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    view = MatchSettlementRollbackPreviewView(
        adapter=adapter,
        context=context,
        preview=MatchSettlementRollbackPreview(queries.plan, "공식 결과 오류"),
    )
    cancel = RecordingInteraction(interaction_id=700)

    asyncio.run(_layout_button(view, label="취소").callback(cancel))  # type: ignore[arg-type]

    assert commands.calls == []
    assert authorization.calls == [(cancel, "match.staff.settlement-rollback")]
    assert cancel.response.defers == [{"thinking": False}]
    assert "DB에는 기록되지 않았습니다" in _layout_text(cancel.edits[0]["view"])

    mismatch_view = MatchSettlementRollbackPreviewView(
        adapter=adapter,
        context=context,
        preview=MatchSettlementRollbackPreview(queries.plan, "공식 결과 오류"),
    )
    mismatch = RecordingInteraction(interaction_id=701, user_id=999)
    asyncio.run(mismatch_view.confirm(mismatch))  # type: ignore[arg-type]
    assert commands.calls == []
    assert mismatch.response.messages
    assert not mismatch_view.is_finished()
    assert mismatch.response.defers == []


def test_delivery_failure_after_source_retirement_sends_fresh_entry_notice() -> None:
    adapter, queries, commands, _ = _adapter()
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    source_view = RecordingSourceView()
    interaction = RecordingInteraction(fail_edit=True)

    asyncio.run(
        adapter.preview_rollback(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            reason="공식 결과 오류",
            context=context,
            source_view=source_view,  # type: ignore[arg-type]
        )
    )

    assert source_view.stopped
    assert commands.calls == []
    assert queries.calls == [("get", 71)]
    assert interaction.followup.messages
    assert "/match staff settlement" in interaction.followup.messages[0][0]
