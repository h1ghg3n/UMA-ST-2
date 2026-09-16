"""Discord Match staff workflow registration and shared course-filter tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord import app_commands

from uma_st2.adapters.discord import (
    MatchCommandGroup,
    MatchCourseSelection,
    MatchStaffCommandGroup,
    MatchStaffInteractionContext,
    MatchStaffPanelKind,
    MatchStaffPanelView,
    MatchStaffReasonModal,
    MatchStaffTargetView,
    MatchStaffWorkflowDiscordAdapter,
    normalize_course_selection,
    selected_course,
    update_course_selection,
)
from uma_st2.application.match import MatchCourseChoice
from uma_st2.domain.match import (
    MatchDirection,
    MatchSurface,
    StadiumCourseLayout,
)


def _choices() -> tuple[MatchCourseChoice, ...]:
    return (
        MatchCourseChoice(
            id=11,
            stadium_id=1,
            stadium_name="도쿄",
            surface=MatchSurface.TURF,
            distance=2400,
            direction=MatchDirection.LEFT,
            layout=StadiumCourseLayout.STANDARD,
        ),
        MatchCourseChoice(
            id=12,
            stadium_id=1,
            stadium_name="도쿄",
            surface=MatchSurface.TURF,
            distance=2400,
            direction=MatchDirection.LEFT,
            layout=StadiumCourseLayout.OUTER,
        ),
        MatchCourseChoice(
            id=21,
            stadium_id=2,
            stadium_name="나카야마",
            surface=MatchSurface.TURF,
            distance=2500,
            direction=MatchDirection.RIGHT,
            layout=StadiumCourseLayout.OUTER_TO_INNER,
        ),
    )


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

    async def edit_message(self, **kwargs: object) -> None:
        self.done = True
        self.edits.append(kwargs)

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.done = True
        self.messages.append((content, kwargs))

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self.done = True
        self.modals.append(modal)

    async def defer(self, **kwargs: object) -> None:
        self.done = True
        self.defers.append(kwargs)

    def is_done(self) -> bool:
        return self.done


@dataclass
class RecordingFollowup:
    messages: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    async def send(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))


class RecordingInteraction:
    def __init__(
        self,
        *,
        user_id: int = 123,
        guild_id: int = 987,
        channel_id: int = 654,
        fail_original_edit: bool = False,
    ) -> None:
        self.id = 555
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.response = RecordingResponse()
        self.followup = RecordingFollowup()
        self.edits: list[dict[str, object]] = []
        self.fail_original_edit = fail_original_edit

    async def edit_original_response(self, **kwargs: object) -> None:
        if self.fail_original_edit:
            raise RuntimeError("simulated edit failure")
        self.edits.append(kwargs)


class RecordingSetupAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def start_creation(self, interaction: object, **kwargs: object) -> None:
        self.calls.append(("create", kwargs))

    async def show_edit_targets(self, interaction: object, **kwargs: object) -> None:
        self.calls.append(("edit", kwargs))


class RecordingActionAdapter:
    def __init__(self) -> None:
        self.autocomplete_calls: list[tuple[object, str]] = []
        self.action_calls: list[tuple[str, int, str | None]] = []
        self.odds_calls: list[tuple[object, object, object]] = []
        self.race_transition_calls: list[tuple[str, object, object]] = []
        self.result_transition_calls: list[tuple[str, object, object]] = []
        self.settlement_transition_calls: list[tuple[str, object, object]] = []

    async def autocomplete_targets(
        self,
        interaction: object,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        self.autocomplete_calls.append((interaction, current))
        return [app_commands.Choice(name="제12회 정기전", value=71)]

    async def preview_opening(
        self,
        interaction: object,
        *,
        match_id: int,
        context: object,
        source_view: object,
    ) -> None:
        self.action_calls.append(("open", match_id, None))
        self.race_transition_calls.append(("open", context, source_view))

    async def preview_close(
        self,
        interaction: object,
        *,
        match_id: int,
        context: object,
        source_view: object,
    ) -> None:
        self.action_calls.append(("close", match_id, None))
        self.race_transition_calls.append(("close", context, source_view))

    async def preview_cancellation(
        self,
        interaction: object,
        *,
        match_id: int,
        reason: str | None,
        context: object,
        source_view: object,
    ) -> None:
        self.action_calls.append(("cancel", match_id, reason))
        self.race_transition_calls.append(("cancel", context, source_view))

    async def start_submission(
        self,
        interaction: object,
        *,
        match_id: int,
        reason: str | None,
        context: object,
        source_view: object,
    ) -> None:
        self.action_calls.append(("submit", match_id, reason))
        self.result_transition_calls.append(("submit", context, source_view))

    async def start_review(
        self,
        interaction: object,
        *,
        match_id: int,
        context: object,
        source_view: object,
    ) -> None:
        self.action_calls.append(("review", match_id, None))
        self.result_transition_calls.append(("review", context, source_view))

    async def start_confirmation(
        self,
        interaction: object,
        *,
        match_id: int,
        context: object,
        source_view: object,
    ) -> None:
        self.action_calls.append(("confirm", match_id, None))
        self.result_transition_calls.append(("confirm", context, source_view))

    async def preview_settlement(
        self,
        interaction: object,
        *,
        match_id: int,
        reason: str | None,
        context: object,
        source_view: object,
    ) -> None:
        self.action_calls.append(("settle", match_id, reason))
        self.settlement_transition_calls.append(("settle", context, source_view))

    async def preview_rollback(
        self,
        interaction: object,
        *,
        match_id: int,
        reason: str,
        context: object,
        source_view: object,
    ) -> None:
        self.action_calls.append(("rollback", match_id, reason))
        self.settlement_transition_calls.append(("rollback", context, source_view))

    async def publish_result(
        self,
        interaction: object,
        *,
        match_id: int,
        context: object,
        source_view: object,
    ) -> None:
        self.action_calls.append(("recover", match_id, None))
        self.settlement_transition_calls.append(("recover", context, source_view))

    async def show_status(
        self,
        interaction: object,
        *,
        navigation: object,
        context: object,
        source_view: object,
    ) -> None:
        self.odds_calls.append((interaction, context, source_view))


def _workflow(
    *,
    preparation: RecordingPreparation | None = None,
    authorization: RecordingAuthorization | None = None,
) -> tuple[MatchStaffWorkflowDiscordAdapter, RecordingSetupAdapter, RecordingActionAdapter]:
    setup = RecordingSetupAdapter()
    actions = RecordingActionAdapter()
    adapter = MatchStaffWorkflowDiscordAdapter(
        setup_adapter=setup,  # type: ignore[arg-type]
        betting_open_adapter=actions,  # type: ignore[arg-type]
        betting_close_adapter=actions,  # type: ignore[arg-type]
        cancellation_adapter=actions,  # type: ignore[arg-type]
        result_submission_adapter=actions,  # type: ignore[arg-type]
        result_review_adapter=actions,  # type: ignore[arg-type]
        result_confirmation_adapter=actions,  # type: ignore[arg-type]
        settlement_adapter=actions,  # type: ignore[arg-type]
        settlement_rollback_adapter=actions,  # type: ignore[arg-type]
        publication_adapter=actions,  # type: ignore[arg-type]
        odds_mode_adapter=actions,  # type: ignore[arg-type]
        prepare_command=preparation or RecordingPreparation(),
        authorize_interaction=authorization or RecordingAuthorization(),
    )
    return adapter, setup, actions


def _button(view: discord.ui.LayoutView, label: str) -> discord.ui.Button:
    return next(item for item in view.walk_children() if isinstance(item, discord.ui.Button) and item.label == label)


def _select(view: discord.ui.LayoutView) -> discord.ui.Select:
    return next(item for item in view.walk_children() if isinstance(item, discord.ui.Select))


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


def test_course_filtering_exposes_only_real_master_combinations() -> None:
    selection = MatchCourseSelection(choices=_choices(), grade="LISTED", timezone="UTC")
    tokyo = update_course_selection(selection, step="stadium", value="1")
    turf = update_course_selection(tokyo, step="surface", value="turf")
    distance = update_course_selection(turf, step="distance", value="2400")
    assert distance.direction == MatchDirection.LEFT
    assert distance.layout is None
    standard = update_course_selection(distance, step="layout", value="standard")
    assert selected_course(standard) == _choices()[0]

    invalid = MatchCourseSelection(
        choices=_choices(),
        grade="G1",
        timezone="KST",
        stadium_id=2,
        surface=MatchSurface.DIRT,
    )
    assert normalize_course_selection(invalid).surface == MatchSurface.DIRT
    assert selected_course(invalid) is None


def test_match_staff_command_registration_is_exactly_three_parameterless_panels() -> None:
    workflow, _, _ = _workflow()
    rating_adapter = SimpleNamespace(list_ratings=AsyncMock())
    root = MatchCommandGroup(
        betting_adapter=SimpleNamespace(),
        rating_adapter=rating_adapter,
        staff_group=MatchStaffCommandGroup(adapter=workflow),
    )

    assert [command.name for command in root.commands] == [
        "races",
        "ratings",
        "bets",
        "bet",
        "bet-change",
        "staff",
    ]
    races = next(command for command in root.commands if command.name == "races")
    assert len(races.parameters) == 1
    assert races.parameters[0].name == "match_id"
    assert races.parameters[0].required is False
    assert races.parameters[0].autocomplete is True
    ratings = next(command for command in root.commands if command.name == "ratings")
    assert [parameter.name for parameter in ratings.parameters] == ["rank", "persona"]
    assert all(parameter.required is False for parameter in ratings.parameters)
    bet = next(command for command in root.commands if command.name == "bet")
    bet_parameters = {parameter.name: parameter for parameter in bet.parameters}
    assert bet_parameters["match_id"].autocomplete is True
    assert bet_parameters["amount"].autocomplete is True
    interaction = RecordingInteraction()
    asyncio.run(ratings.callback(root, interaction, 12, None))  # type: ignore[arg-type,union-attr]
    rating_adapter.list_ratings.assert_awaited_once_with(interaction, rank=12, persona=None)
    staff = next(command for command in root.commands if command.name == "staff")
    assert [command.name for command in staff.commands] == ["race", "result", "settlement"]  # type: ignore[attr-defined]
    assert all(not command.parameters for command in staff.commands)  # type: ignore[attr-defined]
    removed = {
        "race-create",
        "race-condition-set",
        "race-entries-set",
        "betting-open",
        "betting-close",
        "result-submit",
        "result-review",
        "result-confirm",
        "settlement-rollback",
        "publish",
        "race-cancel",
    }
    assert removed.isdisjoint(command.name for command in staff.commands)  # type: ignore[attr-defined]


def test_each_panel_exposes_the_confirmed_operator_action_catalog() -> None:
    workflow, _, _ = _workflow()
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    expected = {
        MatchStaffPanelKind.RACE: {
            "새 경기 생성",
            "기존 경기 수정",
            "베팅 시작",
            "베팅 마감",
            "경기 전체 취소",
            "배당 공지 모드",
        },
        MatchStaffPanelKind.RESULT: {"결과 입력/정정", "pending 결과 검토", "pending 결과 확정"},
        MatchStaffPanelKind.SETTLEMENT: {"정산", "정산 롤백", "결과 공개 복구"},
    }
    for kind, labels in expected.items():
        view = MatchStaffPanelView(adapter=workflow, context=context, kind=kind)
        actual = {
            item.label for item in view.walk_children() if isinstance(item, discord.ui.Button) and item.label != "닫기"
        }
        assert actual == labels


def test_panel_open_is_private_and_action_rechecks_bound_context() -> None:
    preparation = RecordingPreparation()
    authorization = RecordingAuthorization()
    workflow, setup, _ = _workflow(preparation=preparation, authorization=authorization)
    opening = RecordingInteraction()

    asyncio.run(workflow.start_panel(opening, kind=MatchStaffPanelKind.RACE))  # type: ignore[arg-type]

    assert preparation.calls == [(opening, "match.staff.race", True)]
    view = opening.edits[0]["view"]
    assert isinstance(view, MatchStaffPanelView)

    mismatch = RecordingInteraction(user_id=999)
    asyncio.run(_button(view, "새 경기 생성").callback(mismatch))  # type: ignore[arg-type]
    assert setup.calls == []
    assert authorization.calls == []
    assert mismatch.response.messages


def test_panel_close_replaces_components_v2_with_buttonless_terminal_layout() -> None:
    workflow, _, _ = _workflow()
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    source = MatchStaffPanelView(adapter=workflow, context=context, kind=MatchStaffPanelKind.RACE)
    stop_calls = _track_stop(source)
    interaction = RecordingInteraction()

    asyncio.run(_button(source, "닫기").callback(interaction))  # type: ignore[arg-type]

    assert interaction.response.defers == [{"thinking": False}]
    assert stop_calls == [True]
    payload = interaction.edits[0]
    terminal = payload["view"]
    assert payload["content"] is None
    assert payload["embeds"] == []
    assert payload["attachments"] == []
    assert isinstance(terminal, discord.ui.LayoutView)
    assert _layout_text(terminal) == "작업을 취소했습니다. DB에는 기록되지 않았습니다."
    assert not any(isinstance(item, discord.ui.Button) for item in terminal.walk_children())


def test_target_selection_is_zero_write_and_delegates_only_after_selection() -> None:
    authorization = RecordingAuthorization()
    workflow, _, actions = _workflow(authorization=authorization)
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    source = MatchStaffPanelView(adapter=workflow, context=context, kind=MatchStaffPanelKind.RACE)
    stop_calls = _track_stop(source)
    interaction = RecordingInteraction()

    asyncio.run(_button(source, "베팅 시작").callback(interaction))  # type: ignore[arg-type]

    assert interaction.response.defers == [{"thinking": False}]
    assert stop_calls == [True]
    target_view = interaction.edits[0]["view"]
    assert isinstance(target_view, MatchStaffTargetView)
    assert actions.autocomplete_calls == [(interaction, "")]
    assert actions.action_calls == []

    target_interaction = RecordingInteraction()
    select = _select(target_view)
    select._values = ["71"]
    asyncio.run(select.callback(target_interaction))  # type: ignore[arg-type]
    assert actions.action_calls == [("open", 71, None)]
    assert actions.race_transition_calls == [("open", context, target_view)]
    assert authorization.calls == [
        (interaction, "match.staff.race"),
        (target_interaction, "match.staff.race"),
    ]


def test_target_list_failure_after_defer_keeps_source_panel_open() -> None:
    workflow, _, actions = _workflow()
    actions.autocomplete_targets = AsyncMock(side_effect=RuntimeError("query failed"))
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    source = MatchStaffPanelView(adapter=workflow, context=context, kind=MatchStaffPanelKind.RESULT)
    stop_calls = _track_stop(source)
    interaction = RecordingInteraction()

    asyncio.run(_button(source, "pending 결과 검토").callback(interaction))  # type: ignore[arg-type]

    assert interaction.response.defers == [{"thinking": False}]
    assert stop_calls == []
    assert interaction.edits == []
    assert interaction.followup.messages[0][0] == "대상 경기를 불러오지 못했습니다."


def test_target_list_authorization_rejection_after_defer_keeps_source_panel_open() -> None:
    authorization = RecordingAuthorization(allowed=False)
    workflow, _, actions = _workflow(authorization=authorization)
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    source = MatchStaffPanelView(adapter=workflow, context=context, kind=MatchStaffPanelKind.RACE)
    stop_calls = _track_stop(source)
    interaction = RecordingInteraction()

    asyncio.run(_button(source, "베팅 시작").callback(interaction))  # type: ignore[arg-type]

    assert interaction.response.defers == [{"thinking": False}]
    assert authorization.calls == [(interaction, "match.staff.race")]
    assert actions.autocomplete_calls == []
    assert stop_calls == []
    assert interaction.edits == []


def test_target_list_edit_failure_closes_source_and_sends_reopen_notice() -> None:
    workflow, _, _ = _workflow()
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    source = MatchStaffPanelView(adapter=workflow, context=context, kind=MatchStaffPanelKind.SETTLEMENT)
    stop_calls = _track_stop(source)
    interaction = RecordingInteraction(fail_original_edit=True)

    asyncio.run(_button(source, "정산").callback(interaction))  # type: ignore[arg-type]

    assert interaction.response.defers == [{"thinking": False}]
    assert stop_calls == [True]
    assert interaction.followup.messages[0][0] == (
        "정산 작업 화면을 갱신하지 못했습니다. `/match staff settlement`를 다시 열어 주세요."
    )


def test_target_back_replaces_selector_with_fresh_panel() -> None:
    workflow, _, _ = _workflow()
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    source = MatchStaffTargetView(
        adapter=workflow,
        context=context,
        kind=MatchStaffPanelKind.RESULT,
        action="review",
        choices=(app_commands.Choice(name="경기", value=71),),
    )
    stop_calls = _track_stop(source)
    interaction = RecordingInteraction()

    asyncio.run(_button(source, "작업 목록").callback(interaction))  # type: ignore[arg-type]

    assert interaction.response.defers == [{"thinking": False}]
    assert stop_calls == [True]
    assert isinstance(interaction.edits[0]["view"], MatchStaffPanelView)


def test_result_target_handoff_leaves_source_lifecycle_to_result_adapter() -> None:
    workflow, _, actions = _workflow()
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    source = MatchStaffTargetView(
        adapter=workflow,
        context=context,
        kind=MatchStaffPanelKind.RESULT,
        action="review",
        choices=(app_commands.Choice(name="경기", value=71),),
    )
    interaction = RecordingInteraction()
    select = _select(source)
    select._values = ["71"]

    asyncio.run(select.callback(interaction))  # type: ignore[arg-type]

    assert actions.action_calls == [("review", 71, None)]
    assert actions.result_transition_calls == [("review", context, source)]
    assert not source.is_finished()


def test_settlement_target_handoff_leaves_source_lifecycle_to_settlement_adapter() -> None:
    workflow, _, actions = _workflow()
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    source = MatchStaffTargetView(
        adapter=workflow,
        context=context,
        kind=MatchStaffPanelKind.SETTLEMENT,
        action="recover",
        choices=(app_commands.Choice(name="경기", value=71),),
    )
    interaction = RecordingInteraction()
    select = _select(source)
    select._values = ["71"]

    asyncio.run(select.callback(interaction))  # type: ignore[arg-type]

    assert actions.action_calls == [("recover", 71, None)]
    assert actions.settlement_transition_calls == [("recover", context, source)]
    assert not source.is_finished()


def test_odds_mode_action_rechecks_authority_and_bypasses_match_target_selection() -> None:
    authorization = RecordingAuthorization()
    workflow, _, actions = _workflow(authorization=authorization)
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    source = MatchStaffPanelView(adapter=workflow, context=context, kind=MatchStaffPanelKind.RACE)
    interaction = RecordingInteraction()

    asyncio.run(_button(source, "배당 공지 모드").callback(interaction))  # type: ignore[arg-type]

    assert authorization.calls == [(interaction, "match.staff.race")]
    assert actions.autocomplete_calls == []
    assert actions.odds_calls == [(interaction, context, source)]


def test_optional_and_required_reason_actions_open_modal_before_existing_preview() -> None:
    workflow, _, actions = _workflow()
    context = MatchStaffInteractionContext(user_id=123, guild_id=987, channel_id=654)
    source = MatchStaffTargetView(
        adapter=workflow,
        context=context,
        kind=MatchStaffPanelKind.RACE,
        action="cancel",
        choices=(app_commands.Choice(name="경기", value=71),),
    )
    interaction = RecordingInteraction()
    select = _select(source)
    select._values = ["71"]

    asyncio.run(select.callback(interaction))  # type: ignore[arg-type]

    modal = interaction.response.modals[0]
    assert isinstance(modal, MatchStaffReasonModal)
    assert modal.reason.required is False
    assert actions.action_calls == []
    modal.reason._value = " 운영 취소 "
    submit = RecordingInteraction()
    asyncio.run(modal.on_submit(submit))  # type: ignore[arg-type]
    assert actions.action_calls == [("cancel", 71, "운영 취소")]
    assert actions.race_transition_calls == [("cancel", context, source)]

    rollback = MatchStaffReasonModal(
        adapter=workflow,
        context=context,
        kind=MatchStaffPanelKind.SETTLEMENT,
        action="rollback",
        match_id=72,
        source_view=None,
    )
    assert rollback.reason.required is True
