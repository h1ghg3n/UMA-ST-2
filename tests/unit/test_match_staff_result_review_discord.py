"""Discord native Match pending-result review and rejection tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import discord

from uma_st2.adapters.discord import (
    MatchResultRejectModal,
    MatchResultReviewDiscordAdapter,
    MatchResultReviewView,
    MatchStaffInteractionContext,
    format_match_result_review,
    match_result_review_autocomplete_choices,
)
from uma_st2.application.match import (
    MatchResultCandidate,
    MatchResultCandidateEntry,
    MatchResultReviewTargetChoice,
    MatchResultRevisionReference,
    MatchResultSubmissionEntry,
    MatchResultSubmissionTarget,
    RejectedMatchResultSubmission,
    RejectMatchResultSubmission,
)
from uma_st2.domain.match import (
    MatchResultSourceKind,
    MatchResultSubmissionStatus,
    MatchSourceKind,
    MatchStatus,
)

NOW = datetime(2026, 8, 28, 13, 0, tzinfo=UTC)


def _candidate(entry_count: int) -> MatchResultCandidate:
    return MatchResultCandidate(
        entries=tuple(
            MatchResultCandidateEntry(
                entry_id=100 + index,
                entry_number=index,
                rank=index,
                popularity_rank=index,
                margin=None if index == 1 else f"{index - 1}/2마신",
            )
            for index in range(1, entry_count + 1)
        ),
        finish_time_ms=92_300,
    )


def _reference(
    *,
    submission_id: int,
    revision_number: int,
    status: MatchResultSubmissionStatus,
    entry_count: int,
) -> MatchResultRevisionReference:
    return MatchResultRevisionReference(
        submission_id=submission_id,
        revision_number=revision_number,
        status=status,
        source_kind=MatchResultSourceKind.MANUAL,
        candidate=_candidate(entry_count),
    )


def _target(*, entry_count: int = 3, confirmed: bool = True) -> MatchResultSubmissionTarget:
    return MatchResultSubmissionTarget(
        match_id=71,
        match_name="제12회 @everyone 정기전",
        source_kind=MatchSourceKind.NATIVE_V2,
        status=MatchStatus.RESULT_CONFIRMED if confirmed else MatchStatus.BETTING_CLOSED,
        entries=tuple(
            MatchResultSubmissionEntry(
                entry_id=100 + index,
                entry_number=index,
                game_account_name=f"계정 {index}",
                horse_name=f"말 {index}",
            )
            for index in range(1, entry_count + 1)
        ),
        next_revision_number=3 if confirmed else 2,
        pending=_reference(
            submission_id=902,
            revision_number=2 if confirmed else 1,
            status=MatchResultSubmissionStatus.PENDING,
            entry_count=entry_count,
        ),
        confirmed=(
            _reference(
                submission_id=901,
                revision_number=1,
                status=MatchResultSubmissionStatus.CONFIRMED,
                entry_count=entry_count,
            )
            if confirmed
            else None
        ),
    )


class RecordingQueries:
    def __init__(self, target: MatchResultSubmissionTarget) -> None:
        self.target = target
        self.calls: list[tuple[object, ...]] = []

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchResultReviewTargetChoice, ...]:
        self.calls.append(("search", search, limit))
        assert self.target.pending is not None
        return (
            MatchResultReviewTargetChoice(
                match_id=self.target.match_id,
                match_name=self.target.match_name,
                match_status=self.target.status,
                submission_id=self.target.pending.submission_id,
                revision_number=self.target.pending.revision_number,
                source_kind=self.target.pending.source_kind,
                entry_count=len(self.target.entries),
            ),
        )

    def get_target(self, *, match_id: int) -> MatchResultSubmissionTarget:
        self.calls.append(("get", match_id))
        return self.target


class RecordingCommands:
    def __init__(self) -> None:
        self.calls: list[RejectMatchResultSubmission] = []

    def reject(self, command: RejectMatchResultSubmission) -> RejectedMatchResultSubmission:
        self.calls.append(command)
        return RejectedMatchResultSubmission(
            submission_id=command.submission_id,
            match_id=command.match_id,
            match_name="제12회 @everyone 정기전",
            revision_number=2,
            source_kind=MatchResultSourceKind.MANUAL,
            status=MatchResultSubmissionStatus.REJECTED,
            candidate_fingerprint=command.expected_candidate_fingerprint,
            reason=command.reason,
            rejected_at=NOW,
            preserved_confirmed_submission_id=901,
        )


class RecordingAuthorization:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[object, str]] = []

    async def __call__(self, interaction: object, command_name: str) -> bool:
        self.calls.append((interaction, command_name))
        return self.allowed


@dataclass
class RecordingSourceView:
    stopped: bool = False

    def stop(self) -> None:
        self.stopped = True


@dataclass
class RecordingResponse:
    messages: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    edits: list[dict[str, object]] = field(default_factory=list)
    modals: list[discord.ui.Modal] = field(default_factory=list)
    defers: list[dict[str, object]] = field(default_factory=list)

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))

    async def edit_message(self, **kwargs: object) -> None:
        self.edits.append(kwargs)

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self.modals.append(modal)

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
    target: MatchResultSubmissionTarget,
) -> tuple[
    MatchResultReviewDiscordAdapter,
    RecordingQueries,
    RecordingCommands,
    RecordingAuthorization,
]:
    queries = RecordingQueries(target)
    commands = RecordingCommands()
    authorization = RecordingAuthorization()
    return (
        MatchResultReviewDiscordAdapter(
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


def test_autocomplete_and_private_review_are_bounded_mention_safe_and_zero_write() -> None:
    target = _target()
    adapter, queries, commands, authorization = _adapter(target)
    interaction = RecordingInteraction()
    context = MatchStaffInteractionContext.from_interaction(interaction)
    source_view = RecordingSourceView()

    choices = asyncio.run(adapter.autocomplete_targets(interaction, "정기"))  # type: ignore[arg-type]
    asyncio.run(
        adapter.start_review(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            context=context,
            source_view=source_view,
        )
    )

    assert authorization.calls == [
        (interaction, "match.staff.result-review"),
        (interaction, "match.staff.result-review"),
    ]
    assert interaction.response.defers == [{"thinking": False}]
    assert source_view.stopped
    assert queries.calls == [("search", "정기", 25), ("get", 71)]
    assert commands.calls == []
    assert len(choices) == 1
    assert len(choices[0].name) <= 100
    assert "@everyone" not in choices[0].name
    view = interaction.edits[0]["view"]
    assert isinstance(view, MatchResultReviewView)
    assert "ResultSubmission Review" in _layout_text(view)
    assert "Existing confirmed `#901`" in _layout_text(view)
    assert "@everyone" not in _layout_text(view)

    duplicates = match_result_review_autocomplete_choices(
        tuple(
            MatchResultReviewTargetChoice(
                match_id=index,
                match_name="중복 경기",
                match_status=MatchStatus.BETTING_CLOSED,
                submission_id=100 + index,
                revision_number=1,
                source_kind=MatchResultSourceKind.MANUAL,
                entry_count=3,
            )
            for index in (1, 2)
        )
    )
    assert all("ID" in choice.name for choice in duplicates)


def test_pagination_and_correction_guide_remain_read_only() -> None:
    target = _target(entry_count=9)
    adapter, _, commands, authorization = _adapter(target)
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    first = MatchResultReviewView(adapter=adapter, context=context, target=target)

    page = RecordingInteraction(interaction_id=700)
    asyncio.run(_layout_button(first, label="다음").callback(page))  # type: ignore[arg-type]
    second = page.response.edits[0]["view"]
    assert isinstance(second, MatchResultReviewView)
    assert second.page_index == 1
    assert "page 2/2" in _layout_text(second)
    assert "9착" in _layout_text(second)

    guide = RecordingInteraction(interaction_id=701)
    asyncio.run(_layout_button(second, label="정정 방법").callback(guide))  # type: ignore[arg-type]
    assert "/match staff result" in guide.response.messages[0][0]
    assert "결과 입력/정정" in guide.response.messages[0][0]
    assert commands.calls == []
    assert len(authorization.calls) == 2
    assert len(format_match_result_review(target, page_index=0)) <= 3500


def test_required_reason_modal_rejects_once_with_final_interaction_key() -> None:
    target = _target()
    adapter, _, commands, authorization = _adapter(target)
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    view = MatchResultReviewView(adapter=adapter, context=context, target=target)

    open_modal = RecordingInteraction(interaction_id=800)
    asyncio.run(_layout_button(view, label="Submission Reject").callback(open_modal))  # type: ignore[arg-type]
    modal = open_modal.response.modals[0]
    assert isinstance(modal, MatchResultRejectModal)
    assert modal.reason.required is True
    modal.reason._value = "  공식 결과와 다름  "

    final = RecordingInteraction(interaction_id=991)
    with patch.object(view, "stop", wraps=view.stop) as stop:
        asyncio.run(modal.on_submit(final))  # type: ignore[arg-type]

        assert stop.call_count == 1
        assert final.response.defers == [{"thinking": False}]
    assert len(commands.calls) == 1
    command = commands.calls[0]
    assert command.idempotency_key == "match-result-reject:991"
    assert command.reason == "공식 결과와 다름"
    assert command.expected_state_fingerprint == target.state_fingerprint
    assert command.expected_candidate_fingerprint == target.pending.candidate.fingerprint  # type: ignore[union-attr]
    assert command.actor_discord_user_id == "123"
    assert command.guild_id == "987"
    assert "Reject 완료" in _layout_text(final.edits[0]["view"])
    assert "Preserved confirmed: `#901`" in _layout_text(final.edits[0]["view"])
    assert "@everyone" not in _layout_text(final.edits[0]["view"])

    duplicate = RecordingInteraction(interaction_id=992)
    with patch.object(view, "stop", wraps=view.stop) as duplicate_stop:
        asyncio.run(adapter.reject_pending(duplicate, context=context, source_view=view, reason="재요청"))  # type: ignore[arg-type]
        assert duplicate_stop.call_count == 1
    assert len(commands.calls) == 1
    assert len(authorization.calls) == 3
    duplicate_payload = duplicate.edits[0]
    duplicate_terminal = duplicate_payload["view"]
    assert duplicate_payload["content"] is None
    assert duplicate_payload["embeds"] == []
    assert duplicate_payload["attachments"] == []
    assert isinstance(duplicate_terminal, discord.ui.LayoutView)
    assert "이미 처리 중이거나 완료되었습니다" in _layout_text(duplicate_terminal)
    assert not any(isinstance(item, discord.ui.Button) for item in duplicate_terminal.walk_children())
    assert duplicate.response.defers == [{"thinking": False}]


def test_close_and_context_mismatch_are_zero_write() -> None:
    target = _target()
    adapter, _, commands, authorization = _adapter(target)
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    view = MatchResultReviewView(adapter=adapter, context=context, target=target)
    close = RecordingInteraction(interaction_id=700)

    asyncio.run(_layout_button(view, label="닫기").callback(close))  # type: ignore[arg-type]

    assert commands.calls == []
    assert authorization.calls == [(close, "match.staff.result-review")]
    assert "DB에는 기록되지 않았습니다" in _layout_text(close.response.edits[0]["view"])

    mismatch_view = MatchResultReviewView(adapter=adapter, context=context, target=target)
    mismatch = RecordingInteraction(interaction_id=701, user_id=999)
    asyncio.run(
        adapter.reject_pending(  # type: ignore[arg-type]
            mismatch,
            context=context,
            source_view=mismatch_view,
            reason="공식 결과와 다름",
        )
    )
    assert commands.calls == []
    assert mismatch.response.messages
