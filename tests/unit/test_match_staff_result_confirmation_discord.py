"""Discord native Match authoritative result confirmation tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import discord

from uma_st2.adapters.discord import (
    MatchResultConfirmationDiscordAdapter,
    MatchResultConfirmationView,
    MatchStaffInteractionContext,
    format_match_result_confirmation,
)
from uma_st2.application.match import (
    ConfirmedMatchResultSubmission,
    ConfirmMatchResultSubmission,
    MatchResultCandidate,
    MatchResultCandidateEntry,
    MatchResultReviewTargetChoice,
    MatchResultRevisionReference,
    MatchResultSubmissionEntry,
    MatchResultSubmissionTarget,
)
from uma_st2.domain.match import (
    MatchResultSourceKind,
    MatchResultSubmissionStatus,
    MatchSourceKind,
    MatchStatus,
)

NOW = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)


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


def _target(*, entry_count: int = 3, confirmed: bool = True) -> MatchResultSubmissionTarget:
    candidate = _candidate(entry_count)
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
        pending=MatchResultRevisionReference(
            submission_id=902,
            revision_number=2 if confirmed else 1,
            status=MatchResultSubmissionStatus.PENDING,
            source_kind=MatchResultSourceKind.MANUAL,
            candidate=candidate,
        ),
        confirmed=(
            MatchResultRevisionReference(
                submission_id=901,
                revision_number=1,
                status=MatchResultSubmissionStatus.CONFIRMED,
                source_kind=MatchResultSourceKind.MANUAL,
                candidate=candidate,
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
        self.calls: list[ConfirmMatchResultSubmission] = []

    def confirm(self, command: ConfirmMatchResultSubmission) -> ConfirmedMatchResultSubmission:
        self.calls.append(command)
        target = _target(entry_count=9)
        assert target.pending is not None
        return ConfirmedMatchResultSubmission(
            submission_id=command.submission_id,
            match_id=command.match_id,
            match_name=target.match_name,
            revision_number=target.pending.revision_number,
            source_kind=target.pending.source_kind,
            status=MatchResultSubmissionStatus.CONFIRMED,
            match_status=MatchStatus.RESULT_CONFIRMED,
            candidate=target.pending.candidate,
            candidate_fingerprint=command.expected_candidate_fingerprint,
            previous_confirmed_submission_id=901,
            confirmed_at=NOW,
        )


class RecordingAuthorization:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []

    async def __call__(self, interaction: object, command_name: str) -> bool:
        self.calls.append((interaction, command_name))
        return True


@dataclass
class RecordingSourceView:
    stopped: bool = False

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
    target: MatchResultSubmissionTarget,
) -> tuple[
    MatchResultConfirmationDiscordAdapter,
    RecordingQueries,
    RecordingCommands,
    RecordingAuthorization,
]:
    queries = RecordingQueries(target)
    commands = RecordingCommands()
    authorization = RecordingAuthorization()
    return (
        MatchResultConfirmationDiscordAdapter(
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


def test_private_confirmation_preview_is_bounded_mention_safe_and_zero_write() -> None:
    target = _target(entry_count=9)
    adapter, queries, commands, authorization = _adapter(target)
    interaction = RecordingInteraction()
    context = MatchStaffInteractionContext.from_interaction(interaction)
    source_view = RecordingSourceView()

    choices = asyncio.run(adapter.autocomplete_targets(interaction, "정기"))  # type: ignore[arg-type]
    asyncio.run(
        adapter.start_confirmation(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            context=context,
            source_view=source_view,
        )
    )

    assert authorization.calls == [
        (interaction, "match.staff.result-confirm"),
        (interaction, "match.staff.result-confirm"),
    ]
    assert interaction.response.defers == [{"thinking": False}]
    assert source_view.stopped
    assert queries.calls == [("search", "정기", 25), ("get", 71)]
    assert commands.calls == []
    assert len(choices) == 1
    view = interaction.edits[0]["view"]
    assert isinstance(view, MatchResultConfirmationView)
    assert _layout_button(view, label="결과 확정").disabled is True
    assert "page 1/2" in _layout_text(view)
    assert "@everyone" not in _layout_text(view)
    assert len(format_match_result_confirmation(target, page_index=0, viewed_pages=frozenset({0}))) <= 3500


def test_all_pages_are_required_and_final_interaction_confirms_once() -> None:
    target = _target(entry_count=9)
    adapter, _, commands, authorization = _adapter(target)
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())

    premature_source = MatchResultConfirmationView(adapter=adapter, context=context, target=target)
    premature = RecordingInteraction(interaction_id=800)
    with patch.object(premature_source, "stop", wraps=premature_source.stop) as premature_stop:
        asyncio.run(premature_source.confirm(premature))  # type: ignore[arg-type]
        assert premature_stop.call_count == 0
    assert commands.calls == []
    assert "모든 결과 page" in premature.response.messages[0][0]
    assert premature.response.defers == []

    first = MatchResultConfirmationView(adapter=adapter, context=context, target=target)
    page = RecordingInteraction(interaction_id=801)
    asyncio.run(_layout_button(first, label="다음").callback(page))  # type: ignore[arg-type]
    second = page.response.edits[0]["view"]
    assert isinstance(second, MatchResultConfirmationView)
    assert second.all_pages_viewed is True
    assert _layout_button(second, label="결과 확정").disabled is False

    final = RecordingInteraction(interaction_id=991)
    with patch.object(second, "stop", wraps=second.stop) as stop:
        asyncio.run(_layout_button(second, label="결과 확정").callback(final))  # type: ignore[arg-type]

        assert stop.call_count == 1
    assert len(commands.calls) == 1
    command = commands.calls[0]
    assert command.idempotency_key == "match-result-confirm:991"
    assert command.expected_state_fingerprint == target.state_fingerprint
    assert command.expected_candidate_fingerprint == target.pending.candidate.fingerprint  # type: ignore[union-attr]
    assert command.actor_discord_user_id == "123"
    assert command.guild_id == "987"
    assert "결과 확정 완료" in _layout_text(final.edits[0]["view"])
    assert "Superseded confirmed: `#901`" in _layout_text(final.edits[0]["view"])
    assert final.response.defers == [{"thinking": False}]

    duplicate = RecordingInteraction(interaction_id=992)
    with patch.object(second, "stop", wraps=second.stop) as stop:
        asyncio.run(second.confirm(duplicate))  # type: ignore[arg-type]
        assert stop.call_count == 1
    duplicate_payload = duplicate.edits[0]
    duplicate_terminal = duplicate_payload["view"]
    assert len(commands.calls) == 1
    assert duplicate_payload["content"] is None
    assert duplicate_payload["embeds"] == []
    assert duplicate_payload["attachments"] == []
    assert isinstance(duplicate_terminal, discord.ui.LayoutView)
    assert "이미 처리 중이거나 완료되었습니다" in _layout_text(duplicate_terminal)
    assert not any(isinstance(item, discord.ui.Button) for item in duplicate_terminal.walk_children())
    assert duplicate.response.defers == [{"thinking": False}]
    assert len(authorization.calls) == 4


def test_cancel_and_context_mismatch_are_zero_write() -> None:
    target = _target()
    adapter, _, commands, authorization = _adapter(target)
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    view = MatchResultConfirmationView(adapter=adapter, context=context, target=target)
    cancel = RecordingInteraction(interaction_id=700)

    asyncio.run(_layout_button(view, label="취소").callback(cancel))  # type: ignore[arg-type]

    assert commands.calls == []
    assert authorization.calls == [(cancel, "match.staff.result-confirm")]
    assert "DB에는 기록되지 않았습니다" in _layout_text(cancel.response.edits[0]["view"])

    mismatch_view = MatchResultConfirmationView(adapter=adapter, context=context, target=target)
    mismatch = RecordingInteraction(interaction_id=701, user_id=999)
    asyncio.run(mismatch_view.confirm(mismatch))  # type: ignore[arg-type]
    assert commands.calls == []
    assert mismatch.response.messages
