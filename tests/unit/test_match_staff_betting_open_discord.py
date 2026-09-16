"""Discord native Match betting-open adapter tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from uma_st2.adapters.discord import (
    MatchBettingOpenDiscordAdapter,
    MatchBettingOpenPreview,
    MatchBettingOpenPreviewView,
    MatchStaffInteractionContext,
    format_match_betting_open_preview,
    match_betting_open_autocomplete_choices,
)
from uma_st2.application.match import (
    MatchBettingOpenRatingRuleCoverage,
    MatchBettingOpenTarget,
    MatchBettingOpenTargetChoice,
    OpenedMatchBetting,
    OpenMatchBetting,
    StoredMatchOpeningPublication,
)
from uma_st2.application.publication import (
    MatchOpeningCondition,
    MatchOpeningCourse,
    MatchOpeningEntry,
    MatchPublicationDestination,
)
from uma_st2.domain.match import (
    MatchDirection,
    MatchGrade,
    MatchSeason,
    MatchSourceKind,
    MatchStatus,
    MatchSurface,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
    StadiumCourseLayout,
)
from uma_st2.domain.publication import PublicationStatus

NOW = datetime(2026, 8, 28, 7, 0, tzinfo=UTC)


def _target(
    *,
    count: int = 3,
    condition: bool = True,
    covered_converted_ranks: tuple[int, ...] | None = None,
) -> MatchBettingOpenTarget:
    if covered_converted_ranks is None:
        covered_converted_ranks = tuple(range(1, count + 1))
    return MatchBettingOpenTarget(
        match_id=71,
        match_name="제12회 @everyone 정기전",
        description=None,
        source_kind=MatchSourceKind.NATIVE_V2,
        status=MatchStatus.SCHEDULED,
        grade=MatchGrade.G1,
        scheduled_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        course=MatchOpeningCourse(
            course_id=11,
            stadium_id=5,
            stadium_name="도쿄 @everyone",
            surface=MatchSurface.TURF,
            distance=2400,
            direction=MatchDirection.LEFT,
            layout=StadiumCourseLayout.STANDARD,
        ),
        condition=MatchOpeningCondition(
            MatchSeason.AUTUMN,
            MatchWeather.SUNNY,
            MatchTimeOfDay.DAY,
            MatchTrackCondition.FIRM,
        )
        if condition
        else None,
        rating_rule_coverage=MatchBettingOpenRatingRuleCoverage(
            current_version_available=True,
            current_version_complete=True,
            covered_converted_ranks=covered_converted_ranks,
        ),
        entries=tuple(
            MatchOpeningEntry(
                entry_id=100 + index,
                entry_number=index,
                game_account_name=f"계정 @{index}",
                horse_name=f"말 **{index}**",
            )
            for index in range(1, count + 1)
        ),
        destination=MatchPublicationDestination("987", True, "777"),
    )


class RecordingQueries:
    def __init__(self, target: MatchBettingOpenTarget) -> None:
        self.target = target
        self.calls: list[tuple[object, ...]] = []

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchBettingOpenTargetChoice, ...]:
        self.calls.append(("search", search, limit))
        return (
            MatchBettingOpenTargetChoice(
                match_id=self.target.match_id,
                match_name=self.target.match_name,
                grade=self.target.grade,
                entry_count=len(self.target.entries),
                has_complete_condition=self.target.condition is not None,
                rating_rule_coverage=self.target.rating_rule_coverage,
            ),
        )

    def get_target(self, *, match_id: int, guild_id: str) -> MatchBettingOpenTarget:
        self.calls.append(("get", match_id, guild_id))
        return self.target


class RecordingCommands:
    def __init__(self, target: MatchBettingOpenTarget) -> None:
        self.target = target
        self.calls: list[OpenMatchBetting] = []

    def open_betting(self, command: OpenMatchBetting) -> OpenedMatchBetting:
        self.calls.append(command)
        return OpenedMatchBetting(
            match_id=self.target.match_id,
            match_name=self.target.match_name,
            status=MatchStatus.BETTING_OPEN,
            opened_at=NOW,
            entry_count=len(self.target.entries),
            markets=self.target.markets,
            publication=StoredMatchOpeningPublication(
                publication_id=901,
                event_key="match:71:betting-open:v1",
                payload_fingerprint="a" * 64,
                status=PublicationStatus.READY,
                target_channel_id="777",
            ),
        )


class RecordingAuthorization:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[object, str]] = []

    async def __call__(self, interaction: object, command_name: str) -> bool:
        self.calls.append((interaction, command_name))
        return self.allowed


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


@dataclass
class RecordingFollowup:
    messages: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    async def send(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))


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


def _adapter(
    target: MatchBettingOpenTarget,
) -> tuple[
    MatchBettingOpenDiscordAdapter,
    RecordingQueries,
    RecordingCommands,
    RecordingAuthorization,
]:
    queries = RecordingQueries(target)
    commands = RecordingCommands(target)
    authorization = RecordingAuthorization()
    return (
        MatchBettingOpenDiscordAdapter(
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
    adapter, queries, commands, _ = _adapter(_target())
    interaction = RecordingInteraction()
    context = MatchStaffInteractionContext.from_interaction(interaction)
    source_view = RecordingSourceView()

    asyncio.run(
        adapter.preview_opening(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            context=context,
            source_view=source_view,
        )
    )

    assert interaction.response.defers == [{"thinking": False}]
    assert source_view.stopped
    assert queries.calls == [("get", 71, "987")]
    assert commands.calls == []
    view = interaction.edits[0]["view"]
    assert isinstance(view, MatchBettingOpenPreviewView)
    rendered = _layout_text(view)
    assert "베팅 오픈 Preview" in rendered
    assert "모든 유효 조합" in rendered
    assert "@everyone" not in rendered


def test_target_handoff_authorization_rejection_keeps_source_view_live() -> None:
    adapter, queries, commands, authorization = _adapter(_target())
    authorization.allowed = False
    interaction = RecordingInteraction()
    context = MatchStaffInteractionContext.from_interaction(interaction)
    source_view = RecordingSourceView()

    asyncio.run(
        adapter.preview_opening(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            context=context,
            source_view=source_view,
        )
    )

    assert interaction.response.defers == [{"thinking": False}]
    assert authorization.calls == [(interaction, "match.staff.betting-open")]
    assert source_view.stopped is False
    assert queries.calls == []
    assert commands.calls == []


def test_failed_target_replacement_sends_fresh_race_entry_notice() -> None:
    adapter, queries, commands, _ = _adapter(_target())
    interaction = RecordingInteraction(fail_edit=True)
    context = MatchStaffInteractionContext.from_interaction(interaction)
    source_view = RecordingSourceView()

    asyncio.run(
        adapter.preview_opening(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            context=context,
            source_view=source_view,
        )
    )

    assert source_view.stopped
    assert queries.calls == [("get", 71, "987")]
    assert commands.calls == []
    assert interaction.followup.messages
    assert "/match staff race" in interaction.followup.messages[0][0]


def test_long_duplicate_autocomplete_labels_retain_disambiguating_ids() -> None:
    targets = tuple(
        MatchBettingOpenTargetChoice(
            match_id=index,
            match_name="@everyone " + "긴이름" * 40,
            grade=MatchGrade.G1,
            entry_count=3,
            has_complete_condition=True,
            rating_rule_coverage=MatchBettingOpenRatingRuleCoverage(
                current_version_available=True,
                current_version_complete=True,
                covered_converted_ranks=(1, 2, 3),
            ),
        )
        for index in (1, 2)
    )

    choices = match_betting_open_autocomplete_choices(targets)

    assert all(len(choice.name) <= 100 for choice in choices)
    assert all("@everyone" not in choice.name for choice in choices)
    assert [choice.name.rsplit(" · ID ", maxsplit=1)[1] for choice in choices] == ["1", "2"]


def test_incomplete_or_unreviewed_preview_cannot_confirm() -> None:
    incomplete_adapter, _, _, _ = _adapter(_target(condition=False))
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    incomplete = MatchBettingOpenPreviewView(
        adapter=incomplete_adapter,
        context=context,
        preview=MatchBettingOpenPreview(_target(condition=False)),
    )
    assert _layout_button(incomplete, label="베팅 오픈 확정").disabled is True
    assert "환경 조건이 확정되지 않았습니다" in _layout_text(incomplete)

    one_entry_adapter, _, _, _ = _adapter(_target(count=1))
    one_entry = MatchBettingOpenPreviewView(
        adapter=one_entry_adapter,
        context=context,
        preview=MatchBettingOpenPreview(_target(count=1)),
    )
    assert _layout_button(one_entry, label="베팅 오픈 확정").disabled is True
    assert "Entry가 최소 2명 필요합니다. (현재 1명)" in _layout_text(one_entry)

    too_many_adapter, _, _, _ = _adapter(_target(count=19))
    too_many = MatchBettingOpenPreviewView(
        adapter=too_many_adapter,
        context=context,
        preview=MatchBettingOpenPreview(_target(count=19)),
    )
    assert _layout_button(too_many, label="베팅 오픈 확정").disabled is True
    assert "Entry는 최대 18명까지 허용됩니다. (현재 19명)" in _layout_text(too_many)

    missing_rule_adapter, _, _, _ = _adapter(_target(covered_converted_ranks=(1, 2)))
    missing_rule = MatchBettingOpenPreviewView(
        adapter=missing_rule_adapter,
        context=context,
        preview=MatchBettingOpenPreview(_target(covered_converted_ranks=(1, 2))),
    )
    assert _layout_button(missing_rule, label="베팅 오픈 확정").disabled is True
    assert "G1 · Entry 3명 규칙이 완전하지 않습니다" in _layout_text(missing_rule)

    adapter, _, commands, _ = _adapter(_target(count=9))
    first = MatchBettingOpenPreviewView(
        adapter=adapter,
        context=context,
        preview=MatchBettingOpenPreview(_target(count=9)),
    )
    assert _layout_button(first, label="베팅 오픈 확정").disabled is True
    page_interaction = RecordingInteraction(interaction_id=600)
    asyncio.run(_layout_button(first, label="다음").callback(page_interaction))  # type: ignore[arg-type]
    second = page_interaction.edits[0]["view"]
    assert isinstance(second, MatchBettingOpenPreviewView)
    assert second.preview.all_pages_reviewed is True
    assert _layout_button(second, label="베팅 오픈 확정").disabled is False
    assert commands.calls == []


def test_final_confirm_uses_final_interaction_key_once() -> None:
    target = _target()
    adapter, _, commands, _ = _adapter(target)
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    view = MatchBettingOpenPreviewView(
        adapter=adapter,
        context=context,
        preview=MatchBettingOpenPreview(target),
    )
    interaction = RecordingInteraction(interaction_id=991)

    asyncio.run(_layout_button(view, label="베팅 오픈 확정").callback(interaction))  # type: ignore[arg-type]

    assert interaction.response.defers == [{"thinking": False}]
    assert len(commands.calls) == 1
    command = commands.calls[0]
    assert command.idempotency_key == "match-betting-open:991"
    assert command.expected_state_fingerprint == target.state_fingerprint
    assert command.actor_discord_user_id == "123"
    assert command.guild_id == "987"
    receipt = interaction.edits[0]["view"]
    assert isinstance(receipt, discord.ui.LayoutView)
    assert "베팅 오픈 완료" in _layout_text(receipt)

    duplicate = RecordingInteraction(interaction_id=992)
    asyncio.run(
        adapter.confirm_opening(  # type: ignore[arg-type]
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
    view = MatchBettingOpenPreviewView(
        adapter=adapter,
        context=context,
        preview=MatchBettingOpenPreview(target),
    )
    cancel = RecordingInteraction(interaction_id=700)

    asyncio.run(_layout_button(view, label="취소").callback(cancel))  # type: ignore[arg-type]

    assert commands.calls == []
    assert authorization.calls == [(cancel, "match.staff.betting-open")]
    assert cancel.response.defers == [{"thinking": False}]
    assert "DB에는 기록되지 않았습니다" in _layout_text(cancel.edits[0]["view"])

    mismatch = RecordingInteraction(interaction_id=701, user_id=999)
    asyncio.run(
        adapter.confirm_opening(  # type: ignore[arg-type]
            mismatch,
            context=context,
            preview=view.preview,
            source_view=view,
        )
    )
    assert commands.calls == []
    assert mismatch.response.messages
    assert "@everyone" not in format_match_betting_open_preview(view.preview)
