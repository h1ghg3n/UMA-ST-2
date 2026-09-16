"""Discord native Match settlement adapter tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import discord

from uma_st2.adapters.discord import (
    MatchSettlementDiscordAdapter,
    MatchSettlementPreview,
    MatchSettlementPreviewView,
    MatchSettlementRatingSelectionDraft,
    MatchSettlementRatingSelectionView,
    MatchStaffInteractionContext,
    format_match_settlement_preview,
    match_settlement_autocomplete_choices,
)
from uma_st2.application.match import (
    MatchSettlementBet,
    MatchSettlementEntry,
    MatchSettlementPayout,
    MatchSettlementRating,
    MatchSettlementRatingSelectionTarget,
    MatchSettlementResultAuthority,
    MatchSettlementReward,
    MatchSettlementRuleReference,
    MatchSettlementTarget,
    MatchSettlementTargetChoice,
    MatchSettlementWallet,
    SettledMatch,
    SettleMatch,
    build_match_settlement_plan,
)
from uma_st2.domain.betting import BetType
from uma_st2.domain.match import MatchGrade, MatchSourceKind, MatchStatus
from uma_st2.domain.rating import RatingRule

NOW = datetime(2026, 8, 28, 13, 0, tzinfo=UTC)


def _target() -> MatchSettlementTarget:
    entries = tuple(
        MatchSettlementEntry(
            match_entry_id=10 + rank,
            entry_number=rank,
            game_account_id=100 + rank,
            owner_at_event_persona_id=f"persona-{rank}",
            game_account_name=f"trainer-{rank} @everyone",
            horse_name=f"horse-{rank}",
            affiliation_at_event="circle-a",
            rank=rank,
            rating_before=Decimal(100),
        )
        for rank in range(1, 4)
    )
    return MatchSettlementTarget(
        match_id=71,
        match_name="제12회 @everyone 정기전",
        source_kind=MatchSourceKind.NATIVE_V2,
        status=MatchStatus.RESULT_CONFIRMED,
        grade=MatchGrade.G1,
        scheduled_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        result=MatchSettlementResultAuthority(301, 2, "a" * 64),
        entries=entries,
        active_bets=(
            MatchSettlementBet(201, "persona-1", BetType.WIN, (11,), 10),
            MatchSettlementBet(202, "persona-2", BetType.QUINELLA, (11, 12), 20),
            MatchSettlementBet(203, "persona-3", BetType.TRIO, (11, 12, 13), 10),
        ),
        wallets=tuple(MatchSettlementWallet(f"persona-{rank}", rank * 100) for rank in range(1, 4)),
        rating_rule_version=MatchSettlementRuleReference(41, 3, "b" * 64),
        rating_rules=tuple(
            [
                RatingRule(MatchGrade.G1, 2, 1, Decimal("5")),
                RatingRule(MatchGrade.G1, 2, 2, Decimal("-2")),
            ]
            + [
                RatingRule(MatchGrade.G1, 3, rank, Decimal(delta))
                for rank, delta in enumerate(("10", "0", "-5"), start=1)
            ]
        ),
    )


def _committed() -> SettledMatch:
    plan = build_match_settlement_plan(_target())
    return SettledMatch(
        match_id=plan.target.match_id,
        match_name=plan.target.match_name,
        previous_status=plan.target.status,
        status=MatchStatus.SETTLED,
        grade=plan.target.grade,
        settled_at=NOW,
        result=plan.target.result,
        settlement_fingerprint=plan.settlement_fingerprint,
        active_bet_ids=tuple(bet.bet_id for bet in plan.target.active_bets),
        active_stake_total=plan.target.active_stake_total,
        applied_odds=plan.applied_odds,
        payouts=tuple(
            MatchSettlementPayout(item.persona_id, item.bet_ids, item.amount, 1000 + index)
            for index, item in enumerate(plan.payouts, start=1)
        ),
        rewards=tuple(
            MatchSettlementReward(
                item.persona_id,
                item.selected_match_entry_id,
                item.selected_game_account_id,
                item.selected_rank,
                item.suppressed_match_entry_ids,
                item.amount,
                2000 + index,
            )
            for index, item in enumerate(plan.rewards, start=1)
        ),
        rating_rule_version=plan.target.rating_rule_version,
        ratings=tuple(
            MatchSettlementRating(
                item.match_entry_id,
                item.entry_number,
                item.game_account_id,
                item.game_account_name,
                item.horse_name,
                item.affiliation_at_event,
                item.rank,
                item.rating_before,
                item.base_delta,
                item.adjustment_delta,
                item.amount,
                item.rating_after,
                3000 + index,
            )
            for index, item in enumerate(plan.ratings, start=1)
        ),
    )


class RecordingQueries:
    def __init__(self) -> None:
        self.plan = build_match_settlement_plan(_target())
        self.selection = MatchSettlementRatingSelectionTarget.from_settlement_target(self.plan.target)
        self.calls: list[tuple[object, ...]] = []

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchSettlementTargetChoice, ...]:
        self.calls.append(("search", search, limit))
        target = self.plan.target
        return (
            MatchSettlementTargetChoice(
                target.match_id,
                target.match_name,
                len(target.entries),
                len(target.active_bets),
            ),
        )

    def get_rating_selection(self, *, match_id: int) -> MatchSettlementRatingSelectionTarget:
        self.calls.append(("selection", match_id))
        return self.selection

    def get_preview(
        self,
        *,
        match_id: int,
        excluded_rating_entry_ids: tuple[int, ...] = (),
    ):  # type: ignore[no-untyped-def]
        self.calls.append(("get", match_id, excluded_rating_entry_ids))
        return build_match_settlement_plan(
            self.plan.target,
            excluded_rating_entry_ids=excluded_rating_entry_ids,
        )


class RecordingCommands:
    def __init__(self) -> None:
        self.calls: list[SettleMatch] = []

    def settle_match(self, command: SettleMatch) -> SettledMatch:
        self.calls.append(command)
        return _committed()


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
    MatchSettlementDiscordAdapter,
    RecordingQueries,
    RecordingCommands,
    RecordingAuthorization,
]:
    queries = RecordingQueries()
    commands = RecordingCommands()
    authorization = RecordingAuthorization()
    return (
        MatchSettlementDiscordAdapter(
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


def test_target_handoff_builds_private_zero_write_rating_selection() -> None:
    adapter, queries, commands, authorization = _adapter()
    interaction = RecordingInteraction()
    context = MatchStaffInteractionContext.from_interaction(interaction)
    source_view = RecordingSourceView()

    asyncio.run(
        adapter.preview_settlement(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            reason="공식 확정",
            context=context,
            source_view=source_view,  # type: ignore[arg-type]
        )
    )

    assert interaction.response.defers == [{"thinking": False}]
    assert authorization.calls == [(interaction, "match.staff.settlement")]
    assert source_view.stopped
    assert queries.calls == [("selection", 71)]
    assert commands.calls == []
    view = interaction.edits[0]["view"]
    assert isinstance(view, MatchSettlementRatingSelectionView)
    rendered = _layout_text(view)
    assert "룸매치 Rating 반영 선택" in rendered
    assert "공식 착순, Bet 판정과 착순 보상은 바뀌지 않습니다" in rendered
    assert "Rating 제외 0명 · 반영 3명" in rendered
    assert "@everyone" not in rendered


def test_autocomplete_is_bounded_and_mention_safe() -> None:
    targets = tuple(MatchSettlementTargetChoice(index, "@everyone " + "긴이름" * 40, 9, 3) for index in (1, 2))

    choices = match_settlement_autocomplete_choices(targets)

    assert len(choices) == 2
    assert all(len(choice.name) <= 100 for choice in choices)
    assert all("@everyone" not in choice.name for choice in choices)
    assert all("ID" in choice.name for choice in choices)


def test_rating_selection_is_adapter_local_and_builds_selected_preview() -> None:
    adapter, queries, commands, authorization = _adapter()
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    initial = MatchSettlementRatingSelectionView(
        adapter=adapter,
        context=context,
        draft=MatchSettlementRatingSelectionDraft(queries.selection, (), "공식 확정"),
    )
    selection_interaction = RecordingInteraction(interaction_id=700)

    asyncio.run(
        adapter.update_rating_selection(
            selection_interaction,  # type: ignore[arg-type]
            context=context,
            draft=initial.draft,
            values=("13",),
            source_view=initial,
        )
    )

    assert commands.calls == []
    assert queries.calls == []
    assert authorization.calls == [(selection_interaction, "match.staff.settlement")]
    assert selection_interaction.response.defers == [{"thinking": False}]
    selected_view = selection_interaction.edits[0]["view"]
    assert isinstance(selected_view, MatchSettlementRatingSelectionView)
    assert selected_view.draft.excluded_rating_entry_ids == (13,)

    preview_interaction = RecordingInteraction(interaction_id=701)
    asyncio.run(_layout_button(selected_view, label="정산 Preview").callback(preview_interaction))  # type: ignore[arg-type]

    assert preview_interaction.response.defers == [{"thinking": False}]
    assert authorization.calls[-1] == (preview_interaction, "match.staff.settlement")
    assert queries.calls == [("get", 71, (13,))]
    assert commands.calls == []
    preview_view = preview_interaction.edits[0]["view"]
    assert isinstance(preview_view, MatchSettlementPreviewView)
    assert preview_view.preview.plan.excluded_rating_entry_ids == (13,)
    assert "Rating 제외" in _layout_text(preview_view)


def test_final_confirm_uses_preview_fingerprint_and_final_interaction_key_once() -> None:
    adapter, queries, commands, authorization = _adapter()
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    view = MatchSettlementPreviewView(
        adapter=adapter,
        context=context,
        preview=MatchSettlementPreview(queries.plan, queries.selection, "공식 확정"),
    )
    interaction = RecordingInteraction(interaction_id=991)

    asyncio.run(_layout_button(view, label="정산 확정").callback(interaction))  # type: ignore[arg-type]

    assert interaction.response.defers == [{"thinking": False}]
    assert authorization.calls == [(interaction, "match.staff.settlement")]
    assert len(commands.calls) == 1
    command = commands.calls[0]
    assert command.idempotency_key == "match-settlement:991"
    assert command.expected_settlement_fingerprint == queries.plan.settlement_fingerprint
    assert command.excluded_rating_entry_ids == ()
    assert command.reason == "공식 확정"
    assert command.actor_discord_user_id == "123"
    assert command.guild_id == "987"
    receipt = interaction.edits[0]["view"]
    assert isinstance(receipt, discord.ui.LayoutView)
    assert "룸매치 정산 완료" in _layout_text(receipt)
    assert "결과 공지 intent도 함께 저장" in _layout_text(receipt)

    duplicate = RecordingInteraction(interaction_id=992)
    asyncio.run(view.confirm(duplicate))  # type: ignore[arg-type]
    assert len(commands.calls) == 1
    assert duplicate.response.defers == [{"thinking": False}]


def test_cancel_and_context_mismatch_are_zero_write() -> None:
    adapter, queries, commands, authorization = _adapter()
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    view = MatchSettlementPreviewView(
        adapter=adapter,
        context=context,
        preview=MatchSettlementPreview(queries.plan, queries.selection),
    )
    cancel = RecordingInteraction(interaction_id=700)

    asyncio.run(_layout_button(view, label="취소").callback(cancel))  # type: ignore[arg-type]

    assert commands.calls == []
    assert authorization.calls == [(cancel, "match.staff.settlement")]
    assert cancel.response.defers == [{"thinking": False}]
    assert "DB에는 기록되지 않았습니다" in _layout_text(cancel.edits[0]["view"])

    mismatch_view = MatchSettlementPreviewView(
        adapter=adapter,
        context=context,
        preview=MatchSettlementPreview(queries.plan, queries.selection),
    )
    mismatch = RecordingInteraction(interaction_id=701, user_id=999)
    asyncio.run(mismatch_view.confirm(mismatch))  # type: ignore[arg-type]
    assert commands.calls == []
    assert mismatch.response.messages
    assert not mismatch_view.is_finished()
    assert mismatch.response.defers == []
    assert "@everyone" not in format_match_settlement_preview(mismatch_view.preview)


def test_delivery_failure_after_source_retirement_sends_fresh_entry_notice() -> None:
    adapter, queries, commands, _ = _adapter()
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    source_view = RecordingSourceView()
    interaction = RecordingInteraction(fail_edit=True)

    asyncio.run(
        adapter.preview_settlement(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            reason=None,
            context=context,
            source_view=source_view,  # type: ignore[arg-type]
        )
    )

    assert source_view.stopped
    assert commands.calls == []
    assert queries.calls == [("selection", 71)]
    assert interaction.followup.messages
    assert "/match staff settlement" in interaction.followup.messages[0][0]
