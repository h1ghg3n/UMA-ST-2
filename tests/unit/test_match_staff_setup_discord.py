"""Discord adapter-local Match configured-create and setup editor tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import discord
from discord.ui.view import ViewStore

from uma_st2.adapters.discord import (
    MatchConditionSelection,
    MatchSetupBasicModal,
    MatchSetupConditionView,
    MatchSetupConfirmView,
    MatchSetupDiscordAdapter,
    MatchSetupDraft,
    MatchSetupEditorView,
    MatchSetupMode,
    MatchStaffInteractionContext,
)
from uma_st2.adapters.discord.match_staff_setup import MatchSetupBasicView
from uma_st2.application.match import (
    CreatedMatch,
    CreateMatch,
    MatchConditionRecord,
    MatchConditionValues,
    MatchCourseChoice,
    MatchCreationCourse,
    MatchCreationSnapshot,
    MatchSetupEditorTarget,
    MatchSetupTarget,
    UpdatedMatchSetup,
    UpdateMatchSetup,
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

SCHEDULED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


def _choice() -> MatchCourseChoice:
    return MatchCourseChoice(
        id=21,
        stadium_id=2,
        stadium_name="나카야마 @everyone",
        surface=MatchSurface.TURF,
        distance=2500,
        direction=MatchDirection.RIGHT,
        layout=StadiumCourseLayout.OUTER_TO_INNER,
    )


def _course() -> MatchCreationCourse:
    choice = _choice()
    return MatchCreationCourse(
        id=choice.id,
        stadium_id=choice.stadium_id,
        stadium_name=choice.stadium_name,
        surface=choice.surface,
        distance=choice.distance,
        direction=choice.direction,
        layout=choice.layout,
    )


def _values() -> MatchConditionValues:
    return MatchConditionValues(
        season=MatchSeason.AUTUMN,
        weather=MatchWeather.SUNNY,
        time_of_day=MatchTimeOfDay.NIGHT,
        track_condition=MatchTrackCondition.FIRM,
    )


def _record(*, changed_at: datetime = NOW) -> MatchConditionRecord:
    return MatchConditionRecord(values=_values(), created_at=NOW, updated_at=changed_at)


def _target() -> MatchSetupEditorTarget:
    return MatchSetupEditorTarget(
        setup=MatchSetupTarget(
            match_id=71,
            name="제12회 정기전",
            description="기존 설명",
            source_kind=MatchSourceKind.NATIVE_V2,
            grade=MatchGrade.G1,
            course=_course(),
            scheduled_at=SCHEDULED_AT,
            status=MatchStatus.SCHEDULED,
            condition=_record(),
            updated_at=NOW,
            setup_version=5,
        ),
        entry_count=3,
    )


class RecordingCreationQueries:
    def __init__(self) -> None:
        self.calls = 0

    def list_course_choices(self) -> tuple[MatchCourseChoice, ...]:
        self.calls += 1
        return (_choice(),)


class RecordingSetupQueries:
    def __init__(self) -> None:
        self.search_calls = 0
        self.get_calls: list[int] = []

    def search_targets(self, *, search: str = "", limit: int = 25) -> tuple[MatchSetupEditorTarget, ...]:
        self.search_calls += 1
        return (_target(),)

    def get_target(self, *, match_id: int) -> MatchSetupEditorTarget | None:
        self.get_calls.append(match_id)
        return _target() if match_id == 71 else None


class RecordingCommands:
    def __init__(self) -> None:
        self.creation_calls: list[CreateMatch] = []
        self.setup_calls: list[UpdateMatchSetup] = []

    def create_match(self, command: CreateMatch) -> CreatedMatch:
        self.creation_calls.append(command)
        return CreatedMatch(
            snapshot=MatchCreationSnapshot(
                match_id=72,
                name=command.name,
                description=command.description,
                source_kind=MatchSourceKind.NATIVE_V2,
                grade=command.grade,
                course=_course(),
                scheduled_at=command.scheduled_at,
                condition=_record(),
                status=MatchStatus.SCHEDULED,
                created_at=NOW,
            )
        )

    def update_setup(self, command: UpdateMatchSetup) -> UpdatedMatchSetup:
        self.setup_calls.append(command)
        return UpdatedMatchSetup(
            snapshot=MatchSetupTarget(
                match_id=command.match_id,
                name=command.name,
                description=command.description,
                source_kind=MatchSourceKind.NATIVE_V2,
                grade=command.grade,
                course=_course(),
                scheduled_at=command.scheduled_at,
                status=MatchStatus.SCHEDULED,
                condition=_record(),
                updated_at=NOW,
                setup_version=None,
            )
        )


class RecordingEntryAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str | None]] = []
        self.source_views: list[discord.ui.LayoutView] = []

    async def start_replacement(
        self,
        interaction: object,
        *,
        context: MatchStaffInteractionContext,
        match_id: int,
        reason: str | None,
        source_view: discord.ui.LayoutView,
    ) -> None:
        self.calls.append((match_id, reason))
        self.source_views.append(source_view)


class RecordingPreparation:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[object, str, bool]] = []

    async def __call__(self, interaction: object, command_name: str, *, ephemeral: bool) -> bool:
        self.calls.append((interaction, command_name, ephemeral))
        return self.allowed


class RecordingAuthorization:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[object, str]] = []

    async def __call__(self, interaction: object, command_name: str) -> bool:
        self.calls.append((interaction, command_name))
        return self.allowed


@dataclass
class RecordingResponse:
    edits: list[dict[str, object]] = field(default_factory=list)
    messages: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    modals: list[discord.ui.Modal] = field(default_factory=list)
    defers: list[dict[str, object]] = field(default_factory=list)
    done: bool = False

    def is_done(self) -> bool:
        return self.done

    async def defer(self, **kwargs: object) -> None:
        self.defers.append(kwargs)
        self.done = True

    async def edit_message(self, **kwargs: object) -> None:
        self.edits.append(kwargs)
        self.done = True

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))
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
        fail_edit: bool = False,
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
        self.fail_edit = fail_edit

    async def edit_original_response(self, **kwargs: object) -> None:
        if self.fail_edit:
            raise RuntimeError("simulated edit failure")
        self.edits.append(kwargs)
        view = kwargs.get("view")
        if isinstance(view, discord.ui.LayoutView) and self.view_store is not None and self.message_id is not None:
            self.view_store.add_view(view, self.message_id)


async def _inline[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    return operation()


def _adapter() -> tuple[
    MatchSetupDiscordAdapter,
    RecordingCreationQueries,
    RecordingSetupQueries,
    RecordingCommands,
    RecordingEntryAdapter,
    RecordingPreparation,
    RecordingAuthorization,
]:
    creation_queries = RecordingCreationQueries()
    setup_queries = RecordingSetupQueries()
    commands = RecordingCommands()
    entries = RecordingEntryAdapter()
    preparation = RecordingPreparation()
    authorization = RecordingAuthorization()
    return (
        MatchSetupDiscordAdapter(
            creation_queries=creation_queries,  # type: ignore[arg-type]
            setup_queries=setup_queries,  # type: ignore[arg-type]
            creation_commands=commands,  # type: ignore[arg-type]
            setup_commands=commands,  # type: ignore[arg-type]
            entry_adapter=entries,
            authorize_interaction=authorization,
            blocking_runner=_inline,  # type: ignore[arg-type]
        ),
        creation_queries,
        setup_queries,
        commands,
        entries,
        preparation,
        authorization,
    )


def _button(view: discord.ui.LayoutView, label: str) -> discord.ui.Button:
    return next(item for item in view.walk_children() if isinstance(item, discord.ui.Button) and item.label == label)


def _layout_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def _track_stop(view: discord.ui.LayoutView) -> list[bool]:
    calls: list[bool] = []
    original_stop = view.stop

    def record_stop() -> None:
        calls.append(True)
        original_stop()

    view.stop = record_stop  # type: ignore[method-assign]
    return calls


def _complete_create_draft() -> MatchSetupDraft:
    return MatchSetupDraft(
        mode=MatchSetupMode.CREATE,
        course_choices=(_choice(),),
        name="제13회 @everyone 정기전",
        description="새 설명",
        grade=MatchGrade.LISTED,
        scheduled_at=SCHEDULED_AT,
        course=_choice(),
        condition=_values(),
    )


def _complete_edit_draft() -> MatchSetupDraft:
    return MatchSetupDraft(
        mode=MatchSetupMode.EDIT,
        course_choices=(_choice(),),
        target=_target(),
        name="제12회 정기전 수정",
        description="수정 설명",
        grade=MatchGrade.G2,
        scheduled_at=SCHEDULED_AT,
        course=_choice(),
        condition=_values(),
        reason="운영 정정",
    )


def test_creation_open_and_all_editor_subviews_are_zero_write() -> None:
    adapter, creation_queries, _, commands, _, preparation, authorization = _adapter()
    interaction = RecordingInteraction()
    context = MatchStaffInteractionContext.from_interaction(interaction)

    asyncio.run(adapter.start_creation(interaction, context=context, source_view=None))  # type: ignore[arg-type]

    assert interaction.response.defers == [{"thinking": False}]
    view = interaction.edits[0]["view"]
    assert isinstance(view, MatchSetupEditorView)
    assert creation_queries.calls == 1
    assert commands.creation_calls == commands.setup_calls == []
    assert preparation.calls == []

    condition_interaction = RecordingInteraction()
    asyncio.run(_button(view, "환경 조건 편집").callback(condition_interaction))  # type: ignore[arg-type]
    assert condition_interaction.response.defers == [{"thinking": False}]
    condition_view = condition_interaction.edits[0]["view"]
    assert isinstance(condition_view, MatchSetupConditionView)
    condition_selects = {
        item.custom_id: item for item in condition_view.walk_children() if isinstance(item, discord.ui.Select)
    }
    weather_random = next(
        option for option in condition_selects["match-setup-condition-weather"].options if option.value == "random"
    )
    track_random = next(
        option
        for option in condition_selects["match-setup-condition-track_condition"].options
        if option.value == "random"
    )
    assert weather_random.label == "랜덤"
    assert track_random.label == "랜덤"
    assert authorization.calls == [(condition_interaction, "match.staff.race")]
    assert commands.creation_calls == commands.setup_calls == []


def test_setup_open_edit_failure_closes_source_and_sends_reopen_notice() -> None:
    adapter, *_ = _adapter()
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    source = MatchSetupEditorView(adapter=adapter, context=context, draft=_complete_create_draft())
    stop_calls = _track_stop(source)
    interaction = RecordingInteraction(fail_edit=True)

    asyncio.run(adapter.start_creation(interaction, context=context, source_view=source))  # type: ignore[arg-type]

    assert interaction.response.defers == [{"thinking": False}]
    assert stop_calls == [True]
    assert interaction.followup.messages
    assert "/match staff race" in interaction.followup.messages[0][0]


def test_empty_edit_target_query_keeps_source_panel_live() -> None:
    adapter, _, setup_queries, commands, _, _, _ = _adapter()
    setup_queries.search_targets = lambda *, search="", limit=25: ()  # type: ignore[method-assign]
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    source = discord.ui.LayoutView(timeout=600)
    stop_calls = _track_stop(source)
    interaction = RecordingInteraction()

    asyncio.run(adapter.show_edit_targets(interaction, context=context, source_view=source))  # type: ignore[arg-type]

    assert interaction.response.defers == [{"thinking": False}]
    assert stop_calls == []
    assert interaction.edits == []
    assert interaction.followup.messages[0][0] == "현재 선택할 수 있는 대상 경기가 없습니다."
    assert commands.creation_calls == commands.setup_calls == []


def test_invalid_modal_submit_keeps_source_editor_live() -> None:
    adapter, _, _, commands, _, _, authorization = _adapter()
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    draft = _complete_create_draft()
    source = MatchSetupBasicView(adapter=adapter, context=context, draft=draft)
    interaction = RecordingInteraction()

    asyncio.run(
        adapter.apply_basic_text(
            interaction,  # type: ignore[arg-type]
            context=context,
            draft=draft,
            name="경기",
            date_value="not-a-date",
            time_value="12:00",
            description="",
            reason="",
            source_view=source,
        )
    )

    assert interaction.response.defers == [{"thinking": False}]
    assert authorization.calls == [(interaction, "match.staff.race")]
    assert not source.is_finished()
    assert interaction.edits == []
    assert interaction.followup.messages
    assert commands.creation_calls == commands.setup_calls == []


def test_setup_cancel_replaces_components_v2_with_buttonless_terminal_layout() -> None:
    adapter, _, _, commands, _, _, authorization = _adapter()
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    source = MatchSetupEditorView(adapter=adapter, context=context, draft=_complete_create_draft())
    interaction = RecordingInteraction()

    asyncio.run(_button(source, "취소").callback(interaction))  # type: ignore[arg-type]

    assert interaction.response.defers == [{"thinking": False}]
    payload = interaction.edits[0]
    terminal = payload["view"]
    assert payload["content"] is None
    assert payload["embeds"] == []
    assert payload["attachments"] == []
    assert isinstance(terminal, discord.ui.LayoutView)
    assert _layout_text(terminal) == "룸매치 설정 작업을 취소했습니다. DB에는 기록되지 않았습니다."
    assert not any(isinstance(item, discord.ui.Button) for item in terminal.walk_children())
    assert authorization.calls == [(interaction, "match.staff.race")]
    assert commands.creation_calls == commands.setup_calls == []


def test_metadata_modal_is_text_input_only_and_prefills_edit_values() -> None:
    adapter, *_ = _adapter()
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    draft = _complete_edit_draft()
    modal = MatchSetupBasicModal(
        adapter=adapter,
        context=context,
        draft=draft,
        source_view=None,
    )

    assert len(modal.children) == 5
    assert all(isinstance(item, discord.ui.TextInput) for item in modal.children)
    assert modal.name.default == draft.name
    assert modal.description.default == draft.description
    assert modal.reason.required is True
    assert modal.reason.default == "운영 정정"

    source = MatchSetupBasicView(adapter=adapter, context=context, draft=draft)
    interaction = RecordingInteraction()
    asyncio.run(_button(source, "텍스트·일정 입력").callback(interaction))  # type: ignore[arg-type]
    assert interaction.response.defers == []
    assert len(interaction.response.modals) == 1
    assert not source.is_finished()


def test_basic_choice_replacement_keeps_new_view_registered() -> None:
    async def scenario() -> None:
        adapter, *_ = _adapter()
        context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
        draft = MatchSetupDraft(mode=MatchSetupMode.CREATE, course_choices=(_choice(),))
        source = MatchSetupBasicView(adapter=adapter, context=context, draft=draft)
        message_id = 777
        view_store = ViewStore(SimpleNamespace())  # type: ignore[arg-type]
        view_store.add_view(source, message_id)
        interaction = RecordingInteraction(view_store=view_store, message_id=message_id)

        await adapter.update_basic_choice(
            interaction,  # type: ignore[arg-type]
            context=context,
            draft=draft,
            field="grade",
            value=MatchGrade.G2.value,
            source_view=source,
        )

        assert interaction.response.defers == [{"thinking": False}]
        replacement = interaction.edits[0]["view"]
        dispatch_items = tuple(view_store._views[message_id].values())  # noqa: SLF001
        assert dispatch_items
        assert all(item.view is replacement for item in dispatch_items)

    asyncio.run(scenario())


def test_condition_selection_and_entry_handoff_change_no_canonical_setup() -> None:
    adapter, _, _, commands, entries, _, _ = _adapter()
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    draft = _complete_edit_draft()
    source = MatchSetupEditorView(adapter=adapter, context=context, draft=draft)
    interaction = RecordingInteraction()

    asyncio.run(_button(source, "Entry 편집").callback(interaction))  # type: ignore[arg-type]

    assert entries.calls == [(71, None)]
    assert entries.source_views == [source]
    assert not source.is_finished()
    assert commands.creation_calls == commands.setup_calls == []

    partial = MatchConditionSelection()
    assert partial.to_values() is None
    assert MatchConditionSelection.from_values(_values()).to_values() == _values()


def test_create_final_confirm_calls_atomic_configured_create_once() -> None:
    adapter, _, _, commands, _, preparation, authorization = _adapter()
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    draft = _complete_create_draft()
    view = MatchSetupConfirmView(adapter=adapter, context=context, draft=draft)
    interaction = RecordingInteraction(interaction_id=991)

    asyncio.run(_button(view, "최종 확정").callback(interaction))  # type: ignore[arg-type]

    assert len(commands.creation_calls) == 1
    command = commands.creation_calls[0]
    assert command.condition == _values()
    assert command.idempotency_key == "match-create:991"
    assert command.actor_discord_user_id == "123"
    assert command.guild_id == "987"
    assert interaction.response.defers == [{"thinking": False}]
    terminal = interaction.edits[0]["view"]
    assert "룸매치 생성 완료" in _layout_text(terminal)
    assert "@everyone" not in _layout_text(terminal)
    assert preparation.calls == []
    assert authorization.calls == [(interaction, "match.staff.race")]

    retry_click = RecordingInteraction(interaction_id=992)
    asyncio.run(_button(view, "최종 확정").callback(retry_click))  # type: ignore[arg-type]
    assert len(commands.creation_calls) == 1
    assert retry_click.response.defers == [{"thinking": False}]
    assert "이미" in retry_click.followup.messages[0][0]


def test_edit_final_confirm_carries_preview_authority_and_required_reason() -> None:
    adapter, _, _, commands, _, _, _ = _adapter()
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    draft = _complete_edit_draft()
    view = MatchSetupConfirmView(adapter=adapter, context=context, draft=draft)
    interaction = RecordingInteraction(interaction_id=992)

    asyncio.run(_button(view, "최종 확정").callback(interaction))  # type: ignore[arg-type]

    command = commands.setup_calls[0]
    assert command.match_id == 71
    assert command.expected_state_fingerprint == _target().setup.state_fingerprint
    assert command.expected_setup_version == 5
    assert command.reason == "운영 정정"
    assert command.idempotency_key == "match-setup:992"
    assert "설정 수정 완료" in _layout_text(interaction.edits[0]["view"])


def test_committed_setup_with_delivery_failure_closes_source_and_sends_reopen_notice() -> None:
    adapter, _, _, commands, _, _, _ = _adapter()
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    draft = _complete_create_draft()
    view = MatchSetupConfirmView(adapter=adapter, context=context, draft=draft)
    stop_calls = _track_stop(view)
    interaction = RecordingInteraction(interaction_id=993, fail_edit=True)

    asyncio.run(_button(view, "최종 확정").callback(interaction))  # type: ignore[arg-type]

    assert len(commands.creation_calls) == 1
    assert stop_calls == [True]
    assert interaction.followup.messages
    assert "/match staff race" in interaction.followup.messages[0][0]


def test_preview_text_is_mention_safe_and_context_mismatch_is_zero_write() -> None:
    adapter, _, _, commands, _, preparation, authorization = _adapter()
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    draft = _complete_create_draft()
    view = MatchSetupEditorView(adapter=adapter, context=context, draft=draft)
    assert "@everyone" not in _layout_text(view)

    mismatch = RecordingInteraction(user_id=999)
    asyncio.run(_button(view, "최종 저장").callback(mismatch))  # type: ignore[arg-type]

    assert commands.creation_calls == commands.setup_calls == []
    assert preparation.calls == []
    assert authorization.calls == []
    assert mismatch.response.messages
