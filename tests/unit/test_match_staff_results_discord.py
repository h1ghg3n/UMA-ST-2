"""Discord native Match result-submission adapter tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import discord
import pytest

from uma_st2.adapters.discord import (
    MatchResultDraftRow,
    MatchResultSubmissionDiscordAdapter,
    MatchResultSubmissionDraft,
    MatchResultSubmissionEditorView,
    MatchStaffInteractionContext,
    format_match_finish_time,
    format_match_result_submission_editor,
    match_result_submission_autocomplete_choices,
    parse_match_finish_time,
)
from uma_st2.application.match import (
    MatchResultCandidate,
    MatchResultCandidateEntry,
    MatchResultSubmissionEntry,
    MatchResultSubmissionTarget,
    MatchResultSubmissionTargetChoice,
    SavedMatchResultSubmission,
    SaveMatchResultSubmission,
)
from uma_st2.domain.match import (
    MatchResultSubmissionStatus,
    MatchSourceKind,
    MatchStatus,
)

NOW = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)


def _target(*, entry_count: int = 3, status: MatchStatus = MatchStatus.BETTING_CLOSED) -> MatchResultSubmissionTarget:
    return MatchResultSubmissionTarget(
        match_id=71,
        match_name="제12회 @everyone 정기전",
        source_kind=MatchSourceKind.NATIVE_V2,
        status=status,
        entries=tuple(
            MatchResultSubmissionEntry(
                entry_id=100 + index,
                entry_number=index,
                game_account_name=f"계정 {index}",
                horse_name=f"말 {index}",
            )
            for index in range(1, entry_count + 1)
        ),
        next_revision_number=1,
    )


def _complete_draft(*, entry_count: int = 3) -> MatchResultSubmissionDraft:
    target = _target(entry_count=entry_count)
    rows = tuple(
        MatchResultDraftRow(
            rank=index,
            entry_id=entry.entry_id,
            entry_number=entry.entry_number,
            game_account_name=entry.game_account_name,
            horse_name=entry.horse_name,
            popularity_rank=index,
            margin=None if index == 1 else f"{index - 1}/2마신",
        )
        for index, entry in enumerate(target.entries, start=1)
    )
    return MatchResultSubmissionDraft(
        target=target,
        rows=rows,
        finish_time_ms=92_300,
        reason="운영 확인",
    )


class RecordingQueries:
    def __init__(self, target: MatchResultSubmissionTarget) -> None:
        self.target = target
        self.calls: list[tuple[object, ...]] = []

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchResultSubmissionTargetChoice, ...]:
        self.calls.append(("search", search, limit))
        return (
            MatchResultSubmissionTargetChoice(
                match_id=self.target.match_id,
                match_name=self.target.match_name,
                status=self.target.status,
                entry_count=len(self.target.entries),
                next_revision_number=self.target.next_revision_number,
            ),
        )

    def get_target(self, *, match_id: int) -> MatchResultSubmissionTarget:
        self.calls.append(("get", match_id))
        return self.target


class RecordingCommands:
    def __init__(self) -> None:
        self.calls: list[SaveMatchResultSubmission] = []

    def submit(self, command: SaveMatchResultSubmission) -> SavedMatchResultSubmission:
        self.calls.append(command)
        return SavedMatchResultSubmission(
            submission_id=501,
            match_id=command.match_id,
            match_name="제12회 @everyone 정기전",
            revision_number=1,
            source_kind=command.source_kind,
            status=MatchResultSubmissionStatus.PENDING,
            candidate=command.candidate,
            candidate_fingerprint=command.candidate.fingerprint,
            superseded_submission_id=None,
            corrects_confirmed=False,
            submitted_at=NOW,
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
    *,
    authorization: RecordingAuthorization | None = None,
) -> tuple[
    MatchResultSubmissionDiscordAdapter,
    RecordingQueries,
    RecordingCommands,
    RecordingAuthorization,
]:
    queries = RecordingQueries(target)
    commands = RecordingCommands()
    actual_authorization = authorization or RecordingAuthorization()
    return (
        MatchResultSubmissionDiscordAdapter(
            queries=queries,  # type: ignore[arg-type]
            commands=commands,  # type: ignore[arg-type]
            authorize_autocomplete=actual_authorization,
            authorize_interaction=actual_authorization,
            blocking_runner=_inline,  # type: ignore[arg-type]
        ),
        queries,
        commands,
        actual_authorization,
    )


def _layout_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def _layout_button(view: discord.ui.LayoutView, *, label: str) -> discord.ui.Button:
    return next(item for item in view.walk_children() if isinstance(item, discord.ui.Button) and item.label == label)


def test_finish_time_parser_preserves_exact_tenth_second() -> None:
    assert parse_match_finish_time("") is None
    assert parse_match_finish_time("1:32.3") == 92_300
    assert parse_match_finish_time("92.3") == 92_300
    assert format_match_finish_time(92_300) == "1:32.3"

    for value in ("1:60.0", "92", "0.0", "１:３２.３"):
        with pytest.raises(ValueError):
            parse_match_finish_time(value)


def test_draft_assignment_correction_and_reset_are_adapter_local() -> None:
    target = _target()
    draft = MatchResultSubmissionDraft(target=target)
    for entry in (target.entries[1], target.entries[0], target.entries[2]):
        draft = draft.assign(entry_id=entry.entry_id)

    assert draft.is_complete
    assert [row.entry_number for row in draft.rows] == [2, 1, 3]
    corrected = draft.correct(
        rank=1,
        entry_number=1,
        popularity_rank=2,
        detail="92.3",
    )
    assert [row.entry_number for row in corrected.rows] == [1, 2, 3]
    assert corrected.finish_time_ms == 92_300
    assert corrected.candidate().entries[0].popularity_rank == 2

    with pytest.raises(ValueError, match="unique"):
        corrected.correct(
            rank=2,
            entry_number=2,
            popularity_rank=2,
            detail="1/2마신",
        )

    reset = MatchResultSubmissionDraft(target=target, reason=corrected.reason)
    assert reset.rows == ()
    assert reset.finish_time_ms is None


def test_bound_target_replaces_private_message_with_zero_write_editor() -> None:
    target = _target()
    adapter, queries, commands, authorization = _adapter(target)
    interaction = RecordingInteraction()
    context = MatchStaffInteractionContext.from_interaction(interaction)
    source_view = RecordingSourceView()

    asyncio.run(
        adapter.start_submission(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            reason=None,
            context=context,
            source_view=source_view,
        )
    )

    assert authorization.calls == [(interaction, "match.staff.result-submit")]
    assert interaction.response.defers == [{"thinking": False}]
    assert source_view.stopped
    assert queries.calls == [("get", 71)]
    assert commands.calls == []
    view = interaction.edits[0]["view"]
    assert isinstance(view, MatchResultSubmissionEditorView)
    assert not view.draft.is_complete
    assert "Submission Draft" in _layout_text(view)
    assert "@everyone" not in _layout_text(view)


def test_autocomplete_is_bounded_authorized_and_mention_safe() -> None:
    target = _target()
    adapter, queries, _, authorization = _adapter(target)
    interaction = RecordingInteraction()

    choices = asyncio.run(adapter.autocomplete_targets(interaction, "정기"))  # type: ignore[arg-type]

    assert authorization.calls == [(interaction, "match.staff.result-submit")]
    assert queries.calls == [("search", "정기", 25)]
    assert len(choices) == 1
    assert len(choices[0].name) <= 100
    assert "@everyone" not in choices[0].name

    duplicate_choices = match_result_submission_autocomplete_choices(
        tuple(
            MatchResultSubmissionTargetChoice(
                match_id=index,
                match_name="중복 경기",
                status=MatchStatus.BETTING_CLOSED,
                entry_count=3,
                next_revision_number=1,
            )
            for index in (1, 2)
        )
    )
    assert all("ID" in choice.name for choice in duplicate_choices)


def test_all_complete_board_pages_must_be_seen_before_one_final_submission() -> None:
    draft = _complete_draft(entry_count=9)
    adapter, _, commands, _ = _adapter(draft.target)
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    first_page = MatchResultSubmissionEditorView(
        adapter=adapter,
        context=context,
        draft=draft,
    )
    assert _layout_button(first_page, label="Submission 저장").disabled is True

    page_interaction = RecordingInteraction(interaction_id=800)
    asyncio.run(_layout_button(first_page, label="다음").callback(page_interaction))  # type: ignore[arg-type]
    second_page = page_interaction.response.edits[0]["view"]
    assert isinstance(second_page, MatchResultSubmissionEditorView)
    assert second_page.seen_pages == frozenset({0, 1})
    assert _layout_button(second_page, label="Submission 저장").disabled is False

    final = RecordingInteraction(interaction_id=991)
    with patch.object(second_page, "stop", wraps=second_page.stop) as stop:
        asyncio.run(_layout_button(second_page, label="Submission 저장").callback(final))  # type: ignore[arg-type]

        assert stop.call_count == 1
        assert final.response.defers == [{"thinking": False}]
    assert len(commands.calls) == 1
    command = commands.calls[0]
    assert command.idempotency_key == "match-result-submit:991"
    assert command.expected_state_fingerprint == draft.target.state_fingerprint
    assert command.candidate == draft.candidate()
    assert command.actor_discord_user_id == "123"
    assert command.guild_id == "987"
    receipt = final.edits[0]["view"]
    assert isinstance(receipt, discord.ui.LayoutView)
    assert "저장 완료" in _layout_text(receipt)
    assert "canonical rank/Match status는 아직 변경하지 않았습니다" in _layout_text(receipt)
    assert "@everyone" not in _layout_text(receipt)

    duplicate = RecordingInteraction(interaction_id=992)
    with patch.object(second_page, "stop", wraps=second_page.stop) as duplicate_stop:
        asyncio.run(
            adapter.submit_draft(  # type: ignore[arg-type]
                duplicate,
                context=context,
                source_view=second_page,
            )
        )
        assert duplicate_stop.call_count == 1
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


def test_cancel_and_context_mismatch_are_zero_write() -> None:
    draft = _complete_draft()
    adapter, _, commands, authorization = _adapter(draft.target)
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    view = MatchResultSubmissionEditorView(adapter=adapter, context=context, draft=draft)
    cancel = RecordingInteraction(interaction_id=700)

    asyncio.run(_layout_button(view, label="취소").callback(cancel))  # type: ignore[arg-type]

    assert commands.calls == []
    assert authorization.calls == [(cancel, "match.staff.result-submit")]
    assert "DB에는 기록되지 않았습니다" in _layout_text(cancel.response.edits[0]["view"])

    mismatch_view = MatchResultSubmissionEditorView(adapter=adapter, context=context, draft=draft)
    mismatch = RecordingInteraction(interaction_id=701, user_id=999)
    asyncio.run(
        adapter.submit_draft(  # type: ignore[arg-type]
            mismatch,
            context=context,
            source_view=mismatch_view,
        )
    )
    assert commands.calls == []
    assert mismatch.response.messages
    assert "@everyone" not in format_match_result_submission_editor(
        draft,
        page_index=0,
        seen_pages=frozenset({0}),
    )


def test_candidate_payload_from_complete_draft_is_full_and_rank_ordered() -> None:
    draft = _complete_draft()

    assert draft.candidate() == MatchResultCandidate(
        entries=tuple(
            MatchResultCandidateEntry(
                entry_id=row.entry_id,
                entry_number=row.entry_number,
                rank=row.rank,
                popularity_rank=row.popularity_rank,
                margin=row.margin,
            )
            for row in draft.rows
        ),
        finish_time_ms=92_300,
    )
