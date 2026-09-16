"""Discord Match Entry replacement adapter tests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest
from discord.ui.view import ViewStore

from uma_st2.adapters.discord import (
    MatchEntryCandidateDetailView,
    MatchEntryCandidateSelectionDraft,
    MatchEntryCandidateView,
    MatchEntryDiscordAdapter,
    MatchEntryInputView,
    MatchEntryPreviewView,
    MatchEntryReplacementModal,
    MatchEntryReplacementPreview,
    MatchStaffInteractionContext,
    format_match_entry_preview,
    parse_match_entry_lines,
)
from uma_st2.application.match import (
    MatchEntryAccountTarget,
    MatchEntryCandidateDraft,
    MatchEntryCandidateRow,
    MatchEntryCharacterTarget,
    MatchEntryDraftEntry,
    MatchEntryReplacementDraft,
    MatchEntryRosterSnapshot,
    MatchEntrySearchLine,
    MatchEntrySelection,
    MatchEntrySnapshot,
    MatchEntryTargetChoice,
    ReplacedMatchEntries,
    ReplaceMatchEntries,
)
from uma_st2.domain.identity import GameRegion
from uma_st2.domain.match import MatchSourceKind, MatchStatus

NOW = datetime(2026, 8, 28, 4, 0, tzinfo=UTC)


def _account(index: int) -> MatchEntryAccountTarget:
    return MatchEntryAccountTarget(
        id=100 + index,
        persona_id=f"00000000-0000-0000-0000-{index:012d}",
        game_region=GameRegion.KR,
        nickname=f"계정 @{index}",
    )


def _character(index: int) -> MatchEntryCharacterTarget:
    return MatchEntryCharacterTarget(
        umamusume_id=200 + index,
        umamusume_variant_id=None,
        display_name=f"말 **{index}**",
    )


def _current_entry(index: int) -> MatchEntrySnapshot:
    account = _account(index)
    character = _character(index)
    return MatchEntrySnapshot(
        entry_id=300 + index,
        entry_number=index,
        game_account_id=account.id,
        owner_at_event_persona_id=account.persona_id,
        affiliation_at_event=None,
        game_region=account.game_region,
        game_account_name=account.nickname,
        umamusume_id=character.umamusume_id,
        umamusume_variant_id=None,
        umamusume_name=character.display_name,
        created_at=NOW,
    )


def _roster(*, count: int = 0) -> MatchEntryRosterSnapshot:
    return MatchEntryRosterSnapshot(
        match_id=71,
        match_name="제12회 @everyone 정기전",
        source_kind=MatchSourceKind.NATIVE_V2,
        status=MatchStatus.SCHEDULED,
        entries=tuple(_current_entry(index) for index in range(1, count + 1)),
    )


class RecordingQueries:
    def __init__(self, *, desired_count: int = 2) -> None:
        self.desired_count = desired_count
        self.target = _roster(count=1)
        self.lines_seen: list[tuple[MatchEntrySearchLine, ...]] = []
        self.selections_seen: list[tuple[MatchEntrySelection, ...]] = []

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchEntryTargetChoice, ...]:
        return (MatchEntryTargetChoice(71, self.target.match_name, MatchStatus.SCHEDULED, 1),)

    def get_target(self, *, match_id: int) -> MatchEntryRosterSnapshot:
        return self.target

    def prepare_candidates(
        self,
        *,
        match_id: int,
        lines: tuple[MatchEntrySearchLine, ...],
    ) -> MatchEntryCandidateDraft:
        self.lines_seen.append(lines)
        return MatchEntryCandidateDraft(
            current=self.target,
            rows=tuple(
                MatchEntryCandidateRow(
                    entry_number=index,
                    search=line,
                    accounts=(_account(index + 20),),
                )
                for index, line in enumerate(lines, start=1)
            ),
            characters=tuple(_character(index + 20) for index in range(1, 31)),
        )

    def prepare_replacement(
        self,
        *,
        match_id: int,
        selections: tuple[MatchEntrySelection, ...],
    ) -> MatchEntryReplacementDraft:
        self.selections_seen.append(selections)
        return MatchEntryReplacementDraft(
            current=self.target,
            desired_entries=tuple(
                MatchEntryDraftEntry(
                    index,
                    _account(selection.game_account_id - 100),
                    MatchEntryCharacterTarget(
                        selection.umamusume_id,
                        selection.umamusume_variant_id,
                        f"말 **{selection.umamusume_id - 200}**",
                    ),
                )
                for index, selection in enumerate(selections, start=1)
            ),
        )


class RecordingCommands:
    def __init__(self) -> None:
        self.calls: list[ReplaceMatchEntries] = []

    def replace_entries(self, command: ReplaceMatchEntries) -> ReplacedMatchEntries:
        self.calls.append(command)
        entries = tuple(
            MatchEntrySnapshot(
                entry_id=500 + reference.entry_number,
                entry_number=reference.entry_number,
                game_account_id=reference.game_account_id,
                owner_at_event_persona_id=f"00000000-0000-0000-0000-{reference.entry_number:012d}",
                affiliation_at_event=None,
                game_region=GameRegion.KR,
                game_account_name=f"계정 {reference.entry_number}",
                umamusume_id=reference.umamusume_id,
                umamusume_variant_id=reference.umamusume_variant_id,
                umamusume_name=f"말 {reference.entry_number}",
                created_at=NOW,
            )
            for reference in command.entries
        )
        return ReplacedMatchEntries(
            snapshot=MatchEntryRosterSnapshot(
                match_id=71,
                match_name="제12회 정기전",
                source_kind=MatchSourceKind.NATIVE_V2,
                status=MatchStatus.SCHEDULED,
                entries=entries,
            )
        )


class RecordingAuthorization:
    def __init__(self, *, require_acknowledged: bool = False) -> None:
        self.calls: list[tuple[object, str]] = []
        self.require_acknowledged = require_acknowledged

    async def __call__(self, interaction: object, command_name: str) -> bool:
        if self.require_acknowledged:
            assert interaction.response.is_done()  # type: ignore[attr-defined]
        self.calls.append((interaction, command_name))
        return True


@dataclass
class RecordingResponse:
    messages: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    edits: list[dict[str, object]] = field(default_factory=list)
    modals: list[discord.ui.Modal] = field(default_factory=list)
    defers: list[dict[str, object]] = field(default_factory=list)
    done: bool = False

    def is_done(self) -> bool:
        return self.done

    async def defer(self, **kwargs: object) -> None:
        self.defers.append(kwargs)
        self.done = True

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))
        self.done = True

    async def edit_message(self, **kwargs: object) -> None:
        self.edits.append(kwargs)
        self.done = True

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self.modals.append(modal)
        self.done = True


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
        view_store: ViewStore | None = None,
        message_id: int | None = None,
        edit_errors: list[Exception] | None = None,
    ) -> None:
        self.id = interaction_id
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.response = RecordingResponse()
        self.followup = RecordingFollowup()
        self.edits: list[dict[str, object]] = []
        self.view_store = view_store
        self.message_id = message_id
        self.edit_errors = list(edit_errors or ())

    async def edit_original_response(self, **kwargs: object) -> None:
        if self.edit_errors:
            raise self.edit_errors.pop(0)
        self.edits.append(kwargs)
        view = kwargs.get("view")
        if isinstance(view, discord.ui.LayoutView) and self.view_store is not None and self.message_id is not None:
            self.view_store.add_view(view, self.message_id)


async def _inline[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    return operation()


def _adapter(
    *,
    queries: RecordingQueries | None = None,
    commands: RecordingCommands | None = None,
    authorization: RecordingAuthorization | None = None,
) -> tuple[MatchEntryDiscordAdapter, RecordingQueries, RecordingCommands]:
    query_double = queries or RecordingQueries()
    command_double = commands or RecordingCommands()
    authorization_double = authorization or RecordingAuthorization()
    return (
        MatchEntryDiscordAdapter(
            queries=query_double,  # type: ignore[arg-type]
            commands=command_double,  # type: ignore[arg-type]
            authorize_autocomplete=authorization_double,
            authorize_interaction=authorization_double,
            blocking_runner=_inline,  # type: ignore[arg-type]
        ),
        query_double,
        command_double,
    )


def _layout_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def _layout_button(view: discord.ui.LayoutView, *, label: str) -> discord.ui.Button:
    return next(item for item in view.walk_children() if isinstance(item, discord.ui.Button) and item.label == label)


def _track_stop(view: discord.ui.LayoutView) -> list[bool]:
    calls: list[bool] = []
    original_stop = view.stop

    def record_stop() -> None:
        calls.append(True)
        original_stop()

    view.stop = record_stop  # type: ignore[method-assign]
    return calls


def test_setup_handoff_opens_entry_editor_in_same_message() -> None:
    adapter, queries, commands = _adapter()
    interaction = RecordingInteraction()
    context = MatchStaffInteractionContext.from_interaction(interaction)
    source = discord.ui.LayoutView(timeout=600)
    stop_calls = _track_stop(source)

    asyncio.run(
        adapter.start_replacement(
            interaction,  # type: ignore[arg-type]
            context=context,
            match_id=71,
            reason=None,
            source_view=source,
        )
    )

    assert interaction.response.defers == [{"thinking": False}]
    assert stop_calls == [True]
    assert isinstance(interaction.edits[0]["view"], MatchEntryInputView)
    assert commands.calls == []
    assert queries.target.match_id == 71


def test_setup_handoff_query_failure_keeps_source_view_live() -> None:
    adapter, queries, commands = _adapter()

    def fail_target(*, match_id: int) -> MatchEntryRosterSnapshot:
        raise ValueError(f"missing Match {match_id}")

    queries.get_target = fail_target  # type: ignore[method-assign]
    interaction = RecordingInteraction()
    context = MatchStaffInteractionContext.from_interaction(interaction)
    source = discord.ui.LayoutView(timeout=600)
    stop_calls = _track_stop(source)

    asyncio.run(
        adapter.start_replacement(
            interaction,  # type: ignore[arg-type]
            context=context,
            match_id=71,
            reason=None,
            source_view=source,
        )
    )

    assert interaction.response.defers == [{"thinking": False}]
    assert stop_calls == []
    assert interaction.edits == []
    assert interaction.followup.messages
    assert commands.calls == []


def test_bulk_parser_accepts_one_game_account_chunk_per_non_empty_line() -> None:
    parsed = parse_match_entry_lines("계정 하나\n\n계정 둘")

    assert [line.account_chunk for line in parsed] == ["계정 하나", "계정 둘"]


def test_modal_submit_builds_zero_write_candidate_picker() -> None:
    adapter, queries, commands = _adapter()
    opening = RecordingInteraction()
    context = MatchStaffInteractionContext.from_interaction(opening)
    source = MatchEntryInputView(adapter=adapter, context=context, target=queries.target, reason=None)
    interaction = RecordingInteraction(interaction_id=600)

    assert "최대 18명" in _layout_text(source)
    modal = MatchEntryReplacementModal(
        adapter=adapter,
        context=context,
        target=queries.target,
        reason=None,
        source_view=source,
    )
    assert modal.entries.label == "게임계정 일부 혹은 전체"

    asyncio.run(
        adapter.show_candidates(  # type: ignore[arg-type]
            interaction,
            context=context,
            target=queries.target,
            raw_entries="계정\n다른 계정",
            reason=None,
            source_view=source,
        )
    )

    assert commands.calls == []
    picker = interaction.edits[0]["view"]
    assert isinstance(picker, MatchEntryCandidateView)
    rendered = _layout_text(picker)
    assert "@everyone" not in rendered
    assert "Entry 설정" in rendered
    assert "PID" not in rendered
    assert [line.account_chunk for line in queries.lines_seen[0]] == ["계정", "다른 계정"]
    entry_buttons = [
        item
        for item in picker.walk_children()
        if isinstance(item, discord.ui.Button)
        and item.custom_id
        and item.custom_id.startswith("match-entry-candidate-entry-")
    ]
    assert [item.custom_id for item in entry_buttons] == [
        "match-entry-candidate-entry-1",
        "match-entry-candidate-entry-2",
    ]


def test_modal_submit_supports_eighteen_entries_without_component_overflow() -> None:
    async def scenario() -> None:
        authorization = RecordingAuthorization(require_acknowledged=True)
        adapter, queries, _ = _adapter(authorization=authorization)
        opening = RecordingInteraction()
        context = MatchStaffInteractionContext.from_interaction(opening)
        source = MatchEntryInputView(adapter=adapter, context=context, target=queries.target, reason=None)
        interaction = RecordingInteraction(interaction_id=602)

        await adapter.show_candidates(
            interaction,  # type: ignore[arg-type]
            context=context,
            target=queries.target,
            raw_entries="\n".join(f"계정 {index}" for index in range(1, 19)),
            reason=None,
            source_view=source,
        )

        assert interaction.response.defers == [{"thinking": False}]
        assert authorization.calls == [(interaction, "match.staff.race-entries-set")]
        roster = interaction.edits[0]["view"]
        assert isinstance(roster, MatchEntryCandidateView)
        entry_buttons = [
            item
            for item in roster.walk_children()
            if isinstance(item, discord.ui.Button)
            and bool(item.custom_id)
            and item.custom_id.startswith("match-entry-candidate-entry-")
        ]
        assert len(entry_buttons) == 18
        assert roster.total_children_count <= 40
        assert roster.content_length() <= 4000
        assert roster.to_components()

    asyncio.run(scenario())


def test_modal_submit_unexpected_query_failure_keeps_input_live_and_reports_reference() -> None:
    async def scenario() -> None:
        authorization = RecordingAuthorization(require_acknowledged=True)
        adapter, queries, commands = _adapter(authorization=authorization)

        def fail_candidates(**_kwargs: object) -> MatchEntryCandidateDraft:
            raise RuntimeError("database unavailable")

        queries.prepare_candidates = fail_candidates  # type: ignore[method-assign]
        opening = RecordingInteraction()
        context = MatchStaffInteractionContext.from_interaction(opening)
        source = MatchEntryInputView(adapter=adapter, context=context, target=queries.target, reason=None)
        interaction = RecordingInteraction(interaction_id=603)

        await adapter.show_candidates(
            interaction,  # type: ignore[arg-type]
            context=context,
            target=queries.target,
            raw_entries="계정 1",
            reason=None,
            source_view=source,
        )

        assert interaction.response.defers == [{"thinking": False}]
        assert authorization.calls == [(interaction, "match.staff.race-entries-set")]
        assert source.is_finished() is False
        assert interaction.edits == []
        assert commands.calls == []
        assert len(interaction.followup.messages) == 1
        assert "참조 ID: `603`" in interaction.followup.messages[0][0]

    asyncio.run(scenario())


def test_candidate_selection_replacement_keeps_cancel_and_selects_registered() -> None:
    async def scenario() -> None:
        authorization = RecordingAuthorization(require_acknowledged=True)
        adapter, queries, _ = _adapter(authorization=authorization)
        context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
        candidates = queries.prepare_candidates(
            match_id=71,
            lines=(MatchEntrySearchLine("계정"),),
        )
        draft = MatchEntryCandidateSelectionDraft(candidates=candidates)
        source = MatchEntryCandidateDetailView(
            adapter=adapter,
            context=context,
            draft=draft,
            reason=None,
        )
        message_id = 777
        view_store = ViewStore(SimpleNamespace())  # type: ignore[arg-type]
        view_store.add_view(source, message_id)
        interaction = RecordingInteraction(view_store=view_store, message_id=message_id)

        await adapter.update_candidate_account(
            interaction,  # type: ignore[arg-type]
            context=context,
            draft=draft,
            reason=None,
            account_id=121,
            source_view=source,
        )

        assert interaction.response.defers == [{"thinking": False}]
        assert authorization.calls == [(interaction, "match.staff.race-entries-set")]
        replacement = interaction.edits[0]["view"]
        dispatch_items = tuple(view_store._views[message_id].values())  # noqa: SLF001
        assert dispatch_items
        assert all(item.view is replacement for item in dispatch_items)
        assert any(item.custom_id == "match-entry-replacement-cancel" for item in dispatch_items)

    asyncio.run(scenario())


def test_candidate_selection_replacement_failure_requires_fresh_entry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        authorization = RecordingAuthorization(require_acknowledged=True)
        adapter, queries, _ = _adapter(authorization=authorization)
        context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
        candidates = queries.prepare_candidates(
            match_id=71,
            lines=(MatchEntrySearchLine("계정"),),
        )
        draft = MatchEntryCandidateSelectionDraft(candidates=candidates)
        source = MatchEntryCandidateDetailView(
            adapter=adapter,
            context=context,
            draft=draft,
            reason=None,
        )
        interaction = RecordingInteraction(edit_errors=[RuntimeError("replacement failed")])

        with caplog.at_level(logging.ERROR):
            await adapter.update_candidate_account(
                interaction,  # type: ignore[arg-type]
                context=context,
                draft=draft,
                reason=None,
                account_id=121,
                source_view=source,
            )

        assert interaction.response.defers == [{"thinking": False}]
        assert authorization.calls == [(interaction, "match.staff.race-entries-set")]
        assert source.is_finished() is True
        assert interaction.edits == []
        assert len(interaction.followup.messages) == 1
        assert "/match staff race" in interaction.followup.messages[0][0]
        assert "response_kind=entry-candidate-account" in caplog.text

    asyncio.run(scenario())


def test_entry_button_opens_detail_and_pages_all_canonical_umamusume() -> None:
    async def scenario() -> None:
        adapter, queries, _ = _adapter()
        context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
        candidates = queries.prepare_candidates(
            match_id=71,
            lines=(MatchEntrySearchLine("계정 하나"), MatchEntrySearchLine("계정 둘")),
        )
        draft = MatchEntryCandidateSelectionDraft(candidates=candidates)
        roster = MatchEntryCandidateView(
            adapter=adapter,
            context=context,
            draft=draft,
            reason=None,
        )
        message_id = 778
        view_store = ViewStore(SimpleNamespace())  # type: ignore[arg-type]
        view_store.add_view(roster, message_id)
        entry_button = next(
            item
            for item in roster.walk_children()
            if isinstance(item, discord.ui.Button) and item.custom_id == "match-entry-candidate-entry-2"
        )
        interaction = RecordingInteraction(view_store=view_store, message_id=message_id)

        await entry_button.callback(interaction)  # type: ignore[arg-type]

        assert interaction.response.defers == [{"thinking": False}]
        detail = interaction.edits[0]["view"]
        assert isinstance(detail, MatchEntryCandidateDetailView)
        assert "Entry 2 설정" in _layout_text(detail)
        assert all(item.view is detail for item in view_store._views[message_id].values())  # noqa: SLF001
        detail_buttons = [item for item in detail.walk_children() if isinstance(item, discord.ui.Button)]
        assert [item.label for item in detail_buttons] == [
            "이전 캐릭터",
            "다음 캐릭터",
            "선택",
            "취소",
        ]
        assert [item.custom_id for item in detail_buttons] == [
            "match-entry-character-page-이전",
            "match-entry-character-page-다음",
            "match-entry-candidate-back",
            "match-entry-replacement-cancel",
        ]
        character_select = next(
            item
            for item in detail.walk_children()
            if isinstance(item, discord.ui.Select) and item.custom_id == "match-entry-candidate-character"
        )
        assert len(character_select.options) == 25
        next_button = next(
            item
            for item in detail.walk_children()
            if isinstance(item, discord.ui.Button)
            and item.custom_id
            and item.custom_id.startswith("match-entry-character-page-")
            and not item.disabled
        )
        paged_interaction = RecordingInteraction(
            interaction_id=601,
            view_store=view_store,
            message_id=message_id,
        )

        await next_button.callback(paged_interaction)  # type: ignore[arg-type]

        assert paged_interaction.response.defers == [{"thinking": False}]
        second_page = paged_interaction.edits[0]["view"]
        assert isinstance(second_page, MatchEntryCandidateDetailView)
        assert "우마무스메 목록: 2/2" in _layout_text(second_page)
        assert all(item.view is second_page for item in view_store._views[message_id].values())  # noqa: SLF001
        second_page_select = next(
            item
            for item in second_page.walk_children()
            if isinstance(item, discord.ui.Select) and item.custom_id == "match-entry-candidate-character"
        )
        assert len(second_page_select.options) == 5
        assert any(
            isinstance(item, discord.ui.Button) and item.custom_id == "match-entry-replacement-cancel"
            for item in second_page.walk_children()
        )

    asyncio.run(scenario())


def test_preview_unexpected_query_failure_keeps_candidate_view_live_and_reports_reference() -> None:
    async def scenario() -> None:
        authorization = RecordingAuthorization(require_acknowledged=True)
        adapter, queries, commands = _adapter(authorization=authorization)

        def fail_replacement(**_kwargs: object) -> MatchEntryReplacementDraft:
            raise RuntimeError("database unavailable")

        queries.prepare_replacement = fail_replacement  # type: ignore[method-assign]
        context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
        candidates = queries.prepare_candidates(
            match_id=71,
            lines=(MatchEntrySearchLine("계정"),),
        )
        draft = MatchEntryCandidateSelectionDraft(candidates=candidates).with_account(121).with_character((221, None))
        source = MatchEntryCandidateView(
            adapter=adapter,
            context=context,
            draft=draft,
            reason=None,
        )
        interaction = RecordingInteraction(interaction_id=604)

        await adapter.prepare_preview(
            interaction,  # type: ignore[arg-type]
            context=context,
            draft=draft,
            reason=None,
            source_view=source,
        )

        assert interaction.response.defers == [{"thinking": False}]
        assert authorization.calls == [(interaction, "match.staff.race-entries-set")]
        assert source.is_finished() is False
        assert interaction.edits == []
        assert commands.calls == []
        assert len(interaction.followup.messages) == 1
        assert "참조 ID: `604`" in interaction.followup.messages[0][0]

    asyncio.run(scenario())


def test_final_confirm_calls_command_once_with_final_interaction_key() -> None:
    adapter, queries, commands = _adapter()
    draft = queries.prepare_replacement(
        match_id=71,
        selections=(MatchEntrySelection(121, 221), MatchEntrySelection(122, 222)),
    )
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    view = MatchEntryPreviewView(
        adapter=adapter,
        context=context,
        preview=MatchEntryReplacementPreview(
            draft=draft,
            search_lines=parse_match_entry_lines("계정 1\n계정 2"),
            reason="공식 명단",
        ),
    )
    interaction = RecordingInteraction(interaction_id=991)

    asyncio.run(_layout_button(view, label="Entry 교체 확정").callback(interaction))  # type: ignore[arg-type]

    assert len(commands.calls) == 1
    command = commands.calls[0]
    assert command.idempotency_key == "match-entry-replacement:991"
    assert command.correlation_id == "991"
    assert command.actor_discord_user_id == "123"
    assert command.guild_id == "987"
    receipt = interaction.edits[0]["view"]
    assert isinstance(receipt, discord.ui.LayoutView)
    assert "Entry 저장 완료" in _layout_text(receipt)

    duplicate = RecordingInteraction(interaction_id=992)
    asyncio.run(
        adapter.confirm_replacement(  # type: ignore[arg-type]
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


def test_multi_page_preview_requires_every_page_before_confirm() -> None:
    queries = RecordingQueries(desired_count=9)
    adapter, _, commands = _adapter(queries=queries)
    draft = queries.prepare_replacement(
        match_id=71,
        selections=tuple(MatchEntrySelection(120 + index, 220 + index) for index in range(1, 10)),
    )
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    first = MatchEntryPreviewView(
        adapter=adapter,
        context=context,
        preview=MatchEntryReplacementPreview(
            draft=draft,
            search_lines=tuple(MatchEntrySearchLine(f"계정 {index}") for index in range(1, 10)),
        ),
    )

    assert _layout_button(first, label="Entry 교체 확정").disabled is True
    page_interaction = RecordingInteraction(interaction_id=700)
    asyncio.run(_layout_button(first, label="다음").callback(page_interaction))  # type: ignore[arg-type]
    assert page_interaction.response.defers == [{"thinking": False}]
    second = page_interaction.edits[0]["view"]
    assert isinstance(second, MatchEntryPreviewView)
    assert second.preview.all_pages_reviewed is True
    assert _layout_button(second, label="Entry 교체 확정").disabled is False
    assert commands.calls == []


def test_context_mismatch_is_zero_write() -> None:
    adapter, queries, commands = _adapter()
    context = MatchStaffInteractionContext.from_interaction(RecordingInteraction())
    source = MatchEntryInputView(adapter=adapter, context=context, target=queries.target, reason=None)
    mismatch = RecordingInteraction(user_id=999)

    asyncio.run(
        adapter.show_candidates(  # type: ignore[arg-type]
            mismatch,
            context=context,
            target=queries.target,
            raw_entries="계정",
            reason=None,
            source_view=source,
        )
    )

    assert commands.calls == []
    assert mismatch.response.messages
    assert "111" not in format_match_entry_preview(
        MatchEntryReplacementPreview(
            draft=queries.prepare_replacement(
                match_id=71,
                selections=(MatchEntrySelection(121, 221), MatchEntrySelection(122, 222)),
            ),
            search_lines=parse_match_entry_lines("계정 1\n계정 2"),
        )
    )
